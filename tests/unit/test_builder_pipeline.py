"""Pin moonlit.builder pass 1 (orchestration) to specs/02-build-pipeline.md §3.

Pass 1 covers: argument validation, workspace integration, output preflight,
resolver call sequence, wheel-artifact validation, console-script resolution,
build_id computation (delegated), atomic .pyz output rename (D15), tempdir
lifecycle (D17), POSIX chmod. Pass 2 (`test_builder_archive.py`) covers the
archive-assembly internals (zip contents, _bootstrap copy, env.json bytes,
__main__.py template).

Resolver subprocess calls are monkeypatched; mocks may side-effect the
staging tree to simulate uv (drop fake wheels in dist/, write requirements.txt,
extract dist-info into site-packages).
"""

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit import builder
from moonlit.builder import BuildConfig, build
from moonlit.errors import (
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

# ---------- fixtures ----------


def make_fake_wheel(
    path: Path,
    *,
    name: str,
    version: str = "0.1.0",
    console_scripts: dict[str, str] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        di = f"{name}-{version}.dist-info"
        zf.writestr(
            f"{di}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        zf.writestr(f"{di}/WHEEL", "Wheel-Version: 1.0\n")
        zf.writestr(f"{di}/RECORD", "")
        if console_scripts:
            ep = "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in console_scripts.items())
            zf.writestr(f"{di}/entry_points.txt", ep)


def write_pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A non-workspace project rooted with name=myapp."""
    root = tmp_path / "project"
    root.mkdir()
    write_pyproject(
        root, '[project]\nname = "myapp"\nversion = "0.1.0"\nrequires-python = ">=3.13"\n'
    )
    (root / "uv.lock").write_text("# fake\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A uv-workspace root with greeter + shouter members."""
    root = tmp_path / "ws"
    root.mkdir()
    write_pyproject(root, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    (root / "uv.lock").write_text("# fake\n", encoding="utf-8")
    for name in ("greeter", "shouter"):
        member = root / "packages" / name
        member.mkdir(parents=True)
        write_pyproject(member, f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    return root


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True)
    return out


@pytest.fixture
def fake_resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Monkeypatch resolver.* with stubs that record calls and side-effect tmp."""
    state: dict[str, Any] = {
        "calls": [],
        # Files to drop into target_dir during pip_install (path → bytes).
        "stage_files": {},
        # Wheels to drop into out_dir during build_wheel (filename, name, version,
        # console_scripts).
        "wheels_to_make": [
            ("myapp-0.1.0-py3-none-any.whl", "myapp", "0.1.0", None),
        ],
        # Files to drop into target_dir during pip_install of a wheel — keyed by
        # wheel filename. (Useful for staging dist-info content from a wheel.)
        "wheel_install_files": {},
    }

    def fake_export(project_root: Path, output_file: Path, *, package: str | None = None) -> None:
        state["calls"].append(("export", project_root, output_file, package))
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# fake reqs\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
    ) -> None:
        state["calls"].append(("pip_install", project_root, target_dir, requirement, wheel))
        target_dir.mkdir(parents=True, exist_ok=True)
        # On every pip_install call, lay down generic stage_files (simulates
        # uv installing transitive deps + the target package).
        for arcname, content in state["stage_files"].items():
            dest = target_dir / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            dest.write_bytes(data)
        # On wheel install, also lay down per-wheel files.
        if wheel is not None:
            wheel_name = Path(wheel).name
            for arcname, content in state["wheel_install_files"].get(wheel_name, {}).items():
                dest = target_dir / arcname
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                dest.write_bytes(data)

    def fake_build_wheel(project_root: Path, out_dir: Path, *, all_packages: bool = False) -> None:
        state["calls"].append(("build_wheel", project_root, out_dir, all_packages))
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, name, version, console_scripts in state["wheels_to_make"]:
            make_fake_wheel(
                out_dir / filename,
                name=name,
                version=version,
                console_scripts=console_scripts,
            )

    monkeypatch.setattr(builder.resolver, "export", fake_export)
    monkeypatch.setattr(builder.resolver, "pip_install_target", fake_pip_install_target)
    monkeypatch.setattr(builder.resolver, "build_wheel", fake_build_wheel)
    return state


def make_config(
    project_root: Path,
    output_path: Path,
    *,
    entry_point: str | None = "myapp.cli:main",
    console_script: str | None = None,
    python_shebang: str = "/usr/bin/env python3",
    package: str | None = None,
    force: bool = False,
    verbosity: int = 0,
) -> BuildConfig:
    return BuildConfig(
        project_root=project_root,
        output_path=output_path,
        entry_point=entry_point,
        console_script=console_script,
        python_shebang=python_shebang,
        package=package,
        force=force,
        verbosity=verbosity,
    )


# ---------- _validate_config ----------


def test_neither_entry_point_nor_console_script_raises_internal(
    project_root: Path, output_path: Path
) -> None:
    config = make_config(project_root, output_path, entry_point=None, console_script=None)
    with pytest.raises(InternalError):
        build(config)


def test_both_entry_point_and_console_script_raises_internal(
    project_root: Path, output_path: Path
) -> None:
    config = make_config(
        project_root,
        output_path,
        entry_point="x:y",
        console_script="myscript",
    )
    with pytest.raises(InternalError):
        build(config)


# ---------- _select_target ----------


def test_workspace_without_package_raises(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(workspace_root, output_path, package=None)
    with pytest.raises(MissingPackageError):
        build(config)


def test_non_workspace_with_package_raises(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, package="something")
    with pytest.raises(NotAWorkspaceError):
        build(config)


def test_workspace_with_unknown_package_raises(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(workspace_root, output_path, package="nonexistent")
    with pytest.raises(UnknownPackageError, match="nonexistent"):
        build(config)


def test_workspace_with_pep503_normalized_match_succeeds(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    # Workspace member is "shouter"; user passes "Shouter" (case-insensitive).
    fake_resolver["wheels_to_make"] = [
        ("shouter-0.1.0-py3-none-any.whl", "shouter", "0.1.0", None),
        ("greeter-0.1.0-py3-none-any.whl", "greeter", "0.1.0", None),
    ]
    config = make_config(workspace_root, output_path, package="Shouter")
    assert build(config) == 0


def test_non_workspace_missing_project_name_raises_malformed(
    tmp_path: Path, output_path: Path, fake_resolver: dict
) -> None:
    project_root = tmp_path / "noname"
    project_root.mkdir()
    write_pyproject(project_root, "# no [project] table\n")
    config = make_config(project_root, output_path)
    with pytest.raises(MalformedPyprojectError):
        build(config)


# ---------- entry-point format validation ----------


@pytest.mark.parametrize(
    "value", ["", "no_colon", "two:colons:here", "1bad:func", "a:1bad", "a.:b"]
)
def test_invalid_entry_point_raises_bad_entry_point(
    project_root: Path, output_path: Path, fake_resolver: dict, value: str
) -> None:
    config = make_config(project_root, output_path, entry_point=value)
    with pytest.raises(BadEntryPointError):
        build(config)


# ---------- _preflight_output ----------


def test_output_parent_does_not_exist_raises(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out = tmp_path / "missing_parent" / "app.pyz"
    config = make_config(project_root, out)
    with pytest.raises(OutputNotWritableError, match="parent directory does not exist"):
        build(config)


def test_output_path_is_a_directory_raises(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True)
    out.mkdir()  # output path itself is a dir
    config = make_config(project_root, out)
    with pytest.raises(OutputNotWritableError, match="not a regular file"):
        build(config)


def test_output_path_is_directory_force_does_not_override(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True)
    out.mkdir()
    config = make_config(project_root, out, force=True)
    with pytest.raises(OutputNotWritableError):
        build(config)


def test_output_exists_without_force_raises(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    output_path.write_bytes(b"existing\n")
    config = make_config(project_root, output_path, force=False)
    with pytest.raises(OutputExistsError, match="--force"):
        build(config)


def test_output_exists_with_force_overwrites(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    output_path.write_bytes(b"old content\n")
    config = make_config(project_root, output_path, force=True)
    assert build(config) == 0
    # The file is overwritten with the produced pyz.
    new_bytes = output_path.read_bytes()
    assert new_bytes != b"old content\n"
    assert new_bytes.startswith(b"#!")


# ---------- happy-path integration ----------


def test_non_workspace_build_calls_resolver_in_spec_order(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    assert build(config) == 0

    call_kinds = [c[0] for c in fake_resolver["calls"]]
    # Steps 3, 4, 5, 6.
    assert call_kinds == ["export", "pip_install", "build_wheel", "pip_install"]


def test_workspace_build_uses_all_packages_and_installs_each_wheel(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheels_to_make"] = [
        ("greeter-0.1.0-py3-none-any.whl", "greeter", "0.1.0", None),
        ("shouter-0.1.0-py3-none-any.whl", "shouter", "0.1.0", None),
    ]
    config = make_config(workspace_root, output_path, package="shouter")
    assert build(config) == 0

    # Step 3 uses --package shouter; step 5 uses --all-packages; step 6 calls
    # pip_install once per wheel.
    export_call = next(c for c in fake_resolver["calls"] if c[0] == "export")
    assert export_call[3] == "shouter"
    bw_call = next(c for c in fake_resolver["calls"] if c[0] == "build_wheel")
    assert bw_call[3] is True  # all_packages
    pip_calls = [c for c in fake_resolver["calls"] if c[0] == "pip_install"]
    # 1 for requirements + 1 per wheel = 3.
    assert len(pip_calls) == 3


def test_export_does_not_pass_package_for_non_workspace(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    export_call = next(c for c in fake_resolver["calls"] if c[0] == "export")
    assert export_call[3] is None


def test_build_wheel_all_packages_false_for_non_workspace(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    bw_call = next(c for c in fake_resolver["calls"] if c[0] == "build_wheel")
    assert bw_call[3] is False


# ---------- _validate_wheels ----------


def test_no_wheels_produced_raises(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheels_to_make"] = []
    config = make_config(project_root, output_path)
    with pytest.raises(WheelArtifactError, match="no wheels"):
        build(config)


def test_non_workspace_multiple_wheels_raises(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheels_to_make"] = [
        ("myapp-0.1.0-py3-none-any.whl", "myapp", "0.1.0", None),
        ("extra-0.1.0-py3-none-any.whl", "extra", "0.1.0", None),
    ]
    config = make_config(project_root, output_path)
    with pytest.raises(WheelArtifactError, match="expected 1"):
        build(config)


def test_non_workspace_wheel_name_mismatch_raises(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheels_to_make"] = [
        ("wrong-0.1.0-py3-none-any.whl", "wrong", "0.1.0", None),
    ]
    config = make_config(project_root, output_path)
    with pytest.raises(WheelArtifactError, match="wrong"):
        build(config)


def test_non_workspace_wheel_pep503_normalized_match_succeeds(
    tmp_path: Path, output_path: Path, fake_resolver: dict
) -> None:
    # Project name "My_App" should match a wheel named "my-app".
    project_root = tmp_path / "project"
    project_root.mkdir()
    write_pyproject(project_root, '[project]\nname = "My_App"\n')
    (project_root / "uv.lock").write_text("# fake\n")
    fake_resolver["wheels_to_make"] = [
        ("my_app-0.1.0-py3-none-any.whl", "my-app", "0.1.0", None),
    ]
    config = make_config(project_root, output_path)
    assert build(config) == 0


def test_workspace_does_not_validate_wheel_name_match(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    # Spec D2: workspaces overbuild; we just install all wheels.
    fake_resolver["wheels_to_make"] = [
        ("greeter-0.1.0-py3-none-any.whl", "greeter", "0.1.0", None),
        ("shouter-0.1.0-py3-none-any.whl", "shouter", "0.1.0", None),
        ("extra-0.1.0-py3-none-any.whl", "extra", "0.1.0", None),
    ]
    config = make_config(workspace_root, output_path, package="shouter")
    assert build(config) == 0


# ---------- console-script resolution (step 7) ----------


def test_console_script_resolves_to_entry_point(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheel_install_files"] = {
        "myapp-0.1.0-py3-none-any.whl": {
            "myapp-0.1.0.dist-info/entry_points.txt": (
                "[console_scripts]\nmyapp = myapp.cli:main\n"
            ),
            "myapp-0.1.0.dist-info/METADATA": "Name: myapp\nVersion: 0.1.0\n",
        }
    }
    config = make_config(project_root, output_path, entry_point=None, console_script="myapp")
    assert build(config) == 0

    # env.json (written by pass-1 placeholder) should record the resolved entry_point.
    env = _read_env_json(output_path)
    assert env["entry_point"] == "myapp.cli:main"


def test_console_script_not_found_lists_available(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheel_install_files"] = {
        "myapp-0.1.0-py3-none-any.whl": {
            "myapp-0.1.0.dist-info/entry_points.txt": ("[console_scripts]\nfoo = m:f\nbar = m:b\n"),
        }
    }
    config = make_config(project_root, output_path, entry_point=None, console_script="missing")
    with pytest.raises(ConsoleScriptNotFoundError) as excinfo:
        build(config)
    msg = str(excinfo.value)
    assert "missing" in msg
    assert "foo" in msg
    assert "bar" in msg


def test_console_script_not_found_when_no_console_scripts_declared(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, entry_point=None, console_script="anything")
    with pytest.raises(ConsoleScriptNotFoundError, match="--entry-point"):
        build(config)


def test_console_script_ambiguous_lists_files(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["stage_files"] = {
        "pkg_a-0.1.0.dist-info/entry_points.txt": ("[console_scripts]\nshared = a:main\n"),
        "pkg_b-0.1.0.dist-info/entry_points.txt": ("[console_scripts]\nshared = b:main\n"),
    }
    config = make_config(project_root, output_path, entry_point=None, console_script="shared")
    with pytest.raises(ConsoleScriptNotFoundError, match="ambiguous"):
        build(config)


def test_console_script_value_validated_as_entry_point(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["wheel_install_files"] = {
        "myapp-0.1.0-py3-none-any.whl": {
            "myapp-0.1.0.dist-info/entry_points.txt": (
                "[console_scripts]\nmyapp = malformed value with spaces\n"
            ),
        }
    }
    config = make_config(project_root, output_path, entry_point=None, console_script="myapp")
    with pytest.raises(BadEntryPointError):
        build(config)


# ---------- env.json contents (pass-1 placeholder writes it) ----------


def test_env_json_includes_target_name_and_build_id(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, entry_point="myapp.cli:main")
    build(config)
    env = _read_env_json(output_path)
    assert env["schema_version"] == 1
    assert env["name"] == "myapp"
    assert env["entry_point"] == "myapp.cli:main"
    assert env["python_shebang"] == "/usr/bin/env python3"
    assert len(env["build_id"]) == 64
    int(env["build_id"], 16)
    assert env["moonlit_version"]


def test_env_json_uses_raw_workspace_member_name(
    workspace_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    # Raw member name is "shouter" (from pyproject.toml). User passes "Shouter".
    fake_resolver["wheels_to_make"] = [
        ("greeter-0.1.0-py3-none-any.whl", "greeter", "0.1.0", None),
        ("shouter-0.1.0-py3-none-any.whl", "shouter", "0.1.0", None),
    ]
    config = make_config(
        workspace_root, output_path, entry_point="shouter.cli:main", package="Shouter"
    )
    build(config)
    env = _read_env_json(output_path)
    assert env["name"] == "shouter"  # raw, per D5


# ---------- archive output side effects ----------


def test_pyz_starts_with_shebang_prefix(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(
        project_root,
        output_path,
        entry_point="myapp:main",
        python_shebang="/custom/python",
    )
    build(config)
    assert output_path.read_bytes().startswith(b"#!/custom/python\n")


def test_pyz_is_a_valid_zipfile_after_shebang(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, entry_point="myapp:main")
    build(config)
    assert zipfile.is_zipfile(output_path)


def test_atomic_rename_does_not_leave_tmp_sibling_on_success(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, entry_point="myapp:main")
    build(config)
    siblings = list(output_path.parent.iterdir())
    tmp_siblings = [p for p in siblings if ".tmp." in p.name]
    assert tmp_siblings == []


def test_atomic_rename_cleans_tmp_on_archive_failure(
    project_root: Path,
    output_path: Path,
    fake_resolver: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated archive write failure")

    monkeypatch.setattr(builder, "_create_archive", boom)
    config = make_config(project_root, output_path, entry_point="myapp:main")
    with pytest.raises(RuntimeError, match="simulated archive write failure"):
        build(config)
    siblings = list(output_path.parent.iterdir())
    tmp_siblings = [p for p in siblings if ".tmp." in p.name]
    assert tmp_siblings == []
    assert not output_path.exists()


# ---------- tempdir lifecycle (D17) ----------


def test_tempdir_cleaned_on_success(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, entry_point="myapp:main")
    build(config)
    leftover = [p for p in Path(_tempdir()).iterdir() if p.name.startswith("moonlit-build-")]
    assert leftover == []


def test_tempdir_cleaned_on_resolver_failure(
    project_root: Path,
    output_path: Path,
    fake_resolver: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_export(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated export failure")

    monkeypatch.setattr(builder.resolver, "export", fake_export)
    config = make_config(project_root, output_path, entry_point="myapp:main")
    with pytest.raises(RuntimeError):
        build(config)
    leftover = [p for p in Path(_tempdir()).iterdir() if p.name.startswith("moonlit-build-")]
    assert leftover == []


# ---------- POSIX chmod ----------


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only chmod")
def test_chmod_0o755_on_posix(project_root: Path, output_path: Path, fake_resolver: dict) -> None:
    config = make_config(project_root, output_path, entry_point="myapp:main")
    build(config)
    mode = output_path.stat().st_mode & 0o777
    assert mode == 0o755


# ---------- helpers ----------


def _read_env_json(pyz: Path) -> dict:
    with zipfile.ZipFile(pyz, "r") as zf:
        return json.loads(zf.read("env.json").decode("utf-8"))


def _tempdir() -> str:
    import tempfile

    return tempfile.gettempdir()
