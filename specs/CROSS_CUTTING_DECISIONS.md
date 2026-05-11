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

When `--bundle-python` is set, the build produces a `.exe` that embeds a full Python interpreter under `_python/` in the zip body. The launcher (D22) unpacks and dispatches it on first run, so end-users can run the `.exe` without Python installed on PATH. Phase 1 is Windows-only; POSIX is deferred.

**D21a — CLI shape and gating.** `--bundle-python` requires `--windows-exe` (spec 01 §3 rule 6, invariant I13). The CLI rejects the combination as a usage error (exit 2). `BuildConfig.bundle_python: bool = False` carries the flag; `_validate_config` raises `InternalError` (11) if `bundle_python and not windows_exe`, defensive against programmatic callers.

**D21b — Python source: `uv python install`.** The only `uv` subcommand added is `uv python install`. The new resolver function is:

```
uv python install --install-dir <staging>/python --no-bin --no-registry <version>
```

- `<version>` is `BuildConfig.python_version` (e.g. `"3.13"`) when set, else `f"{sys.version_info.major}.{sys.version_info.minor}"` — same fallback as `env.json.python_version` (D20c), so a bundle's interpreter version stays consistent with what the build pipeline targeted.
- `--no-bin` skips installing executables under the bin directory; `--no-registry` (Windows-only flag, harmless elsewhere) skips Windows-registry registration. Both keep the install fully isolated to the staging dir.
- uv emits exactly one distribution directory under `--install-dir`, named like `cpython-3.13.X-windows-x64-none/`. The resolver discovers it by listing the install dir after a successful run — the patch version is never hardcoded. Anything other than exactly one child dir → `PythonBundleError`.

**D21c — Error class and exit code.** `PythonBundleError(MoonlitError, exit_code=13)` covers (a) non-zero exit from `uv python install`, (b) the discovery rule above (zero or >1 child dir), and (c) any I/O failure copying the distribution into the build's zip stream. Falls back to exit 3 (`UvNotFoundError`) when `uv` itself is missing from PATH, preserving the resolver's universal pattern.

**D21d — Pipeline placement.** `uv python install` runs as a new **step 8.5** in spec 02 §3, strictly between step 8 (`compute_build_id` over staged `site-packages/`) and the env.json dict build. This is the load-bearing ordering: `compute_build_id` MUST run before the Python tree is touched so the per-app cache key is independent of which CPython patch uv shipped that day. Build_id stability is the falsifier — two `--bundle-python` builds across a `uv python install` upgrade produce the same `build_id`, only the bundled-python fingerprint and the `_python/*` zip entries change.

**D21e — Cross-platform host today.** Phase 1 runs on Windows hosts producing Windows bundles. The Rust launcher binaries under `_launchers/` are Windows PE only; the bundled-Python feature has the same host-and-target scope as `--windows-exe`. Cross-OS builds (host = Linux → `.exe` for Windows) are explicitly deferred under D20e's umbrella.

## D22 — Launcher bundled-Python cache and dispatch

The Rust launcher under `launcher/src/bundle.rs` implements the runtime side of `--bundle-python` (D21). It runs before any Python code and is the only piece on disk that can dispatch a Python interpreter on a Python-free machine.

**D22a — Detection.** Before the historical shebang path, the launcher seeks to the file's tail, scans backward for the EOCD signature (`PK\x05\x06` within the last 65557 bytes, plus zip64 fallback for the rare large-zip case), parses the central directory, and looks for any entry whose filename starts with `_python/`. If none, the launcher falls through to its pre-D22 shebang behavior (the `.exe` was built without `--bundle-python`). Detection does NOT consult `env.json` — the central directory is authoritative and reading it in Rust is cheap.

**D22b — Fingerprint.** The launcher computes its own fingerprint by replaying the spec 02 §4a recipe over the `_python/*` central-directory entries it just walked: sort by UTF-8 byte filename, then SHA-256-stream `filename || \0 || crc32_le(4 bytes) || \0` per entry. The CRC32 is taken from the central directory record (no decompression). The producer and launcher MUST agree byte-for-byte; the test `bundle::tests::fingerprint_matches_python_reference_value` pins a value computed externally by Python `hashlib.sha256` + `zlib.crc32` against the Rust output.

**D22c — Cache layout.** Per-fingerprint extraction sits at `%LOCALAPPDATA%\moonlit\python\<fingerprint>\` (the extracted dist tree, `python.exe` at the root) with a sibling `<fingerprint>.lock` for the first-run lock. If `LOCALAPPDATA` is unset, the launcher errors out with `"LOCALAPPDATA is not set; bundled Python cache unreachable"`. This is the bundled-Python cache; it is INDEPENDENT of the moonlit per-build cache (D5/D6) which is keyed by `build_id` and lives under the same parent (`%LOCALAPPDATA%\moonlit\`) but is the bootstrap's responsibility, not the launcher's.

**D22d — Lock protocol.** Win32 `CreateFileW` on `<fingerprint>.lock` with `OPEN_ALWAYS | GENERIC_READ|GENERIC_WRITE | FILE_SHARE_READ|FILE_SHARE_WRITE`, then `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY)`. On contention (`ERROR_LOCK_VIOLATION` 33) the launcher polls every 50 ms with a 60 s ceiling — same numerical parameters as D13 on the Python side. The lock is released when the file handle closes; the lock file persists. Do NOT unlink the lock file after release — `LockFileEx` is per-open-file-description and a concurrent opener would race.

**D22e — Double-checked first-run.** Fast path (no lock): if `<cache>\python.exe` exists, skip the extract. Slow path: acquire the lock, re-check the fast-path predicate (a sibling may have completed extraction between the predicate and the lock), then extract to `<fingerprint>.tmp.<pid>\` and `MoveFileExW(tmp, cache, REPLACE_EXISTING)` (mirrors D4 + D15 on the Python side). The kernel releases the lock on process death; no jamming after a crash.

**D22f — Spawn.** `<cache>\python.exe -I <self_path> <forwarded args>` via `CreateProcessW` with inherited stdio, `CREATE_UNICODE_ENVIRONMENT`, and a child environment derived from the parent's plus `MOONLIT_BUNDLED_PYTHON=<fingerprint>`. `-I` (isolated mode) implies `-E -s` and ignores `PYTHONPATH`/user site-packages — the bundled CPython runs as if installed cleanly. The `MOONLIT_BUNDLED_PYTHON` env var is the carve-out signal the bootstrap reads at spec 03 §2 step 4a to skip its python-version mismatch check (the bundled interpreter is, by construction, the right one).

**D22g — Zip parsing scope.** The launcher implements its own minimal central-directory walker (`launcher/src/bundle.rs::read_central_directory`) — no `zip` crate, no `flate2`. Two and only two added crate dependencies: `miniz_oxide` (deflate, ~30 KiB) and `sha2` (~25 KiB). The walker tolerates zip64 (offset/size/count of `0xFFFFFFFF`/`0xFFFF` triggers a Zip64 EOCD locator + Zip64 EOCD read). Local-file-header reads use the per-entry offset from the CD record to avoid scanning. Zip-slip is rejected at extract time (`..` or empty segments in the relative arcname → error).

**D22h — Phase-1 scope.** Windows host produces Windows `.exe`; no POSIX launcher binaries today. Cross-OS targets are deferred under D20e's umbrella. `.exe` size grows by the size of the python-build-standalone distribution (~30 MiB compressed) when `--bundle-python` is set.

---

These decisions are the binding contract for v2. Authors who deviate must explicitly justify why; otherwise apply them mechanically.
