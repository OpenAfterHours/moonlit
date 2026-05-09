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

---

These decisions are the binding contract for v2. Authors who deviate must explicitly justify why; otherwise apply them mechanically.
