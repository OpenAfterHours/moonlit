# moonlit — uv-powered zipapp builder (MVP)

## Context

The repo is a clean slate (just a stub `pyproject.toml` for `moonlit`, `requires-python >=3.13`, no deps, no source). The goal is a CLI tool that builds fully self-contained Python zipapps per PEP 441 — like LinkedIn's **shiv**, but using **uv** for dependency resolution/installation and supporting **uv workspaces** as a first-class concept.

User decisions locked in:
- CLI command: `moonlit` (e.g. `moonlit build -e pkg.cli:main -o app.pyz`)
- Scope: **MVP only** — build pipeline + runtime bootstrap. Defer `--reproducible`, `--compile-pyc`, `--no-modify` hash verification, `--preamble`, `--extend-pythonpath`, the `info` subcommand.
- Workspace UX: `moonlit build --package <member>` mirroring `uv`'s own flag.

Constraint: the user is on Windows 11. Cross-platform locking and the `.pyz` vs `.exe` question both have to be answered, even if the answer is "defer the Windows launcher."

## Approach

A two-piece tool:

1. **Build-time** (`moonlit` CLI): shell out to `uv` to resolve deps for the target package against `uv.lock`, install into a staging dir, build the target's own wheel and install it too, then assemble a `.pyz` via stdlib `zipapp`/`zipfile` semantics.
2. **Run-time** (`_bootstrap` package baked into every .pyz): on first execution, extract site-packages to a per-build cache dir under `%LOCALAPPDATA%\moonlit` (Win) or `~/.moonlit` (POSIX), set up `sys.path` via `site.addsitedir`, then import + invoke the entry point. Subsequent runs reuse the cache.

The bootstrap is **stdlib-only** — it runs before the staged site-packages is on `sys.path`, so it cannot depend on third-party code (including click).

## Package layout

```
src/moonlit/
├── __init__.py            # __version__
├── __main__.py            # `python -m moonlit` -> cli.main
├── cli.py                 # click command group + `build` subcommand
├── builder.py             # zipapp assembly, shebang prefix, env.json, __main__ template
├── resolver.py            # subprocess wrapper around uv (export, build, pip install --target)
├── workspace.py           # parse [tool.uv.workspace], validate --package
├── hashing.py             # sorted SHA-256 over (relpath, content) -> build_id
├── errors.py              # MoonlitError hierarchy
├── _templates/
│   └── main_py.tmpl       # source for generated __main__.py inside the .pyz
└── _bootstrap/            # SHIPPED INSIDE THE .PYZ — stdlib-only
    ├── __init__.py        # bootstrap() entry
    ├── environment.py     # parse env.json (dataclass)
    ├── extract.py         # cache-dir resolution, atomic extract
    ├── locking.py         # cross-platform file lock
    └── runner.py          # sys.path setup + entry-point invocation

tests/
├── unit/                  # workspace, hashing, resolver (mock subprocess), builder
└── e2e/fixtures/          # tiny demo workspace for the smoke test
```

CLI lib choice: **click** (`>=8.1`). Justified by the planned growth of the CLI surface (info subcommand, more flags) — argparse would just get refactored away.

## CLI surface (MVP)

```
moonlit build [PROJECT]                # positional, default = cwd
  -e, --entry-point TEXT               # "pkg.module:callable"
  -c, --console-script TEXT            # mutually exclusive with -e; resolved post-stage
  -o, --output-file PATH    [required]
  -p, --python TEXT                    # shebang; default "/usr/bin/env python3"
      --package TEXT                   # workspace member; required iff project is a workspace
      --no-dev                         # default ON
      --force                          # overwrite OUTPUT
  -q/-v
```

Rules:
- `-e` xor `-c` is required (exit 2 otherwise).
- `--package` is **required** when `[tool.uv.workspace]` exists, **forbidden** when it doesn't (mirrors uv).
- `-c` resolution happens after Step 6 of the build pipeline by reading `*.dist-info/entry_points.txt` from the staging dir.

## Build pipeline

Driven from `cli.build` calling `builder.build(BuildConfig)`:

1. **Workspace detection** (`workspace.detect(project_root)`). Parse `pyproject.toml` with `tomllib`. Expand `members` globs, apply `exclude`, return `Workspace(root, members)` or `None`. Validate `--package`.
2. **Pick target package.** Workspace + `--package foo` → `members["foo"]`. Else → project root, name read from `[project].name`.
3. **`uv export`** (`resolver.export`):
   ```
   uv export --frozen --no-dev --no-emit-workspace --format requirements-txt
             [--package <name>] --output-file <tmp>/requirements.txt
   ```
   Run from project root. `--no-emit-workspace` strips `-e file://` self-refs.
4. **Stage third-party deps** (`resolver.pip_install_target`):
   ```
   uv pip install --target <staging>/site-packages --no-deps
                  --requirement <tmp>/requirements.txt
                  --python <sys.executable>
   ```
   `--no-deps` because the lockfile is already complete. `--python <sys.executable>` works around uv's "needs a venv" quirk.
5. **Build target wheel** (`resolver.build_wheel`):
   ```
   uv build --wheel [--package <name>] --out-dir <tmp>/dist
   ```
6. **Install target wheel into staging:**
   ```
   uv pip install --target <staging>/site-packages --no-deps
                  --reinstall-package <name> <tmp>/dist/<wheel>
   ```
   `--reinstall-package` is cheap insurance against future `uv export` semantics changes.
7. **Resolve `-c`** if used. Walk `*.dist-info/entry_points.txt`, parse with `configparser`, look up `[console_scripts][name]`. Convert to `module:function`. If missing, error with the discovered list.
8. **Compute build_id** (see "Build ID" below).
9. **Assemble zipapp** (`builder.create_archive`):
   1. Open output as `ZipFile(path, "w", ZIP_DEFLATED)`.
   2. **Before** opening the zip, write `b"#!" + python.encode() + b"\n"` directly to the file (zipapp tolerates a prefix — same trick shiv uses).
   3. Walk `staging/site-packages/`, write each file with arcname relative to staging.
   4. Copy the `_bootstrap/` package from the installed `moonlit` location via `importlib.resources.files("moonlit") / "_bootstrap"`.
   5. Render `__main__.py` from the template:
      ```python
      import sys
      from _bootstrap import bootstrap
      sys.exit(bootstrap())
      ```
   6. Write `env.json` at zip root.
10. **Finalize.** POSIX: `os.chmod(output, 0o755)`. Windows: no-op. Tempdir cleanup.

### Why `uv pip install --target`, not `uv add`

`uv add` is project-management: it mutates `[project.dependencies]` in `pyproject.toml`, updates `uv.lock`, and installs into the project's `.venv`. We need the opposite — a stateless "install these wheels into this staging dir, don't touch anything else." `uv pip install --target` is the right primitive. The "pip" in the name is the surface (pip-compatible flags); the implementation is native uv (Rust resolver/installer, no pip shell-out). `uv sync` and `uv tool install` are similarly project- or user-scoped and have no `--target`.

## Bootstrap runtime

`_bootstrap/__init__.py::bootstrap()`, stdlib-only:

1. Locate the running zipapp via `os.path.abspath(sys.argv[0])`.
2. Read `env.json` via `zipfile.ZipFile(archive).read("env.json")`, hydrate `Environment`.
3. Resolve cache root: `MOONLIT_ROOT` env var, else `%LOCALAPPDATA%\moonlit` (Win) / `~/.moonlit` (POSIX).
4. `site_dir = cache_root / f"{name}_{build_id}" / "site-packages"`.
5. Skip extraction if `site_dir` exists and `MOONLIT_FORCE_EXTRACT` is unset.
6. Otherwise: acquire lock → re-check (TOCTOU) → extract to `cache_root / f".{name}_{build_id}.tmp.{pid}"` → `os.replace()` to final path (atomic on POSIX and Windows since Python 3.3) → release lock.
7. `site.addsitedir(str(site_dir))` — handles `.pth` files correctly.
8. Resolve entry point: `MOONLIT_ENTRY_POINT` env var overrides `env.entry_point`. Split on `:`, `importlib.import_module`, `getattr` walking dots.
9. Invoke; return `0` if `None`, else `int(result)`.

**Cross-platform locking** (`_bootstrap/locking.py`): use `os.open(..., O_CREAT | O_EXCL | O_RDWR)` on a `.lock` sentinel file with a polling retry loop (50ms, 60s timeout). Portable, no `fcntl`/`msvcrt` divergence. Trade-off: stale lock on crashed extraction; recovered manually or via `MOONLIT_FORCE_EXTRACT=1`. A real `flock`/`msvcrt.LK_NBLCK` implementation is a v0.2 follow-up — document the limitation in README.

Env vars honored: `MOONLIT_ROOT`, `MOONLIT_FORCE_EXTRACT`, `MOONLIT_ENTRY_POINT`.

## env.json schema

```json
{
  "schema_version": 1,
  "name": "myapp",
  "build_id": "<64hex>",
  "entry_point": "myapp.cli:main",
  "built_at": "2026-05-08T15:23:01Z",
  "moonlit_version": "0.1.0",
  "python_shebang": "/usr/bin/env python3"
}
```

Bootstrap reads `name`, `build_id`, `entry_point`. Rest is for human inspection / future `info` subcommand.

## Build ID

`hashing.compute_build_id(staging_root)`:

```python
h = hashlib.sha256()
for relpath in sorted(all_files_under(staging_root)):
    h.update(relpath.encode("utf-8")); h.update(b"\0")
    h.update((staging_root / relpath).read_bytes()); h.update(b"\0")
return h.hexdigest()
```

Forward-slash relpaths regardless of platform. Computed before `env.json` is written, so env.json contents don't feed into the id.

## Windows considerations

**MVP outputs `.pyz` only.** Run via `python app.pyz` or `py app.pyz`. The shebang is harmless on Windows.

**Deferred to v0.2:** a `--windows-exe` flag that prepends a distlib launcher .exe to the zipapp to produce a native-runnable `app.exe`. Document this gap in README so users aren't surprised.

Cache root on Windows is `%LOCALAPPDATA%\moonlit` (not `~/.moonlit`) to avoid bloating roaming profiles.

## Error cases (MVP)

| Condition | Class | Exit |
|---|---|---|
| `uv` not on PATH | `UvNotFoundError` | 3 |
| `uv.lock` missing | `NoLockfileError` | 4 |
| `--package` set, no `[tool.uv.workspace]` | `NotAWorkspaceError` | 5 |
| `--package foo` not a member | `UnknownPackageError` | 5 |
| Entry-point string unparseable | `BadEntryPointError` | 6 |
| `-c name` not in any installed dist | `ConsoleScriptNotFoundError` | 6 |
| Output exists, no `--force` | `OutputExistsError` | 7 |

`KeyboardInterrupt` → exit 130, no traceback. Tracebacks only with `--verbose`.

## Critical files to create

- `pyproject.toml` (extend: add `click>=8.1`, `[project.scripts] moonlit = "moonlit.cli:main"`, build-system, `[tool.hatch.build.targets.wheel] packages = ["src/moonlit"]` or equivalent)
- `src/moonlit/cli.py`
- `src/moonlit/builder.py`
- `src/moonlit/resolver.py`
- `src/moonlit/workspace.py`
- `src/moonlit/hashing.py`
- `src/moonlit/errors.py`
- `src/moonlit/_templates/main_py.tmpl`
- `src/moonlit/_bootstrap/__init__.py`
- `src/moonlit/_bootstrap/environment.py`
- `src/moonlit/_bootstrap/extract.py`
- `src/moonlit/_bootstrap/locking.py`
- `src/moonlit/_bootstrap/runner.py`

Reuse from stdlib (no new deps for these): `tomllib`, `zipfile`, `zipapp`, `site`, `importlib.resources`, `importlib.import_module`, `configparser` (for entry_points.txt), `hashlib`, `subprocess`, `tempfile`.

## Documentation

Docs are built with **[zensical](https://zensical.org)** — the modern static-site generator from the Material for MkDocs team. Rust + Python core, differential builds, configured via `zensical.toml`. Requires Python ≥3.10 (well within the project's `requires-python >=3.13`).

- Markdown source lives under `docs/`; site config in `zensical.toml` at the repo root.
- Install via a `docs` dependency group: `uv add --group docs zensical`. The group keeps it out of the runtime install closure for the `moonlit` package itself.
- Build / preview: `uv run zensical build`, `uv run zensical serve`.
- Initial docs scope (MVP-aligned): `index.md` (intro + install), `getting-started.md` (single-package and workspace examples mirroring the verification commands below), `cli-reference.md` (auto or hand-written from `moonlit build --help`), `runtime.md` (env vars, cache layout). Defer the full reference site until v0.2.
- **Do not introduce MkDocs, Sphinx, or any other docs framework** — zensical is the chosen tool.

## Verification (end-to-end)

Create demo workspace at `C:\tmp\moonlit-demo\`:

```
moonlit-demo/
├── pyproject.toml        # [tool.uv.workspace] members = ["packages/*"]
├── uv.lock
└── packages/
    ├── greeter/  (name="greeter", deps=["click>=8.1"], cli:main prints "hello from greeter")
    └── shouter/  (name="shouter", deps=["greeter"], [tool.uv.sources] greeter={workspace=true};
                   cli:main imports greeter and uppercases its output)
```

PowerShell smoke test:

```powershell
cd C:\tmp\moonlit-demo
uv lock
uv run moonlit build --package shouter -e shouter.cli:main -o shouter.pyz
python .\shouter.pyz                                  # expect "HELLO FROM GREETER"
$env:MOONLIT_FORCE_EXTRACT="1"; python .\shouter.pyz  # re-extracts; same output
ls $env:LOCALAPPDATA\moonlit                          # confirm cache dir
python .\shouter.pyz                                  # cache hit; near-instant
Remove-Item Env:MOONLIT_FORCE_EXTRACT
```

Negative paths to spot-check:
- `moonlit build --package nonexistent` → exit 5
- `moonlit build` (no `-e`/`-c`) → exit 2
- Build twice without `--force` → exit 7
- Delete `uv.lock` and rebuild → exit 4

Unit tests cover: workspace parsing/validation, build_id determinism, resolver subprocess argv assembly (mocked), `-c` resolution against synthetic `entry_points.txt`.

## Open risks

- **uv `--target` without active venv**: passing `--python <sys.executable>` should work, but uv's behavior here has been a moving target — verify on first implementation pass. If it breaks, fall back to creating a throwaway venv in the tempdir.
- **Build interpreter == run interpreter**: native-extension wheels are tagged for a specific Python. MVP documents "build on the same major.minor as you run." Cross-interpreter builds (`--python-version`/`--platform` pass-through to uv) deferred to v0.3.
- **Lock file robustness**: `O_CREAT|O_EXCL` sentinels leak on crash. Acceptable for MVP; real `flock`/`LK_NBLCK` is v0.2.
- **Workspace root that itself has `[project]`**: uv allows it. The `--package` validator should accept the root's name. Add a unit test.
- **`.pth` files in staged deps**: `site.addsitedir` handles them, but absolute-path `.pth` files from editable installs would point outside the staging dir. Going through wheel install (Step 6) avoids this; verify with a test that `uv export --no-emit-workspace` truly excludes editable workspace members from the requirements file.

## Follow-ups (post-MVP, explicitly out of scope)

`--reproducible` (zeroed mtimes, sorted entries, `SOURCE_DATE_EPOCH`) · `--compile-pyc` · `--no-modify` hash verification · `--preamble` · `--extend-pythonpath` · `--site-packages` (extra dirs to bundle) · `moonlit info <pyz>` subcommand · `--windows-exe` launcher · real `flock`/`msvcrt` locking · cross-interpreter builds.
