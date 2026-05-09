"""Pass 2: pin moonlit.builder archive-assembly internals to spec 02 §3 step 9.

These tests exercise the contents of the produced .pyz: shebang prefix,
arcname layout (D1), _bootstrap/ copy, __main__.py template rendering,
env.json producer recipe (spec 05 §5), POSIX exec-bit propagation, and the
spec 01 §8 success line.

Reuses the same fake_resolver fixture pattern as test_builder_pipeline.py.
"""

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit import builder
from moonlit.builder import BuildConfig, build

# ---------- shared fixtures (mirror of test_builder_pipeline.py) ----------


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
def output_path(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True)
    return out


@pytest.fixture
def fake_resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "stage_files": {},
        "wheels_to_make": [
            ("myapp-0.1.0-py3-none-any.whl", "myapp", "0.1.0"),
        ],
    }

    def fake_export(
        project_root: Path, output_file: Path, *, package=None, **_kwargs
    ) -> None:
        state["calls"].append(("export", project_root, output_file, package))
        output_file.write_text("# fake reqs\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
        **_kwargs,
    ) -> None:
        state["calls"].append(("pip_install", project_root, target_dir, requirement, wheel))
        target_dir.mkdir(parents=True, exist_ok=True)
        for arcname, content in state["stage_files"].items():
            dest = target_dir / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            dest.write_bytes(data)

    def fake_build_wheel(
        project_root: Path, out_dir: Path, *, all_packages=False, **_kwargs
    ) -> None:
        state["calls"].append(("build_wheel", project_root, out_dir, all_packages))
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, name, version in state["wheels_to_make"]:
            make_fake_wheel(out_dir / filename, name=name, version=version)

    monkeypatch.setattr(builder.resolver, "export", fake_export)
    monkeypatch.setattr(builder.resolver, "pip_install_target", fake_pip_install_target)
    monkeypatch.setattr(builder.resolver, "build_wheel", fake_build_wheel)
    return state


def make_config(project_root: Path, output_path: Path, **overrides: Any) -> BuildConfig:
    defaults = dict(
        project_root=project_root,
        output_path=output_path,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="/usr/bin/env python3",
        package=None,
        force=False,
        verbosity=0,
    )
    defaults.update(overrides)
    return BuildConfig(**defaults)


# ---------- shebang prefix (step 9.3) ----------


def test_shebang_is_first_bytes_of_file(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, python_shebang="/usr/bin/env python3")
    build(config)
    assert output_path.read_bytes().startswith(b"#!/usr/bin/env python3\n")


def test_custom_shebang_is_preserved(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path, python_shebang="/opt/python/3.13/bin/python")
    build(config)
    assert output_path.read_bytes().startswith(b"#!/opt/python/3.13/bin/python\n")


def test_zip_header_follows_shebang(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    raw = output_path.read_bytes()
    # Skip past the shebang line, then the bytes should be a valid zip.
    nl = raw.index(b"\n")
    rest = raw[nl + 1 :]
    # Local file header signature: PK\x03\x04
    assert rest[:4] == b"PK\x03\x04"


# ---------- D1 arcname layout (step 9.5) ----------


def test_site_packages_files_zipped_under_prefix(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["stage_files"] = {
        "mypkg/__init__.py": b"# mypkg\n",
        "mypkg/cli.py": b"def main():\n    return 0\n",
    }
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        names = set(zf.namelist())
        assert "site-packages/mypkg/__init__.py" in names
        assert "site-packages/mypkg/cli.py" in names
        assert zf.read("site-packages/mypkg/__init__.py") == b"# mypkg\n"
        assert zf.read("site-packages/mypkg/cli.py") == b"def main():\n    return 0\n"


def test_arcnames_use_forward_slashes(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["stage_files"] = {
        "a/b/c/file.py": b"x\n",
    }
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        for info in zf.infolist():
            assert "\\" not in info.filename, f"backslash in {info.filename!r}"


def test_arcnames_have_no_dotdot_segments(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["stage_files"] = {"pkg/inner.py": b""}
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        for info in zf.infolist():
            assert ".." not in info.filename.split("/"), info.filename


# ---------- _bootstrap/ copy (step 9.6) ----------


def test_bootstrap_modules_are_copied(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        names = set(zf.namelist())
    for module in (
        "_bootstrap/__init__.py",
        "_bootstrap/errors.py",
        "_bootstrap/environment.py",
        "_bootstrap/extract.py",
        "_bootstrap/locking.py",
        "_bootstrap/runner.py",
    ):
        assert module in names, f"missing {module}"


def test_bootstrap_pycache_and_pyc_are_not_copied(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        for name in zf.namelist():
            assert "__pycache__" not in name, name
            assert not name.endswith(".pyc"), name


def test_bootstrap_files_match_source_bytes(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    src_root = Path(__file__).resolve().parents[2] / "src" / "moonlit" / "_bootstrap"
    with zipfile.ZipFile(output_path, "r") as zf:
        for name in [n for n in zf.namelist() if n.startswith("_bootstrap/")]:
            rel = name[len("_bootstrap/") :]
            src_path = src_root / rel
            assert src_path.exists(), f"source missing: {src_path}"
            assert zf.read(name) == src_path.read_bytes()


# ---------- __main__.py template (step 9.7) ----------


def test_main_py_is_rendered_from_template(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        main_py = zf.read("__main__.py")
    expected = b"import sys\nfrom _bootstrap import bootstrap\nsys.exit(bootstrap())\n"
    assert main_py == expected


def test_main_py_uses_lf_line_endings(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        main_py = zf.read("__main__.py")
    assert b"\r\n" not in main_py


# ---------- env.json producer recipe (step 9.8 / spec 05 §5) ----------


def test_env_json_is_present_at_archive_root(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        assert "env.json" in zf.namelist()


def test_env_json_ends_with_single_newline(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        env_bytes = zf.read("env.json")
    assert env_bytes.endswith(b"\n")
    assert not env_bytes.endswith(b"\n\n")


def test_env_json_keys_are_sorted(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    # Producer recipe pins sort_keys=True.
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        text = zf.read("env.json").decode("utf-8")
    keys_in_order = re.findall(r'^  "(\w+)":', text, re.MULTILINE)
    assert keys_in_order == sorted(keys_in_order)


def test_env_json_uses_two_space_indent(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        text = zf.read("env.json").decode("utf-8")
    # Top-level fields are indented by 2 spaces.
    assert re.search(r'^  "schema_version":', text, re.MULTILINE)


def test_env_json_decodes_to_expected_payload(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        env = json.loads(zf.read("env.json").decode("utf-8"))
    assert env["schema_version"] == 1
    assert env["name"] == "myapp"
    assert env["entry_point"] == "myapp.cli:main"
    assert env["python_shebang"] == "/usr/bin/env python3"
    assert len(env["build_id"]) == 64


# ---------- compression (step 9.4) ----------


def test_zip_uses_deflate_compression(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    fake_resolver["stage_files"] = {"data.txt": b"x" * 10_000}
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED


# ---------- POSIX exec-bit propagation (step 9.5) ----------


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit propagation only")
def test_exec_bit_is_propagated_to_zipinfo(tmp_path: Path) -> None:

    staging = tmp_path / "staging"
    site_packages = staging / "site-packages"
    site_packages.mkdir(parents=True)
    exec_file = site_packages / "scripts" / "myscript"
    exec_file.parent.mkdir()
    exec_file.write_bytes(b"#!/bin/sh\necho hi\n")
    exec_file.chmod(0o755)

    plain_file = site_packages / "data.txt"
    plain_file.write_bytes(b"x")
    plain_file.chmod(0o644)

    tmp_out = tmp_path / "out.pyz"
    env_dict = {
        "schema_version": 1,
        "name": "x",
        "build_id": "a" * 64,
        "entry_point": "x:y",
        "built_at": "2026-01-01T00:00:00Z",
        "moonlit_version": "0.1.0",
        "python_shebang": "/usr/bin/python3",
    }
    builder._create_archive(tmp_out, staging, env_dict, "/usr/bin/python3")

    with zipfile.ZipFile(tmp_out, "r") as zf:
        exec_info = zf.getinfo("site-packages/scripts/myscript")
        plain_info = zf.getinfo("site-packages/data.txt")
    assert exec_info.external_attr == 0o755 << 16
    assert plain_info.external_attr != 0o755 << 16


# ---------- success line (spec 01 §8 / invariant I8) ----------


_SUCCESS_LINE_RE = re.compile(r"^wrote .+ \(\d+(\.\d+)? (B|KiB|MiB|GiB|TiB), \d+ entries\)$")


def test_success_line_matches_spec_format(
    project_root: Path,
    output_path: Path,
    fake_resolver: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    line = capsys.readouterr().out.strip()
    assert _SUCCESS_LINE_RE.match(line), f"unexpected success line: {line!r}"


def test_success_line_includes_output_path(
    project_root: Path,
    output_path: Path,
    fake_resolver: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(project_root, output_path)
    build(config)
    out = capsys.readouterr().out
    assert str(output_path) in out


def test_success_line_entry_count_matches_zip(
    project_root: Path,
    output_path: Path,
    fake_resolver: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_resolver["stage_files"] = {
        "pkg/a.py": b"a\n",
        "pkg/b.py": b"b\n",
        "pkg/sub/c.py": b"c\n",
    }
    config = make_config(project_root, output_path)
    build(config)
    out = capsys.readouterr().out
    match = re.search(r"(\d+) entries", out)
    assert match is not None
    with zipfile.ZipFile(output_path, "r") as zf:
        actual = len(zf.namelist())
    assert int(match.group(1)) == actual


# ---------- _humanize_bytes ----------


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0 B"),
        (1, "1 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (1024 * 1024 * 1024, "1.0 GiB"),
    ],
)
def test_humanize_bytes_examples(n: int, expected: str) -> None:
    assert builder._humanize_bytes(n) == expected


# ---------- end-to-end entry composition ----------


def test_archive_contains_at_minimum_bootstrap_main_envjson(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """Even with zero site-packages files, the archive must include the
    bootstrap, __main__.py, and env.json entries."""
    fake_resolver["stage_files"] = {}  # nothing in site-packages
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        names = zf.namelist()
        assert "__main__.py" in names
        assert "env.json" in names
        bootstrap_entries = [n for n in names if n.startswith("_bootstrap/")]
        assert len(bootstrap_entries) >= 5  # init + errors + 4 modules


def test_zip_entries_are_sorted_for_bootstrap(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """_walk_traversable sorts by name → bootstrap entries appear in alpha order."""
    config = make_config(project_root, output_path)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        bootstrap_names = [n for n in zf.namelist() if n.startswith("_bootstrap/")]
    assert bootstrap_names == sorted(bootstrap_names)
