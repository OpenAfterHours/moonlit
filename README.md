# moonlit

`moonlit` is a CLI that bundles a uv-managed Python project (and optionally a uv workspace) into a single self-contained zipapp per [PEP 441](https://peps.python.org/pep-0441/). The produced `.pyz` ships every transitive dependency from `uv.lock`; on the end user's machine it extracts to a per-build cache on first run, then dispatches the configured entry point.

It is similar to LinkedIn's [shiv](https://github.com/linkedin/shiv), with two differences:

- **Built on `uv`, not `pip`.** Resolution is done by `uv export --frozen` against `uv.lock`; staging is done by `uv pip install --target` (no virtualenv); the target's wheel is built by `uv build --wheel`.
- **uv workspaces are first-class.** `--package <member>` selects a workspace target; transitive workspace deps are bundled automatically via `uv build --all-packages`.

## Status

Pre-release (0.x). API and CLI surface are stabilizing toward 1.0; the produced `.pyz` runtime contract is pinned by the design specs under [`specs/`](specs/).

## Install

`moonlit` is not yet on PyPI. From source:

```sh
git clone <repo> moonlit
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

The produced `.pyz` is self-contained: `uv.lock`'s entire dependency closure is bundled, plus the target's own wheel. On first run it extracts `site-packages/` to a per-build cache (`%LOCALAPPDATA%\moonlit` on Windows, `~/.moonlit` on POSIX); subsequent runs hit the cache directly without unpacking.

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
| [`docs/index.md`](docs/index.md) | Overview and at-a-glance example. |
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
├── cli.py              # Click frontend
├── builder.py          # 10-step build pipeline orchestrator
├── resolver.py         # the only module that calls `uv` subprocesses
├── workspace.py        # parses [tool.uv.workspace]
├── hashing.py          # deterministic build_id
├── errors.py           # MoonlitError hierarchy with stable exit codes
├── _templates/main_py.tmpl
└── _bootstrap/         # SHIPPED INSIDE EVERY .pyz — stdlib-only
    ├── __init__.py     # bootstrap() orchestrator
    ├── environment.py  # env.json validation
    ├── extract.py      # D4 atomic-replace, D14 fast path
    ├── locking.py      # O_CREAT|O_EXCL sentinel lock
    ├── runner.py       # site.addsitedir, entry-point resolution
    └── errors.py

specs/                  # Foundational design contracts (start here for hacking)
tests/
├── unit/               # 451 unit tests
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
| `--reproducible` builds (zeroed mtimes, sorted entries) | deferred to v0.2 |
| `--compile-pyc` | deferred to v0.2 |
| `--no-modify` integrity verification | deferred to v0.2 |
| `--windows-exe` native launcher | deferred to v0.2 |
| Real `flock`/`msvcrt` locking | deferred to v0.2 |
| `moonlit info <pyz>` subcommand | deferred to v0.2 |

## Contributing

Read [`CLAUDE.md`](CLAUDE.md) for development conventions and [`specs/`](specs/) for the design contracts (start with `specs/README.md`, then `specs/00-architecture.md`). Run the test suite with:

```sh
uv run pytest                        # 476 tests, ~10s with e2e
uv run pytest tests/unit              # unit only, <2s
```

The e2e suite (`tests/e2e/`) shells out to real `uv` and produces real `.pyz` files; it skips automatically if `uv` is not on `PATH`.
