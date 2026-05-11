# Bootstrap Runtime Specification

Status: normative for v0.1. Stdlib-only.

## 0. Exit codes (runtime enumeration, per D3)

The bootstrap is a separate process from the build-time CLI; its exit codes are an **independent enumeration** from the build-time codes documented in `specs/01-cli.md`.

| Code | Meaning |
|------|---------|
| 0 | Success (entry point returned `None` or coerced to 0). |
| 1 | Generic bootstrap error: `env.json` missing/malformed, archive unreadable, extraction I/O failure, `_bootstrap` collision, empty `sys.argv[0]`, runtime Python's major.minor differs from `env.python_version`. |
| 2 | Entry-point resolution or return-value coercion failure. |
| 3 | Lock acquisition timeout (60 s wall clock). |

Any other non-zero code originated from user code via its own `sys.exit` or via `int()` of its return value.

## 1. Glossary

- **archive** — `os.path.abspath(sys.argv[0])`; the running `.pyz` file.
- **cache_root** — base directory for all moonlit caches (Section 3).
- **cache_key** — `f"{normalized_name}_{build_id}"`, where `normalized_name = re.sub(r"[-_.]+", "-", env.name).lower()` (D5).
- **site_parent** — `cache_root / cache_key`.
- **site_dir** — `site_parent / "site-packages"`; the directory passed to `site.addsitedir`.
- **tmp_dir** — `cache_root / f".{cache_key}.tmp.{pid}"`; staging area for one extraction.
- **lock_path** — `cache_root / f"{cache_key}.lock"`; sibling of `site_parent`, NOT inside it (D13).

## 2. Entry point and order of operations

Generated `__main__.py`:

```python
import sys
from _bootstrap import bootstrap
sys.exit(bootstrap())
```

`bootstrap()` MUST return an `int` in `[0, 255]`. The full sequence:

1. Resolve **archive**. If `sys.argv[0]` is empty, exit 1 with `"cannot locate zipapp (sys.argv[0] is empty)"`. If the archive is not a zipfile (e.g. `python -m _bootstrap`), exit 1 with `"not a moonlit zipapp: <path>"`.
2. Open the archive via `zipfile.ZipFile(archive, "r")`. On `BadZipFile` / `OSError`, exit 1.
3. Read `env.json`, validate per D8 (existence, UTF-8 decode, `json.loads`, top-level dict, `schema_version` int and `not isinstance(_, bool)`, `== 1`, required keys, types, formats, optional `python_version` type/format). First failure exits 1 with the matrix message.
4. Hydrate `Environment` dataclass.
4a. **Python version check.** If `env.python_version` is present and `f"{sys.version_info.major}.{sys.version_info.minor}"` is not equal to it, exit 1 with `"this archive was built for Python <X.Y>, but you are running Python <A.B>; install a Python <X.Y> interpreter or rebuild with \`moonlit build --python <python-X.Y>\`"`. When the field is absent (older archives), skip this check. The check fires before cache-root resolution and extraction so a wrong-Python invocation never touches the cache.

**Carve-out for bundled Python (D21):** when `env.bundled_python` is present AND `os.environ.get("MOONLIT_BUNDLED_PYTHON", "") == env.bundled_python.fingerprint`, the version check is skipped. The launcher sets `MOONLIT_BUNDLED_PYTHON` to the bundled-Python fingerprint exactly when it dispatches its own cached interpreter — so the test confirms "I am the launcher's bundled interpreter, not a wrong system Python." If the fingerprint doesn't match (a forged env var, or a stale value from a previous run of a different .exe), fall through to the strict check. If `MOONLIT_BUNDLED_PYTHON` is absent (the .exe was launched without going through the launcher's bundled path) the strict check applies — which is correct, because the running interpreter is the user's system Python.
5. Compute `normalized_name = re.sub(r"[-_.]+", "-", env.name).lower()` and `cache_key`.
6. Resolve **cache_root** (Section 3).
7. Compute `site_parent` and `site_dir`.
8. **Fast path (D14, no lock):** if `site_dir.is_dir()` AND `MOONLIT_FORCE_EXTRACT` is unset → goto 11.
9. **Slow path:** acquire **lock_path** (Section 5). Re-check `site_dir.is_dir()` AND `MOONLIT_FORCE_EXTRACT` unset; if both true (sibling won the race), release lock and goto 11.
10. Extract (Section 6). Atomically install (D4). Sweep stale `.old.<otherpid>` siblings best-effort. Release lock in `finally`.
11. Verify the staged tree does not collide with `_bootstrap` (Section 7). Call `site.addsitedir(str(site_dir))`.
12. Resolve entry point (Section 8). Import + invoke with no arguments.
13. Coerce return value (Section 8). Mask `& 0xFF`. Return.

## 3. Cache root resolution

In order:

1. If `os.environ.get("MOONLIT_ROOT", "")` is non-empty → `Path(value).expanduser().resolve()`.
2. Else on Windows (`os.name == "nt"`): `Path(os.environ["LOCALAPPDATA"]) / "moonlit"` if `LOCALAPPDATA` is set, else `Path.home() / ".moonlit"`.
3. Else (POSIX): `Path.home() / ".moonlit"`.

`os.makedirs(cache_root, exist_ok=True)` MUST succeed before lock acquisition; otherwise exit 1.

## 4. Fast-path / slow-path semantics (D14)

The cache-hit fast path is **unsynchronized**. Readers of a populated cache do not contend. Two invariants make this safe:

- Once `os.replace` has installed `site_parent`, every subsequent reader observes a fully populated `site-packages/` (the directory is moved in one inode operation).
- A reader observing `site_dir.is_dir()` MAY proceed without the lock, because no writer ever mutates a published `site_dir` in place — D4 always renames out and renames in.

The slow path is the only mutator. Under the lock, the writer re-checks the same predicate before extracting; this resolves the race between two cold-cache invocations.

`MOONLIT_FORCE_EXTRACT=1` does NOT bypass the lock (D13). It only causes the slow path to skip the existence re-check, forcing extraction. Concurrent FORCE_EXTRACT runs serialize correctly: the second one sees the first's installed tree, replaces it via D4, and the first reader is unaffected because it already holds an open `addsitedir` reference.

## 5. Lock protocol (D13)

OS-managed advisory locking. The lock file at `<cache_root>/<cache_key>.lock` is opened (`O_CREAT | O_RDWR`, no `O_EXCL`); exclusivity comes from `fcntl.flock` on POSIX and `msvcrt.locking` on Windows. The kernel releases the lock on process death, so crashed processes do not leave the cache permanently jammed.

```python
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
deadline = time.monotonic() + 60.0
try:
    while True:
        if _try_lock(fd):  # platform dispatch; see below
            break
        if time.monotonic() >= deadline:
            os.close(fd)
            raise LockTimeout(lock_path)
        time.sleep(0.050)
    try:
        ...  # critical section
    finally:
        os.close(fd)  # closing the fd releases the OS lock
except BaseException:
    os.close(fd)  # propagate after closing on rare error paths during acquisition
    raise
```

`_try_lock(fd)` is platform-dispatched:

```python
# POSIX
def _try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False

# Windows
def _try_lock(fd):
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
```

- First attempt has no preceding sleep; the 50 ms sleep is between retries only.
- Timeout message: `"moonlit: lock acquisition timed out (60s) at <lock_path>; remove this file or set MOONLIT_FORCE_EXTRACT=1"`. Exit 3. The "remove this file" hint remains as a safety net even though the kernel now releases on crash — it costs nothing and helps users with frozen-but-alive holders (debugger pauses, deep AV scans).
- The lock file is NOT unlinked on release. Unlinking would race against a concurrent opener: the unlinker's flock is on the open file description, and a new `os.open(lock_path)` after the unlink creates a fresh inode whose lock is unrelated.

## 6. Extraction protocol (D1, D4)

1. Create `tmp_dir` via `os.makedirs(tmp_dir, exist_ok=False)`. If it already exists from a prior crashed run with the same pid (vanishingly unlikely), `shutil.rmtree(tmp_dir)` first.
2. Re-open the archive. For each `ZipInfo` in `zf.infolist()`:
   - If `info.filename` does not start with `"site-packages/"` → skip (D1: only that prefix is extracted; `_bootstrap/`, `__main__.py`, `env.json` stay in the zip).
   - Compute `rel = info.filename[len("site-packages/"):]`. If `rel` is empty → skip directory marker.
   - Reject any `rel` containing `..` segments after `posixpath.normpath`; exit 1.
   - Extract to `tmp_dir / "site-packages" / rel`. POSIX symlinks (mode bits `0o120000`) MUST be recreated as symlinks; on Windows, follow the link target inside the archive and write the resolved bytes as a regular file.
3. After all entries are written, atomically install via D4:
   ```python
   def atomic_replace_dir(src, dst, pid):
       old_path = None
       if dst.exists():
           old_path = dst.with_name(f"{dst.name}.old.{pid}")
           os.rename(dst, old_path)
       try:
           os.replace(src, dst)
       except Exception:
           if old_path and old_path.exists():
               os.rename(old_path, dst)
           raise
       if old_path:
           shutil.rmtree(old_path, ignore_errors=True)
   ```
   The argument `src` is `tmp_dir`, `dst` is `site_parent`. After this, `site_dir` is observable to fast-path readers.
4. On Windows, AV/EDR may briefly hold extracted DLLs open. Wrap `os.replace` in a 3-attempt retry loop with 100 ms backoff before giving up with `errno`-aware error message; persistent failure exits 1.
5. `tmp_dir` is cleaned in `finally` regardless; if the disk is full during cleanup, log under `MOONLIT_DEBUG` and ignore.

## 7. sys.path setup

Call `site.addsitedir(str(site_dir))`. This processes `.pth` files. The archive remains on `sys.path[0]` so that `_bootstrap` itself is still importable for the duration of the call.

**Collision check:** if `os.listdir(site_dir)` contains an entry whose **case-folded** name equals `"_bootstrap"`, exit 1 with `"_bootstrap collision in staged tree"`. Case-fold comparison covers Windows / HFS+ case-insensitive filesystems.

## 8. Entry point resolution and return-value coercion

Source: `os.environ.get("MOONLIT_ENTRY_POINT", "")` if non-empty, else `env.entry_point`. Format: exactly one `:` separating `module` and `attr`. The `attr` portion may contain dots (e.g. `pkg.cli:cli.main`).

- Malformed (zero or multiple `:`, empty attr, empty module, `attr.split(".")` yields an empty fragment): exit 2 with `"invalid entry point: <value>"`.
- `importlib.import_module(module)` raising `ImportError` / `ModuleNotFoundError`: exit 2 with `"cannot import <module>: <exc>"`.
- `getattr` walk failure: exit 2 with `"attribute <attr> not found on <module>"`.

The resolved object is invoked with **no positional or keyword arguments** — `obj()`. User code reads `sys.argv` itself if it needs CLI args.

Coercion of the return value:

- `result is None` → `0`.
- `isinstance(result, int)` → `result & 0xFF`. (This branch covers `bool` because `bool` IS `int` in Python; `True` → 1, `False` → 0. No separate bool branch.)
- Else: try `int(result) & 0xFF`; on `TypeError` / `ValueError` exit 2 with `"entry point returned uncoercible value: <type>"`.

## 9. Environment variables (D16)

Five are read. "Truthy" means present and non-empty after `os.environ.get(name, "")`.

| Variable | Meaning |
|----------|---------|
| `MOONLIT_ROOT` | Override cache root. |
| `MOONLIT_FORCE_EXTRACT` | Force re-extraction even on cache hit (does NOT bypass lock). |
| `MOONLIT_ENTRY_POINT` | Override `env.entry_point`. |
| `MOONLIT_DEBUG` | Print bootstrap-internal tracebacks to stderr on failure. |
| `MOONLIT_BUNDLED_PYTHON` | Set by the Windows launcher (D22) to the bundled-Python fingerprint when it dispatches its own cached interpreter. The bootstrap reads it in §2 step 4a to skip the python-version mismatch check. **Not user-facing**: users SHOULD NOT set this manually; doing so does not bypass extraction or the lock, it only skips the version check, which is harmless when bogus (the wrong Python will fail later at import time). |

`MOONLIT_FORCE_EXTRACT=0` is **non-empty hence truthy** — surprising but consistent with the policy. No special-casing of `0` / `false` / `no`. `MOONLIT_PREPEND_PYTHONPATH` and `MOONLIT_INTERPRETER` are NOT recognized in v0.1; they are reserved for v0.2 features and listed nowhere in the v1 contract.

## 10. Error model

Bootstrap-internal failures print a single line `moonlit: <message>` to stderr and exit 1/2/3 per Section 0. Tracebacks are suppressed unless `MOONLIT_DEBUG` is truthy. **User-code exceptions propagate normally** — Python's default `sys.excepthook` runs, the traceback is unconditional, and the process exits 1 from the unhandled exception. `MOONLIT_DEBUG` does not affect user-code traceback printing.

## 11. Cross-platform invariants

- `os.replace(src, dst)` is atomic when `dst` does not exist (POSIX and Windows, Python 3.3+). When `dst` may exist, the D4 protocol applies.
- `os.rename` is forbidden in production paths except inside D4 (where it is used on a non-existent target).
- `os.chmod` calls are best-effort; failures on Windows are ignored.
- Long paths on Windows: paths exceeding 240 characters are extracted via the `\\?\` prefix on raw `os.open`/`shutil` calls. Documented MVP limitation: paths still subject to filesystem-level limits.
- `Path.resolve()` on offline UNC drives may hang. Documented limitation; out of scope for MVP recovery.

## 12. Stdlib-only constraint

The bootstrap MUST import only from the Python 3.13 standard library. Allowed modules and their justifications:

| Module | Why |
|--------|-----|
| `os` | path ops, `os.open`, `os.replace`, `os.environ` |
| `sys` | `sys.argv`, `sys.exit`, `sys.path`, `sys.stdlib_module_names`, `sys.version_info` |
| `json` | env.json parse |
| `zipfile` | archive read + entry extraction |
| `site` | `site.addsitedir` |
| `importlib` | entry-point import |
| `time` | lock poll deadline |
| `pathlib` | `Path` |
| `shutil` | `shutil.rmtree` for tmp / `.old.<pid>` cleanup |
| `traceback` | `MOONLIT_DEBUG` traceback printing |
| `re` | cache-key normalization (D5) |
| `errno` | distinguishing `EEXIST`, `ENOTEMPTY`, `EACCES` in extract / replace error paths; classifying lock-held errnos on Windows |
| `dataclasses` | `Environment` dataclass |
| `posixpath` | `posixpath.normpath` for arcname `..` rejection |
| `fcntl` | `fcntl.flock(LOCK_EX \| LOCK_NB)` on POSIX (D13). Imported only on `os.name != "nt"`. |
| `msvcrt` | `msvcrt.locking(LK_NBLCK, 1)` on Windows (D13). Imported only on `os.name == "nt"`. |

Enforced by **`tests/unit/test_bootstrap_stdlib_only.py`** (D7), which AST-walks `src/moonlit/_bootstrap/`, collects every absolute import name, and asserts each is in `sys.stdlib_module_names`. The test runs in CI on every push. There is no runtime self-check; the test is the gate. A second test in the same file asserts that no module under `_bootstrap/` references `os.rename` outside of the documented D4 protocol.

## 13. Edge cases (enumerated)

1. **Run via `python -m _bootstrap`** — no `.pyz` archive. `zipfile.is_zipfile(sys.argv[0])` is False; exit 1 with `"not a moonlit zipapp"`.
2. **Empty `sys.argv[0]`** — exit 1 with `"cannot locate zipapp (sys.argv[0] is empty)"`.
3. **`name` containing `..` or `/` in env.json** — env.json validation rejects per PEP 508 regex (D11); cache-key derivation also rejects, joint defense.
4. **Two processes, same `(name, build_id)`, cold cache** — one wins the lock, extracts, releases; the other re-checks under the lock, sees populated `site_dir`, releases without extracting.
5. **Filesystem case-insensitivity** — `MyApp` and `myapp` produce the same `cache_key` (lowercased per D5). By design.
6. **SIGKILL during extraction** — `tmp_dir` leaks (cleaned by `_sweep_old_siblings` on the next bootstrap run for the same `cache_key`). The OS releases the lock automatically; the persistent `lock_path` file is reusable by the next holder.
7. **AV/EDR holds DLL during `os.replace` on Windows** — 3-attempt retry with 100 ms backoff inside D4; persistent failure exits 1.
8. **Disk full mid-extraction** — `tmp_dir` cleanup in `finally`; failure during cleanup is logged under `MOONLIT_DEBUG`, otherwise silent.
9. **Symlinks in the archive** — POSIX recreates as symlinks; Windows resolves and writes as regular file.
10. **`MOONLIT_FORCE_EXTRACT=0`** — truthy (non-empty), forces re-extraction. Surprising; documented in Section 9.
11. **Stale `.old.<pid>` siblings** — opportunistically swept by the next process holding the lock for that `cache_key`, errors ignored.
12. **`MOONLIT_ROOT` on offline network drive** — `Path.resolve()` may hang. Out of scope.
13. **`_bootstrap` directory shipped inside site-packages** — Section 7 collision check exits 1.
14. **Byte-identical re-extraction precondition** — modes match across two FORCE_EXTRACT runs only if the source build is deterministic and (on POSIX) the source file modes are stable. Documented.
