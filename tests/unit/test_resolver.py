"""Pin moonlit.resolver to specs/02-build-pipeline.md §3.

resolver.py is the only module that calls ``subprocess.run(['uv', ...])``.
These unit tests assert: (a) the exact argv sent for each pipeline step;
(b) the pinned ``subprocess.run`` kwargs; (c) the error mapping
(FileNotFoundError → UvNotFoundError, regex-matched stderr → specific
classes, otherwise generic class per step).
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from moonlit import resolver
from moonlit.errors import (
    ExportError,
    InternalError,
    NoLockfileError,
    StagingError,
    UvNotFoundError,
    WheelArtifactError,
)

# ---------- FakeRun fixture ----------


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode: int = 0
        self.stdout: str = ""
        self.stderr: str = ""
        self.raises: BaseException | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append((argv, dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> _FakeRun:
    fake = _FakeRun()
    monkeypatch.setattr(resolver.subprocess, "run", fake)
    return fake


# ---------- export ----------


def test_export_argv_without_package(fake_run: _FakeRun, tmp_path: Path) -> None:
    out = tmp_path / "req.txt"
    resolver.export(tmp_path, out)
    assert fake_run.calls[0][0] == [
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-workspace",
        "--format",
        "requirements-txt",
        "--output-file",
        str(out),
    ]


def test_export_argv_with_package(fake_run: _FakeRun, tmp_path: Path) -> None:
    out = tmp_path / "req.txt"
    resolver.export(tmp_path, out, package="shouter")
    assert fake_run.calls[0][0] == [
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-workspace",
        "--format",
        "requirements-txt",
        "--package",
        "shouter",
        "--output-file",
        str(out),
    ]


def test_export_uses_pinned_subprocess_kwargs(fake_run: _FakeRun, tmp_path: Path) -> None:
    resolver.export(tmp_path, tmp_path / "r.txt")
    _, kwargs = fake_run.calls[0]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["cwd"] == tmp_path
    # env must be a fresh copy of os.environ, not the live mapping itself.
    assert kwargs["env"] is not os.environ
    assert kwargs["env"] == dict(os.environ)


def test_export_uv_binary_missing_raises_uv_not_found(fake_run: _FakeRun, tmp_path: Path) -> None:
    fake_run.raises = FileNotFoundError(2, "No such file: 'uv'")
    with pytest.raises(UvNotFoundError):
        resolver.export(tmp_path, tmp_path / "r.txt")


@pytest.mark.parametrize(
    "stderr_text",
    [
        "Error: uv.lock not found",
        "uv.lock NOT FOUND",
        "Error: no lockfile in this directory",
        "no Lockfile present",
    ],
)
def test_export_no_lockfile_stderr_raises(
    fake_run: _FakeRun, tmp_path: Path, stderr_text: str
) -> None:
    fake_run.returncode = 1
    fake_run.stderr = stderr_text
    with pytest.raises(NoLockfileError):
        resolver.export(tmp_path, tmp_path / "r.txt")


@pytest.mark.parametrize(
    "stderr_text",
    [
        "uv.lock is out of date",
        "OUT OF DATE",
        "Error: --frozen is set but lock would change",
        "FROZEN check failed",
    ],
)
def test_export_drift_stderr_raises_with_specific_message(
    fake_run: _FakeRun, tmp_path: Path, stderr_text: str
) -> None:
    fake_run.returncode = 1
    fake_run.stderr = stderr_text
    with pytest.raises(ExportError, match="out of date"):
        resolver.export(tmp_path, tmp_path / "r.txt")


def test_export_drift_message_suggests_uv_lock(fake_run: _FakeRun, tmp_path: Path) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "uv.lock is out of date with pyproject.toml"
    with pytest.raises(ExportError) as excinfo:
        resolver.export(tmp_path, tmp_path / "r.txt")
    assert "`uv lock`" in str(excinfo.value)


def test_export_other_failure_raises_generic_export_error(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "some unrelated uv failure"
    with pytest.raises(ExportError, match="some unrelated uv failure"):
        resolver.export(tmp_path, tmp_path / "r.txt")


def test_export_no_lockfile_takes_precedence_over_drift(fake_run: _FakeRun, tmp_path: Path) -> None:
    # Stderr containing both signals: NoLockfileError is checked first.
    fake_run.returncode = 1
    fake_run.stderr = "uv.lock not found and out of date"
    with pytest.raises(NoLockfileError):
        resolver.export(tmp_path, tmp_path / "r.txt")


def test_export_success_returns_none(fake_run: _FakeRun, tmp_path: Path) -> None:
    assert resolver.export(tmp_path, tmp_path / "r.txt") is None


def test_export_appends_python_when_version_set(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    # D20: cross-interpreter builds. uv export accepts only --python (which
    # takes a version spec like "3.12"); --python-version is not a valid
    # flag for `uv export`.
    out = tmp_path / "req.txt"
    resolver.export(tmp_path, out, python_version="3.12")
    argv = fake_run.calls[0][0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == "3.12"
    # The resolver-hint --python-version flag must NOT be added (uv export
    # would reject it).
    assert "--python-version" not in argv


def test_export_omits_python_by_default(fake_run: _FakeRun, tmp_path: Path) -> None:
    resolver.export(tmp_path, tmp_path / "r.txt")
    assert "--python" not in fake_run.calls[0][0]


# ---------- pip_install_target ----------


def test_pip_install_with_requirement_argv(fake_run: _FakeRun, tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    req = tmp_path / "req.txt"
    resolver.pip_install_target(tmp_path, target, requirement=req)
    assert fake_run.calls[0][0] == [
        "uv",
        "pip",
        "install",
        "--target",
        str(target),
        "--no-deps",
        "--requirement",
        str(req),
        "--python",
        sys.executable,
    ]


def test_pip_install_with_wheel_argv(fake_run: _FakeRun, tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    wheel = tmp_path / "myapp-0.1.0-py3-none-any.whl"
    resolver.pip_install_target(tmp_path, target, wheel=wheel)
    assert fake_run.calls[0][0] == [
        "uv",
        "pip",
        "install",
        "--target",
        str(target),
        "--no-deps",
        "--python",
        sys.executable,
        str(wheel),
    ]


def test_pip_install_uses_pinned_subprocess_kwargs(fake_run: _FakeRun, tmp_path: Path) -> None:
    resolver.pip_install_target(tmp_path, tmp_path / "s", requirement=tmp_path / "r.txt")
    _, kwargs = fake_run.calls[0]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"] is not os.environ


def test_pip_install_neither_requirement_nor_wheel_raises_internal(
    tmp_path: Path,
) -> None:
    with pytest.raises(InternalError):
        resolver.pip_install_target(tmp_path, tmp_path / "s")


def test_pip_install_both_requirement_and_wheel_raises_internal(
    tmp_path: Path,
) -> None:
    with pytest.raises(InternalError):
        resolver.pip_install_target(
            tmp_path,
            tmp_path / "s",
            requirement=tmp_path / "r.txt",
            wheel=tmp_path / "x.whl",
        )


def test_pip_install_uv_binary_missing_raises_uv_not_found(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    fake_run.raises = FileNotFoundError(2, "No such file: 'uv'")
    with pytest.raises(UvNotFoundError):
        resolver.pip_install_target(tmp_path, tmp_path / "s", requirement=tmp_path / "r.txt")


def test_pip_install_failure_raises_staging_error(fake_run: _FakeRun, tmp_path: Path) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "uv pip install: failure"
    with pytest.raises(StagingError, match="uv pip install"):
        resolver.pip_install_target(tmp_path, tmp_path / "s", requirement=tmp_path / "r.txt")


def test_pip_install_wheel_failure_raises_staging_error(fake_run: _FakeRun, tmp_path: Path) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "could not install wheel"
    with pytest.raises(StagingError):
        resolver.pip_install_target(tmp_path, tmp_path / "s", wheel=tmp_path / "x.whl")


def test_pip_install_python_version_replaces_executable_path(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    # D20: --python takes either a path (sys.executable, default) or a
    # version spec ("3.12"); the resolver swaps one for the other rather
    # than passing both. (uv's `--python-version` flag on `pip install` is a
    # resolver hint, not interpreter selection — not what we want here.)
    target = tmp_path / "site-packages"
    req = tmp_path / "req.txt"
    resolver.pip_install_target(tmp_path, target, requirement=req, python_version="3.12")
    argv = fake_run.calls[0][0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == "3.12"
    # Exactly one --python token; sys.executable is NOT also passed.
    assert argv.count("--python") == 1
    assert sys.executable not in argv
    # Resolver-hint flag must not be confused with interpreter selection.
    assert "--python-version" not in argv


def test_pip_install_keeps_executable_path_when_no_version(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    # Backward-compat: without python_version, --python <sys.executable>.
    target = tmp_path / "site-packages"
    req = tmp_path / "req.txt"
    resolver.pip_install_target(tmp_path, target, requirement=req)
    argv = fake_run.calls[0][0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == sys.executable


def test_pip_install_python_version_with_wheel_argv(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    target = tmp_path / "site-packages"
    wheel = tmp_path / "myapp-0.1.0-py3-none-any.whl"
    resolver.pip_install_target(tmp_path, target, wheel=wheel, python_version="3.12")
    argv = fake_run.calls[0][0]
    assert argv[argv.index("--python") + 1] == "3.12"
    # The wheel positional must still be the last token (uv pip install <wheel>).
    assert argv[-1] == str(wheel)


# ---------- build_wheel ----------


def test_build_wheel_argv_default(fake_run: _FakeRun, tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"
    resolver.build_wheel(tmp_path, out_dir)
    assert fake_run.calls[0][0] == [
        "uv",
        "build",
        "--wheel",
        "--out-dir",
        str(out_dir),
    ]


def test_build_wheel_argv_with_all_packages(fake_run: _FakeRun, tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"
    resolver.build_wheel(tmp_path, out_dir, all_packages=True)
    assert fake_run.calls[0][0] == [
        "uv",
        "build",
        "--all-packages",
        "--wheel",
        "--out-dir",
        str(out_dir),
    ]


def test_build_wheel_appends_python_when_version_set(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    # D20: uv runs the project's PEP 517 build backend; --python takes a
    # version spec like "3.12" and uv auto-fetches a managed standalone
    # CPython if the requested version isn't locally installed. uv build
    # does NOT accept --python-version.
    out_dir = tmp_path / "dist"
    resolver.build_wheel(tmp_path, out_dir, python_version="3.12")
    argv = fake_run.calls[0][0]
    assert "--python" in argv
    assert argv[argv.index("--python") + 1] == "3.12"
    assert "--python-version" not in argv


def test_build_wheel_omits_python_by_default(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    resolver.build_wheel(tmp_path, tmp_path / "dist")
    assert "--python" not in fake_run.calls[0][0]


def test_build_wheel_uses_pinned_subprocess_kwargs(fake_run: _FakeRun, tmp_path: Path) -> None:
    resolver.build_wheel(tmp_path, tmp_path / "d")
    _, kwargs = fake_run.calls[0]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"] is not os.environ


def test_build_wheel_uv_binary_missing_raises_uv_not_found(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    fake_run.raises = FileNotFoundError(2, "No such file: 'uv'")
    with pytest.raises(UvNotFoundError):
        resolver.build_wheel(tmp_path, tmp_path / "d")


def test_build_wheel_failure_raises_wheel_artifact_error(
    fake_run: _FakeRun, tmp_path: Path
) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "uv build: failure"
    with pytest.raises(WheelArtifactError, match="uv build"):
        resolver.build_wheel(tmp_path, tmp_path / "d")


# ---------- argv constructed only inside resolver (CLAUDE.md invariant) ----------


def test_resolver_is_the_only_subprocess_caller(fake_run: _FakeRun, tmp_path: Path) -> None:
    # The CLAUDE.md/spec 02 §3 invariant: builder never calls subprocess directly.
    # Asserting indirectly: every public call here goes through fake_run, and we
    # verify exactly one subprocess.run invocation per public call.
    resolver.export(tmp_path, tmp_path / "r.txt")
    resolver.pip_install_target(tmp_path, tmp_path / "s", requirement=tmp_path / "r.txt")
    resolver.build_wheel(tmp_path, tmp_path / "d")
    assert len(fake_run.calls) == 3


# ---------- --verbose: spec 01 §8 `+ uv <argv>` echo ----------


def test_verbose_echoes_uv_argv_in_posix_shlex(
    fake_run: _FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec 01 §8: --verbose echoes `+ uv <argv>` on stderr per uv invocation."""
    resolver.export(tmp_path, tmp_path / "r.txt", verbosity=1)
    err = capsys.readouterr().err
    assert err.startswith("+ uv export "), err
    # Path is shlex-joined — on POSIX/Windows it appears verbatim when no
    # special chars; the key invariant is that the line begins with `+ uv `.


def test_default_verbosity_does_not_echo(
    fake_run: _FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver.export(tmp_path, tmp_path / "r.txt")  # default verbosity == 0
    err = capsys.readouterr().err
    assert "+ uv" not in err


def test_verbose_echo_for_each_resolver_call(
    fake_run: _FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver.export(tmp_path, tmp_path / "r.txt", verbosity=1)
    resolver.pip_install_target(
        tmp_path, tmp_path / "s", requirement=tmp_path / "r.txt", verbosity=1
    )
    resolver.build_wheel(tmp_path, tmp_path / "d", verbosity=1)
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(err_lines) == 3
    assert err_lines[0].startswith("+ uv export ")
    assert err_lines[1].startswith("+ uv pip install ")
    assert err_lines[2].startswith("+ uv build ")
