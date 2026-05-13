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
import platform
import re
import shutil
import stat
import sys
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
    PythonBundleError,
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
    # D19: when True, prepend a native Windows launcher to the zip body so the
    # produced file runs as a `.exe` without an explicit Python prefix.
    windows_exe: bool = False
    # D20: when set (e.g. "3.12"), thread `--python-version` through every uv
    # invocation so wheels are tagged for that Python's ABI rather than the
    # build host's. Stamped into env.json's `python_version` for the runtime
    # mismatch check (spec 03 §2 step 4a).
    python_version: str | None = None
    # D21: when True, produce a *folder* containing <basename>.exe (launcher),
    # <basename>.pyz (the application zipapp), and _python/ (a managed CPython
    # tree). Recipients without Python installed can still run the bundle by
    # invoking <basename>.exe; the launcher (D22) finds the bundled interpreter
    # via a sibling-file probe and spawns it directly. No runtime extraction.
    bundle_python: bool = False


@dataclass(frozen=True)
class _Target:
    """Resolved target for the build: raw [project].name + member directory."""

    name: str
    directory: Path


@dataclass(frozen=True)
class _BundledPython:
    """Step 8.5 result: the python-build-standalone distribution dir that will
    be copied verbatim into the output folder's ``_python/`` subdirectory (D21).
    """

    dist_root: Path


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
    if config.bundle_python:
        _preflight_bundle_dir(output_path, config.force)
        return
    if output_path.exists() or output_path.is_symlink():
        if not output_path.is_file():
            raise OutputNotWritableError(f"output path is not a regular file: {output_path}")
        if not config.force:
            raise OutputExistsError(
                f"output already exists; pass --force to overwrite: {output_path}"
            )


def _preflight_bundle_dir(output_path: Path, force: bool) -> None:
    """D21g: a folder target may be overwritten only when it looks like a
    previous moonlit bundle. Any other existing path at -o is refused, even
    under --force, so the flag cannot turn into an ``rm -rf``.
    """
    if not (output_path.exists() or output_path.is_symlink()):
        return
    if not output_path.is_dir() or output_path.is_symlink():
        raise OutputNotWritableError(f"output path is not a directory: {output_path}")
    if not _is_moonlit_bundle_dir(output_path):
        raise OutputNotWritableError(
            f"output path is a directory but not a moonlit bundle: {output_path}"
        )
    if not force:
        raise OutputExistsError(
            f"output already exists; pass --force to overwrite: {output_path}"
        )


def _is_moonlit_bundle_dir(path: Path) -> bool:
    """A directory is a recognized moonlit bundle iff it contains
    ``<basename>.exe``, ``<basename>.pyz``, and ``_python/python.exe`` (D21g).
    """
    basename = path.name
    return (
        (path / f"{basename}.exe").is_file()
        and (path / f"{basename}.pyz").is_file()
        and (path / "_python" / "python.exe").is_file()
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
            config.project_root,
            req_path,
            package=package_for_export,
            python_version=config.python_version,
            verbosity=verbosity,
        )
        n_reqs = _count_requirements(req_path)
        step.set_result(f"frozen · {n_reqs} packages")

    with Step("installing dependencies into staging", verbosity=verbosity) as step:
        resolver.pip_install_target(
            config.project_root,
            site_packages,
            requirement=req_path,
            python_version=config.python_version,
            verbosity=verbosity,
        )
        step.set_result(f"installed · {n_reqs} packages")

    with Step("building wheels (uv build)", verbosity=verbosity) as step:
        resolver.build_wheel(
            config.project_root,
            dist_dir,
            all_packages=is_workspace,
            python_version=config.python_version,
            verbosity=verbosity,
        )
        wheels = sorted(dist_dir.glob("*.whl"))
        _validate_wheels(wheels, target, is_workspace)
        step.set_result(f"built · {len(wheels)} wheel{'s' if len(wheels) != 1 else ''}")

    with Step("installing wheels into staging", verbosity=verbosity) as step:
        for wheel in wheels:
            resolver.pip_install_target(
                config.project_root,
                site_packages,
                wheel=wheel,
                python_version=config.python_version,
                verbosity=verbosity,
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

    # Step 8.5 (D21): bundled-Python install runs strictly AFTER compute_build_id
    # so the app's cache key cannot drift on a uv-shipped CPython patch bump.
    bundled = _install_bundled_python(config, tmp_root) if config.bundle_python else None

    env_dict = _build_env_dict(target, build_id, entry_point, config)

    with Step("writing archive", verbosity=verbosity) as step:
        if config.bundle_python:
            assert bundled is not None  # narrow for type checker; gated by config.bundle_python
            entry_count = _write_bundle_atomically(config, staging, env_dict, bundled)
        else:
            entry_count = _write_archive_atomically(config, staging, env_dict)
            # D19d: skip the POSIX exec-bit chmod for windows_exe mode — a
            # `.exe` doesn't need it, and the file is normally written to a
            # Windows-style filesystem anyway.
            if os.name != "nt" and not config.windows_exe:
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


def _install_bundled_python(config: BuildConfig, tmp_root: Path) -> _BundledPython:
    """Run step 8.5 (D21): install Python into the build tempdir, locate
    ``python.exe`` at its dist root. The dist tree is copied verbatim into the
    output folder's ``_python/`` subdirectory at archive-assembly time.
    """
    install_dir = tmp_root / "python"
    install_dir.mkdir(parents=True, exist_ok=True)
    version = config.python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
    dist_root = resolver.python_install(install_dir, version=version, verbosity=config.verbosity)
    python_exe = dist_root / "python.exe"
    if not python_exe.is_file():
        raise PythonBundleError(f"python.exe not found in bundled distribution: {python_exe}")
    return _BundledPython(dist_root=dist_root)


def _build_env_dict(
    target: _Target,
    build_id: str,
    entry_point: str,
    config: BuildConfig,
) -> dict:
    # Note (D21h): env.json is byte-identical between bundle and non-bundle
    # builds (modulo built_at). The bundle's "ships its own Python" state is
    # observable from the on-disk folder layout, not from env.json.
    return {
        "schema_version": 1,
        "name": target.name,
        "build_id": build_id,
        "entry_point": entry_point,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "moonlit_version": _MOONLIT_VERSION,
        "python_shebang": config.python_shebang,
        # v1-optional per spec 05 §7. Stamp the *target* Python's major.minor
        # so the bootstrap can fail fast on ABI-tag mismatch. When the user
        # passed --python-version (D20), it overrides the build host's version
        # — that's the whole point of cross-interpreter builds.
        "python_version": (
            config.python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        ),
    }


def _write_archive_atomically(
    config: BuildConfig,
    staging: Path,
    env_dict: dict,
) -> int:
    """Write a single-file archive via a temp-then-rename dance (D15).

    Used for the default ``.pyz`` and ``--windows-exe`` shapes; the bundle
    shape goes through :func:`_write_bundle_atomically` instead.
    """
    output_path = config.output_path.resolve(strict=False)
    tmp_out = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
    try:
        entry_count = _create_archive(
            tmp_out, staging, env_dict, prepend_launcher=config.windows_exe,
            python_shebang=config.python_shebang,
        )
        os.replace(tmp_out, output_path)
        return entry_count
    finally:
        # If os.replace succeeded, tmp_out no longer exists; this is a no-op.
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


def _write_bundle_atomically(
    config: BuildConfig,
    staging: Path,
    env_dict: dict,
    bundled: _BundledPython,
) -> int:
    """Write a folder bundle via the D4 directory-replace protocol (D21f).

    The staging dir at ``<output>.tmp.<pid>/`` is populated with the launcher,
    the application zipapp, and the bundled Python tree; on success it is
    swapped into place at ``output_path``. Any prior moonlit-recognized bundle
    at ``output_path`` is renamed aside before the swap and removed after.
    Returns the zip-entry count of the inner ``<basename>.pyz``.
    """
    output_path = config.output_path.resolve(strict=False)
    basename = output_path.name
    tmp_dir = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
    try:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        # 1. The application zipapp goes inside the folder. Its zip body is
        #    byte-identical (entry-wise) to what a non-bundle build would
        #    produce given the same inputs (invariant I11b). We deliberately do
        #    NOT prepend the launcher PE: in folder mode the launcher is a
        #    sibling .exe, not a prefix.
        inner_pyz = tmp_dir / f"{basename}.pyz"
        entry_count = _create_archive(
            inner_pyz, staging, env_dict, prepend_launcher=False,
            python_shebang=config.python_shebang,
        )

        # 2. The launcher .exe sits next to the .pyz. Just the vendored bytes
        #    for the host arch — no appended zip, no shebang line.
        launcher_path = tmp_dir / f"{basename}.exe"
        launcher_path.write_bytes(_load_launcher_bytes())

        # 3. Copy the python-build-standalone tree into _python/.
        _copy_python_tree(bundled.dist_root, tmp_dir / "_python")

        # 4. Swap the staged directory into place via D4.
        _atomic_replace_dir(tmp_dir, output_path)
        return entry_count
    finally:
        # D21f: if the staging dir survived to this point, the swap didn't
        # happen — clean it up so a crashed build never leaves a half-written
        # bundle alongside the target.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _create_archive(
    tmp_out: Path,
    staging: Path,
    env_dict: dict,
    *,
    prepend_launcher: bool,
    python_shebang: str,
) -> int:
    """Write the zip body (spec 02 §3 step 9-single / 9-bundle.3). Returns
    total zip-entry count.

    Body layout (invariant I11/I11b):
      site-packages/ → _bootstrap/ → __main__.py → env.json

    When ``prepend_launcher`` is True (single-file ``--windows-exe`` mode), the
    file starts with the vendored launcher PE bytes; in all modes the shebang
    line follows the (optional) launcher and precedes the zip header.
    """
    site_packages = staging / "site-packages"
    env_payload = _serialize_env_json(env_dict)
    main_py_bytes = _read_main_template()
    bootstrap_files = list(_iter_bootstrap_files())

    entry_count = 0
    with open(tmp_out, "wb") as fp:
        if prepend_launcher:
            fp.write(_load_launcher_bytes())
        fp.write(b"#!" + python_shebang.encode("ascii") + b"\n")
        with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as zf:
            for src_file, arcname, mode in _iter_staging_files(site_packages):
                _write_file_to_zip(zf, src_file, arcname, mode)
                entry_count += 1
            for rel, content in bootstrap_files:
                zf.writestr(f"_bootstrap/{rel}", content)
                entry_count += 1
            zf.writestr("__main__.py", main_py_bytes)
            entry_count += 1
            zf.writestr("env.json", env_payload)
            entry_count += 1
        fp.flush()
        os.fsync(fp.fileno())
    return entry_count


def _copy_python_tree(dist_root: Path, dest_root: Path) -> None:
    """Copy every file under ``dist_root`` to the matching path under
    ``dest_root``. Used by 9-bundle.5 to populate ``<output>/_python/``.
    """
    for src in dist_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(dist_root)
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # copy2 preserves mtime + mode; on Windows the mode bits are mostly
        # ignored but on POSIX-host cross-builds we want python.exe to keep
        # any executable bits the standalone build shipped with.
        shutil.copy2(src, dst)


def _atomic_replace_dir(src: Path, dst: Path) -> None:
    """D4: directory-replace via rename-aside, then rename-in.

    ``src`` becomes ``dst``. If ``dst`` already exists, it is moved aside to
    ``<dst>.old.<pid>`` first; on success we best-effort remove the old copy.
    On any failure during the swap we roll the old copy back.
    """
    old_path: Path | None = None
    if dst.exists():
        old_path = dst.with_name(f"{dst.name}.old.{os.getpid()}")
        os.rename(dst, old_path)
    try:
        os.replace(src, dst)
    except Exception:
        if old_path is not None and old_path.exists() and not dst.exists():
            os.rename(old_path, dst)
        raise
    if old_path is not None:
        shutil.rmtree(old_path, ignore_errors=True)


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


def _load_launcher_bytes() -> bytes:
    """Read the vendored Windows launcher for the host architecture (D19a).

    Raises :class:`InternalError` if the host platform doesn't map to a known
    launcher arch or the expected ``t-<arch>.exe`` is missing from the
    package data.
    """
    arch = _detect_launcher_arch()
    res = importlib.resources.files("moonlit._launchers") / f"t-{arch}.exe"
    if not res.is_file():
        raise InternalError(
            f"missing launcher binary: moonlit/_launchers/t-{arch}.exe; "
            f"reinstall moonlit or rebuild via launcher/ (see launcher/README.md)"
        )
    return res.read_bytes()


def _detect_launcher_arch() -> str:
    """Map ``(os.name, platform.machine())`` to a launcher arch tag.

    Per D19a, returns one of ``x64``, ``x86``, or ``arm64``. Anything else
    raises :class:`InternalError` (exit 11) — building a Windows .exe on an
    unrecognized host is treated as a configuration bug.
    """
    machine = platform.machine().upper()
    # Windows reports AMD64/ARM64; POSIX hosts cross-building report x86_64,
    # aarch64, etc. Both shapes are mapped here.
    arch_map = {
        "AMD64": "x64",
        "X86_64": "x64",
        "ARM64": "arm64",
        "AARCH64": "arm64",
        "X86": "x86",
        "I686": "x86",
        "I386": "x86",
    }
    arch = arch_map.get(machine)
    if arch is None:
        raise InternalError(
            f"unsupported host architecture for --windows-exe: "
            f"os.name={os.name!r}, platform.machine()={platform.machine()!r}"
        )
    return arch


def _print_success_line(output_path: Path, entry_count: int) -> None:
    """spec 01 §8: ``wrote <path> (<size>, <N> entries)`` to stdout.

    For a folder bundle (D21), <size> is the sum of every file's size under
    the output dir and <N> is the count of files in the bundle. The literal
    format is preserved so invariant I8's parser still passes.
    """
    if output_path.is_dir():
        size, count = _bundle_dir_totals(output_path)
        print(f"wrote {output_path} ({humanize_bytes(size)}, {count} entries)")
        return
    size = output_path.stat().st_size
    print(f"wrote {output_path} ({humanize_bytes(size)}, {entry_count} entries)")


def _bundle_dir_totals(root: Path) -> tuple[int, int]:
    total_size = 0
    file_count = 0
    for p in root.rglob("*"):
        if p.is_file():
            total_size += p.stat().st_size
            file_count += 1
    return total_size, file_count


def humanize_bytes(n: int) -> str:
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
