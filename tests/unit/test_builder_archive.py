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

    def fake_export(project_root: Path, output_file: Path, *, package=None, **_kwargs) -> None:
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
    config = BuildConfig(
        project_root=tmp_path,
        output_path=tmp_out,
        entry_point="x:y",
        console_script=None,
        python_shebang="/usr/bin/python3",
        package=None,
        force=False,
        verbosity=0,
    )
    builder._create_archive(tmp_out, staging, env_dict, config, None)

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


# ---------- humanize_bytes ----------


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
    assert builder.humanize_bytes(n) == expected


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


# ---------- --windows-exe (D19) ----------


@pytest.fixture
def exe_output_path(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir(parents=True)
    return out


def _find_pe_end(buf: bytes) -> int:
    """Mirror of launcher/src/main.rs::find_pe_end for byte-level test assertions.

    Returns the offset of the first byte past the PE image; trailing data
    (shebang line + zip body in our wrapper format) starts there.
    """
    assert buf[:2] == b"MZ", "not a PE file"
    e_lfanew = int.from_bytes(buf[0x3C:0x40], "little")
    assert buf[e_lfanew : e_lfanew + 4] == b"PE\0\0", "not a PE file"
    file_header = buf[e_lfanew + 4 : e_lfanew + 24]
    num_sections = int.from_bytes(file_header[2:4], "little")
    opt_size = int.from_bytes(file_header[16:18], "little")
    section_table_start = e_lfanew + 24 + opt_size
    pe_end = section_table_start + 40 * num_sections
    for i in range(num_sections):
        sh = buf[section_table_start + i * 40 : section_table_start + (i + 1) * 40]
        size_raw = int.from_bytes(sh[16:20], "little")
        ptr_raw = int.from_bytes(sh[20:24], "little")
        if ptr_raw == 0:
            continue
        end = ptr_raw + size_raw
        if end > pe_end:
            pe_end = end
    return pe_end


def test_windows_exe_starts_with_pe_magic(
    project_root: Path, exe_output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(
        project_root, exe_output_path, windows_exe=True, python_shebang="python.exe"
    )
    build(config)
    raw = exe_output_path.read_bytes()
    assert raw[:2] == b"MZ"


def test_windows_exe_shebang_immediately_follows_pe_image(
    project_root: Path, exe_output_path: Path, fake_resolver: dict
) -> None:
    config = make_config(
        project_root, exe_output_path, windows_exe=True, python_shebang="python.exe"
    )
    build(config)
    raw = exe_output_path.read_bytes()
    pe_end = _find_pe_end(raw)
    assert raw[pe_end : pe_end + len(b"#!python.exe\n")] == b"#!python.exe\n"


def test_windows_exe_zip_body_matches_pyz_per_entry(
    project_root: Path,
    tmp_path: Path,
    fake_resolver: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant I11: a .pyz and the .exe built from the same project share
    # the same set of zip entries with the same content bytes. Byte-identical
    # zip bodies are NOT a contract today — zipfile embeds mtimes and our
    # builds happen at different wall-clock instants. Once `--reproducible`
    # lands, I11 can tighten to byte-identity.
    #
    # `built_at` uses second-resolution `datetime.now(UTC)`, so two
    # back-to-back builds can land in different seconds and produce
    # divergent env.json content. Freeze the clock for the duration of this
    # test so the per-entry comparison is deterministic.
    from datetime import datetime as _real_datetime

    class _FrozenDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _real_datetime(2026, 5, 9, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(builder, "datetime", _FrozenDatetime)

    fake_resolver["stage_files"] = {
        "mypkg/__init__.py": b"# mypkg\n",
        "mypkg/cli.py": b"def main():\n    return 0\n",
    }
    pyz_path = tmp_path / "out" / "app.pyz"
    exe_path = tmp_path / "out" / "app.exe"
    pyz_path.parent.mkdir(parents=True)
    shebang = "python.exe"
    build(make_config(project_root, pyz_path, python_shebang=shebang))
    build(make_config(project_root, exe_path, windows_exe=True, python_shebang=shebang))

    # Both files are openable as zips; compare per-entry content.
    with zipfile.ZipFile(pyz_path, "r") as pyz_zf, zipfile.ZipFile(exe_path, "r") as exe_zf:
        pyz_names = sorted(pyz_zf.namelist())
        exe_names = sorted(exe_zf.namelist())
        assert pyz_names == exe_names
        for name in pyz_names:
            assert pyz_zf.read(name) == exe_zf.read(name), f"entry {name!r} differs"


def test_windows_exe_zipfile_round_trips(
    project_root: Path, exe_output_path: Path, fake_resolver: dict
) -> None:
    # Python's zipfile module reads the central directory from the END of the
    # file, so a leading PE prefix is harmless: env.json + __main__.py + the
    # _bootstrap/ tree must all still be enumerable.
    config = make_config(
        project_root, exe_output_path, windows_exe=True, python_shebang="python.exe"
    )
    build(config)
    with zipfile.ZipFile(exe_output_path, "r") as zf:
        names = zf.namelist()
    assert "env.json" in names
    assert "__main__.py" in names
    assert any(n.startswith("_bootstrap/") for n in names)


# ---------- arch detection (D19a) ----------


@pytest.mark.parametrize(
    "machine,expected",
    [
        ("AMD64", "x64"),
        ("amd64", "x64"),
        ("x86_64", "x64"),
        ("ARM64", "arm64"),
        ("aarch64", "arm64"),
        ("x86", "x86"),
        ("i686", "x86"),
        ("i386", "x86"),
    ],
)
def test_detect_launcher_arch_normalizes_known_values(
    monkeypatch: pytest.MonkeyPatch, machine: str, expected: str
) -> None:
    monkeypatch.setattr(builder.platform, "machine", lambda: machine)
    assert builder._detect_launcher_arch() == expected


def test_detect_launcher_arch_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.platform, "machine", lambda: "MIPS")
    with pytest.raises(builder.InternalError, match="MIPS"):
        builder._detect_launcher_arch()


def test_load_launcher_bytes_returns_pe_image() -> None:
    # Sanity: the vendored x64 launcher is shipped with the package.
    raw = builder._load_launcher_bytes()
    assert raw[:2] == b"MZ"
    assert len(raw) > 1024  # not a stub


def test_load_launcher_bytes_raises_when_arch_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend we're on an arch we haven't vendored yet.
    monkeypatch.setattr(builder.platform, "machine", lambda: "ARM64")
    with pytest.raises(builder.InternalError, match="missing launcher binary"):
        builder._load_launcher_bytes()
