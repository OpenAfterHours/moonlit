"""Pin moonlit.cli to specs/01-cli.md (preflight order, exit codes, error format).

Tests invoke :func:`moonlit.cli.main` directly with monkeypatched ``sys.argv``
so that the top-level error-translation layer (Click → ``error: <msg>``,
``MoonlitError`` → ``<ClassName>: <message>``) is exercised end-to-end.
``shutil.which("uv")`` is autouse-mocked to a fake path and the resolver
subprocess wrappers are stubbed so no real ``uv`` is invoked.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit import builder
from moonlit import cli as cli_module

# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def _fake_uv_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: pretend ``uv`` is on PATH. Tests can override via monkeypatch."""
    real_which = shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        if name == "uv":
            return "/fake/uv"
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def _make_fake_wheel(path: Path, *, name: str, version: str = "0.1.0") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        di = f"{name}-{version}.dist-info"
        zf.writestr(
            f"{di}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        zf.writestr(f"{di}/WHEEL", "Wheel-Version: 1.0\n")
        zf.writestr(f"{di}/RECORD", "")


@pytest.fixture(autouse=True)
def _fake_resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default resolver stubs that simulate a non-workspace ``myapp`` build."""
    state: dict[str, Any] = {
        "calls": [],
        "stage_files": {},
        "wheels_to_make": [
            ("myapp-0.1.0-py3-none-any.whl", "myapp", "0.1.0"),
        ],
    }

    def fake_export(project_root: Path, output_file: Path, *, package: str | None = None) -> None:
        state["calls"].append(("export", package))
        output_file.write_text("# fake reqs\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
    ) -> None:
        state["calls"].append(("pip_install", requirement, wheel))
        target_dir.mkdir(parents=True, exist_ok=True)
        for arc, content in state["stage_files"].items():
            dest = target_dir / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            dest.write_bytes(data)

    def fake_build_wheel(project_root: Path, out_dir: Path, *, all_packages: bool = False) -> None:
        state["calls"].append(("build_wheel", all_packages))
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, name, version in state["wheels_to_make"]:
            _make_fake_wheel(out_dir / filename, name=name, version=version)

    monkeypatch.setattr(builder.resolver, "export", fake_export)
    monkeypatch.setattr(builder.resolver, "pip_install_target", fake_pip_install_target)
    monkeypatch.setattr(builder.resolver, "build_wheel", fake_build_wheel)
    return state


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nversion = "0.1.0"\nrequires-python = ">=3.13"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("# fake\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text("# fake\n", encoding="utf-8")
    for name in ("greeter", "shouter"):
        member = root / "packages" / name
        member.mkdir(parents=True)
        (member / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
        )
    return root


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True)
    return out


@pytest.fixture
def call_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    """Invoke moonlit.cli.main() with the given argv; return (exit_code, stdout, stderr)."""

    def _run(*args: str) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["moonlit", *args])
        exit_code = 0
        try:
            cli_module.main()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 0
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return _run


# ---------- §2.1 top-level surface ----------


def test_no_args_prints_help_to_stderr_exit_2(call_cli: Any) -> None:
    code, stdout, stderr = call_cli()
    assert code == 2
    assert stderr  # help printed to stderr
    assert stdout == ""


def test_top_level_help_long_to_stdout_exit_0(call_cli: Any) -> None:
    code, stdout, stderr = call_cli("--help")
    assert code == 0
    assert "Usage" in stdout
    assert stderr == ""


def test_top_level_help_short_to_stdout_exit_0(call_cli: Any) -> None:
    code, stdout, _ = call_cli("-h")
    assert code == 0
    assert "Usage" in stdout


def test_version_long_prints_to_stdout(call_cli: Any) -> None:
    code, stdout, _ = call_cli("--version")
    assert code == 0
    assert stdout.startswith("moonlit ")


def test_version_short_prints_to_stdout(call_cli: Any) -> None:
    code, stdout, _ = call_cli("-V")
    assert code == 0
    assert stdout.startswith("moonlit ")


def test_unknown_subcommand_exit_2_with_specific_message(call_cli: Any) -> None:
    code, _, stderr = call_cli("nope")
    assert code == 2
    # spec §2.1 literal: "error: no such subcommand: <name>".
    assert "error: no such subcommand: nope" in stderr


def test_unknown_subcommand_with_help_still_errors(call_cli: Any) -> None:
    # spec invariant I10: --help does NOT redeem an unknown subcommand.
    code, _, stderr = call_cli("nope", "--help")
    assert code == 2
    assert "no such subcommand: nope" in stderr


def test_unknown_top_level_option_exit_2(call_cli: Any) -> None:
    code, _, stderr = call_cli("--bogus")
    assert code == 2
    assert "error:" in stderr


# ---------- §3 flag interactions (exit 2) ----------


def test_neither_e_nor_c_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    # spec invariant I1.
    code, _, stderr = call_cli("build", str(project_root), "-o", str(output_path))
    assert code == 2
    assert "exactly one of" in stderr


def test_both_e_and_c_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    # spec invariant I1.
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "-c",
        "myscript",
    )
    assert code == 2


def test_quiet_and_verbose_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "-q",
        "-v",
    )
    assert code == 2
    assert "mutually exclusive" in stderr


def test_no_dev_and_dev_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "--no-dev",
        "--dev",
    )
    assert code == 2
    assert "mutually exclusive" in stderr


def test_missing_o_exit_2(call_cli: Any, project_root: Path) -> None:
    code, _, _ = call_cli("build", str(project_root), "-e", "x:y")
    assert code == 2


def test_unknown_build_option_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "--bogus",
    )
    assert code == 2
    assert "error:" in stderr


# ---------- §4 preflight checks ----------


def test_project_does_not_exist_exit_2(call_cli: Any, output_path: Path, tmp_path: Path) -> None:
    code, _, _ = call_cli(
        "build",
        str(tmp_path / "missing"),
        "-o",
        str(output_path),
        "-e",
        "x:y",
    )
    assert code == 2


def test_uv_not_on_path_exit_3(
    call_cli: Any,
    project_root: Path,
    output_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *_a, **_kw: None)
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
    )
    assert code == 3
    assert "UvNotFoundError:" in stderr


def test_missing_uv_lock_exit_4(call_cli: Any, output_path: Path, tmp_path: Path) -> None:
    proj = tmp_path / "no_lock"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    code, _, stderr = call_cli("build", str(proj), "-o", str(output_path), "-e", "x:y")
    assert code == 4
    assert "NoLockfileError:" in stderr


def test_missing_pyproject_exit_5(call_cli: Any, output_path: Path, tmp_path: Path) -> None:
    proj = tmp_path / "no_pyproject"
    proj.mkdir()
    code, _, stderr = call_cli("build", str(proj), "-o", str(output_path), "-e", "x:y")
    assert code == 5
    assert "MalformedPyprojectError:" in stderr


def test_uv_missing_takes_precedence_over_uv_lock_missing(
    call_cli: Any,
    output_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # spec invariant I2: missing uv AND missing uv.lock → exit 3, not 4.
    proj = tmp_path / "double_fault"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    # No uv.lock.
    monkeypatch.setattr(shutil, "which", lambda *_a, **_kw: None)
    code, _, _ = call_cli("build", str(proj), "-o", str(output_path), "-e", "x:y")
    assert code == 3


# ---------- §4 step 6: workspace shape ----------


def test_workspace_without_package_exit_5(
    call_cli: Any, workspace_root: Path, output_path: Path
) -> None:
    code, _, stderr = call_cli("build", str(workspace_root), "-o", str(output_path), "-e", "x:y")
    assert code == 5
    assert "MissingPackageError:" in stderr


def test_non_workspace_with_package_exit_5(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "--package",
        "x",
    )
    assert code == 5
    assert "NotAWorkspaceError:" in stderr


def test_unknown_package_exit_5_lists_member_names(
    call_cli: Any, workspace_root: Path, output_path: Path
) -> None:
    code, _, stderr = call_cli(
        "build",
        str(workspace_root),
        "-o",
        str(output_path),
        "-e",
        "x:y",
        "--package",
        "nonexistent",
    )
    assert code == 5
    assert "UnknownPackageError:" in stderr
    # spec §7: lists raw member names sorted ascending.
    msg = stderr
    assert "greeter" in msg
    assert "shouter" in msg
    assert msg.index("greeter") < msg.index("shouter")


def test_pep503_normalized_package_match_succeeds(
    call_cli: Any,
    workspace_root: Path,
    output_path: Path,
    _fake_resolver: dict,
) -> None:
    # spec invariant I5.
    _fake_resolver["wheels_to_make"] = [
        ("greeter-0.1.0-py3-none-any.whl", "greeter", "0.1.0"),
        ("shouter-0.1.0-py3-none-any.whl", "shouter", "0.1.0"),
    ]
    code, _, _ = call_cli(
        "build",
        str(workspace_root),
        "-o",
        str(output_path),
        "-e",
        "shouter.cli:main",
        "--package",
        "Shouter",
    )
    assert code == 0


# ---------- §4 step 7: entry-point syntactic validity ----------


def test_invalid_entry_point_exit_6(call_cli: Any, project_root: Path, output_path: Path) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "no_colon",
    )
    assert code == 6
    assert "BadEntryPointError:" in stderr


# ---------- §4 step 8: output preflight ----------


def test_output_path_is_directory_exit_7(call_cli: Any, project_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out_as_dir.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    code, _, stderr = call_cli("build", str(project_root), "-o", str(out), "-e", "x:y")
    assert code == 7
    assert "OutputNotWritableError:" in stderr


def test_output_exists_without_force_exit_7(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"old\n")
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
    )
    assert code == 7
    # spec invariant I3: distinct class for "exists vs not writable".
    assert "OutputExistsError:" in stderr


def test_force_does_not_override_directory_target(
    call_cli: Any, project_root: Path, tmp_path: Path
) -> None:
    # spec invariant I4.
    out = tmp_path / "outdir.pyz"
    out.mkdir()
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(out),
        "-e",
        "x:y",
        "--force",
    )
    assert code == 7
    assert "OutputNotWritableError:" in stderr


def test_force_overwrites_existing_regular_file(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"old\n")
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "--force",
    )
    assert code == 0
    assert output_path.read_bytes() != b"old\n"


def test_output_parent_does_not_exist_exit_7(
    call_cli: Any, project_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "no_parent_dir" / "app.pyz"
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(out),
        "-e",
        "myapp:main",
    )
    assert code == 7
    assert "OutputNotWritableError:" in stderr


# ---------- §7 error-message format ----------


def test_moonlit_error_format_class_colon_message(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"existing\n")
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
    )
    assert code == 7
    # spec §7: <ClassName>: <message>, single line.
    err_line = stderr.strip().splitlines()[0]
    assert err_line.startswith("OutputExistsError: ")


def test_parser_level_error_format_starts_with_error_colon(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    code, _, stderr = call_cli("build", str(project_root), "-o", str(output_path))
    assert code == 2
    assert stderr.startswith("error: ")


def test_quiet_does_not_suppress_errors(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"x\n")
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "-q",
    )
    assert code == 7
    assert "OutputExistsError:" in stderr


def test_verbose_appends_traceback_to_error(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"x\n")
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "-v",
    )
    assert code == 7
    assert "OutputExistsError:" in stderr
    assert "Traceback" in stderr


def test_no_verbose_suppresses_traceback(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    output_path.write_bytes(b"x\n")
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
    )
    assert code == 7
    assert "Traceback" not in stderr


# ---------- success path & spec invariant I8 ----------


def test_successful_build_exits_0_and_prints_success_line(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    code, stdout, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
    )
    assert code == 0, stderr
    assert output_path.exists()
    assert stdout.startswith("wrote ")
    assert " entries)" in stdout


def test_quiet_preserves_stdout_success_line(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    # spec §8: --quiet suppresses stderr but preserves stdout success line.
    code, stdout, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "-q",
    )
    assert code == 0
    assert stdout.startswith("wrote ")


# ---------- §3.5 / I6: MOONLIT_* env vars ignored at build time ----------


def test_moonlit_entry_point_env_var_ignored_at_build_time(
    call_cli: Any,
    project_root: Path,
    output_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # spec invariant I6: setting MOONLIT_ENTRY_POINT during a build is ignored;
    # the produced env.json records the -e value, not the env var.
    monkeypatch.setenv("MOONLIT_ENTRY_POINT", "should:be_ignored")
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "real.module:main",
    )
    assert code == 0
    with zipfile.ZipFile(output_path, "r") as zf:
        env = json.loads(zf.read("env.json").decode("utf-8"))
    assert env["entry_point"] == "real.module:main"


# ---------- shebang validation ----------


def test_invalid_shebang_with_newline_exit_2(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "-p",
        "/usr/bin/python\nmalicious",
    )
    assert code == 2
    assert "error:" in stderr


def test_overlong_shebang_exit_2(call_cli: Any, project_root: Path, output_path: Path) -> None:
    overlong = "/usr/bin/" + "x" * 200
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
        "-p",
        overlong,
    )
    assert code == 2


# ---------- I9: --help short-circuits validation ----------


def test_build_help_works_without_pyproject(call_cli: Any, tmp_path: Path) -> None:
    # spec invariant I9: build --help in a directory with no pyproject.toml → exit 0.
    code, stdout, _ = call_cli("build", "--help", str(tmp_path))
    assert code == 0
    assert "Usage" in stdout


# ---------- I7: SIGINT cleanup (proxy via KeyboardInterrupt during build) ----------


def test_keyboard_interrupt_during_build_exits_130(
    call_cli: Any,
    project_root: Path,
    output_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_kw: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(builder.resolver, "export", boom)
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "-o",
        str(output_path),
        "-e",
        "myapp:main",
    )
    assert code == 130
    assert not output_path.exists()
    siblings = list(output_path.parent.iterdir())
    assert all(".tmp." not in p.name for p in siblings)


# ---------- python -m moonlit ----------


def test_python_dash_m_invokes_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # __main__.py imports moonlit.cli.main; invoking it should run the same
    # path as `moonlit ...`. We verify by importing __main__ as a module and
    # checking the binding.
    import moonlit.__main__ as dunder_main

    assert dunder_main.main is cli_module.main
