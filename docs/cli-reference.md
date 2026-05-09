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

The MVP defines exactly one subcommand: `build`.

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
| `-o` | `--output-file` | path | yes | — | Destination `.pyz`. |
| `-p` | `--python` | string | no | `/usr/bin/env python3` | Shebang line baked into `env.json` and prefixed to the `.pyz`. ASCII only, no `\n`/`\r`/`\x00`, ≤127 bytes. |
|  | `--package` | string | iff workspace | — | Workspace member to build; required iff `[tool.uv.workspace]` is present, forbidden otherwise. PEP 503 normalized on both sides. |
|  | `--no-dev` | flag | no | (default) | Exclude dev-group dependencies (default behavior). |
|  | `--dev` | flag | no | off | Opt in to dev-group dependencies. Mutually exclusive with `--no-dev`. |
|  | `--force` | flag | no | off | Overwrite an existing regular-file output. Does **not** override a directory target. |
| `-q` | `--quiet` | flag | no | off | Suppress progress output on stderr; the success line on stdout is preserved. |
| `-v` | `--verbose` | flag | no | off | On error, append the full traceback to stderr. Mutually exclusive with `--quiet`. |
| `-h` | `--help` | flag | no | — | Print this help to stdout, exit 0. Short-circuits all other validation. |

### Flag interactions

- Exactly one of `-e` / `-c` is required. Neither or both → exit 2.
- `-q` and `-v` are mutually exclusive → exit 2.
- `--no-dev` and `--dev` are mutually exclusive → exit 2.
- `--package` is required iff the project is a uv workspace → exit 5 on mismatch.
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
| 130 | SIGINT (Ctrl-C) | — |

Build-time and runtime exit codes are independent enumerations; the runtime codes (0–3) live in [Runtime](runtime.md). The same numeric value can mean different things in the two namespaces.

### stdout / stderr semantics

- **Default**: progress lines go to stderr; the final line `wrote <output> (<size>, <N> entries)` goes to stdout.
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

## `python -m moonlit`

`python -m moonlit ...` is equivalent to `moonlit ...`; both delegate to the same `moonlit.cli.main()`. Useful when the `moonlit` console script isn't on `PATH` but `moonlit` is importable.
