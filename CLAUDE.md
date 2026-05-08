# CLAUDE.md

Operating notes for future Claude instances working in this repo. Read `IMPLEMENTATION_PLAN.md` for the full design and rationale; this file captures the rules and conventions that aren't obvious from the code.

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

## Invariants — don't break these

- **Build-id determinism**: `hashing.compute_build_id` hashes sorted relative paths (forward-slash, regardless of platform) interleaved with file content, separated by `\0`. Cache correctness depends on this being deterministic across runs of the same staging dir.
- **`env.json` is not part of the build_id input**. Compute the id first, then write env.json.
- **Shebang prefix goes before the zip header**, not as a zip entry. This is what makes `.pyz` files Unix-executable. `zipapp` and `zipfile` both tolerate a leading `#!...\n` line.
- **`--no-deps` on every `uv pip install`** in the build pipeline. The lockfile is the single source of truth for resolution; we do not want uv re-resolving and disagreeing with `uv.lock`.
- **`--reinstall-package <name>`** in step 6 — defensive against future changes to `uv export` semantics.
- **Cache root on Windows is `%LOCALAPPDATA%\moonlit`**, not `~/.moonlit`. Roaming profiles must not bloat.
- **`os.replace()` for atomic rename** — works on both POSIX and Windows since Python 3.3. Don't use `os.rename()` (Windows fails if the target exists).
- **Locking is `O_CREAT|O_EXCL` sentinels**, not `fcntl` (POSIX-only) or `msvcrt.locking` (Windows byte-range, different semantics). Documented limitation: stale lock on crash, recovered via `MOONLIT_FORCE_EXTRACT=1`.

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
- `moonlit info <pyz>` subcommand
- `--windows-exe` launcher (distlib-style native .exe wrapping)
- Real `fcntl.flock` / `msvcrt.LK_NBLCK` locking (replacement for the sentinel approach)
- Cross-interpreter builds (`--python-version` / `--platform` pass-through to uv)

## Platform note

Primary dev environment is **Windows 11**. PowerShell syntax for any commands surfaced to the user. Code paths that touch the filesystem, locking, or paths must be tested mentally on both Windows and POSIX before being committed.
