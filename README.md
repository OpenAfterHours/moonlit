# moonlit

`moonlit` is a CLI that bundles a uv-managed Python project (and optionally a uv workspace) into a single self-contained zipapp per [PEP 441](https://peps.python.org/pep-0441/). The produced `.pyz` ships every transitive dependency from `uv.lock`; on the end user's machine it extracts to a per-build cache on first run, then dispatches the configured entry point.

It is similar to LinkedIn's [shiv](https://github.com/linkedin/shiv), with two differences:

- **Built on `uv`, not `pip`.** Resolution is done by `uv export --frozen` against `uv.lock`; staging is done by `uv pip install --target` (no virtualenv); the target's wheel is built by `uv build --wheel`.
- **uv workspaces are first-class.** `--package <member>` selects a workspace target; transitive workspace deps are bundled automatically via `uv build --all-packages`.

## Status

Pre-release (0.x). API and CLI surface are stabilizing toward 1.0; the produced `.pyz` runtime contract is pinned by the design specs under [`specs/`](specs/).

## Install

```sh
uv tool install moonlit
```

Or with pipx / pip:

```sh
pipx install moonlit
# or
pip install --user moonlit
```

From source:

```sh
git clone https://github.com/OpenAfterHours/moonlit.git
cd moonlit
uv sync
uv run moonlit --help
```

## Quick start

In a uv-managed project (`pyproject.toml` + `uv.lock`):

```sh
uv run moonlit build -e myapp.cli:main -o myapp.pyz
python ./myapp.pyz
```

In a uv workspace:

```sh
uv run moonlit build --package shouter -e shouter.cli:main -o shouter.pyz
python ./shouter.pyz
```

The produced `.pyz` is self-contained in the dependency sense: `uv.lock`'s entire dependency closure is bundled, plus the target's own wheel. On first run it extracts `site-packages/` to a per-build cache (`%LOCALAPPDATA%\moonlit` on Windows, `~/.moonlit` on POSIX); subsequent runs hit the cache directly without unpacking. Like [shiv](https://github.com/linkedin/shiv), the `.pyz`/`.exe` does **not** bundle the Python interpreter itself — recipients still need a Python on `PATH` (or a `py.exe`-discoverable install on Windows) whose `major.minor` matches the build's target ABI. See [Cross-interpreter builds](#cross-interpreter-builds) for how to control that target.

### Cross-interpreter builds

Native-extension wheels (e.g. `msgspec`, `numpy`, `pydantic-core`) carry `cp<X><Y>` ABI tags and only load on the matching Python `major.minor`. By default `moonlit build` targets the build host's interpreter; pass `--python-version <X.Y>` to target a different one — useful when the dev box runs a different Python than the recipients:

```sh
# Build for Python 3.12 from a Python 3.13 dev box.
# uv auto-fetches a managed standalone CPython 3.12 if one isn't installed.
uv run moonlit build --python-version 3.12 -e myapp:main -o myapp-py312.pyz
```

The flag threads through every `uv` invocation (`export`, `pip install --target`, `build`) and is stamped into `env.json.python_version`. At runtime the bootstrap compares this against the recipient's `sys.version_info.major.minor` and exits 1 with a clear message on mismatch (no more cryptic `ModuleNotFoundError: No module named '<pkg>._core'`):

```
moonlit: this archive was built for Python 3.12, but you are running Python 3.13;
install a Python 3.12 interpreter or rebuild with `moonlit build --python <python-3.12>`
```

For `--windows-exe` builds, combining `--python-version <X.Y>` with the default shebang automatically pivots the launcher's interpreter selection to `py -<X.Y>` so the Windows PEP 397 launcher pins to the matching Python on the recipient's machine. Pass `--python` explicitly to override.

Multi-version-in-one-artifact (one `.pyz` that runs on multiple Pythons) is **not** supported; build one artifact per target version.

### Build output

Default mode shows per-step progress on stderr — a Braille spinner per step on TTYs (`⠋ freezing dependencies (uv export)` → `✓ frozen · 87 packages · 0.7s`), or plain `→`/`✓` lines when stderr is not a TTY (CI logs, file redirect, pipe). The spec-frozen success line `wrote <path> (<size>, <N> entries)` always lands on stdout. `-q`/`--quiet` suppresses stderr; `-v`/`--verbose` additionally echoes `+ uv <argv>` (POSIX-shlex format) before each `uv` call.

## How it works

The `moonlit build` pipeline runs ten ordered steps:

1. **Workspace detection.** Parse `[tool.uv.workspace]` from `pyproject.toml`; expand `members` globs; apply `exclude`; PEP 503 normalize.
2. **Target selection.** Workspace + `--package <name>` → matched member; non-workspace → project root.
3. **`uv export`** writes a frozen requirements file from `uv.lock`.
4. **`uv pip install --target`** stages the third-party closure under a temp `site-packages/` (no venv).
5. **`uv build --wheel`** (or `--all-packages` for workspaces) builds the target's wheel.
6. Each produced wheel is installed into the same `site-packages/`.
7. If `-c <script>` was used, the entry point is resolved from staged `*.dist-info/entry_points.txt`.
8. **`build_id`** is computed: a sorted SHA-256 over every file in `site-packages/` (excluding `__pycache__`/`.pyc`).
9. **Archive assembly:** shebang prefix, then a `ZIP_DEFLATED` archive containing `site-packages/`, the stdlib-only `_bootstrap/` package, the rendered `__main__.py`, and `env.json`.
10. **Atomic finalize:** temp-then-rename to the output path; POSIX `chmod 0o755`.

At runtime, the `_bootstrap` package reads `env.json`, derives a cache key from `(name, build_id)`, takes either the lock-free fast path (cache hit) or the locked slow path (extract under `O_CREAT|O_EXCL` sentinel, atomic-replace into the cache via `os.rename` + `os.replace`), then calls `site.addsitedir()` and invokes the entry point.

## Documentation

| | |
|---|---|
| [`docs/index.md`](docs/index.md) | Marketing landing page (rendered via the standalone `overrides/home.html` template — markdown body intentionally empty). |
| [`docs/getting-started.md`](docs/getting-started.md) | Walkthroughs for single-package projects and uv workspaces. |
| [`docs/cli-reference.md`](docs/cli-reference.md) | Every flag, every exit code, preflight order, stdout/stderr semantics. |
| [`docs/runtime.md`](docs/runtime.md) | What runs inside the `.pyz`: cache layout, env vars, runtime exit codes, stale-lock recovery. |

The docs are built with [zensical](https://zensical.org):

```sh
uv sync --group docs
uv run zensical serve   # http://127.0.0.1:8000
```

## Project layout

```
src/moonlit/
├── __init__.py         # __version__
├── __main__.py         # `python -m moonlit` entry
├── cli.py              # Click frontend
├── builder.py          # 10-step build pipeline orchestrator
├── resolver.py         # the only module that calls `uv` subprocesses
├── workspace.py        # parses [tool.uv.workspace]
├── hashing.py          # deterministic build_id
├── errors.py           # MoonlitError hierarchy with stable exit codes
├── _progress.py        # spinner + step-line progress reporter (build-time)
├── _templates/
│   ├── __init__.py
│   └── main_py.tmpl    # rendered into every .pyz as __main__.py
└── _bootstrap/         # SHIPPED INSIDE EVERY .pyz — stdlib-only
    ├── __init__.py     # bootstrap() orchestrator
    ├── environment.py  # env.json validation
    ├── extract.py      # D4 atomic-replace, D14 fast path
    ├── locking.py      # O_CREAT|O_EXCL sentinel lock
    ├── runner.py       # site.addsitedir, entry-point resolution
    └── errors.py

specs/                  # Foundational design contracts (start here for hacking)
overrides/home.html     # Standalone landing template (docs homepage)
scripts/release.py      # Version-bump + tag helper (run before publishing)
tests/
├── unit/               # 556 unit tests
└── e2e/                # 25 contract tests via subprocess
```

## Status of features

| Feature | State |
|---|---|
| Build single-package projects | done |
| Build uv workspaces with transitive deps | done |
| `--entry-point` (`-e`) and `--console-script` (`-c`) | done |
| Atomic `.pyz` output (temp-then-rename) | done |
| First-run extraction + cache-hit fast path | done |
| Cross-platform caching (`%LOCALAPPDATA%`, `~/.moonlit`) | done |
| `MOONLIT_ROOT`, `MOONLIT_FORCE_EXTRACT`, `MOONLIT_ENTRY_POINT`, `MOONLIT_DEBUG` | done |
| `--windows-exe` native launcher | done |
| Real `flock`/`msvcrt` locking | done |
| `moonlit info <pyz>` subcommand | done |
| `--python-version` cross-interpreter builds | done |
| Bootstrap Python-version mismatch check (clear error vs cryptic `ModuleNotFoundError`) | done |
| `--reproducible` builds (zeroed mtimes, sorted entries) | deferred |
| `--compile-pyc` | deferred |
| `--no-modify` integrity verification | deferred |
| `--python-platform` (cross-OS / cross-arch builds) | deferred |
| Multi-version-in-one-artifact (single `.pyz` that runs on multiple Pythons) | deferred |

## Contributing

Read [`CLAUDE.md`](CLAUDE.md) for development conventions and [`specs/`](specs/) for the design contracts (start with `specs/README.md`, then `specs/00-architecture.md`).

```sh
uv run pytest                       # 581 tests, ~11s with e2e
uv run pytest tests/unit            # unit only, ~6s
uv run ruff format --check .        # format check (CI gate)
uv run ruff check .                 # lints (CI gate)
uv run zensical build --strict      # docs build (CI gate)
```

The e2e suite (`tests/e2e/`) shells out to real `uv` and produces real `.pyz` files; it skips automatically if `uv` is not on `PATH`.

CI runs all four gates on every pull request via [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### Cutting a release

`scripts/release.py` is the release helper. It enforces a clean working tree, that you're on the release branch, and that the target tag doesn't already exist; runs pytest + ruff + `uv build` against the current code; bumps the version in `pyproject.toml`, `src/moonlit/__init__.py`, and `overrides/home.html`; runs `uv lock`; commits as `chore: release vX.Y.Z`; and creates an annotated tag. Pushing and `uv publish` are deliberately left to you.

```sh
uv run python scripts/release.py patch        # 0.1.0 -> 0.1.1
uv run python scripts/release.py minor        # 0.1.0 -> 0.2.0
uv run python scripts/release.py 0.2.3        # explicit (must be strictly greater)
uv run python scripts/release.py patch --dry-run
```

## License

[MIT](LICENSE).
