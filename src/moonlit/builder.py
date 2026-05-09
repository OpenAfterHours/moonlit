"""Build pipeline orchestrator (specs/02-build-pipeline.md §3).

Coordinates the 10-step build pipeline: workspace detection, target
selection, ``uv`` subprocess steps via :mod:`moonlit.resolver`, console-script
resolution, build-id computation, and archive assembly under the D17 tempdir
+ D15 atomic-rename protocol. The actual zip contents are written by
:func:`_create_archive`; in pass 1 that function is a placeholder that
emits a minimal valid pyz so the pipeline is end-to-end testable, and pass
2 replaces it with the full assembly per spec 02 §3 step 9.
"""

import configparser
import importlib.resources
import json
import os
import re
import shutil
import stat
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources.abc import Traversable
from pathlib import Path

from . import __version__ as _MOONLIT_VERSION
from . import hashing, resolver, workspace
from ._progress import Step, _format_duration
from .errors import (
    BadEntryPointError,
    ConsoleScriptNotFoundError,
    InternalError,
    MalformedPyprojectError,
    MissingPackageError,
    NotAWorkspaceError,
    OutputExistsError,
    OutputNotWritableError,
    UnknownPackageError,
    WheelArtifactError,
)


@dataclass(frozen=True)
class BuildConfig:
    """Per spec 02 §1. Exactly one of ``entry_point``/``console_script`` is set."""

    project_root: Path
    output_path: Path
    entry_point: str | None
    console_script: str | None
    python_shebang: str
    package: str | None
    force: bool
    verbosity: int


@dataclass(frozen=True)
class _Target:
    """Resolved target for the build: raw [project].name + member directory."""

    name: str
    directory: Path


_ENTRY_POINT_SIDE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def build(config: BuildConfig) -> int:
    """Run the full build pipeline; return 0 on success.

    On any failure raises a :class:`MoonlitError` subclass for the CLI to
    translate to a process exit code.
    """
    _validate_config(config)
    # Workspace detection and target selection happen before any progress
    # steps so the spinner doesn't briefly appear before a hard preflight
    # error like NotAWorkspaceError or UnknownPackageError.
    workspace_obj = workspace.detect(config.project_root)
    target = _select_target(workspace_obj, config)
    if config.entry_point is not None:
        _validate_entry_point_string(config.entry_point)
    _preflight_output(config)

    tempdir = tempfile.mkdtemp(prefix="moonlit-build-")
    try:
        return _run_pipeline(config, workspace_obj, target, Path(tempdir))
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


# ---------- private helpers, in build()'s call order ----------


def _validate_config(config: BuildConfig) -> None:
    if (config.entry_point is None) == (config.console_script is None):
        raise InternalError("BuildConfig must have exactly one of entry_point or console_script")


def _select_target(workspace_obj: workspace.Workspace | None, config: BuildConfig) -> _Target:
    if workspace_obj is None:
        if config.package is not None:
            raise NotAWorkspaceError(
                f"--package not allowed: {config.project_root} is not a uv workspace"
            )
        name = _read_project_name(config.project_root)
        return _Target(name=name, directory=config.project_root)

    if config.package is None:
        raise MissingPackageError(
            f"--package is required for uv workspaces "
            f"(members: {', '.join(sorted(workspace_obj.members)) or '<empty>'})"
        )
    norm_input = workspace.pep503_normalize(config.package)
    for raw_name, directory in workspace_obj.members.items():
        if workspace.pep503_normalize(raw_name) == norm_input:
            return _Target(name=raw_name, directory=directory)
    raw_names = sorted(workspace_obj.members.keys())
    raise UnknownPackageError(
        f"--package '{config.package}' not in workspace; "
        f"members: {', '.join(raw_names) or '<empty>'}"
    )


def _read_project_name(project_root: Path) -> str:
    pyproject = project_root / "pyproject.toml"
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MalformedPyprojectError(
            f"cannot read [project].name from {pyproject}: {exc}"
        ) from exc
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name:
        raise MalformedPyprojectError(f"missing or empty [project].name in {pyproject}")
    return name


def _validate_entry_point_string(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise BadEntryPointError(f"invalid entry point: {value}")
    lhs, rhs = parts
    if not _ENTRY_POINT_SIDE_RE.fullmatch(lhs) or not _ENTRY_POINT_SIDE_RE.fullmatch(rhs):
        raise BadEntryPointError(f"invalid entry point: {value}")


def _preflight_output(config: BuildConfig) -> None:
    output_path = config.output_path.resolve(strict=False)
    parent = output_path.parent
    if not parent.is_dir():
        raise OutputNotWritableError(f"output parent directory does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        raise OutputNotWritableError(f"output parent directory not writable: {parent}")
    if output_path.exists() or output_path.is_symlink():
        if not output_path.is_file():
            raise OutputNotWritableError(f"output path is not a regular file: {output_path}")
        if not config.force:
            raise OutputExistsError(
                f"output already exists; pass --force to overwrite: {output_path}"
            )


def _run_pipeline(
    config: BuildConfig,
    workspace_obj: workspace.Workspace | None,
    target: _Target,
    tmp_root: Path,
) -> int:
    staging = tmp_root / "staging"
    site_packages = staging / "site-packages"
    site_packages.mkdir(parents=True)
    dist_dir = tmp_root / "dist"
    dist_dir.mkdir()
    req_path = tmp_root / "requirements.txt"

    is_workspace = workspace_obj is not None
    package_for_export = target.name if is_workspace else None
    verbosity = config.verbosity
    total_start = time.perf_counter()

    # Step labels and result text follow the per-step plan in
    # plans/when-the-pyz-is-hashed-salamander.md and the spec 01 §8 default-mode
    # progress-line requirement.

    with Step(f"resolving target package '{target.name}'", verbosity=verbosity) as step:
        if is_workspace:
            assert workspace_obj is not None  # narrows for type checker
            step.set_result(
                f"selected {target.name} (workspace · {len(workspace_obj.members)} members)"
            )
        else:
            step.set_result(f"selected {target.name}")

    with Step("freezing dependencies (uv export)", verbosity=verbosity) as step:
        resolver.export(
            config.project_root, req_path, package=package_for_export, verbosity=verbosity
        )
        n_reqs = _count_requirements(req_path)
        step.set_result(f"frozen · {n_reqs} packages")

    with Step("installing dependencies into staging", verbosity=verbosity) as step:
        resolver.pip_install_target(
            config.project_root, site_packages, requirement=req_path, verbosity=verbosity
        )
        step.set_result(f"installed · {n_reqs} packages")

    with Step("building wheels (uv build)", verbosity=verbosity) as step:
        resolver.build_wheel(
            config.project_root, dist_dir, all_packages=is_workspace, verbosity=verbosity
        )
        wheels = sorted(dist_dir.glob("*.whl"))
        _validate_wheels(wheels, target, is_workspace)
        step.set_result(f"built · {len(wheels)} wheel{'s' if len(wheels) != 1 else ''}")

    with Step("installing wheels into staging", verbosity=verbosity) as step:
        for wheel in wheels:
            resolver.pip_install_target(
                config.project_root, site_packages, wheel=wheel, verbosity=verbosity
            )
        step.set_result(f"installed · {len(wheels)} wheel{'s' if len(wheels) != 1 else ''}")

    with Step("resolving entry point", verbosity=verbosity) as step:
        entry_point = _resolve_entry_point(config, site_packages)
        step.set_result(f"entry · {entry_point}")

    with Step("hashing staged tree", verbosity=verbosity) as step:
        build_id = hashing.compute_build_id(site_packages)
        n_files = _count_site_package_files(site_packages)
        step.set_result(
            f"build id {build_id[:4]}…{build_id[-4:]} · {n_files} files",
            show_duration=False,
        )

    env_dict = _build_env_dict(target, build_id, entry_point, config)

    with Step("writing archive", verbosity=verbosity) as step:
        entry_count = _write_archive_atomically(config, staging, env_dict)
        if os.name != "nt":
            os.chmod(config.output_path, 0o755)
        total_elapsed = time.perf_counter() - total_start
        step.set_result(
            f"wrote {config.output_path.name} · {_format_duration(total_elapsed)} total",
            show_duration=False,
        )

    _print_success_line(config.output_path, entry_count)
    return 0


def _count_requirements(req_path: Path) -> int:
    """Count package lines in a uv-exported requirements file (skip blanks/comments)."""
    if not req_path.is_file():
        return 0
    n = 0
    with open(req_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#"):
                n += 1
    return n


def _count_site_package_files(site_packages: Path) -> int:
    if not site_packages.is_dir():
        return 0
    return sum(1 for p in site_packages.rglob("*") if p.is_file())


def _validate_wheels(wheels: list[Path], target: _Target, is_workspace: bool) -> None:
    if not wheels:
        raise WheelArtifactError("uv build produced no wheels")
    if is_workspace:
        return
    if len(wheels) != 1:
        raise WheelArtifactError(f"non-workspace build produced {len(wheels)} wheels; expected 1")
    wheel_name = _read_wheel_metadata_name(wheels[0])
    if workspace.pep503_normalize(wheel_name) != workspace.pep503_normalize(target.name):
        raise WheelArtifactError(f"wheel name {wheel_name!r} does not match target {target.name!r}")


def _read_wheel_metadata_name(wheel: Path) -> str:
    try:
        with zipfile.ZipFile(wheel, "r") as zf:
            for info in zf.infolist():
                if info.filename.endswith(".dist-info/METADATA"):
                    content = zf.read(info).decode("utf-8")
                    for line in content.splitlines():
                        if line.startswith("Name:"):
                            return line[len("Name:") :].strip()
    except (zipfile.BadZipFile, OSError) as exc:
        raise WheelArtifactError(f"unreadable wheel: {wheel}: {exc}") from exc
    raise WheelArtifactError(f"could not find Name in wheel METADATA: {wheel}")


def _resolve_entry_point(config: BuildConfig, site_packages: Path) -> str:
    if config.entry_point is not None:
        return config.entry_point
    name = config.console_script
    matches: list[tuple[Path, str]] = []
    discovered: set[str] = set()
    for ep_file in sorted(site_packages.glob("*.dist-info/entry_points.txt")):
        cp = configparser.ConfigParser(strict=False)
        cp.read(ep_file, encoding="utf-8")
        if not cp.has_section("console_scripts"):
            continue
        for key in cp["console_scripts"]:
            discovered.add(key)
            if key == name:
                matches.append((ep_file, cp["console_scripts"][key]))

    if not matches:
        if not discovered:
            raise ConsoleScriptNotFoundError(
                f"console script '{name}' not found; "
                f"no console_scripts declared in any dist-info; "
                f"use --entry-point pkg.module:callable"
            )
        raise ConsoleScriptNotFoundError(
            f"console script '{name}' not found; "
            f"available: {', '.join(sorted(discovered))}; "
            f"pass --entry-point <module>:<callable> instead"
        )
    if len(matches) > 1:
        files = sorted({str(p) for p, _ in matches})
        raise ConsoleScriptNotFoundError(f"ambiguous console script '{name}'; declared in {files}")
    _, value = matches[0]
    value = value.strip()
    _validate_entry_point_string(value)
    return value


def _build_env_dict(
    target: _Target,
    build_id: str,
    entry_point: str,
    config: BuildConfig,
) -> dict:
    return {
        "schema_version": 1,
        "name": target.name,
        "build_id": build_id,
        "entry_point": entry_point,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "moonlit_version": _MOONLIT_VERSION,
        "python_shebang": config.python_shebang,
    }


def _write_archive_atomically(config: BuildConfig, staging: Path, env_dict: dict) -> int:
    """Write the archive via a temp-then-rename dance (D15). Returns entry count."""
    output_path = config.output_path.resolve(strict=False)
    tmp_out = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
    try:
        entry_count = _create_archive(tmp_out, staging, env_dict, config.python_shebang)
        os.replace(tmp_out, output_path)
        return entry_count
    finally:
        # If os.replace succeeded, tmp_out no longer exists; this is a no-op.
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


def _create_archive(tmp_out: Path, staging: Path, env_dict: dict, python_shebang: str) -> int:
    """Write the .pyz archive per spec 02 §3 step 9. Returns total zip-entry count.

    Layout: shebang prefix BEFORE the zip header, then a ZIP_DEFLATED archive
    containing ``site-packages/<files>`` (D1), the ``_bootstrap/`` package
    copied verbatim, the rendered ``__main__.py``, and ``env.json``.
    """
    site_packages = staging / "site-packages"
    env_payload = _serialize_env_json(env_dict)
    main_py_bytes = _read_main_template()
    bootstrap_files = list(_iter_bootstrap_files())

    entry_count = 0
    with open(tmp_out, "wb") as fp:
        # Step 9.3: shebang BEFORE the zip header (D1).
        fp.write(b"#!" + python_shebang.encode("ascii") + b"\n")
        with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as zf:
            # Step 9.5: site-packages tree.
            for src_file, arcname, mode in _iter_staging_files(site_packages):
                _write_file_to_zip(zf, src_file, arcname, mode)
                entry_count += 1
            # Step 9.6: _bootstrap package (stdlib-only by D7).
            for rel, content in bootstrap_files:
                zf.writestr(f"_bootstrap/{rel}", content)
                entry_count += 1
            # Step 9.7: rendered __main__.py.
            zf.writestr("__main__.py", main_py_bytes)
            entry_count += 1
            # Step 9.8: env.json.
            zf.writestr("env.json", env_payload)
            entry_count += 1
        # Step 9.9: flush + fsync + close.
        fp.flush()
        os.fsync(fp.fileno())
    return entry_count


def _iter_staging_files(
    site_packages: Path,
) -> Iterator[tuple[Path, str, int]]:
    """Yield (source_path, arcname, mode_bits) for each file under site-packages/."""
    if not site_packages.is_dir():
        return
    for src in site_packages.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(site_packages)
        arcname = "site-packages/" + rel.as_posix()
        yield src, arcname, src.stat().st_mode


def _write_file_to_zip(zf: zipfile.ZipFile, src_file: Path, arcname: str, mode: int) -> None:
    """Write src_file to zf at arcname, propagating exec bit on POSIX."""
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    if os.name != "nt" and (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
        info.external_attr = 0o755 << 16
    with open(src_file, "rb") as f:
        zf.writestr(info, f.read())


def _serialize_env_json(env_dict: dict) -> bytes:
    """spec 05 §5 producer recipe (sorted keys, indent=2, trailing newline)."""
    payload = (
        json.dumps(
            env_dict,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    )
    return payload.encode("utf-8")


def _read_main_template() -> bytes:
    """Read _templates/main_py.tmpl and normalize to LF line endings (spec §3 step 9.7)."""
    template = importlib.resources.files("moonlit") / "_templates" / "main_py.tmpl"
    text = template.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.encode("utf-8")


def _iter_bootstrap_files() -> Iterator[tuple[str, bytes]]:
    """Yield (relpath_under_bootstrap, content_bytes) for each shipped file."""
    root = importlib.resources.files("moonlit") / "_bootstrap"
    yield from _walk_traversable(root, rel_prefix="")


def _walk_traversable(node: Traversable, *, rel_prefix: str) -> Iterator[tuple[str, bytes]]:
    # Sort by name so the bundled order is deterministic across runs.
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        name = child.name
        # __pycache__ and .pyc files are dev-time bytecode; never ship them.
        if name == "__pycache__" or name.endswith(".pyc"):
            continue
        rel = name if not rel_prefix else f"{rel_prefix}/{name}"
        if child.is_file():
            yield rel, child.read_bytes()
        else:
            yield from _walk_traversable(child, rel_prefix=rel)


def _print_success_line(output_path: Path, entry_count: int) -> None:
    """spec 01 §8: ``wrote <path> (<size>, <N> entries)`` to stdout."""
    size = output_path.stat().st_size
    print(f"wrote {output_path} ({_humanize_bytes(size)}, {entry_count} entries)")


def _humanize_bytes(n: int) -> str:
    """Format n as bytes with binary units (spec 01 invariant I8)."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"
