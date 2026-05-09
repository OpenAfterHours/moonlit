# Build Pipeline Specification

Status: MVP. Normative for `moonlit.builder.build(BuildConfig)`. Build-time process; the runtime bootstrap is independent and out of scope here.

## 1. Inputs

`BuildConfig` (frozen dataclass): `project_root: Path`, `output_path: Path`, `entry_point: str | None`, `console_script: str | None`, `python_shebang: str`, `package: str | None`, `force: bool`, `verbosity: int`. Exactly one of `entry_point`/`console_script` is set (CLI enforces, exit 2 otherwise).

`python_shebang` validation (rejected as exit 2 CLI usage error): ASCII only, no `\n`/`\r`/`\x00`, encoded length ≤ 127 bytes.

## 2. Outputs and side effects

On success: exactly one new file at `output_path`, a PEP 441 zipapp prefixed with `b"#!" + python_shebang + b"\n"`. uv may populate its own download cache as a transparent side effect; that is not counted as a moonlit output. No mutation of `pyproject.toml`, `uv.lock`, project `.venv`, or parent-process env vars. The single tempdir (Section 7) is removed in `finally` on every exit path including SIGINT.

## 3. The 10-step sequence

All `subprocess.run` invocations use `shell=False, check=False, capture_output=True, env=os.environ.copy()`. Argv is constructed only inside `resolver.{export, pip_install_target, build_wheel}`; `builder` never calls `subprocess` directly. `cwd` is stated per step.

**Step 1 — Workspace detection.** `workspace.detect(project_root)` parses `pyproject.toml` with `tomllib`, expands `[tool.uv.workspace].members` globs, applies `exclude`. Returns `Workspace(root, members)` or `None`. Validates `--package`: required iff workspace, forbidden otherwise. `--package` matching uses PEP 503 normalization on both sides per D5/D12. Failures: `NotAWorkspaceError`/`UnknownPackageError`/`MissingPackageError`/`MalformedPyprojectError` → exit 5.

**Step 2 — Target selection.** Workspace + `--package <name>` → matched member directory and raw name. Else → `project_root` and raw `[project].name`. The raw name is later written to `env.json.name` (D5).

**Step 3 — `uv export`** (`resolver.export`, `cwd = project_root`):
```
uv export --frozen --no-dev --no-emit-workspace --format requirements-txt [--package <name>] --output-file <tmp>/requirements.txt
```
`FileNotFoundError` for the `uv` binary → `UvNotFoundError` (3). Exit code non-zero with stderr matching `re.search(r"uv\.lock.*not found|no .*lockfile", stderr, re.IGNORECASE)` → `NoLockfileError` (4). Drift (`re.search(r"out.of.date|frozen", stderr, re.IGNORECASE)`) → `ExportError` (8) with message `"uv.lock is out of date with pyproject.toml; run \`uv lock\` and retry."`. Any other non-zero → `ExportError` (8) with prefixed stderr.

**Step 4 — Stage transitive deps** (`resolver.pip_install_target`, `cwd = project_root`):
```
uv pip install --target <staging>/site-packages --no-deps --requirement <tmp>/requirements.txt --python <sys.executable>
```
Non-zero → `StagingError` (9).

**Step 5 — Build wheel(s)** (`resolver.build_wheel`, `cwd = project_root`). Per D2:

If workspace:
```
uv build --all-packages --wheel --out-dir <tmp>/dist
```
Else:
```
uv build --wheel --out-dir <tmp>/dist
```
Non-zero exit → `WheelArtifactError` (10). After success, `wheels = sorted((<tmp>/dist).glob("*.whl"))` (lexicographic on POSIX path; sidecar `*.whl.metadata` files are excluded by the strict `*.whl` glob). Workspaces: `len(wheels) >= 1`. Non-workspaces: `len(wheels) == 1`. The non-workspace single wheel must have `metadata.name` PEP-503-equal to the target name; otherwise `WheelArtifactError` (10).

**Step 6 — Install every wheel into staging** (`resolver.pip_install_target`, `cwd = project_root`). Per D2, loop:
```
for wheel in wheels:
    uv pip install --target <staging>/site-packages --no-deps --python <sys.executable> <wheel>
```
No `--reinstall-package`; no `--reinstall`. Each non-zero exit → `StagingError` (9). Ordering is the lexicographic `wheels` list above; later wheels overwrite earlier ones. For workspaces, this is the mechanism by which transitive workspace deps (e.g. `greeter` for `shouter`) reach the staging tree.

**Step 7 — Resolve console script** (only if `console_script` is set; `cwd = project_root`). Glob `sorted((<staging>/site-packages).glob("*.dist-info/entry_points.txt"))`. For each, `configparser.ConfigParser(strict=False)`, read `[console_scripts]`, look up `console_script`. Collect every match across dist-infos.
- Zero matches → `ConsoleScriptNotFoundError` (6). Message lists every `[console_scripts]` key discovered across dist-infos; if all are empty, hint `"use --entry-point pkg.module:callable"`.
- Two or more matches → `ConsoleScriptNotFoundError` (6) with text `f"ambiguous console script '{name}'; declared in {files}"`.
- One match → split on `:` to produce `entry_point = "module:attr"`. Malformed value → `BadEntryPointError` (6).

**Step 8 — Build ID.** `compute_build_id(<staging>/site-packages)` (Section 4). Files outside `<staging>/site-packages/` (e.g. `<staging>/bin/` console-script wrappers) are not bundled in MVP and not hashed.

**Step 9 — Archive assembly** (`builder.create_archive`). Output shape dispatches on `BuildConfig.windows_exe` per D19; the zip body (steps 9.5-9.8 below) is byte-identical between modes.
1. Pre-flight on `output_path`: parent directory must exist and be writable, else `OutputNotWritableError` (7); if path exists and is a directory or symlink → `OutputExistsError` (7); if path exists as a regular file and `not force` → `OutputExistsError` (7).
2. `tmp_out = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")` (D15). Open `tmp_out` for binary write.
3. **Prefix.** If `windows_exe` is set, write the launcher bytes for the host architecture (D19a) followed by `b"#!" + python_shebang.encode("ascii") + b"\n"`. Otherwise, write only the shebang line. Either way, the file pointer now sits exactly where the zip body begins.
4. Open `zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED)` over the same fp.
5. Walk `<staging>/site-packages/` via `rglob("*")` filtering `is_file()`. For each file, `relpath = p.relative_to(<staging>/site-packages)` and write with `arcname = "site-packages/" + relpath.as_posix()` (D1). On POSIX, if the source file mode has `stat.S_IXUSR | S_IXGRP | S_IXOTH` set, set `ZipInfo.external_attr = (0o755 << 16)`; else default. Arcnames are guaranteed to be relative POSIX paths with no `..` segments by construction (rglob within staging) — zip-slip is impossible.
6. Copy the `_bootstrap/` package via `importlib.resources.files("moonlit") / "_bootstrap"`, recursively writing each file as `_bootstrap/<relpath>.as_posix()`.
7. Render `__main__.py` from `_templates/main_py.tmpl` with LF line endings on all platforms; write at arcname `__main__.py`.
8. Write `env.json` at arcname `env.json` (UTF-8, no BOM).
9. `fp.flush(); os.fsync(fp.fileno()); fp.close()`.

**Step 10 — Finalize.** `os.replace(tmp_out, output_path)` (D15, atomic on POSIX and Windows). On POSIX, then `os.chmod(output_path, 0o755)` UNLESS `windows_exe` is set (D19d) — a `.exe` does not need POSIX exec bits. On Windows, no-op regardless. Tempdir from D17 is removed in the `finally` of `builder.build`. On any failure between Step 9.2 and Step 10, `finally` unlinks `tmp_out` if it exists; `output_path` is never partially written.

## 4. Build-id computation

`hashing.compute_build_id(site_packages_root)` excludes any path containing a `__pycache__` segment and any path with extension `.pyc` (D6).

```python
def compute_build_id(site_packages_root: Path) -> str:
    h = hashlib.sha256()
    files: list[str] = []
    for p in site_packages_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(site_packages_root)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        if rel.suffix == ".pyc":
            continue
        files.append(rel.as_posix())
    for relpath in sorted(files):
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        h.update((site_packages_root / relpath).read_bytes())
        h.update(b"\0")
    return h.hexdigest()
```

Returns 64 lowercase hex. Forward slashes regardless of platform. `env.json` is written *after* this step, so its bytes never feed the hash.

## 5. Determinism

Two builds with the same uv version, Python interpreter (major.minor.patch), `uv.lock`, `pyproject.toml`, and moonlit version produce the same `build_id`. The `__pycache__`/`.pyc` exclusion is the falsifier — without it, transient bytecode caches break the invariant. Byte-identical `.pyz` files are *not* guaranteed in MVP (zip mtimes vary); reproducible `.pyz` is v0.2.

## 6. Error handling

`MoonlitError` subclasses each carry a stable `exit_code`. Top-level CLI catches `MoonlitError`, prints the message, exits with that code. Code 1 is reserved for unhandled Python exceptions only; no user-facing error class maps to 1.

| Trigger | Class | Exit |
|---|---|---|
| `uv` binary not on PATH | `UvNotFoundError` | 3 |
| `uv.lock` missing or `--frozen` rejects | `NoLockfileError` | 4 |
| Workspace shape (not workspace / unknown / missing / malformed) | per workspace spec | 5 |
| Bad entry-point string | `BadEntryPointError` | 6 |
| Console script absent or ambiguous | `ConsoleScriptNotFoundError` | 6 |
| Output exists / parent missing / not a regular file | `OutputExistsError`, `OutputNotWritableError` | 7 |
| `uv export` non-zero (other than NoLockfile) | `ExportError` | 8 |
| `uv pip install --target` non-zero (Step 4 or any Step 6 wheel) | `StagingError` | 9 |
| `uv build` non-zero, zero wheels, or metadata mismatch | `WheelArtifactError` | 10 |
| Internal invariant violation | `InternalError` | 11 |
| SIGINT | — | 130 |

Internal asserts (e.g. "wheels list non-empty after build succeeded") raise `InternalError` (11) and are not user-recoverable; they indicate a moonlit bug.

## 7. Tempdir lifecycle

Per D17, exactly one tempdir per build, created via `tempfile.mkdtemp(prefix="moonlit-build-")`. Layout:

```
<tempdir>/
  requirements.txt        # Step 3 output
  staging/                # Step 4 + 6 install target
    site-packages/
  dist/                   # Step 5 output
    *.whl
```

Removed via `shutil.rmtree(<tempdir>, ignore_errors=False)` in the outermost `finally` of `builder.build`. On SIGINT (D18), the CLI handler also unlinks any `<output_path>.tmp.<pid>` file before exiting 130.

## 8. Concurrency

Two concurrent `moonlit build` invocations against the same project are not supported in MVP. Tempdirs are per-pid (no collision), but uv's own cache is shared and may serialize internally. The `.tmp.<pid>` output suffix prevents two builds from clobbering each other's partial output, but the final `os.replace` is last-writer-wins.

## 9. Cross-platform

Shebang and rendered `__main__.py` use LF on all platforms. POSIX exec-bit propagation is applied per Step 9.5. On Windows the shebang is harmless metadata; the `.pyz` is run via `python app.pyz` or `py app.pyz`. For drop-in `.exe` shipping, build with `--windows-exe` (D19) — the produced file prepends a small native launcher to the same zip body so it runs without an explicit `python` prefix. Cache root semantics belong to the bootstrap spec.

## 10. Edge cases (with test IDs)

- Workspace root that is itself a `[project]` member — `tests/e2e/test_workspace_root_is_member.py::test_root_member_build`.
- Zero third-party deps (empty `requirements.txt`) — `tests/unit/test_builder_no_deps.py::test_empty_requirements_ok`.
- `uv.lock` out of date — `tests/unit/test_resolver_export.py::test_drift_maps_to_export_error`.
- Platform-tagged native wheel — `tests/e2e/test_native_wheel.py::test_cp313_wheel_bundled`.
- `.pth` file in a staged dep — `tests/e2e/test_pth_file.py::test_addsitedir_handles_pth`.
- `output_path` parent missing — `tests/unit/test_builder_preflight.py::test_parent_missing_exits_7`.
- `output_path` is a directory or symlink — `tests/unit/test_builder_preflight.py::test_dir_or_symlink_exits_7`.
- KeyboardInterrupt mid-Step-6 — `tests/unit/test_builder_sigint.py::test_sigint_cleans_tempdir_and_tmp`.
- Empty `entry_points.txt` with `-c` — `tests/unit/test_console_script_resolution.py::test_empty_lists_hint`.
- Ambiguous console script across two dist-infos — `tests/unit/test_console_script_resolution.py::test_ambiguous_lists_files`.
- Non-ASCII / overlong shebang — `tests/unit/test_shebang_validation.py::test_rejects_non_ascii_and_long`.
- Workspace with `shouter→greeter` transitive dep — `tests/e2e/test_workspace_transitive.py::test_shouter_imports_greeter` (canonical demo; verifies D2).
