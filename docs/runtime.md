# Runtime

This page describes what happens *inside* a `.pyz` produced by `moonlit build` — when an end user runs `python app.pyz`, what files appear on disk, and how the bootstrap maps environment variables to behavior.

## What runs first

Every `.pyz` ships with three things at the zip root:

- `__main__.py` — a 3-line shim that imports and invokes `bootstrap()`.
- `_bootstrap/` — a stdlib-only package with the runtime logic.
- `env.json` — the build-time descriptor (name, build_id, entry_point, …).

When you run `python app.pyz`, Python's zipapp machinery prepends the archive to `sys.path` and executes `__main__.py`:

```python
import sys
from _bootstrap import bootstrap
sys.exit(bootstrap())
```

The bootstrap then:

1. Resolves the archive path via `os.path.abspath(sys.argv[0])`.
2. Reads and validates `env.json`.
3. **Python version check.** If `env.json.python_version` is present, compare it against `f"{sys.version_info.major}.{sys.version_info.minor}"`. On mismatch, exit 1 with a `moonlit: this archive was built for Python X.Y, but you are running Python A.B …` line — see [Python version check](#python-version-check).
4. Computes a cache key from the `[project].name` and the build_id.
5. Resolves the cache root (see below).
6. Either takes the **fast path** (cache hit, no lock) or the **slow path** (acquire lock, extract to tempdir, atomically install, release lock).
7. Calls `site.addsitedir(<cache>/<key>/site-packages)` so the staged tree reaches `sys.path` and `.pth` files are processed.
8. Resolves the entry point string (or its `MOONLIT_ENTRY_POINT` override), imports the module, walks the attribute path, calls `obj()`.
9. Coerces the return value: `None` → 0, `int` → masked `& 0xFF`, otherwise `int(result) & 0xFF` (or exit 2 if uncoercible).

## Cache layout

The cache root is resolved once per invocation:

| Platform | Default cache root |
|---|---|
| Windows | `%LOCALAPPDATA%\moonlit` (or `~/.moonlit` if `LOCALAPPDATA` is unset) |
| POSIX (Linux, macOS) | `~/.moonlit` |
| Anywhere | `MOONLIT_ROOT` overrides if set |

Inside the cache root:

```
<cache_root>/
├── <cache_key>/                # Populated, atomic-replaced site-packages parent.
│   └── site-packages/          # Contents of the .pyz's site-packages/.
├── <cache_key>.lock            # Persistent lock file (see "Fast path and slow path").
└── .<cache_key>.tmp.<pid>/     # Staging dir for one in-progress extraction.
```

The **cache_key** is `<normalized_name>_<build_id>`, where:

- `<normalized_name>` is the [PEP 503](https://peps.python.org/pep-0503/) normalization of `env.json.name` (lowercase; runs of `[-_.]` collapsed to `-`). So a project authored as `My_App.Name` produces `my-app-name` here.
- `<build_id>` is a 64-character lowercase hex SHA-256 digest of every file under the staged `site-packages/` (excluding `__pycache__/` segments and `.pyc` files), interleaved with their forward-slash relative paths and separated by `\0` bytes.

Two builds with the same `uv` version, Python interpreter (major.minor.patch), `uv.lock`, and `pyproject.toml` produce the same `build_id` — and therefore the same cache key, so they share a cache.

### Safe to delete

Anything under the cache root is safe to delete. The next invocation will re-extract.

To clear the entire cache:

=== "Windows"

    ```pwsh
    Remove-Item -Recurse -Force $env:LOCALAPPDATA\moonlit
    ```

=== "POSIX"

    ```sh
    rm -rf ~/.moonlit
    ```

To clear just one build's cache:

```sh
rm -rf <cache_root>/<cache_key>/
```

## Fast path and slow path

The cache hit fast path is **unsynchronized** — readers of a populated cache do not contend with each other and do not acquire the lock. The bootstrap proceeds directly to `site.addsitedir()`.

The slow path is the only mutator. It opens `<cache_root>/<cache_key>.lock` with `O_CREAT | O_RDWR` (no `O_EXCL` — the file is shared) and acquires an exclusive **OS-managed advisory lock** on the open file: `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows. Acquisition polls every 50 ms with a 60-second wall-clock timeout. After acquiring, it re-checks the cache (a sibling may have just won the race), extracts to a per-pid tempdir, and atomically installs via:

1. Rename the existing `<cache_key>/` aside to `<cache_key>.old.<pid>/`.
2. `os.replace(<tmp_dir>, <cache_key>/)`.
3. `shutil.rmtree(<cache_key>.old.<pid>/)`, best-effort.

This protocol is correct on POSIX *and* Windows since Python 3.3.

The lock file is **persistent by design** — closing the fd releases the OS lock, and the kernel releases it on process death, so the lock file itself doesn't need to be unlinked (and unlinking would race a concurrent opener, since `flock` is per open file description). A leftover `<cache_key>.lock` on disk is normal and does **not** indicate a stuck cache; only an unreleased OS lock would.

If extraction fails between rename and replace, the original `<cache_key>.old.<pid>/` is renamed back. If the process is hard-killed during extraction, the OS releases the lock automatically; the per-pid tempdir may leak (it's safe to delete).

## Environment variables

The bootstrap reads exactly five:

| Variable | Effect |
|---|---|
| `MOONLIT_ROOT` | Override the cache root. The path is `Path(value).expanduser().resolve()`. |
| `MOONLIT_FORCE_EXTRACT` | Force re-extraction even on a cache hit. **Does not** bypass the lock; only the existence-skip is suppressed. |
| `MOONLIT_ENTRY_POINT` | Override `env.json.entry_point`. Useful for testing. Same `module:attr` syntax. |
| `MOONLIT_DEBUG` | On a bootstrap-internal error, print the Python traceback after the `moonlit:` line. Does not affect user-code traceback printing (Python's default excepthook handles those unconditionally). |
| `MOONLIT_BUNDLED_PYTHON` | Set by the Windows launcher (not the user) when it re-invokes the archive under a bundled interpreter — value is the `env.json.bundled_python.fingerprint`. The bootstrap matches it against the manifest to skip the [Python version check](#python-version-check) for that one nested invocation. A non-matching value falls through to the strict check. |

"Truthy" means *present and non-empty after `os.environ.get(name, "")`*. The empty string is treated as unset; `MOONLIT_FORCE_EXTRACT=0` is **non-empty hence truthy** (surprising but consistent — the policy never special-cases "0", "false", or "no").

Names beginning with `MOONLIT_` other than the five above are reserved for future versions and ignored today.

## Runtime exit codes

The runtime exit-code namespace is **independent** from the build-time CLI's. Different process, different concerns.

| Code | Meaning |
|---|---|
| 0 | Success (entry point returned `None`, an `int` in `[0, 255]`, or anything coercible to one). |
| 1 | Generic bootstrap-internal error: `env.json` missing or fails validation, archive unreadable, extraction I/O failure, `_bootstrap` collision in the staged tree, empty `sys.argv[0]`, runtime Python's `major.minor` differs from `env.json.python_version`. |
| 2 | Entry-point resolution or return-value coercion failure: malformed entry point, module not importable, attribute not found on module, return value can't be coerced to an `int`. |
| 3 | Lock acquisition timed out (60 seconds). |

Other non-zero codes originate from user code via its own `sys.exit()` or the masked `int()` of its return value.

User-code exceptions propagate normally — Python's default `sys.excepthook` runs, the traceback prints unconditionally, and the process exits 1 from the unhandled exception.

## Lock-timeout recovery

Because the lock is OS-managed, a hard-killed extractor does **not** wedge the cache — the kernel releases the lock on process death and the next invocation acquires it immediately. The persistent `<cache_root>/<cache_key>.lock` file on disk is expected and is **not** itself a sign of trouble.

A real timeout means another process is actively holding the lock for longer than 60 seconds (e.g. a very slow extraction on a contended volume, or a paused/stopped extractor process). In that case the next invocation exits 3 with:

```
moonlit: lock acquisition timed out (60.0s) at <path>; remove this file or set MOONLIT_FORCE_EXTRACT=1
```

To recover:

- Find and resume/terminate the process holding the lock — once it dies or releases, the next run proceeds normally.
- Or delete the lock file (`rm <cache_root>/<cache_key>.lock`). This is safe only if you're confident no live process holds the lock; doing so while a sibling is mid-extraction races a concurrent opener.

`MOONLIT_FORCE_EXTRACT=1` does **not** bypass the lock; it only suppresses the existence-skip after the lock is acquired. Two concurrent forced runs serialize correctly: the second sees the first's installed tree, replaces it via the atomic protocol, and the first reader is unaffected because it already holds an open `addsitedir` reference.

## Python version check

Native-extension wheels (`.pyd` on Windows, `.so` elsewhere) are tagged with a `cp<X><Y>` ABI tag. Python's import machinery silently skips files whose tag doesn't match the running interpreter, surfacing as `ModuleNotFoundError: No module named '<pkg>._core'` rather than a clear "wrong Python" error. To turn that confusing failure into an actionable one, `moonlit build` stamps the target Python's `major.minor` into `env.json.python_version`, and the bootstrap rejects any mismatch up-front:

```
moonlit: this archive was built for Python 3.12, but you are running Python 3.13;
install a Python 3.12 interpreter or rebuild with `moonlit build --python <python-3.12>`
```

This check fires **before** cache resolution and extraction, so a wrong-Python invocation never touches the cache. By default the stamped value is the build host's `sys.version_info.major.minor`; pass `--python-version <X.Y>` to `moonlit build` to target a different ABI (cross-interpreter builds — see [CLI reference](cli-reference.md) and [Getting started → Cross-interpreter builds](getting-started.md#cross-interpreter-builds)).

Archives produced by older `moonlit` versions that predate this field omit `python_version`; the bootstrap skips the check in that case (forward-compatible — older `.pyz` files keep working under newer bootstraps and vice versa).

## Threat model

`env.json` is **not authenticated**. A modified `.pyz` could ship a forged `env.json` and the bootstrap would trust it. Integrity verification is the `--no-modify` feature deferred to v0.2. The bootstrap does not auto-execute privileged behavior keyed solely on `name`.

## env.json schema

For reference; the `env.json` produced by `moonlit build` looks like:

```json
{
  "build_id": "<64 hex chars>",
  "built_at": "2026-05-09T15:23:01Z",
  "entry_point": "myapp.cli:main",
  "moonlit_version": "0.3.0",
  "name": "myapp",
  "python_shebang": "/usr/bin/env python3",
  "python_version": "3.13",
  "schema_version": 1
}
```

When the archive was built with `--bundle-python`, an additional `bundled_python` object is present:

```json
{
  "...": "(fields above)",
  "bundled_python": {
    "prefix": "_python/",
    "relative_python_exe": "python.exe",
    "fingerprint": "<64 hex chars>"
  }
}
```

The sub-fields name where in the zip body the interpreter lives (`prefix`, must end in `/`), where `python.exe` sits relative to that prefix (`relative_python_exe`, a non-empty relative POSIX path), and a 64-char lowercase-hex fingerprint over the dist tree. The fingerprint is the value the launcher sets as `MOONLIT_BUNDLED_PYTHON` when re-invoking under the bundled interpreter.

Validation is ordered (the first failure decides the error message): existence in archive, UTF-8 decode, JSON parse, top-level dict, `schema_version` is an integer (not bool) equal to `1`, all required fields present, types correct, format checks (PEP 508 name regex, lowercase 64-hex `build_id`, `module:attr` entry point, `%Y-%m-%dT%H:%M:%SZ` `built_at`, non-empty `moonlit_version`, non-empty `python_shebang` with no embedded newline and no leading `#!`). The optional `python_version` field, when present, must match `^\d+\.\d+$` (`major.minor` only); when absent the runtime version check is skipped — see [Python version check](#python-version-check). The optional `bundled_python` object, when present, must be a dict containing exactly the three string sub-fields above, each matching its format rule; the schema version is **not** bumped (it's an additive v1 field).
