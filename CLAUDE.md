# CLAUDE.md

Operating notes for future Claude instances working in this repo.

**For design contracts, read `specs/`** — the foundational specifications that drive implementation. Start with `specs/README.md`, then `specs/00-architecture.md` for the system view, then the component specs. `specs/CROSS_CUTTING_DECISIONS.md` is binding when any spec disagrees with it.

`IMPLEMENTATION_PLAN.md` (in the repo root) captures the original design rationale and is preserved for context, but the specs in `specs/` are the canonical contract.

This file captures rules and conventions that aren't obvious from the code or the specs.

## What this project is

**moonlit** is a CLI that builds self-contained Python zipapps (PEP 441) — analogous to LinkedIn's [shiv](https://github.com/linkedin/shiv), but built on **uv** instead of pip and aware of **uv workspaces**.

A produced `.pyz` bundles the application + all dependencies. On first run, it extracts site-packages to a per-build cache directory and invokes the configured entry point. Subsequent runs hit the cache.

## Architecture (two halves, hard boundary)

There are two distinct codebases in `src/moonlit/`, and they have **different rules**:

### Build-time code (`cli.py`, `builder.py`, `resolver.py`, `workspace.py`, `hashing.py`, `errors.py`)
- Runs as the user's `moonlit` CLI on their dev machine.
- May depend on third-party packages (currently just `click`).
- Shells out to `uv` via `subprocess` — there is no Python API for uv. The shell-out is in `resolver.py`; do not call `subprocess.run(["uv", ...])` from anywhere else.

### Bootstrap code (`src/moonlit/_bootstrap/`)
- Gets copied verbatim into every `.pyz` produced.
- Runs **before** the bundled site-packages is on `sys.path`.
- **Must be stdlib-only.** No `click`, no anything-else. If you need a third-party feature, reimplement enough of it on stdlib or rethink the design.
- No relative imports outside the `_bootstrap` package.
- Compatible with the *target* Python the user will run the .pyz with — currently the project pins `requires-python >=3.13` and the bootstrap can assume that.

If you find yourself adding a dep to `_bootstrap`, stop. That's a design break, not a tweak.

## The build pipeline at a glance

`cli.build` → `builder.build(BuildConfig)`:

1. `workspace.detect(project_root)` — parse `[tool.uv.workspace]`.
2. Pick target package (workspace member via `--package`, or the project itself).
3. `uv export --frozen --no-dev --no-emit-workspace --format requirements-txt [--package <name>]` → requirements file.
4. `uv pip install --target <staging>/site-packages --no-deps -r <reqs> --python <sys.executable>`.
5. `uv build --wheel [--package <name>] --out-dir <tmp>/dist`.
6. `uv pip install --target <staging>/site-packages --no-deps --reinstall-package <name> <wheel>`.
7. Resolve `-c` (console script) by reading `*.dist-info/entry_points.txt` from staging.
8. `hashing.compute_build_id(staging)` — sorted SHA-256.
9. `builder.create_archive` — write shebang prefix → ZipFile → site-packages → `_bootstrap/` → generated `__main__.py` → `env.json`.
10. POSIX `chmod 0o755`; Windows no-op.

The bootstrap `__main__.py` template is just:
```python
import sys
from _bootstrap import bootstrap
sys.exit(bootstrap())
```

## Development practices

**Test-driven development.** The specs in `specs/` already enumerate falsifiable invariants, and many name explicit test files (e.g. `tests/unit/test_bootstrap_stdlib_only.py`, `tests/unit/test_builder_preflight.py::test_parent_missing_exits_7`). Workflow per change:

1. Pick the spec invariant or named test ID you're implementing.
2. Write the failing test first. Run it. Confirm it fails for the expected reason.
3. Make it pass with the smallest change that works.
4. Refactor with the test green.
5. Commit the test and production code together.

Don't write production code without a failing test. Don't write speculative tests for behavior the specs don't require. When a critique surfaces a missing edge case, add the test first, then fix.

**Stepdown rule — modules read like a book.** Every module is laid out top-to-bottom in order of decreasing abstraction. A reader doing `from moonlit.builder import build` and scrolling top-to-bottom should understand what `build` does without ever scrolling backward.

- Module docstring at the top.
- Public API immediately below — the names another module would import.
- The highest-level function defined first; the functions it calls appear below, in the order they're called.
- Private helpers at the bottom.

If you find yourself jumping up the file to understand a downstream function, the module is in the wrong order — reorder before merging. Same rule applies inside `_bootstrap/`: `bootstrap()` at the top of `__init__.py`; the helpers it dispatches to follow in call order across `environment.py`, `extract.py`, `runner.py`.

**Clean-code defaults** (beyond the system prompt's guidance):

- Small functions, one job each. If a function does two things, split it.
- Names describe intent, not implementation: `compute_build_id`, not `do_sha256`. `materialize`, not `do_extract_step`.
- Guard clauses over deep nesting. Return early on the error path; keep the happy path unindented.
- Pure functions wherever possible — especially in `hashing.py`, `workspace.py`, and `_bootstrap/extract.py`. Pure functions test trivially and don't need fixtures.
- Errors are part of the design, not afterthoughts. Every `MoonlitError` subclass has a stable `exit_code`; raise the right one as early as the spec's preflight order allows (CLI spec §4 is authoritative).
- No dead code, no commented-out blocks, no `_unused` variables left as breadcrumbs. If it's not called, delete it.
- Side effects live at the boundary (CLI layer, `resolver.py` subprocess calls, file I/O). Pure logic in the middle.

## Invariants — don't break these

- **Build-id determinism**: `hashing.compute_build_id` hashes sorted relative paths (forward-slash, regardless of platform) interleaved with file content, separated by `\0`. Cache correctness depends on this being deterministic across runs of the same staging dir.
- **`env.json` is not part of the build_id input**. Compute the id first, then write env.json.
- **Shebang prefix goes before the zip header**, not as a zip entry. This is what makes `.pyz` files Unix-executable. `zipapp` and `zipfile` both tolerate a leading `#!...\n` line.
- **`--no-deps` on every `uv pip install`** in the build pipeline. The lockfile is the single source of truth for resolution; we do not want uv re-resolving and disagreeing with `uv.lock`.
- **`--reinstall-package <name>`** in step 6 — defensive against future changes to `uv export` semantics.
- **Cache root on Windows is `%LOCALAPPDATA%\moonlit`**, not `~/.moonlit`. Roaming profiles must not bloat.
- **`os.replace()` for atomic rename** — works on both POSIX and Windows since Python 3.3. Don't use `os.rename()` (Windows fails if the target exists).
- **Locking is OS-managed**: `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows, both dispatched from `_bootstrap/locking.py`. The lock file at `<cache_root>/<cache_key>.lock` is opened with `O_CREAT | O_RDWR` (no `O_EXCL`) and persists across releases — closing the fd releases the OS lock; the kernel releases it on process death. Do NOT add `os.unlink(lock_path)` to the release path: it would race a concurrent opener since `flock` is per open file description.

## Environment variables (runtime, read by bootstrap)

- `MOONLIT_ROOT` — override cache root.
- `MOONLIT_FORCE_EXTRACT` — re-extract even if cache exists.
- `MOONLIT_ENTRY_POINT` — override the entry point baked into env.json.

When adding new env vars, prefix with `MOONLIT_` and document them in this section.

## Error handling conventions

- All user-facing failures raise a subclass of `MoonlitError` (in `errors.py`), each with a stable `exit_code` attribute.
- Top-level CLI catches `MoonlitError` → prints message, exits with the class's exit code.
- `KeyboardInterrupt` → exit 130, no traceback.
- Tracebacks are only shown with `--verbose`.
- Exit-code map lives in `IMPLEMENTATION_PLAN.md`. Don't reuse codes for unrelated conditions.

## Testing

- `tests/unit/` — pure-Python unit tests, no real subprocess calls. Mock `subprocess.run` in resolver tests.
- `tests/e2e/` — runs the real `moonlit build` against a fixture workspace. Slower, hits real `uv`. Skip if `uv` isn't on PATH (don't fail).
- The bootstrap is tested by building a fixture .pyz and running it as a subprocess, then asserting on stdout/exit code.

## Documentation

Project docs are built with **[zensical](https://zensical.org)** — the modern static-site generator from the Material for MkDocs team (Rust + Python core, differential builds, configured via `zensical.toml`).

- Markdown source lives under `docs/`. Configuration lives in `zensical.toml` at the repo root.
- Add zensical as a dev/docs dependency: `uv add --group docs zensical`. Build with `uv run zensical build`; local preview with `uv run zensical serve`.
- **Do not introduce MkDocs, Sphinx, Read the Docs Sphinx theme, or any other docs framework.** Zensical is the chosen tool; switching is not in scope.

## Common commands

```powershell
# Run the CLI from source during development
uv run moonlit build --help

# Build moonlit's own wheel
uv build --wheel

# Run tests
uv run pytest

# Build the docs site (zensical)
uv run zensical build
uv run zensical serve   # local preview at http://127.0.0.1:8000

# Build a .pyz from a workspace member (the canonical demo)
cd C:\tmp\moonlit-demo
uv lock
uv run moonlit build --package shouter -e shouter.cli:main -o shouter.pyz
python .\shouter.pyz
```

## Out of scope (don't implement without asking)

The following are intentionally deferred from the MVP. If a task touches one of these, surface it before doing the work:

- `--reproducible` builds (zeroed mtimes, sorted entries, `SOURCE_DATE_EPOCH`)
- `--compile-pyc`
- `--no-modify` hash verification at runtime
- `--preamble` script
- `--extend-pythonpath` for subprocesses
- `--site-packages` extra-dirs flag
- `--windows-exe` launcher (distlib-style native .exe wrapping)
- Cross-interpreter builds (`--python-version` / `--platform` pass-through to uv)

## Platform note

Primary dev environment is **Windows 11**. PowerShell syntax for any commands surfaced to the user. Code paths that touch the filesystem, locking, or paths must be tested mentally on both Windows and POSIX before being committed.
