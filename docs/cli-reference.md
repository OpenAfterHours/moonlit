# CLI reference

## `moonlit`

```
moonlit [-V | --version] [-h | --help] <subcommand> [args...]
```

### Top-level flags

| Flag | Description |
|---|---|
| `-V`, `--version` | Print `moonlit <semver>` to stdout and exit 0. |
| `-h`, `--help` | Print top-level help to stdout and exit 0. |

Running `moonlit` with no subcommand and no flag prints the help to stderr and exits 2. Running `moonlit <unknown>` (with or without `--help`) prints `error: no such subcommand: <name>` to stderr and exits 2.

Subcommands: [`build`](#moonlit-build) (produce a `.pyz`/`.exe` from a local uv project), [`pack`](#moonlit-pack) (produce one straight from PyPI packages — no project needed), [`info`](#moonlit-info) (inspect a built archive's `env.json`), and [`clean`](#moonlit-clean) (reap stale cache entries).

## `moonlit build`

```
moonlit build [PROJECT] -e <entry> | -c <script> -o <output> [flags]
```

`PROJECT` is an optional positional that defaults to the current working directory. It must resolve to an existing directory.

### Flags

| Short | Long | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `-e` | `--entry-point` | `module:callable` | one-of `{-e, -c}` | — | Entry point string baked into `env.json`. |
| `-c` | `--console-script` | string | one-of `{-e, -c}` | — | Console-script name; resolved against staged `*.dist-info/entry_points.txt`. |
| `-o` | `--output-file` | path | yes | — | Destination `.pyz` (or `.exe` with `--windows-exe`). With `--bundle-python` set, this is a **directory path** instead — it MUST NOT end in `.exe` or `.pyz`. |
| `-p` | `--python` | string | no | `/usr/bin/env python3` | Shebang line baked into `env.json` and prefixed to the artifact. ASCII only, no `\n`/`\r`/`\x00`, ≤127 bytes. With `--windows-exe`, the default pivots to `python.exe` (or `py -<X.Y>` when `--python-version` is set). |
|  | `--package` | string | iff workspace | — | Workspace member to build; required iff `[tool.uv.workspace]` is present, forbidden otherwise. PEP 503 normalized on both sides. |
|  | `--no-dev` | flag | no | (default) | Exclude dev-group dependencies (default behavior). |
|  | `--dev` | flag | no | off | Opt in to dev-group dependencies. Mutually exclusive with `--no-dev`. |
|  | `--windows-exe` | flag | no | off | Produce a native Windows `.exe` (small Rust launcher prepended to the same zip body) instead of a `.pyz`. Requires `-o` to end in `.exe`. The recipient still needs a Python interpreter on `PATH` or registered with `py.exe`. |
|  | `--python-version` | string `<X.Y>` | no | build host's `sys.version_info.major.minor` | Target Python `major.minor` for cross-interpreter builds (e.g. `3.12`). Threads through every `uv` invocation as `--python <X.Y>` so wheels are tagged for that ABI; stamped into `env.json.python_version`. uv auto-fetches a managed standalone CPython if the requested version isn't locally installed. Format: `^\d+\.\d+$`. |
|  | `--bundle-python` | flag | no | off | Produce a self-contained **directory bundle** instead of a single file. The bundle contains `<basename>.exe` (a thin Rust launcher), `<basename>.pyz` (the application zipapp), and `_python\` (a managed CPython tree from `uv python install`). Recipients without Python on `PATH` run `<basename>.exe`, which probes for the sibling `_python\python.exe` and spawns it directly — nothing is extracted at runtime, which avoids the `Trojan:Win32/Wacatac.B!ml` false positives that hit moonlit 0.3.0's single-file bundle shape. With this flag, `-o` is a directory path; it MUST NOT end in `.exe` or `.pyz`. `--windows-exe` may be set alongside but is a no-op (the folder always contains a launcher `.exe`). |
|  | `--force` | flag | no | off | Overwrite an existing regular-file output. Does **not** override a directory target. |
|  | `--gc` / `--no-gc` | flag | no | `--gc` (on) | Bake runtime cache self-GC into the artifact. On the recipient machine, after a fresh extraction, the app trims its own older cache entries — keeping the most recent — so a recipient's cache root doesn't grow by one extracted `site-packages` per rebuild forever. Stamped into `env.json.gc`; recipients can override at runtime with `MOONLIT_NO_GC` etc. See [Runtime → Automatic cache cleanup](runtime.md#automatic-cache-cleanup). |
|  | `--gc-keep-latest` | int | no | `2` | How many of this app's newest cache entries to keep (≥ 1). `2` = current build + one predecessor (a rollback margin and a guard against reaping a still-running previous version). `1` = leave only the most recent. |
|  | `--gc-grace` | duration | no | `24h` | Only reap entries older than this (`<int><s\|m\|h\|d>`, e.g. `1h`, `7d`). Spares a build a concurrently-running older process may still be using. |
| `-q` | `--quiet` | flag | no | off | Suppress progress output on stderr; the success line on stdout is preserved. |
| `-v` | `--verbose` | flag | no | off | On error, append the full traceback to stderr. Mutually exclusive with `--quiet`. |
| `-h` | `--help` | flag | no | — | Print this help to stdout, exit 0. Short-circuits all other validation. |

### Flag interactions

- Exactly one of `-e` / `-c` is required. Neither or both → exit 2.
- `-q` and `-v` are mutually exclusive → exit 2.
- `--no-dev` and `--dev` are mutually exclusive → exit 2.
- `--package` is required iff the project is a uv workspace → exit 5 on mismatch.
- `--windows-exe` (without `--bundle-python`) requires `--output-file` to end in `.exe` (case-insensitive) → exit 2.
- `--python-version` must match `^\d+\.\d+$` (major.minor only) → exit 2.
- `--bundle-python` rejects `--output-file` values ending in `.exe` or `.pyz` → exit 2. With `--bundle-python` the output is a directory; the directory's basename is reused for the launcher and inner zipapp filenames inside.
- When `--windows-exe` AND `--python-version` are set AND `-p` is at its default, the default shebang pivots from `python.exe` to `py -<X.Y>` so the recipient's PEP 397 launcher pins to the matching interpreter.
- The `MOONLIT_*` environment variables are runtime-only; they are *ignored* during a build.

### Preflight order

The CLI performs these checks in order; the first failure short-circuits with the listed exit code. The order is part of the contract — tests pin which fault wins on multi-fault inputs.

1. Click argument parsing (unknown flag, missing `-o`, both/neither of `-e`/`-c`, `-q`+`-v`, `--no-dev`+`--dev`) → exit 2.
2. `PROJECT` resolves to an existing directory → exit 2.
3. `uv` on `PATH` (`shutil.which("uv")`) → exit 3.
4. `<PROJECT>/pyproject.toml` exists → exit 5.
5. `<PROJECT>/uv.lock` exists → exit 4.
6. Workspace shape vs `--package` → exit 5.
7. `--entry-point` syntactic validity → exit 6.
8. Output-path preflight → exit 7.
9. Build pipeline → exits 6, 8, 9, 10.

### Exit codes

| Code | Meaning | Error class |
|---|---|---|
| 0 | Success | — |
| 1 | Unhandled Python exception (a moonlit bug) | — |
| 2 | CLI usage error (parser-level) | — |
| 3 | `uv` binary not on `PATH` | `UvNotFoundError` |
| 4 | `uv.lock` missing | `NoLockfileError` |
| 5 | Workspace shape mismatch / pyproject malformed | `NotAWorkspaceError`, `UnknownPackageError`, `MissingPackageError`, `MalformedPyprojectError` |
| 6 | Entry-point resolution failed | `BadEntryPointError`, `ConsoleScriptNotFoundError` |
| 7 | Output path issue | `OutputExistsError`, `OutputNotWritableError` |
| 8 | `uv export` failure | `ExportError` |
| 9 | `uv pip install --target` failure | `StagingError` |
| 10 | `uv build` wheel failure or wheel artifact issue | `WheelArtifactError` |
| 11 | Internal invariant violation (a moonlit bug) | `InternalError` |
| 12 | Input archive (for `moonlit info`) is not a valid moonlit `.pyz` | `BadArchiveError` |
| 13 | `--bundle-python`: `uv python install` failed or the install dir's shape was unexpected | `PythonBundleError` |
| 14 | `moonlit clean`: at least one entry was skipped because its lock was held and `--force` was not set | `CleanRefusedError` |
| 15 | `moonlit clean`: I/O failure during deletion (`rmtree` raised, etc.) | `CleanIOError` |
| 130 | SIGINT (Ctrl-C) | — |

Build-time and runtime exit codes are independent enumerations; the runtime codes (0–3) live in [Runtime](runtime.md). The same numeric value can mean different things in the two namespaces.

### stdout / stderr semantics

- **Default**: progress lines go to stderr — one line per pipeline step with a brief result and elapsed time (e.g. `writing archive · wrote myapp.pyz · 1.3s total`). The final line `wrote <output> (<size>, <N> entries)` goes to stdout.
- **`--quiet`**: stderr is suppressed; the stdout success line is preserved.
- **`--verbose`**: on error, the full traceback follows the error line on stderr.
- **Errors**: every error is a single line on stderr, formatted as `<ErrorClassName>: <message>`. With `--quiet`, errors are still emitted.
- **Parser-level errors**: formatted as `error: <message>` (lowercase `error:`), distinct from the class-prefixed format used for `MoonlitError` subclasses.

### Examples

Build a single-package project, current directory:

```sh
moonlit build -e myapp.cli:main -o myapp.pyz
```

Build a workspace member, with a custom shebang:

```sh
moonlit build /path/to/workspace --package shouter \
    -e shouter.cli:main \
    -p '/usr/bin/env python3.13' \
    -o shouter.pyz
```

Resolve the entry point from a console script declared by the target's wheel:

```sh
moonlit build --package myapp -c myapp -o myapp.pyz
```

Overwrite an existing output file:

```sh
moonlit build -e myapp.cli:main -o myapp.pyz --force
```

Get a traceback when something goes wrong:

```sh
moonlit build -e myapp.cli:main -o myapp.pyz -v
```

Cross-compile a `.pyz` for Python 3.12 from a Python 3.13 dev box (uv auto-fetches a managed CPython 3.12 if needed):

```sh
moonlit build --python-version 3.12 -e myapp.cli:main -o myapp-py312.pyz
```

Produce a native Windows `.exe` pinned to Python 3.12 (shebang auto-pivots to `py -3.12`):

```sh
moonlit build --windows-exe --python-version 3.12 -e myapp.cli:main -o myapp.exe
```

Produce a fully self-contained **folder bundle** that ships a managed CPython next to a thin launcher `.exe` so recipients don't need Python installed:

```sh
moonlit build --bundle-python --python-version 3.12 \
    -e myapp.cli:main -o dist/myapp
```

The bundle directory layout:

```
dist/myapp/
├── myapp.exe       # the launcher (runs ./_python/python.exe ./myapp.pyz)
├── myapp.pyz       # the application zipapp
└── _python/        # bundled CPython tree (~30 MiB)
```

Distribute the folder (typically by zipping it) and run `myapp\myapp.exe`. The folder-bundle shape replaces the v0.3.0 single-`.exe` `--bundle-python` output, which tripped Windows Defender's ML heuristics for self-extracting archives. The new shape extracts nothing at runtime and so isn't flagged.

## `moonlit pack`

```
moonlit pack [SPEC] [--with SPEC ...] [-r FILE ...] -e <entry> | -c <script> -o <output> [flags]
```

Build a `.pyz`/`.exe`/folder-bundle directly from **PyPI requirement specs** — with **no local `pyproject.toml` or `uv.lock`**. This is the moonlit analogue of `uvx --with <extra> <tool>` or `shiv -e mod:fn <pkgs>`: hand it package names, get back a self-contained artifact a recipient can run with neither uv nor PyPI access. `moonlit build` stays the command for your own uv-managed project; `pack` is for bundling things that already live on an index.

`pack` resolves the full dependency closure at build time with `uv pip compile` — that resolution *is* the lock for the produced artifact (there is no `uv.lock` to freeze). It then installs the closure with `uv pip install --no-deps` and runs the same back half as `build` (entry-point resolution → `build_id` → `env.json` → archive). There is no `uv build` wheel step — every package, including the primary one, is just a resolved dependency.

`SPEC` is an optional positional — the *primary* package in PEP 508 form (`mooring`, `mooring==1.4`, `mooring[extra]>=1`). It seeds both the default name and the default entry point.

### Flags

| Short | Long | Type | Required | Default | Description |
|---|---|---|---|---|---|
|  | `--with` | `SPEC` (repeatable) | no | — | Additional package to bundle (mirrors `uvx --with`). May be repeated. |
| `-r` | `--with-requirements` | path (repeatable) | no | — | A requirements file whose pins are bundled. May be repeated. |
| `-e` | `--entry-point` | `module:callable` | one-of `{-e, -c}`* | — | Entry point baked into `env.json`. |
| `-c` | `--console-script` | string | one-of `{-e, -c}`* | — | Console-script name; resolved against staged `*.dist-info/entry_points.txt`. |
|  | `--name` | string | conditional | derived from `SPEC` | `env.json` name (drives the cache key). **Required** when no positional `SPEC` is given. Must be a valid PEP 508 name. |
| `-o` | `--output-file` | path | yes | — | Destination — same shapes as `build` (`.pyz`, `.exe` with `--windows-exe`, directory with `--bundle-python`). |
| `-p` | `--python` | string | no | `/usr/bin/env python3` | Shebang line; same semantics and `--windows-exe`/`--python-version` pivots as `build`. |
|  | `--python-version` | `<X.Y>` | no | build host's | Target Python `major.minor`; threaded into `uv pip compile --python-version` and `uv pip install --python`. |
|  | `--windows-exe` | flag | no | off | Native Windows `.exe` shape. Requires `-o` to end in `.exe` (unless `--bundle-python`). |
|  | `--bundle-python` | flag | no | off | Self-contained directory bundle shipping a managed CPython. `-o` is a directory; MUST NOT end in `.exe`/`.pyz`. |
|  | `--force` | flag | no | off | Overwrite an existing regular-file output (or a recognized bundle dir). |
|  | `--gc` / `--no-gc` | flag | no | `--gc` (on) | Bake runtime cache self-GC. Same as `build`. |
|  | `--gc-keep-latest` | int | no | `2` | Retention count (≥ 1). Same as `build`. |
|  | `--gc-grace` | duration | no | `24h` | Age grace. Same as `build`. |
| `-q` | `--quiet` | flag | no | off | Suppress progress on stderr. |
| `-v` | `--verbose` | flag | no | off | Echo `uv` invocations; tracebacks on error. |
| `-h` | `--help` | flag | no | — | Print help, exit 0. |

`*` Exactly one of `-e`/`-c` is required — **except** when a positional `SPEC` is present and you pass neither, in which case `pack` defaults to `-c <name-from-SPEC>` (resolve the console script named after the primary package, exactly as `uvx <tool>` would run it).

`pack` does **not** accept `--package`, `--dev`, or `--no-dev` — there is no workspace and no dependency groups.

### Flag interactions

- At least one of `SPEC`, `--with`, `--with-requirements` must be present → else exit 2.
- `--name` is required when there is no positional `SPEC` → else exit 2.
- Both `-e` and `-c` → exit 2; neither (with no `SPEC` to default from) → exit 2.
- `-q`+`-v`, the `--windows-exe`/`--bundle-python` suffix rules, and `--python-version` format are validated exactly as for `build`.

### Exit codes

Same enumeration as `build`, with one addition: a `uv pip compile` failure (e.g. an unsatisfiable resolution) is **exit 8** (`CompileError`) — the same resolution-failure code `build` uses for `ExportError`. The install (9), entry-point (6), output (7), and bundled-Python (13) codes are shared with `build` unchanged.

### Determinism note

Because there is no `uv.lock`, `pack` resolves fresh each time. Two packs of the same specs against a moved index may pick newer pins and therefore a different `build_id` (and a different cache key on recipients). If you need frozen, reproducible inputs, pin exact versions in your specs or pass a fully-pinned `--with-requirements` file.

### Examples

Bundle a PyPI tool plus an extra dependency (the `uvx --with polars mooring` case), then run it offline:

```sh
moonlit pack mooring --with polars -o mooring.pyz
python mooring.pyz        # runs mooring's console script, polars bundled in
```

Pick the entry point explicitly instead of the console-script default:

```sh
moonlit pack mooring --with polars -e mooring.cli:main -o mooring.pyz
```

Bundle straight from an existing requirements file (shiv-style):

```sh
moonlit pack --with-requirements requirements.txt --name mooring -c mooring -o mooring.pyz
```

Pin a version and cross-compile for Python 3.12:

```sh
moonlit pack "mooring==1.4" --python-version 3.12 -o mooring-py312.pyz
```

## `moonlit info`

```
moonlit info <pyz> [--json]
```

Print the `env.json` manifest of a moonlit-built archive. `<pyz>` must resolve to an existing regular file.

| Flag | Description |
|---|---|
| `--json` | Emit the raw `env.json` bytes to stdout with no header. Useful for piping to `jq`. |

**Default output**: a one-line header `<path> (<size>, <N> entries)` followed by a sorted listing of the manifest's required fields (`build_id`, `built_at`, `entry_point`, `moonlit_version`, `name`, `python_shebang`, `schema_version`). The optional `python_version` field is **not** included in the default listing today; use `--json` to see it.

If the input is not a zipfile, or `env.json` is missing/malformed, exit 12 with `BadArchiveError: <reason>` on stderr.

Examples:

```sh
moonlit info myapp.pyz
moonlit info myapp.pyz --json | jq .python_version
```

## `moonlit clean`

```
moonlit clean [flags]
```

Reap stale cache entries from the runtime cache root. The cache root is resolved the same way the bootstrap resolves it: `MOONLIT_ROOT` if set, otherwise `%LOCALAPPDATA%\moonlit\` on Windows or `~/.moonlit/` on POSIX.

Every `.pyz` extracts its bundled `site-packages/` to `<cache_root>/<normalized_name>_<build_id>/site-packages/` on first run. By default, artifacts also self-prune their **own** app's older entries automatically (keep newest N, default 2 — see [Runtime → Automatic cache cleanup](runtime.md#automatic-cache-cleanup) and `--gc`), so a single app's footprint is bounded without `moonlit clean`. `moonlit clean` remains the tool for **cross-app** reaping, whole-cache wipes, age- or name-filtered sweeps, and orphan cleanup — and for trimming caches left by artifacts that were built with `--no-gc`.

### Flags

| Flag | Type | Required | Description |
|---|---|---|---|
| `--all` | flag | one-of `{--all, --older-than, --keep-latest, --name}` | Match every well-formed cache entry. |
| `--older-than` | `<int><s\|m\|h\|d>` | conditional | Match entries whose `site-packages/` mtime is older than the given duration (e.g. `30m`, `7d`). Must be positive; compound forms like `1h30m` are not supported. |
| `--keep-latest` | int `>= 0` | conditional | Group entries by normalized name; keep the N newest per group, mark the rest deletable. `--keep-latest 0` deletes every matching entry. |
| `--name` | fnmatch glob | conditional | Match against the PEP-503 normalized name (the part before the trailing `_<64hex>`). Globs like `my*` or `myapp` are accepted. |
| `--force` | flag | no | Skip the try-lock liveness check. Useful when a stuck holder needs to be cleared. See the **Liveness model** note below. |
| `--dry-run` | flag | no | Print the action plan; do not modify the filesystem. |
| `--show-sizes` | flag | no | Compute per-entry sizes for `keep`/`skip` rows (off by default for speed). `delete` and `orphan` rows always show what was freed. |
| `-q`, `--quiet` | flag | no | Suppress the table; print only the trailer line on stdout. |
| `-v`, `--verbose` | flag | no | Show full 64-char `build_id` hex in the table. |

Bare `moonlit clean` (no `--all`, no `--older-than`, no `--keep-latest`, no `--name`) exits 2 with a usage message. There is no implicit "scan and report" default — that would tempt a "do nothing" mental model.

When more than one of `--all`, `--older-than`, `--keep-latest`, `--name` is set, the deletion set is the intersection. `--keep-latest` is applied last, after the other filters narrow the candidate set.

### Output

The action plan is rendered as a table on stderr with columns `ACTION`, `NAME`, `BUILD_ID`, `AGE`, `SIZE`, `PATH`. The trailer is one line on stdout:

```
deleted N entries, freed <bytes_humanized>
```

`--dry-run` swaps the trailer to `would delete N entries, would free …` and leaves the filesystem alone. `--quiet` suppresses the table but the trailer stays on stdout.

`ACTION` is one of:

- `delete` — the entry will be removed (or was removed, in a real run).
- `keep` — within a `--keep-latest` group, this is one of the N newest.
- `skip` — a candidate that could not be deleted (its `<cache_key>.lock` is held and `--force` was not set). The reason appears in parentheses after the path.
- `orphan` — a `.tmp.<pid>`/`.old.<pid>`/`.lock` sibling reaped because its owning cache_key is missing or being deleted.

### Liveness model

`moonlit clean` is **cooperative**. For each cache entry slated for deletion it tries to acquire `<cache_key>.lock` non-blocking. On success it holds the lock through the `rmtree`, so a concurrent extractor serializes against the deletion. On failure the entry is marked `skip (locked)`; the process exit code is 14 to signal partial completion.

The bootstrap's cache-hit fast path reads `site-packages/` *without* holding the lock. `moonlit clean` therefore cannot detect a process that is mid-import. **Do not run `moonlit clean` while a moonlit `.pyz` is actively in use.** This is documented in `specs/CROSS_CUTTING_DECISIONS.md` D23.

`--force` bypasses the try-lock entirely. The cache directory is deleted regardless of lock state; the lock file is left in place (a live holder still owns the byte range on Windows and we do not pull the rug). The next clean run will reap the lock file once nothing holds it.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All targeted entries deleted (or zero candidates matched). |
| 2 | Usage error — no filter, bad `--older-than` syntax, negative `--keep-latest`, etc. |
| 14 | `CleanRefusedError` — at least one entry was skipped because its lock was held and `--force` was not set. |
| 15 | `CleanIOError` — an I/O failure during deletion (`rmtree` raised). Partial progress is possible; the trailer reports what was actually freed. |

### Examples

Preview what `--all` would delete without touching the filesystem:

```sh
moonlit clean --all --dry-run
```

Reap cache entries older than 30 days:

```sh
moonlit clean --older-than 30d
```

Keep the 3 newest builds per app, delete the rest:

```sh
moonlit clean --name '*' --keep-latest 3
```

Delete only `myapp` cache entries:

```sh
moonlit clean --name myapp
```

Force-delete a stuck cache (e.g. after a debugger killed an extracting process and left a stale lock):

```sh
moonlit clean --all --force
```

## `python -m moonlit`

`python -m moonlit ...` is equivalent to `moonlit ...`; both delegate to the same `moonlit.cli.main()`. Useful when the `moonlit` console script isn't on `PATH` but `moonlit` is importable.
