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

Subcommands: [`build`](#moonlit-build) (produce a `.pyz`/`.exe`) and [`info`](#moonlit-info) (inspect a built archive's `env.json`).

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

## `python -m moonlit`

`python -m moonlit ...` is equivalent to `moonlit ...`; both delegate to the same `moonlit.cli.main()`. Useful when the `moonlit` console script isn't on `PATH` but `moonlit` is importable.
