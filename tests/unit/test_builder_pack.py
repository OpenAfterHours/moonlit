"""Pin moonlit.builder.pack() to specs/02-build-pipeline.md §3b + D25.

`pack` is the project-less front half: synthesize requirements.in → uv pip
compile → uv pip install --target --no-deps → shared back half (entry point,
build_id, env.json, archive). It must NOT call `uv export` or `uv build`.

Resolver subprocess calls are monkeypatched; the compile fake writes a fake
requirements.txt and the install fake lays down a staged site-packages tree.
"""

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit import builder
from moonlit.builder import PackConfig, pack
from moonlit.errors import (
    BadEntryPointError,
    ConsoleScriptNotFoundError,
    InternalError,
    OutputExistsError,
    OutputNotWritableError,
)

# ---------- fixtures ----------


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "mooring.pyz"
    out.parent.mkdir(parents=True)
    return out


_DEFAULT_STAGE_FILES = {
    "mooring/__init__.py": "VALUE = 1\n",
    "mooring-1.0.dist-info/entry_points.txt": "[console_scripts]\nmooring = mooring.cli:main\n",
    "mooring-1.0.dist-info/METADATA": "Name: mooring\nVersion: 1.0\n",
}


@pytest.fixture
def fake_resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "compiled_srcs": None,
        "requirements_in_text": None,
        "stage_files": dict(_DEFAULT_STAGE_FILES),
    }

    def fake_compile(
        cwd: Path,
        src_files: list[Path],
        output_file: Path,
        *,
        python_version: str | None = None,
        **_kw: object,
    ) -> None:
        state["calls"].append(("compile", cwd, list(src_files), output_file, python_version))
        state["compiled_srcs"] = [Path(s) for s in src_files]
        first = Path(src_files[0]) if src_files else None
        if first is not None and first.exists():
            state["requirements_in_text"] = first.read_text(encoding="utf-8")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("# compiled\nmooring==1.0\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
        python_version: str | None = None,
        **_kw: object,
    ) -> None:
        state["calls"].append(
            ("pip_install", project_root, target_dir, requirement, wheel, python_version)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        for arcname, content in state["stage_files"].items():
            dest = target_dir / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            dest.write_bytes(data)

    def fake_export(*_a: object, **_kw: object) -> None:
        state["calls"].append(("export",))
        raise AssertionError("pack must not call uv export")

    def fake_build_wheel(*_a: object, **_kw: object) -> None:
        state["calls"].append(("build_wheel",))
        raise AssertionError("pack must not call uv build")

    monkeypatch.setattr(builder.resolver, "compile_requirements", fake_compile)
    monkeypatch.setattr(builder.resolver, "pip_install_target", fake_pip_install_target)
    monkeypatch.setattr(builder.resolver, "export", fake_export)
    monkeypatch.setattr(builder.resolver, "build_wheel", fake_build_wheel)
    return state


def make_pack_config(
    output_path: Path,
    *,
    specs: tuple[str, ...] = ("mooring",),
    requirement_files: tuple[Path, ...] = (),
    name: str = "mooring",
    entry_point: str | None = "mooring.cli:main",
    console_script: str | None = None,
    python_shebang: str = "/usr/bin/env python3",
    force: bool = False,
    verbosity: int = 0,
    python_version: str | None = None,
) -> PackConfig:
    return PackConfig(
        specs=specs,
        requirement_files=requirement_files,
        name=name,
        output_path=output_path,
        entry_point=entry_point,
        console_script=console_script,
        python_shebang=python_shebang,
        force=force,
        verbosity=verbosity,
        python_version=python_version,
    )


def _read_env_json(pyz: Path) -> dict:
    with zipfile.ZipFile(pyz, "r") as zf:
        return json.loads(zf.read("env.json").decode("utf-8"))


# ---------- resolver call sequence (D25b/D25c) ----------


def test_pack_calls_compile_then_install(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    assert pack(config) == 0
    kinds = [c[0] for c in fake_resolver["calls"]]
    assert kinds == ["compile", "pip_install"]


def test_pack_never_calls_export_or_build_wheel(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    kinds = [c[0] for c in fake_resolver["calls"]]
    assert "export" not in kinds
    assert "build_wheel" not in kinds


def test_pack_install_uses_no_deps_requirement(output_path: Path, fake_resolver: dict) -> None:
    # The install must be requirement-based (the compiled closure), not a wheel.
    config = make_pack_config(output_path)
    pack(config)
    install_call = next(c for c in fake_resolver["calls"] if c[0] == "pip_install")
    _, _project_root, _target, requirement, wheel, _pyver = install_call
    assert requirement is not None
    assert wheel is None


# ---------- requirements.in synthesis (Pack-step 1, D25a) ----------


def test_pack_writes_specs_into_requirements_in(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, specs=("mooring", "polars>=1.0"))
    pack(config)
    text = fake_resolver["requirements_in_text"]
    assert text is not None
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == ["mooring", "polars>=1.0"]


def test_pack_requirement_files_follow_requirements_in(
    output_path: Path, fake_resolver: dict, tmp_path: Path
) -> None:
    reqs = tmp_path / "extra-requirements.txt"
    reqs.write_text("rich\n", encoding="utf-8")
    config = make_pack_config(output_path, specs=("mooring",), requirement_files=(reqs,))
    pack(config)
    srcs = fake_resolver["compiled_srcs"]
    # First src is the synthesized requirements.in; user files follow, in order.
    assert srcs[0].name == "requirements.in"
    assert srcs[1] == reqs


def test_pack_requirement_files_only_no_specs(
    output_path: Path, fake_resolver: dict, tmp_path: Path
) -> None:
    reqs = tmp_path / "extra-requirements.txt"
    reqs.write_text("mooring\n", encoding="utf-8")
    config = make_pack_config(output_path, specs=(), requirement_files=(reqs,), name="mooring")
    assert pack(config) == 0
    # requirements.in is still synthesized (empty) and the user file follows.
    srcs = fake_resolver["compiled_srcs"]
    assert srcs[0].name == "requirements.in"
    assert reqs in srcs


# ---------- cross-interpreter threading (D20/D25b) ----------


def test_pack_threads_python_version_into_compile_and_install(
    output_path: Path, fake_resolver: dict
) -> None:
    config = make_pack_config(output_path, python_version="3.12")
    pack(config)
    compile_call = next(c for c in fake_resolver["calls"] if c[0] == "compile")
    assert compile_call[4] == "3.12"  # python_version passed to compile
    install_call = next(c for c in fake_resolver["calls"] if c[0] == "pip_install")
    assert install_call[5] == "3.12"  # python_version passed to install


# ---------- env.json (D25d) ----------


def test_pack_env_json_uses_config_name(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, name="mooring")
    pack(config)
    env = _read_env_json(output_path)
    assert env["name"] == "mooring"
    assert env["entry_point"] == "mooring.cli:main"
    assert env["schema_version"] == 1
    assert len(env["build_id"]) == 64
    int(env["build_id"], 16)


def test_pack_env_json_records_python_version(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    env = _read_env_json(output_path)
    assert env["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_pack_env_json_has_gc_defaults(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    env = _read_env_json(output_path)
    assert env["gc"] == {"enabled": True, "keep_latest": 2, "grace_seconds": 86400}


# ---------- console-script resolution (shared back half) ----------


def test_pack_resolves_console_script(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, entry_point=None, console_script="mooring")
    assert pack(config) == 0
    env = _read_env_json(output_path)
    assert env["entry_point"] == "mooring.cli:main"


def test_pack_console_script_not_found_raises(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, entry_point=None, console_script="nope")
    with pytest.raises(ConsoleScriptNotFoundError):
        pack(config)


# ---------- config validation ----------


def test_pack_neither_entry_nor_console_raises_internal(
    output_path: Path, fake_resolver: dict
) -> None:
    config = make_pack_config(output_path, entry_point=None, console_script=None)
    with pytest.raises(InternalError):
        pack(config)


def test_pack_both_entry_and_console_raises_internal(
    output_path: Path, fake_resolver: dict
) -> None:
    config = make_pack_config(output_path, entry_point="a:b", console_script="mooring")
    with pytest.raises(InternalError):
        pack(config)


def test_pack_no_sources_raises_internal(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, specs=(), requirement_files=())
    with pytest.raises(InternalError):
        pack(config)


def test_pack_invalid_entry_point_raises(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, entry_point="no_colon")
    with pytest.raises(BadEntryPointError):
        pack(config)


def test_pack_invalid_name_raises_internal(output_path: Path, fake_resolver: dict) -> None:
    # The CLI validates --name as a usage error; the builder guards too (a bad
    # name here means a CLI bug). An invalid env.json name must never ship.
    config = make_pack_config(output_path, name="not a valid name")
    with pytest.raises(InternalError):
        pack(config)


# ---------- output preflight reuse ----------


def test_pack_output_exists_without_force_raises(output_path: Path, fake_resolver: dict) -> None:
    output_path.write_bytes(b"existing\n")
    config = make_pack_config(output_path, force=False)
    with pytest.raises(OutputExistsError):
        pack(config)


def test_pack_output_exists_with_force_overwrites(output_path: Path, fake_resolver: dict) -> None:
    output_path.write_bytes(b"old\n")
    config = make_pack_config(output_path, force=True)
    assert pack(config) == 0
    assert output_path.read_bytes().startswith(b"#!")


def test_pack_output_parent_missing_raises(tmp_path: Path, fake_resolver: dict) -> None:
    out = tmp_path / "missing" / "mooring.pyz"
    config = make_pack_config(out)
    with pytest.raises(OutputNotWritableError):
        pack(config)


# ---------- archive output ----------


def test_pack_pyz_starts_with_shebang(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path, python_shebang="/custom/python")
    pack(config)
    assert output_path.read_bytes().startswith(b"#!/custom/python\n")


def test_pack_pyz_is_valid_zipfile(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    assert zipfile.is_zipfile(output_path)


def test_pack_stages_packages_into_site_packages(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        names = zf.namelist()
    assert "site-packages/mooring/__init__.py" in names


# ---------- tempdir lifecycle ----------


def test_pack_tempdir_cleaned_on_success(output_path: Path, fake_resolver: dict) -> None:
    import tempfile

    config = make_pack_config(output_path)
    pack(config)
    leftover = [
        p for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("moonlit-build-")
    ]
    assert leftover == []


# ---------- atomic output ----------


def test_pack_no_tmp_sibling_on_success(output_path: Path, fake_resolver: dict) -> None:
    config = make_pack_config(output_path)
    pack(config)
    tmp_siblings = [p for p in output_path.parent.iterdir() if ".tmp." in p.name]
    assert tmp_siblings == []
