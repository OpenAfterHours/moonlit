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

    def fake_export(
        project_root: Path,
        output_file: Path,
        *,
        package: str | None = None,
        **_kwargs: object,
    ) -> None:
        state["calls"].append(("export", package))
        output_file.write_text("# fake reqs\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
        **_kwargs: object,
    ) -> None:
        state["calls"].append(("pip_install", requirement, wheel))
        target_dir.mkdir(parents=True, exist_ok=True)
        for arc, content in state["stage_files"].items():
            dest = target_dir / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            dest.write_bytes(data)

    def fake_build_wheel(
        project_root: Path,
        out_dir: Path,
        *,
        all_packages: bool = False,
        **_kwargs: object,
    ) -> None:
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


# ---------- §8 progress lines on stderr ----------


def test_quiet_emits_no_progress_on_stderr(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    """spec §8: --quiet suppresses stderr (progress + verbose echo)."""
    code, stdout, stderr = call_cli(
        "build", str(project_root), "-o", str(output_path), "-e", "myapp:main", "-q"
    )
    assert code == 0, stderr
    assert stderr == ""
    # Stdout success line still present (regression guard for I8).
    assert stdout.startswith("wrote ")


def test_default_emits_step_progress_on_stderr(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    """Default mode shows per-step labels on stderr (not a TTY in tests → plain `→`/`✓`)."""
    code, _, stderr = call_cli(
        "build", str(project_root), "-o", str(output_path), "-e", "myapp:main"
    )
    assert code == 0, stderr
    # Pytest captured streams are not TTYs → plain mode → `→` start lines.
    assert "→ freezing dependencies" in stderr
    assert "→ building wheels" in stderr
    assert "→ writing archive" in stderr
    # Success markers for the same steps.
    assert "✓ frozen" in stderr
    assert "✓ built" in stderr


def test_verbose_echoes_uv_argv_on_stderr(
    call_cli: Any, project_root: Path, output_path: Path
) -> None:
    """spec §8: --verbose emits `+ uv <argv>` on stderr per uv invocation."""
    code, _, stderr = call_cli(
        "build", str(project_root), "-o", str(output_path), "-e", "myapp:main", "-v"
    )
    assert code == 0, stderr
    # The fakes don't actually invoke uv, but the resolver wrappers still emit
    # the `+ uv ...` echo before short-circuiting to the fake. Wait: no — the
    # fake REPLACES resolver.export et al, so _run_uv is never called and no
    # echo fires. So under fakes, --verbose has the same stderr as default;
    # the echo is exercised in test_resolver.py instead.
    # We DO still expect the step lines to appear.
    assert "→ freezing dependencies" in stderr
    assert "✓ frozen" in stderr


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


# ---------- §2.3 `moonlit info` ----------


def _valid_env_dict(name: str = "myapp") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": name,
        "build_id": "a" * 64,
        "entry_point": f"{name}.cli:main",
        "built_at": "2026-05-09T00:00:00Z",
        "moonlit_version": "0.1.0",
        "python_shebang": "/usr/bin/env python3",
    }


def _serialize_env(env_dict: dict[str, Any]) -> bytes:
    # spec 05 §5: indent=2, sort_keys, no ensure_ascii, separators pinned, trailing \n.
    return (
        json.dumps(
            env_dict,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _make_pyz_with_env(
    path: Path,
    *,
    env_bytes: bytes | None,
    extra_entries: dict[str, bytes] | None = None,
) -> Path:
    """Hand-craft a minimal .pyz: env.json + a few site-packages entries.

    ``env_bytes=None`` produces an archive with no env.json member at all.
    """
    extra_entries = extra_entries or {}
    with open(path, "wb") as fp:
        fp.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("__main__.py", b"# stub\n")
            if env_bytes is not None:
                zf.writestr("env.json", env_bytes)
            for arc, content in extra_entries.items():
                zf.writestr(arc, content)
    return path


@pytest.fixture
def good_pyz(tmp_path: Path) -> Path:
    return _make_pyz_with_env(
        tmp_path / "app.pyz",
        env_bytes=_serialize_env(_valid_env_dict()),
        extra_entries={"site-packages/myapp/__init__.py": b"# myapp\n"},
    )


def test_info_help_short_circuits(call_cli: Any) -> None:
    code, stdout, _ = call_cli("info", "--help")
    assert code == 0
    assert "Usage" in stdout


def test_info_no_arg_exit_2(call_cli: Any) -> None:
    code, _, stderr = call_cli("info")
    assert code == 2
    assert "error:" in stderr


def test_info_default_prints_all_seven_fields(call_cli: Any, good_pyz: Path) -> None:
    code, stdout, stderr = call_cli("info", str(good_pyz))
    assert code == 0
    assert stderr == ""
    # Header line: <resolved_path> (<size>, <N> entries).
    first_line = stdout.splitlines()[0]
    assert str(good_pyz) in first_line
    assert "entries)" in first_line
    # Field listing: alphabetical, two-space indent, gutter to value column.
    for field in (
        "build_id",
        "built_at",
        "entry_point",
        "moonlit_version",
        "name",
        "python_shebang",
        "schema_version",
    ):
        assert field in stdout
    # Spot-check actual values appear.
    assert "a" * 64 in stdout
    assert "myapp.cli:main" in stdout
    assert "/usr/bin/env python3" in stdout
    assert "2026-05-09T00:00:00Z" in stdout


def test_info_field_listing_is_alphabetical(call_cli: Any, good_pyz: Path) -> None:
    code, stdout, _ = call_cli("info", str(good_pyz))
    assert code == 0
    lines = stdout.splitlines()[1:]  # drop header
    field_names = [line.strip().split()[0] for line in lines if line.strip()]
    assert field_names == sorted(field_names)


def test_info_json_emits_raw_env_bytes(call_cli: Any, tmp_path: Path) -> None:
    # spec §2.3: --json writes the raw env.json bytes from the archive to
    # stdout. The producer recipe (spec 05 §5) is sort_keys + indent=2 + utf-8
    # + trailing \n, so round-tripping through capsys's utf-8 decode is faithful.
    env_bytes = _serialize_env(_valid_env_dict())
    pyz = _make_pyz_with_env(tmp_path / "app.pyz", env_bytes=env_bytes)
    code, stdout, stderr = call_cli("info", str(pyz), "--json")
    assert code == 0
    assert stderr == ""
    assert stdout.encode("utf-8") == env_bytes


def test_info_missing_file_exit_2(call_cli: Any, tmp_path: Path) -> None:
    missing = tmp_path / "nope.pyz"
    code, _, stderr = call_cli("info", str(missing))
    assert code == 2
    assert "error:" in stderr
    assert str(missing) in stderr


def test_info_directory_exit_2(call_cli: Any, tmp_path: Path) -> None:
    a_dir = tmp_path / "x.pyz"
    a_dir.mkdir()
    code, _, stderr = call_cli("info", str(a_dir))
    assert code == 2
    assert "error:" in stderr


def test_info_not_a_zipfile_exit_12(call_cli: Any, tmp_path: Path) -> None:
    plain = tmp_path / "x.pyz"
    plain.write_bytes(b"not a zipfile")
    code, _, stderr = call_cli("info", str(plain))
    assert code == 12
    assert stderr.startswith("BadArchiveError:")
    assert "not a zipfile" in stderr or "not a moonlit" in stderr


def test_info_zip_without_env_json_exit_12(call_cli: Any, tmp_path: Path) -> None:
    pyz = _make_pyz_with_env(tmp_path / "x.pyz", env_bytes=None)
    code, _, stderr = call_cli("info", str(pyz))
    assert code == 12
    assert stderr.startswith("BadArchiveError:")
    assert "env.json missing from archive" in stderr


def test_info_malformed_env_json_exit_12_with_field_message(call_cli: Any, tmp_path: Path) -> None:
    bad = _valid_env_dict()
    bad["entry_point"] = "no_colon_here"
    pyz = _make_pyz_with_env(tmp_path / "x.pyz", env_bytes=_serialize_env(bad))
    code, _, stderr = call_cli("info", str(pyz))
    assert code == 12
    assert stderr.startswith("BadArchiveError:")
    assert "entry_point" in stderr


def test_info_json_validates_before_emitting(call_cli: Any, tmp_path: Path) -> None:
    # spec §2.3: --json still runs validation; a malformed env.json exits 12.
    bad = _valid_env_dict()
    bad["build_id"] = "not_64_hex"
    pyz = _make_pyz_with_env(tmp_path / "x.pyz", env_bytes=_serialize_env(bad))
    code, stdout, stderr = call_cli("info", str(pyz), "--json")
    assert code == 12
    assert stdout == ""
    assert "BadArchiveError:" in stderr


# ---------- --windows-exe flag (D19) ----------


def test_bundle_python_requires_windows_exe_invariant_i13(
    call_cli: Any, project_root: Path, tmp_path: Path
) -> None:
    # spec invariant I13 / D21: --bundle-python without --windows-exe is exit 2.
    # Two forms covered: a .pyz output and a .exe output (still no --windows-exe).
    out_pyz = tmp_path / "out" / "app.pyz"
    out_pyz.parent.mkdir()
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "--bundle-python",
        "-e",
        "myapp:main",
        "-o",
        str(out_pyz),
    )
    assert code == 2
    assert "--windows-exe" in stderr

    out_exe = tmp_path / "out" / "app.exe"
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "--bundle-python",
        "-e",
        "myapp:main",
        "-o",
        str(out_exe),
    )
    assert code == 2
    assert "--windows-exe" in stderr


def test_bundle_python_threads_into_build_config(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    tmp_path: Path,
) -> None:
    # --bundle-python combined with --windows-exe sets BuildConfig.bundle_python.
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "--bundle-python",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured: dict[str, Any] = {}

    def capture(config: Any) -> int:
        captured["bundle_python"] = config.bundle_python
        captured["windows_exe"] = config.windows_exe
        return 0  # short-circuit the pipeline; we're only testing the flag.

    monkeypatch.setattr(cli_module, "run_build", capture)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["windows_exe"] is True
    assert captured["bundle_python"] is True


def test_windows_exe_without_bundle_python_leaves_flag_false(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    tmp_path: Path,
) -> None:
    # Sanity: the default for --bundle-python is False, even with --windows-exe.
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured: dict[str, Any] = {}

    def capture(config: Any) -> int:
        captured["bundle_python"] = config.bundle_python
        return 0

    monkeypatch.setattr(cli_module, "run_build", capture)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["bundle_python"] is False


def test_windows_exe_with_pyz_suffix_exit_2_invariant_i12(
    call_cli: Any, project_root: Path, tmp_path: Path
) -> None:
    # spec invariant I12 / D19b: --windows-exe demands an .exe suffix.
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir()
    code, _, stderr = call_cli(
        "build",
        str(project_root),
        "--windows-exe",
        "-e",
        "myapp:main",
        "-o",
        str(out),
    )
    assert code == 2
    assert "error:" in stderr
    assert ".exe" in stderr


def test_windows_exe_accepts_uppercase_exe_suffix(
    call_cli: Any, project_root: Path, tmp_path: Path
) -> None:
    # D19b: .lower().endswith(".exe") so App.EXE is accepted.
    out = tmp_path / "out" / "App.EXE"
    out.parent.mkdir()
    code, _, _ = call_cli(
        "build",
        str(project_root),
        "--windows-exe",
        "-e",
        "myapp:main",
        "-o",
        str(out),
    )
    assert code == 0
    assert out.read_bytes()[:2] == b"MZ"


def test_windows_exe_default_shebang_pivots_to_python_exe(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    tmp_path: Path,
) -> None:
    # D19c: when --windows-exe is set and -p is left at default, the shebang
    # baked into the launcher payload is "python.exe", not the cross-platform
    # "/usr/bin/env python3".
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured: dict[str, Any] = {}
    real_build = cli_module.run_build

    def capture_and_run(config: Any) -> int:
        captured["python_shebang"] = config.python_shebang
        captured["windows_exe"] = config.windows_exe
        return real_build(config)

    monkeypatch.setattr(cli_module, "run_build", capture_and_run)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["windows_exe"] is True
    assert captured["python_shebang"] == "python.exe"


def test_windows_exe_explicit_python_flag_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    tmp_path: Path,
) -> None:
    # D19c: when -p is explicitly provided, --windows-exe does NOT override it.
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "-p",
            "C:\\custom\\python.exe",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured: dict[str, Any] = {}
    real_build = cli_module.run_build

    def capture_and_run(config: Any) -> int:
        captured["python_shebang"] = config.python_shebang
        return real_build(config)

    monkeypatch.setattr(cli_module, "run_build", capture_and_run)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["python_shebang"] == "C:\\custom\\python.exe"


# ---------- D20: --python-version (cross-interpreter builds) ----------


def _capture_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub run_build so we can inspect the BuildConfig the CLI assembles."""
    captured: dict[str, Any] = {}
    real_build = cli_module.run_build

    def capture_and_run(config: Any) -> int:
        captured["config"] = config
        return real_build(config)

    monkeypatch.setattr(cli_module, "run_build", capture_and_run)
    return captured


def test_python_version_threads_into_buildconfig(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--python-version",
            "3.12",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured = _capture_config(monkeypatch)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["config"].python_version == "3.12"


def test_python_version_default_is_none(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path
) -> None:
    # When the flag is omitted, BuildConfig.python_version stays None so
    # _build_env_dict falls back to the build host's sys.version_info.
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["moonlit", "build", str(project_root), "-e", "myapp:main", "-o", str(out)],
    )
    captured = _capture_config(monkeypatch)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["config"].python_version is None


@pytest.mark.parametrize("bad", ["3", "3.12.0", "py3.12", "", "3.x", "v3.12", " 3.12"])
def test_python_version_invalid_format_errors(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path, bad: str
) -> None:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--python-version",
            bad,
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main()
    assert excinfo.value.code == 2  # click UsageError → exit 2


def test_windows_exe_with_python_version_pivots_shebang_to_py_launcher(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path
) -> None:
    # D20: when --python-version is set in --windows-exe mode and -p is at
    # default, pivot the shebang to `py -X.Y` so the recipient's Windows
    # launcher pins to the matching interpreter.
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "--python-version",
            "3.12",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured = _capture_config(monkeypatch)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["config"].python_shebang == "py -3.12"


def test_windows_exe_with_python_version_respects_explicit_python(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, tmp_path: Path
) -> None:
    # An explicit -p still wins over the cross-version pivot.
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "moonlit",
            "build",
            str(project_root),
            "--windows-exe",
            "--python-version",
            "3.12",
            "-p",
            "C:\\custom\\python.exe",
            "-e",
            "myapp:main",
            "-o",
            str(out),
        ],
    )
    captured = _capture_config(monkeypatch)
    try:
        cli_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert captured["config"].python_shebang == "C:\\custom\\python.exe"
