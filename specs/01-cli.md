# CLI Specification

Status: experimental (0.x). This document is the contract for the `moonlit` build-time CLI surface. Runtime (bootstrap) exit codes live in `specs/03-bootstrap-runtime.md` and are an INDEPENDENT enumeration (D3).

## 1. Glossary

- **staging**: the directory tree built under `<tempdir>/staging/` during a build; its `site-packages/` subtree is what gets bundled.
- **site-packages**: the import target, both inside staging and after extraction at runtime.
- **console-script**: a name registered under `[project.scripts]` (or `[console_scripts]` in `entry_points.txt` of a wheel's dist-info).
- **workspace member**: a package whose path is matched by `[tool.uv.workspace].members` in the project root's `pyproject.toml`.

## 2. Command surface

### 2.1 Top-level

```
moonlit [--version | -V] [--help | -h] <subcommand> [args...]
```

- `--version` / `-V` → `moonlit <semver>\n` to stdout, exit 0. Extra positional args after `--version` are ignored. (`-V` is conventional; `-v` is reserved for `--verbose`.)
- `--help` / `-h` (no subcommand) → top-level help to stdout, exit 0.
- No subcommand and no flag → top-level help to stderr, exit 2.
- Unknown subcommand (with or without `--help`) → `error: no such subcommand: <name>` on stderr, exit 2. (Deliberate: `--help` does not redeem an unknown subcommand; that would mask typos.)

v0.1 defined exactly one subcommand: `build`. v0.2 adds `info`.

### 2.2 `moonlit build`

```
moonlit build [PROJECT] [flags]
```

`PROJECT` is an optional positional; default is `os.getcwd()`. Resolved with `Path(PROJECT).resolve(strict=False)` (handles `..`, trailing slash, symlinks, UNC paths). If the resolved path does not exist or is not a directory → exit 2.

| Short | Long | Type | Default | Required | Description |
|-------|------|------|---------|----------|-------------|
| `-e` | `--entry-point` | `module:callable` | none | one-of {-e, -c} | Entry point baked into `env.json`. |
| `-c` | `--console-script` | string | none | one-of {-e, -c} | Console-script name; resolved against staged `*.dist-info/entry_points.txt` after step 6. |
| `-o` | `--output-file` | path | none | yes | Destination `.pyz`. Resolved against cwd via `Path.resolve(strict=False)`. |
| `-p` | `--python` | string | `/usr/bin/env python3` | no | Shebang line baked into `env.json` and prefixed to the `.pyz`. Same default on Windows (harmless; ignored by the OS). |
|       | `--package` | string | none | conditional | Workspace member to build. Required iff `[tool.uv.workspace]` exists in PROJECT's `pyproject.toml`; forbidden otherwise. Matched per D12 (PEP-503 normalized on both sides). |
|       | `--no-dev` | flag | true (default behavior) | no | Asserts the default: dev-group deps are EXCLUDED from the build. Passing `--no-dev` is idempotent. |
|       | `--dev` | flag | false | no | Opt in to dev-group deps. Mutually exclusive with `--no-dev`; passing both → exit 2. |
|       | `--windows-exe` | flag | false | no | Produce a native Windows `.exe` (launcher + zipapp) instead of a `.pyz`. Requires `-o` to end in `.exe`; defaults `--python` to `python.exe` (or `py -<X.Y>` when `--python-version` is also set, see D20) if not explicitly set. See D19. |
|       | `--python-version` | string | none (build host's `sys.version_info`) | no | Target Python `major.minor` for cross-interpreter builds (e.g. `3.12`). Threaded through every `uv` invocation as `--python-version` so wheels are tagged for that ABI; stamped into `env.json` as `python_version` for the runtime mismatch check. Format: `^\d+\.\d+$`. See D20. |
|       | `--force` | flag | false | no | If `<output>` exists and is a regular file, overwrite it. Has no effect when the path does not exist. Does NOT cover non-file targets (see Section 5). |
| `-q` | `--quiet` | flag | false | no | Suppress non-error stderr. |
| `-v` | `--verbose` | flag | false | no | Echo `uv` invocations as `+ uv <argv>` on stderr (POSIX `shlex.quote` style on all platforms, for copy-paste consistency); show tracebacks on errors. |
|       | `--help` / `-h` | flag | — | no | Print build help to stdout, exit 0. Short-circuits all validation (D3 codes 2-11 are not raised). |

Unknown flags exit 2 with the literal text `error: no such option: <flag>` on stderr.

### 2.3 `moonlit info`

```
moonlit info <PYZ> [--json] [--help|-h]
```

Read-only inspection of a moonlit-built `.pyz`. Prints the contents of `env.json` (the archive's manifest, see `specs/05-env-json-schema.md`) plus a summary line of file size and zip-entry count.

`PYZ` is required; resolved via `Path(PYZ).resolve(strict=False)`.

| Short | Long | Type | Default | Required | Description |
|-------|------|------|---------|----------|-------------|
|       | `--json` | flag | false | no | Emit the raw `env.json` bytes from the archive on stdout, with no header line. Validation still runs first; a malformed `env.json` exits 12 even with `--json`. |
|       | `--help` / `-h` | flag | — | no | Print info help to stdout, exit 0. |

**Validation order** (first failure short-circuits):

1. `PYZ` resolves to an existing path → exit 2 if not.
2. `PYZ` is a regular file (not a directory, FIFO, device, dangling symlink) → exit 2 if not.
3. `PYZ` is a zipfile (`zipfile.is_zipfile`) → exit 12 (`BadArchiveError`) if not.
4. `env.json` member exists in the archive and validates per D8 (`specs/05-env-json-schema.md` §4) → exit 12 if any step fails. The validator's specific message ("env.json: ...") is appended to the BadArchiveError message so users see *why* the archive is malformed.

**Default output (stdout)**:

```
<resolved_PYZ_path> (<bytes_humanized>, <N> entries)
  build_id         <build_id>
  built_at         <built_at>
  entry_point      <entry_point>
  moonlit_version  <moonlit_version>
  name             <name>
  python_shebang   <python_shebang>
  schema_version   <schema_version>
```

The header-line format mirrors the `build` success line (Section 8). Fields are listed alphabetically for byte-stable test assertions.

**`--json` output (stdout)**: the raw bytes of the archive's `env.json` member written to `sys.stdout.buffer` (no decoding, no re-formatting). The build pipeline emits a UTF-8 `\n`-terminated payload (spec 05 §5), so this is a complete JSON document.

**Stability**: the existence of the `info` subcommand is stable from 0.2 onward. The default-mode header and field-listing format MAY change in 0.x; `--json` output (the raw `env.json` bytes) is byte-stable and pinned to spec 05.

## 3. Flag interaction rules

1. Exactly one of `-e`, `-c` is required. Neither or both → exit 2.
2. `-q` and `-v` are mutually exclusive. Both (including the combined form `-qv`) → exit 2.
3. `--no-dev` and `--dev` are mutually exclusive. Both → exit 2.
4. `--package` requirement is determined by parsing PROJECT's `pyproject.toml`; presence/absence of `[tool.uv.workspace]` is authoritative. Mismatch → exit 5.
5. `--windows-exe` requires `--output-file` to end in `.exe` (D19b). `moonlit build --windows-exe -o app.pyz` → exit 2. The check is `output_file.lower().endswith(".exe")` — the case-insensitive form covers user paths like `App.EXE` on Windows file systems.
6. Environment variables prefixed `MOONLIT_` (D16) are RUNTIME-only. They are read by the bootstrap, not by `moonlit build`. Setting any of them while invoking the build is silently ignored.

## 4. Order of preflight checks

The CLI performs these in order; the first failure short-circuits with the listed exit code. This order is part of the contract so that tests can assert which error wins on multi-fault inputs.

1. argparse/Click parsing (unknown flag, missing `-o`, both/neither of `-e`/`-c`, `--quiet`+`--verbose`, `--no-dev`+`--dev`) → exit 2.
2. PROJECT resolves to an existing directory → exit 2 if not.
3. `uv` is on `PATH` (`shutil.which("uv")`) → exit 3.
4. `<PROJECT>/pyproject.toml` exists and parses → exit 5 (`MalformedPyprojectError`) on parse failure.
5. `<PROJECT>/uv.lock` exists → exit 4.
6. Workspace shape vs `--package`: workspace+no-`--package`, no-workspace+`--package`, `--package` value not a member → exit 5.
7. `--entry-point` syntactic validity (exactly one `:`, both sides non-empty, both sides match `^[A-Za-z_][A-Za-z0-9_.]*$`; segments `pkg:`, `:main`, `pkg:a:b`, empty → exit 6).
8. Output-path preflight (Section 5).
9. Build pipeline; resolution-time errors (`-c` not found, uv subprocess failures, wheel artifact issues) → exits 6/8/9/10.

## 5. Output-path preflight

Let `O = Path(--output-file).resolve(strict=False)`.

1. If `O.parent` does not exist or is not a directory → `OutputNotWritableError` ("output parent directory does not exist: <O.parent>"), exit 7.
2. If `O.parent` is not writable (`os.access(O.parent, os.W_OK)` is false) → `OutputNotWritableError` ("output parent directory not writable: <O.parent>"), exit 7.
3. If `O` exists and is a directory, FIFO, socket, block/char device, or symlink whose target is not a regular file → `OutputNotWritableError` ("output path is not a regular file: <O>"), exit 7. `--force` does NOT override this.
4. If `O` exists, is a regular file (or symlink to one), and `--force` is unset → `OutputExistsError` ("output already exists; pass --force to overwrite: <O>"), exit 7.
5. Otherwise: proceed. `/dev/null` and similar character devices are rejected by step 3.

A `.pyz` currently locked by another process on Windows is detected at write time, not preflight; it surfaces during the atomic-replace step (D15) as `OutputNotWritableError`, exit 7.

## 6. Exit codes (build-time)

This table mirrors D3 exactly. Runtime exit codes are independent (see `specs/03-bootstrap-runtime.md`).

| Code | Meaning | Error classes |
|------|---------|---------------|
| 0 | Success | — |
| 1 | Unhandled Python exception (moonlit bug; not a stable contract) | — |
| 2 | CLI usage error (parser-level) | — |
| 3 | uv binary not on PATH | `UvNotFoundError` |
| 4 | `uv.lock` missing | `NoLockfileError` |
| 5 | Workspace shape mismatch / pyproject malformed | `NotAWorkspaceError`, `UnknownPackageError`, `MissingPackageError`, `MalformedPyprojectError` |
| 6 | Entry-point resolution failed | `BadEntryPointError`, `ConsoleScriptNotFoundError` |
| 7 | Output path issue | `OutputExistsError`, `OutputNotWritableError` |
| 8 | `uv export` failure | `ExportError` |
| 9 | `uv pip install --target` failure | `StagingError` |
| 10 | `uv build` wheel failure or wheel artifact issue | `WheelArtifactError` |
| 11 | Internal invariant violation | `InternalError` |
| 12 | Input archive is not a moonlit zipapp (used by `info`) | `BadArchiveError` |
| 130 | SIGINT | — |

`--version` failures (e.g. unreadable package metadata) are exit 1 (unhandled), not exit 0.

## 7. Error message contract

The contract is: error class name + `: ` + a message, single line, on stderr. The class name and the colon-prefix are stable. The human-readable suffix is NOT stable across versions.

Specific message requirements:

- `ConsoleScriptNotFoundError`: message MUST list the console scripts discovered across all staged `*.dist-info/entry_points.txt` files, sorted, deduplicated, comma-separated, suffixed with the hint `; pass --entry-point <module>:<callable> instead`. If a script name occurs in multiple dist-infos, the build fails with this same error class, listing all dist-infos that defined the name.
- `UnknownPackageError`: message lists the workspace member names as authored (raw, un-normalized) for human readability, sorted ascending.
- `OutputExistsError` vs `OutputNotWritableError`: distinct classes (both exit 7) with distinct messages per Section 5.

## 8. stdout / stderr semantics

- Default mode: progress lines on stderr; final line `wrote <output> (<bytes_humanized>, <N> entries)` on stdout, where `<bytes_humanized>` is the file size formatted with binary units (e.g. `1.4 MiB`, `812 KiB`, `512 B`) and `<N>` is the count of zip entries (all of them, including `_bootstrap/`, `__main__.py`, and `env.json`). Format is pinned and tested.
- `--quiet`: stderr is suppressed; the stdout success line is preserved.
- `--verbose`: each `uv` invocation echoed as `+ uv <argv>` on stderr (POSIX-shlex-quoted on all platforms); on error, full traceback follows the error line on stderr.
- Errors: `<ErrorClassName>: <message>\n` on stderr. With `--quiet`, errors are still emitted.

## 9. Signal handling

The CLI installs a SIGINT handler (D18) that:

1. Cleans up the active build's tempdir (D17).
2. Unlinks any partial `<output>.pyz.tmp.<pid>` (D15).
3. Exits 130 with no traceback (even under `--verbose`).

Known leak: a SIGKILL (or a SIGINT delivered between `os.chmod` and process exit on POSIX) may leave a `.pyz` with non-executable mode at the output path. Documented; not a contract violation.

## 10. Argument-parsing precedence

CLI flags > env vars > defaults. MVP defines no env vars that map to CLI flags; the `MOONLIT_*` vars (D16) are runtime-only and not consulted at build time.

## 11. Invariants and falsifiers

Every invariant has a CLI-observable falsifier (no need to inspect the produced `.pyz`).

- **I1: Exactly-one of `-e`/`-c`.** Falsifier: `moonlit build -o x.pyz` → exit 2; `moonlit build -e a:b -c c -o x.pyz` → exit 2.
- **I2: Preflight order.** Falsifier: invoke with simultaneous faults (e.g. missing `uv` AND missing `uv.lock`); observe exit 3, not 4.
- **I3: `OutputExistsError` vs `OutputNotWritableError` distinction.** Falsifier: target a path under a non-existent parent dir → stderr starts with `OutputNotWritableError:`; target an existing regular file without `--force` → stderr starts with `OutputExistsError:`. Both exit 7.
- **I4: `--force` does not override directory targets.** Falsifier: `mkdir x.pyz; moonlit build -e a:b -o x.pyz --force` → exit 7, `OutputNotWritableError`.
- **I5: `--package` case-insensitivity (D12).** Falsifier: workspace with member `My_Pkg`; `--package my-pkg` succeeds; `--package no-such` → exit 5 with raw names listed.
- **I6: MOONLIT_* ignored at build time (D16).** Falsifier: `MOONLIT_ENTRY_POINT=foo:bar moonlit build -e real.mod:main -o x.pyz` succeeds; running the resulting `.pyz` with `MOONLIT_ENTRY_POINT` unset invokes `real.mod:main` (verified via that script's stdout), proving the build-time env var was not persisted.
- **I7: Atomic output (D15).** Falsifier: SIGINT during build leaves no `<output>.pyz` and no `<output>.pyz.tmp.*` siblings (`ls` after the run shows neither).
- **I8: Final-line format.** Falsifier: parse stdout against `^wrote .+ \(\d+(\.\d+)? (B|KiB|MiB|GiB), \d+ entries\)$`.
- **I9: Help short-circuits validation.** Falsifier: `moonlit build --help` in a directory with no `pyproject.toml` → exit 0.
- **I10: Unknown subcommand + `--help` still errors.** Falsifier: `moonlit nope --help` → exit 2.
- **I11: `--windows-exe` zip-body parity (D19).** A `.pyz` and the `.exe` produced from the same project + flags contain the same set of zip entries with the same per-entry content bytes. Falsifier: build twice, open each as a `zipfile.ZipFile`, compare `namelist()` and `read(name)` for each entry — must match. (Byte-identical zip bodies are NOT contracted today: the standard library's `zipfile` embeds wall-clock mtimes. When `--reproducible` lands, I11 can tighten to byte-identity.)
- **I12: `--windows-exe` suffix rule (D19b).** Falsifier: `moonlit build --windows-exe -e a:b -o app.pyz` → exit 2 with a usage message naming `.exe`.

## 12. Stability

Pre-1.0 (0.x):

- **Stable across 0.x**: exit codes 0, 2, 130; the error-class-name + colon-prefix shape on stderr; `--help`/`--version` on stdout; the existence of flags `-e`, `-c`, `-o`, `--package`.
- **May change in 0.x**: exit codes 3-11, full error-message text, flag defaults, `--verbose` echo format, the final-line bytes-humanization rounding, the `--no-dev`/`--dev` default.
- **Frozen at 1.0**: the full table above.
