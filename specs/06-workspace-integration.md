# Workspace Integration Specification

## 1. Scope

Defines how moonlit interprets `[tool.uv.workspace]` in a project's `pyproject.toml`, how it resolves the `--package` flag to a single build target, and how it pre-validates workspace shape before invoking any `uv` subprocess. This spec covers the WHY of workspace handling and the constraints workspace handling imposes on the build pipeline; it does not cover the HOW of subprocess invocation, wheel installation, or staging — those live in `specs/02-build-pipeline.md`. Workspace handling is read-only with respect to the project tree: detection performs no filesystem writes inside `project_root`.

## 2. Definitions

- **Workspace**: a `pyproject.toml` containing a `[tool.uv.workspace]` table with `members` and/or `exclude` keys.
- **Member**: a directory that (a) lies under `project_root`, (b) contains a `pyproject.toml` with a `[project].name`, (c) is matched by a `members` glob, and (d) is not matched by `exclude`.
- **Workspace root member**: `project_root` itself, when it has a top-level `[project]` table. Subject to `exclude` but not to the `members` glob.
- **Target**: the single member chosen for the build, identified by `--package`.

## 3. Detection algorithm

`workspace.detect(project_root: Path) -> Workspace | None`:

1. Read `project_root / "pyproject.toml"` as bytes; parse with `tomllib.loads`. Decode failure or `tomllib.TOMLDecodeError` → `MalformedPyprojectError` (exit 5).
2. If `tool.uv.workspace` is absent, return `None`.
3. Read `members` and `exclude` as `list[str]`. Any non-list, or any non-string element, → `MalformedPyprojectError` (exit 5).
4. Resolve each `exclude` glob relative to `project_root` and collect results into a `set[Path]` of resolved absolute paths (`Path.resolve()`; symlinks and Windows junctions resolved).
5. For each `members` pattern, `project_root.glob(pattern)`. For each match `m`:
   1. If `m` is not a directory → skip.
   2. If `m.resolve()` is not under `project_root.resolve()` → `MalformedPyprojectError` (exit 5). Members outside `project_root` are not supported in MVP.
   3. If `m / "pyproject.toml"` does not exist → skip.
   4. Parse the member `pyproject.toml`; failure → `MalformedPyprojectError` (exit 5).
   5. If `[project].name` is missing or empty → skip silently (matches uv behavior; emit a `--verbose` log line listing the skip).
   6. If `m.resolve()` is in the exclude set → skip.
   7. Otherwise, record `(name, m.resolve())`.
6. If `project_root` has a `[project].name` and `project_root.resolve()` is not in the exclude set, add it as a member. The workspace root is not subject to `members` globs.
7. PEP-503-normalize each recorded name (D5: `re.sub(r"[-_.]+", "-", name).lower()`). If any normalized name appears more than once, raise `MalformedPyprojectError` (exit 5) with message `workspace has duplicate package names: <name>` listing the raw, un-normalized names. This pre-validates before any uv subprocess.
8. Return `Workspace(root=project_root, members={raw_name: directory, ...})`. The mapping uses raw names as keys; matching against `--package` normalizes both sides at lookup time.

Detection is invoked exactly once per build, before any uv subprocess.

## 4. --package flag rules

- **Required iff workspace.** Workspace detected and `--package` not supplied → `MissingPackageError` (exit 5).
- **Forbidden iff non-workspace.** Detection returned `None` and `--package` was supplied → `NotAWorkspaceError` (exit 5).
- **Matching is PEP-503 normalized on both sides** (D12). Normalize the user-supplied value and each member key with the D5 algorithm and compare. So `--package my-pkg` matches a member named `My_Pkg`. No match → `UnknownPackageError` (exit 5); the error message lists the raw (un-normalized) member names for human readability.

## 5. Target resolution

- Workspace + `--package foo` (after normalization match) → `Target(name=raw_member_name, directory=members[raw_member_name])`. The target's `name` is the raw `[project].name` of the matched member (used for env.json `name` and downstream CLI argument `--package <raw_name>`).
- Non-workspace + no `--package` → `Target(name=[project].name, directory=project_root)`.

## 6. Intra-workspace dependencies

moonlit does not parse, walk, or rewrite the workspace dependency graph. The build pipeline is required to:

1. Run `uv export --frozen --no-dev --package <target> --no-emit-workspace --format requirements-txt` for the third-party dependency closure.
2. Run `uv build --all-packages --wheel --out-dir <tmp>/dist` (workspace case) or `uv build --wheel --out-dir <tmp>/dist` (non-workspace case).
3. Install every produced wheel into staging, sorted by filename, each with `--no-deps`.

`--no-emit-workspace` strips ALL workspace member self-references from the exported requirements file, including the target's own `-e file://` line. This is fine and intentional: every workspace member's wheel is built explicitly via `--all-packages` and installed in step 3, so the staging tree contains exactly the target and its workspace siblings as freshly built wheels, plus the third-party closure from step 1.

The `--all-packages` choice overbuilds: wheels are produced for every member, even members not in the target's import closure. This is a known cost. MVP picks correctness (no graph-walking inside moonlit, no fragile `-e file://` parsing) over minimal artifacts.

The brittle "re-run `uv export` without `--no-emit-workspace` and grep for `-e file://`" approach considered earlier is rejected: `uv export`'s output format is not a stable interface for moonlit to parse.

## 7. Edge cases

| # | Case | Behavior |
|---|------|----------|
| 1 | Workspace root also has `[project]` | Root is a member; addressable via `--package <root_name>` (Plan Open Risk D). |
| 2 | Nested workspace (member is itself a workspace) | Undefined for MVP; not validated, not supported, not tested. |
| 3 | Member directory without `pyproject.toml` | Silently skipped. |
| 4 | Member `pyproject.toml` parse failure | `MalformedPyprojectError` (exit 5). |
| 5 | Member without `[project].name` or with empty name | Silently skipped; `--verbose` logs the skip. |
| 6 | Two members with same normalized `[project].name` | `MalformedPyprojectError` (exit 5) with `workspace has duplicate package names: <name>`; pre-validated before uv. |
| 7 | `members` glob matches a file (not a dir) | Skipped. |
| 8 | Member path outside `project_root` (e.g. `members = ["../sibling"]`) | `MalformedPyprojectError` (exit 5). |
| 9 | `exclude = ["."]` and root has `[project]` | Root excluded; treated as a workspace with no root member. |
| 10 | Symlinked/junction member | Resolved (`Path.resolve()`) before exclude comparison; same rules apply on Windows and POSIX. |
| 11 | `exclude` glob matches nothing | Silent; no warning. |
| 12 | Cyclic workspace deps among members | Tolerated. `uv build --all-packages` handles graph; moonlit builds wheels for all members regardless of edges. |
| 13 | `--package` for a non-workspace project | `NotAWorkspaceError` (exit 5). |
| 14 | `--package` value matches no member after normalization | `UnknownPackageError` (exit 5); message lists raw member names. |
| 15 | `[tool.uv.workspace]` present but empty (`members=[]`, `exclude=[]`) | Workspace with empty members map; any `--package` → `UnknownPackageError`. |

## 8. Validation timing

Workspace detection runs first. `--package` is validated against the detected `Workspace` (or its absence) before any uv subprocess. Duplicate-name detection (Section 3 step 7) happens inside `workspace.detect`, not inferred later from a uv error message.

## 9. Error classes and exit codes

All four workspace-shape errors share **exit code 5** by design (D3) — they are all "workspace shape mismatch" failures and do not warrant disambiguating exit codes. The class is preserved for programmatic introspection and for the human-readable message:

| Class | Raised when |
|-------|-------------|
| `NotAWorkspaceError` | `--package` supplied but project is not a workspace. |
| `UnknownPackageError` | `--package` value does not match any member (post-normalization). |
| `MissingPackageError` | Workspace detected and `--package` not supplied. |
| `MalformedPyprojectError` | `pyproject.toml` parse failure, malformed `members`/`exclude`, member outside `project_root`, duplicate normalized names, or empty target name. |

## 10. References

- `specs/CROSS_CUTTING_DECISIONS.md` — D2 (transitive workspace deps), D3 (exit codes), D5 (PEP-503 normalization), D12 (`--package` matching).
- `specs/02-build-pipeline.md` — concrete `uv export` / `uv build --all-packages` / `uv pip install --target` invocations and tempdir layout (D17).
- `specs/01-cli.md` — `--package` flag declaration and parser-level enforcement.
- `IMPLEMENTATION_PLAN.md` — Open Risk D (workspace root with `[project]`).
