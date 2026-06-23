# 00 — System Architecture

**Status:** v2 (supersedes v1 draft); binding decisions live in `specs/CROSS_CUTTING_DECISIONS.md`.

## 1. System overview

moonlit is a two-piece system: a **build-time CLI** (`moonlit`) that emits PEP 441 zipapps, and a **runtime bootstrap** stub baked into every emitted zipapp. The build half resolves a uv-locked dependency closure for a target package, stages it on disk, and assembles a `.pyz`. The runtime half — invoked when an end user executes that `.pyz` — extracts the staged tree into a per-build cache, configures `sys.path`, and dispatches the configured entry point. The `.pyz` is the only artifact that crosses the boundary between the two halves.

```
   developer machine                                end-user machine
+----------------------+                       +-------------------------+
|  moonlit CLI         |                       |  python app.pyz         |
|  (build-time code)   |    .pyz artifact      |  -> _bootstrap.bootstrap|
|  + uv subprocess     |  ===================> |  -> site.addsitedir     |
|  + click, stdlib     |   (deps + bootstrap   |  -> entry_point()       |
|                      |    + env.json)        |  (stdlib-only bootstrap)|
+----------------------+                       +-------------------------+
   reads pyproject/uv.lock                        writes per-build cache
   writes <output>.pyz atomically                 under MOONLIT_ROOT
```

## 2. The two-codebase boundary

moonlit's source tree contains two codebases with **different rules**:

- **Build-time code** lives at `src/moonlit/{__main__,cli,builder,resolver,workspace,hashing,errors}.py` plus `src/moonlit/_templates/`. It runs as the user's `moonlit` CLI on a developer machine, may depend on third-party packages (currently only `click`), and shells out to the `uv` binary on `PATH`. All `subprocess.run(["uv", ...])` calls live in `resolver.py` — nowhere else.
- **Bootstrap code** lives at `src/moonlit/_bootstrap/`. It is copied verbatim into every produced `.pyz` and runs *inside* user zipapps before staged site-packages reaches `sys.path`. **Stdlib-only is a hard rule** — no `click`, no third-party anything. It assumes `requires-python >=3.13`.

The boundary is **one-way**: build-time imports nothing from `_bootstrap`; `_bootstrap` imports nothing from build-time. The build-time code that delivers `_bootstrap/` into the archive does so via filesystem operations (`importlib.resources.files("moonlit") / "_bootstrap"` walked and added as zip entries) — not via Python import.

**Test surface separation.** Build-time has unit tests (mocked `subprocess`) and end-to-end tests against a fixture workspace. `_bootstrap` is exercised exclusively by building fixture `.pyz` files and running them as subprocesses, asserting on stdout/exit code. Importing `_bootstrap` directly into the test process is not a supported test mode.

## 3. Module decomposition

```
                       __main__.py  (python -m moonlit)
                           |
                           v
                         cli.py
                           |
                           v
                       builder.py  --reads--> _templates/main_py.tmpl
                       /   |   \   \-----------------------\
                      v    v    v                          |
              resolver.py  workspace.py  hashing.py        |
                      \    |    /                          |
                       v   v   v                           v
                       errors.py            importlib.resources("moonlit")
                                                 (copies _bootstrap/ tree)

   _bootstrap/__init__.py  (bootstrap())
        |
        +--> environment.py  (json, dataclasses)
        +--> extract.py      (zipfile, os, shutil)  --> locking.py (os, time)
        +--> runner.py       (site, importlib)
        (every node: stdlib only)
```

Per module:

- `__main__.py` — three-line shim so `python -m moonlit` invokes `cli.main`. Public surface: none beyond `if __name__ == "__main__": main()`.
- `cli.py` — click command group and `build` subcommand. Imports `builder`, `errors`. Public surface: `main()` console-script entry. Catches `MoonlitError` subclasses and translates to the build-time exit codes in D3.
- `builder.py` — orchestrates the build pipeline; assembles the zipapp; renders `__main__.py` for the produced .pyz from `_templates/main_py.tmpl`. Imports `resolver`, `workspace`, `hashing`, `errors`, and reads `_bootstrap/` and `_templates/` via `importlib.resources`. Public surface: `build(BuildConfig) -> int` — returns `0` (or another build-time exit code) on the success path; raises a `MoonlitError` subclass on failure for the CLI to translate.
- `resolver.py` — the only module that calls `uv`. Imports `errors`, `subprocess`. Public surface: `export()`, `pip_install_target()`, `build_wheel()`.
- `workspace.py` — parses `[tool.uv.workspace]` from `pyproject.toml`. Imports `errors`, `tomllib`. Public surface: `detect(project_root) -> Workspace | None`.
- `hashing.py` — `compute_build_id(staging_root) -> str`. Imports `hashlib` only.
- `errors.py` — leaf module; defines the `MoonlitError` hierarchy with stable `exit_code` attributes per D3. No internal imports.
- `_templates/main_py.tmpl` — text template for the `__main__.py` written into the produced .pyz. Read by `builder.py` via `importlib.resources`.
- `_bootstrap/__init__.py` — exports `bootstrap()`. `bootstrap()` calls (in order): `environment.load(archive_path)`, `extract.materialize(env, cache_root)`, `runner.run(env, site_dir)`. Public surface: `bootstrap() -> int`.
- `_bootstrap/environment.py` — env.json parsing → `Environment` dataclass; enforces the D8 validation order. Public surface: `load(archive_path) -> Environment`.
- `_bootstrap/extract.py` — cache-key computation (PEP-503 normalized name, per D5), D14 fast path, D4 atomic directory replacement, calls `locking`. Public surface: `materialize(env, cache_root) -> Path` (returns the populated `site-packages` directory).
- `_bootstrap/locking.py` — `O_CREAT|O_EXCL` sentinel locking with polling (D13). Public surface: `acquire(lock_path) -> int` (fd) and `release(fd, lock_path) -> None`, typically used as a context manager.
- `_bootstrap/runner.py` — `site.addsitedir`, entry-point import, invocation, return-code coercion. Public surface: `run(env, site_dir) -> int`.

The graph is asserted to be acyclic on both sides of the boundary and across the boundary; CI may add an automated cycle-detection test in v0.2.

## 4. Runtime artifact contract

A moonlit-built `.pyz` is structurally:

```
<output>.pyz
├── (raw shebang line: "#!<python>\n")     <-- bytes BEFORE the zip header
└── ZIP container (DEFLATED), top-level entries:
    ├── env.json                           <-- specs/05-env-json-schema.md
    ├── __main__.py                        <-- 3-line bootstrap entry
    ├── _bootstrap/                        <-- runtime stub package
    │   ├── __init__.py
    │   ├── environment.py
    │   ├── extract.py
    │   ├── locking.py
    │   └── runner.py
    └── site-packages/                     <-- staged third-party + target
        ├── <pkg-a>/...
        ├── <pkg-b>/...
        └── <dist-info dirs>/
```

**Arcname rule (binding per D1).** Top-level zip entries are exactly `site-packages/`, `_bootstrap/`, `__main__.py`, and `env.json`. Files under `<staging>/site-packages/` are written as `"site-packages/" + relpath.as_posix()`. Files outside `<staging>/site-packages/` (e.g. `<staging>/bin/`) are not bundled in MVP. Bootstrap iterates archive entries and only extracts those whose arcname starts with `site-packages/`, with the prefix stripped, into `<cache>/<cache_key>/site-packages/<remaining>`. `_bootstrap/`, `__main__.py`, and `env.json` are never extracted to the cache; they are read directly from the archive.

## 5. Lifecycle phases

- **Phase A — Build.** Inputs: `pyproject.toml`, `uv.lock`, optionally `--package`. Process: `uv export` → `uv pip install --target` → `uv build --wheel` → install wheel → compute `build_id` (D6) → render `env.json` → assemble zip into `<output>.pyz.tmp.<pid>` in the same directory as `<output>.pyz` → fsync and close → `os.replace(<output>.pyz.tmp.<pid>, <output>.pyz)` (D15). On any failure the `.tmp.<pid>` file is unlinked in `finally`, so a crashed build never leaves a partial `.pyz` at the user-visible output path.
- **Phase B — Distribute.** Out of moonlit's scope.
- **Phase C — First run.** End user invokes `python app.pyz`. Bootstrap reads `env.json`, derives the cache key (D5), discovers no cache, acquires the D13 sentinel lock, extracts to a tempdir, atomically installs via the D4 protocol, releases the lock, sets up `sys.path`, invokes the entry point.
- **Phase D — Subsequent run (cache hit).** Bootstrap reads `env.json`, derives the cache key, and takes the D14 unsynchronized fast path: if `site_dir.is_dir()` and `MOONLIT_FORCE_EXTRACT` is unset, **the lock is not acquired at all**; bootstrap proceeds directly to `addsitedir` and entry-point invocation. This is the steady-state path and is contention-free.
- **Phase E — Forced re-extract.** With `MOONLIT_FORCE_EXTRACT=1`, bootstrap **still acquires the D13 lock** before doing any work. `MOONLIT_FORCE_EXTRACT` only suppresses the existence-based skip inside the lock (D14 step 4); it does not bypass locking. After lock acquisition, behavior is identical to Phase C with the existence check forced to "miss."

## 6. External dependencies and trust posture

**Build-time external.** `uv` binary on `PATH` (required); `click >=8.1`. Optional dev: `pytest`, `zensical`. **Runtime external: none.**

## 7. Data flow

Build-time: `pyproject.toml` + `uv.lock` → `uv export --frozen --no-dev --no-emit-workspace` → `requirements.txt` → `uv pip install --target <staging>/site-packages --no-deps` → staging tree → `uv build --wheel` (with `--all-packages` for workspaces, per D2) → install each produced wheel into staging → `hashing.compute_build_id(staging)` (D6: excludes `__pycache__/` segments and `*.pyc`) → `build_id` → render `env.json` → zipapp assembly to `.pyz.tmp.<pid>` → atomic replace to `.pyz`.

Runtime: `.pyz` → bootstrap reads `env.json` from the archive → derives cache key from `(pep503_normalize(name), build_id)` → fast-path or extract-under-lock → `site.addsitedir(site_dir)` → import-and-call entry point.

`build_id` is the **single value** that ties build to runtime: computed at build time over staging contents, written into `env.json`, used at runtime as the cache-key suffix.

## 8. Process and threading model

**Build-time** is single-process, synchronous. No threading, no asyncio. Concurrency between two simultaneous `moonlit build` invocations on the same project is the user's problem in MVP.

**Build-time tempdir lifecycle (D17).** The CLI creates a single per-build tempdir via `tempfile.mkdtemp(prefix="moonlit-build-")` containing `requirements.txt`, `staging/site-packages/`, and `dist/*.whl`. It is removed in `finally`, regardless of success, failure, or signal.

**SIGINT handling (D18).** The build-time CLI installs a SIGINT handler that cleans up the active tempdir, unlinks any partial `<output>.pyz.tmp.<pid>`, and exits 130 without traceback. On Windows, Python translates `CTRL_C_EVENT` to `KeyboardInterrupt` for the foreground console process, but native console-control semantics differ from POSIX SIGINT in subtle ways (signal delivery to subprocess groups, console-control vs signal); the CLI spec and bootstrap spec carry the platform-specific details. The bootstrap (runtime) does **not** install a SIGINT handler — Python's default `KeyboardInterrupt` applies; if the process is hard-killed mid-extraction, the per-pid tempdir and any `.old.<pid>` sibling may leak and is swept opportunistically per D4.

**Runtime** is single-process per `.pyz` invocation. `_bootstrap/locking.py` uses `os.open(..., O_CREAT | O_EXCL | O_RDWR)` with polling. After extraction, file reads are unsynchronized (D14).

## 9. Trust boundaries, validation, and exit codes

**Build-time trust posture.** moonlit trusts the URL list, hashes, and metadata in `uv.lock` and treats it as the authoritative resolution. moonlit does **not** validate URLs against a whitelist, does **not** re-resolve dependencies, and does **not** verify wheel hashes itself — it relies on `uv` to refuse non-matching hashes during install. moonlit validates: workspace structure (D12 normalization), `--package` value, entry-point string format, `--output-file` writability, the produced wheel artifact's existence.

**Bootstrap trust posture.** The bootstrap trusts `env.json` after schema validation per the D8 ordered checks. It reads the `MOONLIT_*` env vars enumerated below per D16. The cache contents are treated as read-only after extraction, apart from the same-app self-GC the bootstrap itself performs (D24). User code is invoked unsandboxed.

**Independent exit-code enumerations (D3).** Build-time and runtime exit-code spaces are **independent namespaces** — different processes, different concerns, different specs. Build-time codes (0, 1, 2, 3–11, 130) are enumerated in `specs/01-cli.md`; runtime codes (0, 1, 2, 3) are enumerated in `specs/03-bootstrap-runtime.md`. Architecturally, this means a code value like `2` means "CLI usage error" at build time and "entry-point resolution failure" at runtime; neither spec is permitted to import or alias the other's enumeration.

**Reserved env-var surface (D16).** The bootstrap reads seven environment variables: `MOONLIT_ROOT` (cache-root override), `MOONLIT_FORCE_EXTRACT` (force re-extract under lock), `MOONLIT_ENTRY_POINT` (override `env.entry_point`), `MOONLIT_DEBUG` (verbose tracing), and the D24 self-GC controls `MOONLIT_NO_GC` (disable), `MOONLIT_GC_KEEP_LATEST` (override retention count), `MOONLIT_GC_GRACE` (override age grace, seconds). "Truthy" is "present and non-empty after `os.environ.get(name, "")`" — the empty string is treated as unset; no special-casing of `0`/`false`. Any other `MOONLIT_*` name is reserved and unimplemented; see `specs/CROSS_CUTTING_DECISIONS.md` D16 and `specs/03-bootstrap-runtime.md` §9 for full semantics.

## 10. Cross-cutting invariants

Each invariant below names a concrete observation a test can make to falsify it.

- **`build_id` is content-deterministic, forward-slash-normalized, and cache-byproduct-free (D6).** Falsifier: a test that adds a `__pycache__/foo.cpython-313.pyc` file under `<staging>/site-packages/<pkg>/` and re-invokes `compute_build_id` — the digest must be unchanged. Additionally, two stagings with identical files but different host path separators in their relpaths must hash identically; env.json must not be present in the staging tree at hashing time.
- **The bootstrap is stdlib-only (D7).** Falsifier: `tests/unit/test_bootstrap_stdlib_only.py` AST-walks `src/moonlit/_bootstrap/`, collects every `Import`/`ImportFrom` module name (excluding intra-package relative imports), and asserts each is in `sys.stdlib_module_names`. Any third-party import (or any module not in that set) fails the test in CI on every push.
- **Atomic replacement protocol for directory installs (D4).** `os.replace(src, dst)` is atomic when `dst` does not exist; when `dst` may exist, the D4 protocol — rename existing `dst` to `dst.with_name(f"{dst.name}.old.{pid}")`, then `os.replace(src, dst)`, then best-effort `rmtree` of the `.old.<pid>` sibling — applies. Falsifier: a test that pre-populates a cache dir with sentinel files, runs a forced re-extract, and asserts (a) the new content is in place, (b) the old content is gone or under an `.old.<pid>` sibling, and (c) no `os.rename(src_dir, populated_dst_dir)` call appears in the bootstrap source via AST grep.
- **Atomic replacement of the .pyz output (D15).** Falsifier: a test that simulates a mid-build crash (raises after `__main__.py` is written but before `env.json`) and asserts the user-visible `<output>.pyz` is either absent or unchanged from any prior version; only a `<output>.pyz.tmp.<pid>` may exist, and is cleaned in `finally`.
- **Forward slashes in archived paths.** Falsifier: a test that builds a fixture .pyz on a host with `os.sep == "\\"` and asserts every `ZipInfo.filename` matches `r"^[^\\]*$"`.
- **No mutation of user files.** Falsifier: a test that runs `moonlit build` against a fixture and asserts that no path outside `tempfile.gettempdir()` and `<output-file>` is created or modified during the build.
- **Cache fast path does not acquire the lock (D14).** Falsifier: a test that pre-populates the cache, monkey-patches `_bootstrap.locking.acquire` to raise, runs the .pyz, and asserts the entry point still executes successfully (because Phase D never called `acquire`).

## 11. Spec map

| #  | Spec | Concern |
|----|------|---------|
| 00 | `specs/00-architecture.md` | system view (this doc) |
| 01 | `specs/01-cli.md` | user-facing command surface; build-time exit codes (D3) |
| 02 | `specs/02-build-pipeline.md` | build-time component contract |
| 03 | `specs/03-bootstrap-runtime.md` | runtime component contract; runtime exit codes (D3); env-var semantics (D16) |
| 04 | `specs/04-cache-layout.md` | on-disk cache artifact |
| 05 | `specs/05-env-json-schema.md` | wire format; D8/D9/D10/D11 |
| 06 | `specs/06-workspace-integration.md` | uv-workspace coupling; D2/D12 |
| —  | `specs/CROSS_CUTTING_DECISIONS.md` | binding companion document; resolves contradictions across specs |

## 12. Out of scope (architecture level)

Cross-platform wheel compilation, reproducible byte-identical .pyz, native Windows .exe launcher, cache GC, multiple-Python support, real `flock`/`msvcrt` locking, `--no-modify`, `--compile-pyc`, `--preamble`, `--extend-pythonpath`, `moonlit info`, `MOONLIT_PREPEND_PYTHONPATH`, `MOONLIT_INTERPRETER`.

Open questions: none — all resolved in `specs/CROSS_CUTTING_DECISIONS.md`.
