# Cache Layout Specification

Status: stable for moonlit v0.x. Layout version: 1.

## 1. Constants

| Name | Value | Used by |
|---|---|---|
| `LOCK_POLL_INTERVAL` | 50 ms | `_bootstrap/locking.py` |
| `LOCK_TIMEOUT` | 60 s wall-clock | `_bootstrap/locking.py` |
| `TEMPDIR_PREFIX` | `.` | tempdir name |
| `TEMPDIR_SUFFIX` | `.tmp.<pid>` | tempdir name |
| `OLDDIR_SUFFIX` | `.old.<pid>` | replaced-dir name (D4) |
| `LOCKFILE_SUFFIX` | `.lock` | sibling lock file |
| `BUILD_ID_HEX_LEN` | 64 | SHA-256 |

## 2. Cache root location

Resolved in order:
1. `MOONLIT_ROOT` environment variable, if present and non-empty.
2. Else Windows: `%LOCALAPPDATA%\moonlit\`, falling back to `~\.moonlit\`.
3. Else POSIX: `~/.moonlit/`.

Canonicalized via `os.path.realpath`. Created with `os.makedirs(..., exist_ok=True)` on first use. On Windows, `realpath` resolves junctions but does NOT lowercase; case-only differences across processes may compare unequal (accepted limitation; the cache root is per-user).

## 3. Cache key

Per D5, the cache key is computed at runtime by `_bootstrap/extract.py` from the raw `env.json.name` field:

```python
def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

cache_key = f"{pep503(name)}_{build_id}"
```

`build_id` is the 64-char lowercase hex SHA-256 produced by `hashing.compute_build_id` at build time. `env.json.name` itself is the unmodified `[project].name`; normalization is the consumer's job. The legacy phrasing `<name>_<build_id>` is dropped after this introduction; from here on the key is referred to as `<cache_key>`.

## 4. Directory tree

```
<cache_root>/
  <cache_key>/
    site-packages/
      <package>/
      <dist-info-dirs>/
  <cache_key>.lock
  .<cache_key>.tmp.<pid>/        # mid-extraction staging
  .<cache_key>.old.<pid>/        # transient: previous dir during atomic-replace (D4)
```

The `.<cache_key>.old.<pid>/` entry exists only during the rename-then-replace-then-cleanup window of an extraction that overwrites an existing cache directory; under normal cache-hit reads it is absent.

Names with characters illegal on the target FS, or empty after normalization, are fatal before any FS touch.

## 5. Extraction transform (D1)

Given a `.pyz` whose top-level zip arcs are `site-packages/`, `_bootstrap/`, `__main__.py`, and `env.json`, the bootstrap extracts ONLY arcs whose name starts with `site-packages/`. Each such arc `site-packages/<rel>` is written to `<cache_root>/<cache_key>/site-packages/<rel>`. The `site-packages/` prefix is stripped during write. The other three top-level arcs (`_bootstrap/`, `__main__.py`, `env.json`) are read by the bootstrap directly from the zip and are NOT extracted to the cache.

Extraction protocol:
1. Acquire lock `<cache_root>/<cache_key>.lock` (D13).
2. Re-check `<cache_root>/<cache_key>/site-packages/` exists (D14 inside-lock check). If yes and `MOONLIT_FORCE_EXTRACT` is unset, release and return cache hit.
3. Create `.<cache_key>.tmp.<pid>/site-packages/` and extract matching arcs into it. The tempdir is structured so that a single rename of the tempdir produces the desired `<cache_key>/site-packages/` layout.
4. Apply `atomic_replace_dir(.<cache_key>.tmp.<pid>, <cache_key>, pid)` (see Section 8).
5. Opportunistically sweep stale `.<cache_key>.old.*` and `.<cache_key>.tmp.*` siblings (best-effort, errors swallowed).
6. Release lock.

## 6. Lifecycle

Created by bootstrap on first run or when `MOONLIT_FORCE_EXTRACT=1`. Read by bootstrap on cache hit. Modified during normal use only by Python's bytecode compiler (Section 7). Never deleted automatically; manual reaping is the user's job. The `moonlit clean` subcommand (specs/01-cli.md §2.4, policy in §12.1 below) is the supported tool — there is no daemon and no implicit GC.

## 7. Read-mostly contract

The cache is read-mostly, not strictly read-only. Python's import system writes `.pyc` files under `<cache_key>/site-packages/**/__pycache__/` during normal execution; this is EXPECTED and not a violation of the cache contract. The only writers are (a) the bootstrap, during extraction inside the lock, and (b) CPython's bytecode compiler, which writes its own `__pycache__/` entries. No moonlit code mutates `<cache_key>/site-packages/` after the atomic install.

## 8. Atomic rename semantics (D4)

`os.replace(src_dir, dst_dir)` does NOT atomically replace an existing non-empty directory on Windows, and raises `ENOTEMPTY` on POSIX. The protocol when the destination may exist:

```python
def atomic_replace_dir(src, dst, pid):
    old = None
    if dst.exists():
        old = dst.with_name(f"{dst.name}.old.{pid}")
        os.rename(dst, old)        # atomic
    try:
        os.replace(src, dst)        # atomic; dst now does not exist
    except Exception:
        if old and old.exists():
            os.rename(old, dst)     # rollback
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)
```

Stale `.<cache_key>.old.<pid>/` siblings are opportunistically swept on the next bootstrap run that holds the lock for that cache key (best-effort; errors ignored).

## 9. Locking semantics (D13, D14)

- Lock path: `<cache_root>/<cache_key>.lock` (sibling, NOT inside the cache dir).
- Opened via `os.open(lock_path, O_CREAT | O_RDWR, 0o600)`. Exclusion is OS-managed: `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows. Both are non-blocking; the poll loop drives retries (`LOCK_POLL_INTERVAL`, `LOCK_TIMEOUT`). Released by closing the fd; the kernel releases the lock on process death.
- The lock file persists across releases. Unlinking would race against a concurrent opener: the OS lock is per open file description, and a `rm` followed by a fresh `os.open` produces a new inode whose lock is unrelated. The persistent file is small (zero bytes).
- Cache-hit fast path (D14): readers that observe `<cache_key>/site-packages/` exists AND `MOONLIT_FORCE_EXTRACT` unset SKIP the lock entirely. Lock contention is bounded to first-extraction and forced re-extractions.
- `MOONLIT_FORCE_EXTRACT=1` does NOT bypass the lock; it only skips the existence shortcut after the lock is held.
- Stale-lock recovery for crashed processes is automatic — the kernel releases the lock when the process exits. Live contention still surfaces as exit code 3 after the 60 s timeout. Manually `rm <cache_root>/<cache_key>.lock` is harmless and remains a safety net.

## 10. Concurrency invariants

- At most one process holds `<cache_key>.lock` at a time, on a healthy local FS.
- Distinct `<cache_key>` values do not contend.
- A populated `<cache_key>/site-packages/` is read by the cache-hit fast path without synchronization. After `os.replace` installs the directory, all subsequent readers see the populated state. Cooperative invariant: no other tool creates entries under `<cache_key>/`.

## 11. Safe-to-delete table

| Path | Safe-when |
|---|---|
| Entire `<cache_root>/` | No moonlit-built `.pyz` is currently running. |
| `<cache_key>/` | No `.pyz` with this key is currently running; next run will re-extract. |
| `.<cache_key>.tmp.<pid>/` | The owning `<pid>` is not alive. (Liveness check is racy across pid reuse; in MVP, treat all tempdirs as deletable when the user is sure no `moonlit`-extracting process is active.) |
| `.<cache_key>.old.<pid>/` | Same as `.tmp.<pid>` — pid not alive. |
| `<cache_key>.lock` | At any time when no process currently holds the OS lock; the kernel releases the lock on process death and the file is harmless to leave in place. |
| `<cache_key>/site-packages/**/__pycache__/` | At any time; CPython will regenerate. |
| `<cache_key>/` while a `.pyz` of that key runs | NEVER. |
| `.<cache_key>.tmp.<pid>/` mid-extraction | NEVER. |

## 12. GC, disk usage, path normalization

No implicit GC; every distinct `<cache_key>` consumes one staged-site-packages worth of disk until the user explicitly reaps it. `realpath` after env-var expansion; internal comparisons use `os.path.normpath`; on-disk paths use the native separator. Moonlit creates no symlinks.

## 12.1 GC policy (`moonlit clean`)

`moonlit clean` (specs/01-cli.md §2.4) is the only supported way to shrink the cache root. Its policy is pinned here so future revisions do not drift.

**Selection.** Candidates are exactly the well-formed `<cache_key>` directories directly under `<cache_root>/` plus their `.tmp.<pid>/`, `.old.<pid>/`, and `<cache_key>.lock` siblings. Unknown directory names (e.g. a future `v2/` reserved subtree per §14, third-party debris) are skipped silently and never deleted. The flag matrix:

| Flag | Effect on the candidate set |
|------|-----------------------------|
| `--all` | match every well-formed `<cache_key>` directory |
| `--older-than <dur>` | match cache entries whose `<cache_key>/site-packages/` `st_mtime` is older than now − duration |
| `--keep-latest <N>` | group by normalized name; keep the N newest by `site-packages` mtime; mark the rest deletable |
| `--name <pat>` | fnmatch glob over the normalized name (the part before the trailing `_<64hex>`) |

When more than one filter is set, the deletion set is the intersection. `--keep-latest` is applied last (after the other filters narrow the candidate set).

**Liveness model (D23).** For each cache entry slated for deletion, `moonlit clean` tries to acquire `<cache_root>/<cache_key>.lock` non-blocking, using the same OS primitives the bootstrap uses (`fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows). If acquisition fails, the entry is skipped with reason `(locked)`. `--force` overrides the check — the entry is deleted regardless of lock state. Cache-hit fast-path readers (§9 / D14) hold no lock and are therefore not detectable; running `moonlit clean` while a moonlit `.pyz` is actively importing is undefined behavior. The deletion happens **while holding the lock**, so a concurrent extractor that wins the lock after us starts from a clean slate.

**Orphan reap.** `moonlit clean` is the natural place to remove leaked staging junk:
- `.<cache_key>.tmp.<pid>/` and `.<cache_key>.old.<pid>/` siblings are deleted whenever (a) their owning `<cache_key>` is in the deletion set for this run, or (b) the owning `<cache_key>` directory no longer exists. Per-pid liveness checks are NOT performed — they are racy across pid reuse (§11). The user's "no moonlit .pyz is currently extracting" assertion is the contract.
- `<cache_key>.lock` files whose `<cache_key>/` is missing (or being deleted in this run) are unlinked after a successful try-lock confirms no one currently holds them. Unhealthy lock files (`--force` overrode a live holder) are left in place; the kernel releases them on process death and the next opener will create a fresh open file description.

**Output.** A four-action table (`keep`, `delete`, `skip`, `orphan`) on stderr followed by a single trailer line on stdout (`deleted N entries, freed <bytes_humanized>` or `would delete …, would free …` under `--dry-run`). Full grammar in specs/01-cli.md §2.4.

**Bare invocation.** `moonlit clean` with none of `--all`, `--older-than`, `--keep-latest`, `--name` exits 2 with a usage message. There is no implicit "scan and report" default — that would tempt users into a "do nothing" mental model.

## 13. Edge cases

1. Network FS: `fcntl.flock` semantics on NFS<v4 (and on some SMB shares) are weaker than local; the lock degrades to advisory cooperation. Recommendation: keep `MOONLIT_ROOT` on a local filesystem.
2. Read-only FS: extraction fails; bootstrap exits with the runtime generic error.
3. User deletes `site-packages/` mid-execution: behavior undefined (loaded modules survive in memory; new imports fail).
4. Distinct names sharing a `build_id`: distinct cache entries (full key includes name).
5. Same `<cache_key>` from a different bootstrap version: cache hit. The bootstrap that runs is whatever was baked into the `.pyz`; bootstrap version is not part of the key.
6. Stale `.tmp.<pid>` from a SIGKILL'd extractor: leaks until reaped by `moonlit clean` (§12.1) on the next user-initiated run.
7. Stale lock: only possible from a still-live but stuck holder (debugger pause, deep AV scan). 60 s timeout then bootstrap exit code 3. Crashed holders no longer wedge the cache — the kernel releases their lock at process exit.
8. Antivirus interference on Windows holding handles during `os.replace`: best-effort retry inside `atomic_replace_dir` (documented limitation).
9. Disk-full mid-extract: tempdir abandoned, lock released in `finally`; user retries with `MOONLIT_FORCE_EXTRACT=1`.
10. Cross-user shared FS: umask of the first extractor governs others' read access; not supported in MVP.
11. **Windows `MAX_PATH` (260 chars)**: deeply nested wheel files under `<cache_root>\<cache_key>\site-packages\...` can exceed the limit. Mitigations: set `MOONLIT_ROOT` to a short path (e.g. `C:\m`); or enable Windows long-path support in the registry plus the python.exe manifest. Documented limitation in v0.1.

## 14. Compatibility

Layout version 1 is stable across moonlit v0.x. `env.json.schema_version` bumps do not change cache layout. A future incompatible layout change will be parked under a reserved top-level subdir of `<cache_root>` (e.g. `v2/`); v1 caches sit at the cache_root level and will not collide.
