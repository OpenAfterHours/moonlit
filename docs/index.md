# moonlit

`moonlit` is a CLI that bundles a uv-managed Python project into a single self-contained zipapp ([PEP 441](https://peps.python.org/pep-0441/)). The produced `.pyz` ships every transitive dependency from `uv.lock`; on the end user's machine it extracts to a per-build cache on first run and dispatches the configured entry point.

It is similar to LinkedIn's [shiv](https://github.com/linkedin/shiv), with two differences:

- **Built on uv, not pip.** Resolution is done by `uv export --frozen` against `uv.lock`; staging is done by `uv pip install --target` (no virtualenv); the target's wheel is built by `uv build --wheel`. Network and resolution behavior follow uv, not pip.
- **uv workspaces are first-class.** `--package <member>` selects a workspace target; transitive workspace deps are bundled automatically via `uv build --all-packages`.

## At a glance

```sh
moonlit build --package shouter -e shouter.cli:main -o shouter.pyz
python ./shouter.pyz
```

Three outcomes after that run:

1. The `.pyz` exists in the current directory, atomically written (no partial output on a crashed build).
2. A cache directory appears under `%LOCALAPPDATA%\moonlit` (Windows) or `~/.moonlit` (POSIX), keyed by the package name and a content hash of the staged tree.
3. The entry point runs.

The bootstrap that drives step 2 and step 3 is **stdlib-only** — it imports nothing third-party because it runs *before* the staged `site-packages/` reaches `sys.path`.

## Where to next

- [Getting started](getting-started.md) — install, build, and run the canonical demo.
- [CLI reference](cli-reference.md) — every flag, every exit code.
- [Runtime](runtime.md) — what happens inside the `.pyz`: cache layout, env vars, and the runtime contract.

## Status

Pre-release (0.x). The exact CLI flag set and exit codes are stabilizing toward 1.0; the produced `.pyz` runtime contract is pinned by the design specs in the source tree under [`specs/`](https://github.com/anthropics/claude-code/blob/main/specs).

Out of scope for the MVP and deferred to v0.2 or later: byte-reproducible builds, `.pyc` precompilation, integrity verification (`--no-modify`), a Windows native `.exe` launcher, and OS-level `flock`/`msvcrt` locking. The cache uses a sentinel-file lock today; recovery from a crashed extraction is documented in [Runtime](runtime.md).
