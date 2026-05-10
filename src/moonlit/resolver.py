"""The only module that calls ``subprocess.run(['uv', ...])``.

Each public function corresponds to one step of the build pipeline
(specs/02-build-pipeline.md §3): ``export`` (step 3), ``pip_install_target``
(steps 4 and 6), ``build_wheel`` (step 5). All ``subprocess.run`` invocations
use the pinned kwargs ``shell=False, check=False, capture_output=True,
env=os.environ.copy()``; argv is constructed only inside this module.

When the caller passes ``verbosity >= 1`` (CLI ``--verbose``), each uv
invocation is echoed to stderr as ``+ uv <argv>`` (POSIX shlex format) per
spec 01 §8, via :func:`moonlit._progress.emit_aside` so the output stays
coherent with any active progress spinner.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import _progress
from .errors import (
    ExportError,
    InternalError,
    NoLockfileError,
    StagingError,
    UvNotFoundError,
    WheelArtifactError,
)

# ---------- public API ----------


def export(
    project_root: Path,
    output_file: Path,
    *,
    package: str | None = None,
    python_version: str | None = None,
    verbosity: int = 0,
) -> None:
    """Run ``uv export`` to write a frozen requirements file (step 3).

    When ``python_version`` is set (e.g. ``"3.12"``), pass it through as
    ``--python <X.Y>`` so the resolved requirements target that ABI rather
    than the build host's interpreter. uv accepts a version spec on
    ``--python`` and auto-fetches a managed standalone CPython if needed
    (cross-interpreter builds, D20). ``uv export`` does NOT accept
    ``--python-version``; ``--python`` is the universal flag.

    Errors:

    * ``UvNotFoundError`` if the ``uv`` binary is not on PATH.
    * ``NoLockfileError`` if uv reports a missing lockfile.
    * ``ExportError`` with the "out of date" message if uv reports drift,
      otherwise ``ExportError`` with the prefixed stderr.
    """
    argv = [
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-workspace",
        "--format",
        "requirements-txt",
    ]
    if python_version is not None:
        argv += ["--python", python_version]
    if package is not None:
        argv += ["--package", package]
    argv += ["--output-file", str(output_file)]

    proc = _run_uv(argv, cwd=project_root, verbosity=verbosity)
    if proc.returncode == 0:
        return

    stderr = proc.stderr or ""
    if re.search(r"uv\.lock.*not found|no .*lockfile", stderr, re.IGNORECASE):
        raise NoLockfileError(f"uv.lock not found under {project_root}: {stderr.strip()}")
    if re.search(r"out.of.date|frozen", stderr, re.IGNORECASE):
        raise ExportError("uv.lock is out of date with pyproject.toml; run `uv lock` and retry.")
    raise ExportError(f"uv export failed: {stderr.strip()}")


def pip_install_target(
    project_root: Path,
    target_dir: Path,
    *,
    requirement: Path | None = None,
    wheel: Path | None = None,
    python_version: str | None = None,
    verbosity: int = 0,
) -> None:
    """Run ``uv pip install --target`` (step 4 with requirement, step 6 with wheel).

    Exactly one of ``requirement`` or ``wheel`` must be supplied; passing both
    or neither is a programmer error and raises ``InternalError`` (exit 11).

    When ``python_version`` is set, ``--python <X.Y>`` (a version spec) is
    passed instead of ``--python <sys.executable>`` (a path). uv resolves
    the version spec to a managed standalone CPython if no local install
    matches; the install targets that interpreter's ABI (D20: cross-interpreter
    builds). ``uv pip install``'s separate ``--python-version`` flag is a
    resolver minimum-version hint, not interpreter selection — it is NOT
    what we want here.
    """
    if (requirement is None) == (wheel is None):
        raise InternalError("pip_install_target requires exactly one of requirement= or wheel=")

    argv = [
        "uv",
        "pip",
        "install",
        "--target",
        str(target_dir),
        "--no-deps",
    ]
    if requirement is not None:
        argv += ["--requirement", str(requirement)]
    argv += ["--python", python_version if python_version is not None else sys.executable]
    if wheel is not None:
        argv.append(str(wheel))

    proc = _run_uv(argv, cwd=project_root, verbosity=verbosity)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise StagingError(f"uv pip install failed: {stderr}")


def build_wheel(
    project_root: Path,
    out_dir: Path,
    *,
    all_packages: bool = False,
    python_version: str | None = None,
    verbosity: int = 0,
) -> None:
    """Run ``uv build --wheel`` (step 5).

    For workspaces, pass ``all_packages=True`` per D2 — every member's wheel
    is produced and the caller installs each into staging via
    :func:`pip_install_target`.

    When ``python_version`` is set, pass ``--python <X.Y>`` (a version spec)
    so uv runs the project's PEP 517 build backend under that interpreter
    (D20). uv auto-fetches a managed standalone CPython if the requested
    version isn't locally installed. ``uv build`` does NOT accept
    ``--python-version``.
    """
    argv = ["uv", "build"]
    if all_packages:
        argv.append("--all-packages")
    if python_version is not None:
        argv += ["--python", python_version]
    argv += ["--wheel", "--out-dir", str(out_dir)]

    proc = _run_uv(argv, cwd=project_root, verbosity=verbosity)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise WheelArtifactError(f"uv build failed: {stderr}")


# ---------- internal subprocess wrapper ----------


def _run_uv(argv: list[str], *, cwd: Path, verbosity: int = 0) -> subprocess.CompletedProcess:
    if verbosity >= 1:
        # POSIX-shlex format on every platform per spec 01 §8. shlex.join is
        # POSIX-style; the leading "uv" is preserved verbatim.
        _progress.emit_aside(f"+ {shlex.join(argv)}")
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=True,
            env=os.environ.copy(),
            text=True,
        )
    except FileNotFoundError as exc:
        raise UvNotFoundError(f"uv binary not found on PATH (running {argv[0]!r})") from exc
