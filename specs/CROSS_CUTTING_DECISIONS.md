# Cross-cutting design decisions (binding for v2 specs)

These resolve the contradictions surfaced by the v1 critic round. **Every revision author MUST honor them.** Where v1 spec text disagrees with anything below, the decision below wins.

## D1. .pyz arcname layout

The zip archive contains site-packages contents under a `site-packages/` prefix. Top-level zip entries are exactly:
- `site-packages/`
- `_bootstrap/`
- `__main__.py`
- `env.json`

**Build pipeline** writes arcs as `"site-packages/" + relpath_from_site_packages.as_posix()` for each file under `<staging>/site-packages/`. Files outside `<staging>/site-packages/` (e.g. `<staging>/bin/`) are NOT bundled in MVP.

**Bootstrap** iterates archive entries; only those whose arcname starts with `site-packages/` are extracted, with the prefix stripped, into `<cache>/<cache_key>/site-packages/<remaining>`. `_bootstrap/`, `__main__.py`, and `env.json` are NOT extracted to the cache. Bootstrap calls `site.addsitedir("<cache>/<cache_key>/site-packages")`.

## D2. Workspace transitive deps

For a uv workspace, build all member wheels at once:
```
uv build --all-packages --wheel --out-dir <tmp>/dist
```
Then install every produced wheel into staging:
```
for wheel in sorted(<tmp>/dist/*.whl):
    uv pip install --target <staging>/site-packages --no-deps --python <sys.executable> <wheel>
```
For non-workspace projects: `uv build --wheel --out-dir <tmp>/dist` (single wheel), same install loop.

Drop `--reinstall-package`. Drop the brittle "re-export and grep `-e file://`" approach proposed in v1 workspace spec. Use `--all-packages` even though it overbuilds — correctness over efficiency in MVP.

## D3. Exit codes

**Build-time CLI enumeration:**

| Code | Meaning | Class |
|------|---------|-------|
| 0 | Success | — |
| 1 | Unhandled Python exception (moonlit bug; not stable contract) | — |
| 2 | CLI usage error (parser-level) | — |
| 3 | uv binary not on PATH | UvNotFoundError |
| 4 | uv.lock missing | NoLockfileError |
| 5 | Workspace shape mismatch | NotAWorkspaceError, UnknownPackageError, MissingPackageError, MalformedPyprojectError |
| 6 | Entry-point resolution failed | BadEntryPointError, ConsoleScriptNotFoundError |
| 7 | Output path issue | OutputExistsError, OutputNotWritableError |
| 8 | uv export failure | ExportError |
| 9 | uv pip install --target failure | StagingError |
| 10 | uv build wheel failure or wheel artifact issue | WheelArtifactError |
| 11 | Internal invariant violation | InternalError |
| 12 | Input archive is not a moonlit zipapp (used by `info`) | BadArchiveError |
| 13 | Bundled-Python fetch or install failure (D21) | PythonBundleError |
| 14 | `moonlit clean` refused (held lock without `--force`) | CleanRefusedError |
| 15 | `moonlit clean` I/O failure during deletion | CleanIOError |
| 130 | SIGINT | — |

**Runtime (bootstrap) enumeration is INDEPENDENT** — different process, different namespace:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic bootstrap error (env.json missing/malformed, archive unreadable, extraction I/O) |
| 2 | Entry-point resolution / coercion failure |
| 3 | Lock acquisition timeout |

Each spec must state which enumeration applies. Architecture spec calls out that the two are independent.

## D4. os.replace + directory replacement

`os.replace(src_dir, dst_dir)` does NOT atomically replace an existing non-empty directory on Windows (and is `ENOTEMPTY` on POSIX too). The protocol whenever the destination directory may exist:

```python
def atomic_replace_dir(src: Path, dst: Path, pid: int) -> None:
    old_path = None
    if dst.exists():
        old_path = dst.with_name(f"{dst.name}.old.{pid}")
        os.rename(dst, old_path)  # atomic rename of dir; dst now does not exist
    try:
        os.replace(src, dst)  # src -> dst; atomic
    except Exception:
        if old_path and old_path.exists():
            os.rename(old_path, dst)  # roll back
        raise
    if old_path:
        shutil.rmtree(old_path, ignore_errors=True)  # best-effort, non-blocking
```

Stale `.old.<pid>` siblings: opportunistically swept on the next bootstrap run that holds the lock for that cache key (best-effort, errors ignored).

## D5. Name normalization

- `env.json.name` is the **raw** value as authored in `[project].name`.
- The **cache key** uses PEP 503 normalization: lowercase, runs of `[-_.]+` collapsed to `-`. Algorithm: `re.sub(r"[-_.]+", "-", name).lower()`.
- Normalization is performed at runtime by `_bootstrap/extract.py` from `env.json.name`, deterministically.
- Cache key formula: `f"{normalized_name}_{build_id}"`.
- Build-time `--package` matching also normalizes both sides before comparison.

## D6. Build-id determinism

`hashing.compute_build_id(staging_root)` excludes from the hash:
- Any path containing a `__pycache__` segment.
- Any path with extension `.pyc`.

Determinism guarantee is conditioned on: same uv version, same Python interpreter (major.minor.patch), same uv.lock, same `pyproject.toml`, same moonlit version. Any other variation may change `build_id`.

The hash input format remains: for each included file, sorted by forward-slash relative path: `relpath_bytes + b"\0" + file_bytes + b"\0"`.

## D7. Stdlib-only enforcement (bootstrap)

Enforced via `tests/unit/test_bootstrap_stdlib_only.py`. The test:
1. Walks `src/moonlit/_bootstrap/` AST via `ast.parse`.
2. Collects every `Import` and `ImportFrom` module name (excluding relative imports within `_bootstrap`).
3. Asserts each is in `sys.stdlib_module_names` (Python 3.10+ has this attribute; we require 3.13).
4. Allowed stdlib modules: enumerated explicitly in the bootstrap spec.

Runs in CI on every push. No runtime self-check; the test is the gate.

## D8. env.json schema validation order (consumer)

Bootstrap and tooling validate in this exact order; first failure exits/raises:
1. `env.json` member exists in the archive.
2. Bytes decode as UTF-8.
3. `json.loads` succeeds.
4. Top-level value `isinstance(parsed, dict)`.
5. `"schema_version"` key present and `isinstance(value, int)` and not `isinstance(value, bool)`.
6. `value == 1`.
7. All required fields present.
8. All required fields have correct types per Section 2 of env.json spec.
9. All required fields pass their validation regex / format check.

Each step has a specific error message (see env.json spec Section 9 for the matrix).

## D9. Reserved field-name policy (env.json)

- v1 producers MUST NOT emit any reserved field name.
- v1 consumers MUST ignore unknown fields (forward compatibility).
- When a future v0.x release adds an optional field (e.g. `hashes` in v0.2), the field graduates from "reserved" to "v1-optional". This is NOT a schema bump because v1 consumers were already required to ignore it. v0.x producers MAY emit it without bumping `schema_version`.
- Schema bumps apply only when: a required field is renamed/removed, a field's type changes, or the bootstrap contract on field semantics changes.

## D10. built_at format (env.json producer)

Producer recipe:
```python
from datetime import datetime, timezone
built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```
Do NOT use `isoformat() + "Z"` (emits microseconds; self-rejecting).

Consumer validation: `datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")` must succeed. This is the canonical format; `fromisoformat` is NOT used for validation.

## D11. PEP 508 name regex (env.json)

Producer responsibility: `name` field must match
```python
PEP508_NAME = re.compile(
    r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$",
    re.IGNORECASE,
)
```
The `re.IGNORECASE` flag is MANDATORY in any implementation. Without it the regex rejects all-lowercase names — a bug.

## D12. --package case sensitivity

Both the user-supplied `--package` value AND the workspace member `[project].name` strings are PEP-503 normalized (per D5) before comparison. So `--package my-pkg` matches a member named `My_Pkg`. The error message lists the raw (un-normalized) member names for human readability.

## D13. Locking semantics

- Lock file path: `<cache_root>/<cache_key>.lock` (sibling to the cache dir, NOT inside it).
- The lock file is opened via `os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)` (NOT `O_EXCL` — the file is shared; only the OS-managed lock on the open file description is exclusive).
- Acquired via OS-managed advisory locking, dispatched on platform:
  - POSIX: `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`. Failure raises `BlockingIOError`.
  - Windows: `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` against byte 0. Failure raises `OSError` with `errno` in `{EACCES, EAGAIN, EDEADLK}`.
- Both forms are non-blocking. The poll loop drives retries: 50 ms sleep between attempts, 60 s wall-clock timeout. First attempt has no preceding sleep.
- Released by closing the fd. The kernel releases the OS lock on process death (normal exit, signal, SIGKILL, power loss). The lock file itself is NOT unlinked — unlinking it would race against another process opening the same path, since `flock` semantics are per open file description and unrelated open file descriptions on the same inode see different locks. The persistent lock file is small (zero bytes) and lives next to the cache dir it guards.
- `MOONLIT_FORCE_EXTRACT=1` does NOT bypass the lock — it only forces re-extraction after the lock is acquired and the existence check is skipped.
- Stale-lock recovery is no longer required for crashed processes: the kernel releases the lock automatically. A user-visible 60 s timeout still applies for live contention. The previous documented escape hatch (manually `rm <cache_root>/<cache_key>.lock`) is preserved as a no-op safety net — removing the file is harmless when nothing holds the lock.

## D14. Cache hit fast path (double-checked locking)

1. Compute `site_dir = cache_root / cache_key / "site-packages"`.
2. **Outside the lock**: if `site_dir.is_dir()` AND `MOONLIT_FORCE_EXTRACT` is unset → skip extraction, proceed to `addsitedir`.
3. Otherwise, acquire the lock.
4. **Inside the lock**: re-check `site_dir.is_dir()` AND `MOONLIT_FORCE_EXTRACT` unset. If both true (sibling won the race) → release lock, skip extraction.
5. Otherwise → extract to tempdir → atomic_replace_dir (D4) → release lock.

The fast path is unsynchronized — readers of a populated cache do not contend with each other. Once `os.replace` has installed the cache dir, all subsequent readers see the populated state.

## D15. Atomic .pyz output (build pipeline)

Build pipeline writes the .pyz via temp-then-rename:
1. Write to `<output>.pyz.tmp.<pid>` in the same directory.
2. On successful close + fsync, `os.replace(<output>.pyz.tmp.<pid>, <output>.pyz)`.
3. On failure, unlink the .tmp file in `finally`.

This means a crashed `moonlit build` does NOT leave a partial `.pyz` at `output_path`. Updates the v1 build-pipeline spec which said no temp-then-rename in MVP — the cost is trivial and the value is that the next build doesn't see a corrupt file at output_path.

## D16. Reserved env-var policy (bootstrap)

The bootstrap reads only:
- `MOONLIT_ROOT`
- `MOONLIT_FORCE_EXTRACT`
- `MOONLIT_ENTRY_POINT`
- `MOONLIT_DEBUG`

Drop `MOONLIT_PREPEND_PYTHONPATH` and `MOONLIT_INTERPRETER` from the bootstrap spec. They are out of scope until the corresponding v0.2 features land. Adding ghost-feature env-var names to the v1 spec is anti-pattern.

"Truthy" for any of these env vars means: present and non-empty after `os.environ.get(name, "")`. The empty string is treated as unset. No special-casing of `0`/`false`/etc.

## D17. tempdir for build-time

Single tempdir per build: `tempfile.mkdtemp(prefix="moonlit-build-")`. Layout:
```
<tempdir>/
  requirements.txt        # output of uv export
  staging/                # the staging tree
    site-packages/        # the install target
  dist/                   # output of uv build --wheel(s)
    *.whl
```
Cleaned in `finally`, regardless of success/failure/SIGINT.

## D18. Failure cleanup on SIGINT

CLI top-level installs a SIGINT handler that:
1. Cleans up the active build's tempdir if any.
2. Cleans up any partial `<output>.pyz.tmp.<pid>` file (D15).
3. Exits 130 without traceback.

The bootstrap (runtime) does NOT handle SIGINT specially — Python's default applies. The bootstrap's tempdir for extraction is per-pid and is cleaned in `finally` of the extraction function, but if SIGKILL fires, it leaks (documented in D4).

## D19. Output-format dispatch (`--windows-exe`)

The build pipeline produces exactly one of two output shapes; the active mode is selected by the `--windows-exe` CLI flag.

**Default mode (`.pyz`):**
```
<b"#!"><python_shebang><b"\n"><zip body>
```
This is the historical behavior, untouched by D19.

**Windows-exe mode:**
```
<launcher PE bytes><b"#!"><python_shebang><b"\n"><zip body>
```
Where `<launcher PE bytes>` is `src/moonlit/_launchers/t-<arch>.exe` selected by host architecture (D19a). The remainder — shebang line and zip body — is byte-identical to what the same build would emit in default mode. This is enforced by tests (specs/01-cli.md I11 falsifier: hash the trailing zip).

**D19a — Architecture selection.** At build time, the host architecture is normalized:

| `os.name` | `platform.machine()` value | normalized arch |
|-----------|---------------------------|-----------------|
| `nt`      | `AMD64`                   | `x64`           |
| `nt`      | `ARM64`                   | `arm64`         |
| `nt`      | `x86`                     | `x86`           |
| (any)     | `x86_64`                  | `x64`           |
| (any)     | `aarch64`                 | `arm64`         |
| (any)     | `i686` / `i386`           | `x86`           |

If no entry matches, the build raises `InternalError` (exit 11) with a message naming the observed `(os.name, platform.machine())` pair. If the matching `t-<arch>.exe` is missing from the package data (e.g. the wheel was stripped), the build raises `InternalError` and instructs the user to reinstall.

**D19b — Suffix policy.** When `--windows-exe` is set, `--output-file` MUST end in `.exe` (case-insensitive on Windows file systems, but compared with `str.endswith(".exe")` for portability). Mismatch is a CLI usage error → exit 2.

**D19c — Default shebang.** When `--windows-exe` is set and `--python` was left at its default value (i.e. the user did not pass `-p`), the shebang baked into the launcher payload is `python.exe` rather than the cross-platform default `/usr/bin/env python3`. Detected via Click's `ParameterSource.DEFAULT`.

**Interaction with `--bundle-python` (D21):** the shebang is still emitted into the .exe under bundled builds (preserves the byte-layout-compatible-with-distlib contract and means `moonlit info` still shows a meaningful `python_shebang`), but the launcher's bundled-Python path (D22a) wins when it fires — the shebang is informational only. A user who manually strips the bundled `_python/*` entries from a produced .exe would fall back to the shebang path and the launcher would invoke `python.exe` on the recipient's PATH, mirroring the non-bundled `--windows-exe` behavior.

**D19d — POSIX exec-bit.** Windows-exe mode skips the post-write `os.chmod(output_path, 0o755)` regardless of host OS — a `.exe` does not need POSIX exec bits and the chmod is a no-op on the typical target file system anyway.

The bootstrap, env.json, cache layout, and zip body are unaffected by D19. The launcher cedes control to Python with the .exe path as `argv[1]`, and Python's zipapp/zipimport machinery reads the trailing zip exactly as it would for a `.pyz`.

## D20 — Cross-interpreter builds (`--python-version`)

`--python-version <X.Y>` lets a developer build a `.pyz`/`.exe` whose bundled wheels are tagged for a Python ABI different from the build host's. Format: `^\d+\.\d+$` (major.minor only — patch versions don't affect ABI within a minor). Source-of-truth at the resolver layer for which Python uv targets; source-of-truth in `env.json.python_version` for the runtime mismatch check (spec 05 §3.8).

**D20a — Plumbing.** When set, `BuildConfig.python_version` is threaded through every uv invocation as `--python <X.Y>` (uv accepts a version spec on its single `--python` flag): `uv export` (resolution), `uv pip install --target` (download/install of pre-built wheels), `uv build --wheel` (project's own PEP 517 build). uv auto-fetches a managed standalone CPython for the requested version if no local install matches; `UV_PYTHON_DOWNLOADS=never` opts out and turns missing-interpreter into a uv-level error that surfaces as the corresponding moonlit error class for whichever step ran (`StagingError` 9 / `WheelArtifactError` 10 / `ExportError` 8). The user-facing CLI flag is named `--python-version` for semantic clarity ("target Python *version*", not a path); the moonlit resolver maps it onto uv's `--python` because that is the actual flag uv accepts on `export` and `build`.

**D20b — Single-flag form on `uv pip install`.** uv `pip install` accepts both `--python <PYTHON>` (interpreter selection — path or version spec) and `--python-version <X.Y>` (resolver minimum-version *hint*, NOT interpreter selection). moonlit uses `--python` exclusively in all three resolver functions and never passes uv's `--python-version`. `resolver.pip_install_target` swaps the value of the single `--python` token between the version spec (when D20 is active) and `sys.executable` (otherwise) rather than appending a second flag.

**D20c — env.json source-of-truth.** `_build_env_dict` stamps `env.json.python_version` from `BuildConfig.python_version` (when set) else `f"{sys.version_info.major}.{sys.version_info.minor}"`. The runtime version-mismatch check in `_bootstrap/__init__.py:_check_python_version` compares this against the recipient's `sys.version_info.major.minor` — so a cross-compiled artifact carries the **target's** version and rejects the **host's** Python on mismatch, which is the desired symmetry.

**D20d — Windows-exe shebang pivot.** When `--windows-exe` AND `--python-version <X.Y>` are both set AND `--python` is at its Click `ParameterSource.DEFAULT`, the default shebang pivots from `python.exe` to `py -<X.Y>` (PEP 397 launcher). Rationale: in cross-interpreter mode the developer has explicitly declared a target version, so the recipient's launcher should pin to that version rather than picking whatever bare `python.exe` resolves on PATH. Without `--python-version`, the existing `python.exe` default (D19c) is preserved so the developer's local roundtrip isn't broken when their build host's Python isn't py-launcher-registered.

**D20e — `--python-platform` deferred.** The symmetric `--python-platform <triple>` flag (cross-OS / cross-arch builds) is intentionally NOT shipped in v0.x. Reasons: target-platform wheels must exist on PyPI for every dep in `uv.lock`, the validation/error story is significantly larger, and the use case is strictly less common than cross-version-on-the-same-OS. Future addition under the same D20 family.

## D21 — Bundled-Python build option (`--bundle-python`)

When `--bundle-python` is set, the build produces a **folder** that contains a thin launcher `.exe`, the application zipapp, and a python-build-standalone CPython distribution as sibling files. The launcher (D22) finds the bundled interpreter via a sibling-file probe and spawns it directly — nothing is extracted at runtime. Phase 1 is Windows-only; POSIX is deferred.

Rationale: the prior v0.3.0 shape (single `.exe` with the Python tree embedded in the trailing zip and extracted to `%LOCALAPPDATA%` on first run) tripped Windows Defender's `Trojan:Win32/Wacatac.B!ml` heuristic for corporate users — that pattern is indistinguishable from a self-extracting trojan to AV scanners. Putting the Python tree on disk next to the launcher eliminates the runtime extraction step and so eliminates the heuristic trigger.

**D21a — On-disk layout.** For `-o C:\out\myapp` the produced bundle is:

```
C:\out\myapp\
├── myapp.exe       # vendored Rust launcher (no appended zip, no embedded interpreter)
├── myapp.pyz       # the application zipapp — byte-identical to a non-bundle build
└── _python\        # python-build-standalone tree, verbatim from `uv python install`
    ├── python.exe
    ├── python3XX.dll
    ├── Lib\
    └── …
```

`<basename>` is the file name of the `-o` path. The launcher's filename, the `.pyz` filename, and the directory's name all share that basename — that is the contract the launcher's sibling probe relies on (D22a).

**D21b — CLI shape and gating.** `--bundle-python` is sufficient by itself; it no longer requires `--windows-exe`. When `--bundle-python` is set, `-o` is treated as a target directory path: it MUST NOT end in `.exe` or `.pyz` (the CLI rejects either as a usage error). `--windows-exe` is accepted alongside `--bundle-python` and is a no-op in that combination (a folder bundle always contains a launcher `.exe` by construction).

**D21c — Python source: `uv python install`.** Unchanged from v0.3.0:

```
uv python install --install-dir <staging>/python --no-bin --no-registry <version>
```

- `<version>` is `BuildConfig.python_version` (e.g. `"3.13"`) when set, else `f"{sys.version_info.major}.{sys.version_info.minor}"` — same fallback as `env.json.python_version` (D20c).
- `--no-bin` skips installing executables under the bin directory; `--no-registry` (Windows-only, harmless elsewhere) skips Windows-registry registration.
- uv emits exactly one distribution directory under `--install-dir`, named like `cpython-3.13.X-windows-x64-none/`. The resolver discovers it by listing the install dir; the patch version is never hardcoded. Anything other than exactly one child dir → `PythonBundleError`.

**D21d — Error class and exit code.** `PythonBundleError(MoonlitError, exit_code=13)` covers (a) non-zero exit from `uv python install`, (b) the discovery rule above, and (c) any I/O failure when assembling the output folder. Falls back to exit 3 (`UvNotFoundError`) when `uv` itself is missing from PATH.

**D21e — Pipeline placement.** `uv python install` runs as a new **step 8.5** in spec 02 §3, strictly between step 8 (`compute_build_id` over staged `site-packages/`) and the archive-write step. The ordering ensures build_id is independent of which CPython patch uv shipped that day: two `--bundle-python` builds across a `uv python install` patch upgrade produce the same `build_id` and the same `<basename>.pyz` bytes; only the contents of `_python/` differ.

**D21f — Folder-assembly atomicity.** The output folder is built atomically: every file lands first under `<output>.tmp.<pid>/` inside the parent directory, then the staging dir is moved into place via the D4 directory-replace protocol. A crashed or SIGINT'd build leaves no half-written `<output>/` directory.

**D21g — `--force` semantics for folder targets.** A folder target may be overwritten with `--force` only when the existing `<output>/` is a directory containing `<basename>.exe` AND `<basename>.pyz` AND `_python/python.exe` — the signature of a moonlit-built bundle. Any other existing path at `-o` (regular file, foreign directory, symlink, etc.) is rejected even under `--force`. This prevents `--force` from turning into an `rm -rf` of an unrelated directory the user happens to have named the same.

**D21h — env.json is not specialised.** `env.json` inside `<basename>.pyz` is byte-identical between bundle and non-bundle builds (modulo the `built_at` timestamp). No `bundled_python` field is emitted; the on-disk folder layout is the authoritative "this artifact ships Python" signal. The bootstrap inside the `.pyz` does not need to know whether the interpreter that loaded it came from `_python/` or from `PATH`.

**D21i — Cross-platform host today.** Phase 1 runs on Windows hosts producing Windows bundles. The Rust launcher binaries under `_launchers/` are Windows PE only. Cross-OS builds are deferred under D20e's umbrella.

## D22 — Launcher dispatch (sibling-probe mode)

The Rust launcher binary serves two output shapes:

1. **Single-file `--windows-exe`** (no `--bundle-python`): the launcher PE is prepended to a `#!shebang\n` line and a zip body. At runtime it parses its own PE section table to find the trailing data, reads the shebang, and `CreateProcessW`s the interpreter named in the shebang against `self_path`.
2. **Folder bundle (`--bundle-python`)**: the launcher PE is a standalone file in the bundle folder. At runtime it probes for sibling files and, if present, spawns the bundled interpreter directly.

**D22a — Sibling probe (folder mode).** Before any PE / trailing-zip parsing, the launcher computes `<self_dir>` = parent of `self_path` and `<stem>` = file stem of `self_path` (everything before the final `.exe` extension). It then tests:

```
exists(<self_dir>\_python\python.exe) AND exists(<self_dir>\<stem>.pyz)
```

If both succeed, the launcher is in a folder bundle: it `CreateProcessW`s `<self_dir>\_python\python.exe -I <self_dir>\<stem>.pyz <forwarded args>` with inherited stdio, waits, and forwards the exit code. The `-I` (isolated mode) flag is the same `-E -s` combination v0.3.0 used: the bundled interpreter runs as if freshly installed, immune to `PYTHONPATH` and user site-packages leaks on the host.

If either probe target is missing, the launcher falls through to the PE-end + shebang path (single-file mode), unchanged from before D21. This preserves the contract for `--windows-exe`-only builds and for users who manually rename or move the launcher out of its folder.

**D22b — No runtime extraction, no fingerprint, no env-block.** The launcher does NOT walk the central directory, does NOT compute any hash, does NOT acquire any lock, does NOT extract anything to disk, and does NOT inject `MOONLIT_BUNDLED_PYTHON` (or any other variable) into the child environment. The bundled Python on disk IS the cache. The launcher is a process shim, nothing more.

**D22c — Crate dependencies.** Only `windows-sys` is required; `miniz_oxide` and `sha2` (added in v0.3.0 for the runtime-extraction path) are removed. Launcher binary size drops accordingly — from ~200 KiB to ~30–50 KiB per arch under the MSVC toolchain.

**D22d — Phase-1 scope.** Windows host produces Windows bundles. Cross-OS targets are deferred under D20e's umbrella. A bundled folder's on-disk size is dominated by `_python/` (~30 MiB uncompressed for python-build-standalone).

## D23 — `moonlit clean` cooperative liveness

`moonlit clean` (specs/01-cli.md §2.4, policy in specs/04-cache-layout.md §12.1) reaps cache entries under a **cooperative** liveness model:

1. **Try-lock**: for each cache entry slated for deletion, the command try-acquires `<cache_root>/<cache_key>.lock` non-blocking via the same primitives the bootstrap uses (`fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows). On success, deletion proceeds while the lock is held, ensuring no concurrent extractor can populate the directory mid-rmtree. On failure, the entry is marked `skip` with reason `(locked)`.
2. **`--force` escape**: skips the try-lock and deletes unconditionally. Useful when the user knows the holder is dead (debugger killed, kernel lock auto-released but the file remains).
3. **Fast-path reader caveat**: the D14 cache-hit fast path reads `<cache_key>/site-packages/` *without* holding the lock. `moonlit clean` cannot detect such readers. Running `clean` while a moonlit `.pyz` is actively importing is undefined behavior; the contract is the user's assertion that no such reader exists.
4. **No pid liveness on orphan dirs**: `.tmp.<pid>/` and `.old.<pid>/` siblings are reaped by name without consulting the owning pid (racy across pid reuse per spec 04 §11). Their owning `<cache_key>` being in the deletion set, or missing entirely, is the reap signal.
5. **Exit-code distinction**: at least one held-lock skip with `--force` unset → exit 14 (`CleanRefusedError`). I/O failure during deletion → exit 15 (`CleanIOError`). Both differ from a clean success (exit 0) so CI can tell what happened.

D23 is the *only* decision that loosens read-side synchronization on the cache for an additional consumer; the bootstrap's own contract (D13, D14) is unchanged. No new on-disk artifacts, no new env vars, no daemon.

---

These decisions are the binding contract for v2. Authors who deviate must explicitly justify why; otherwise apply them mechanically.
