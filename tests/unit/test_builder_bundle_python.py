"""Pin builder behavior for ``--bundle-python`` (D21, spec 02 §3 step 8.5, I11b).

Mocks ``resolver.python_install`` to simulate uv writing a python-build-standalone
distribution into the tempdir. Asserts:

* The produced ``.exe`` zip body contains ``_python/<rel>`` entries.
* All other entries (site-packages/*, _bootstrap/*, __main__.py) are
  byte-identical to a non-bundled build of the same project (invariant I11b).
* ``build_id`` is byte-identical with or without ``--bundle-python`` —
  bundled Python MUST NOT enter the app's cache key (D21d).
* ``env.json`` carries a ``bundled_python`` object with the required keys and a
  64-hex fingerprint.
* The fingerprint computed by the producer matches the recipe a Rust launcher
  would derive from the produced .exe's central directory (D21/D22 contract).
"""

import json
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from moonlit import builder
from moonlit.builder import BuildConfig, build

# ---------- shared fixtures (mirror of test_builder_archive.py) ----------


def _make_fake_wheel(path: Path, *, name: str, version: str = "0.1.0") -> None:
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
    out = tmp_path / "out" / "app.exe"
    out.parent.mkdir(parents=True)
    return out


@pytest.fixture
def fake_resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "stage_files": {
            "mypkg/__init__.py": b"# mypkg\n",
            "mypkg/cli.py": b"def main():\n    return 0\n",
        },
        "wheels_to_make": [("myapp-0.1.0-py3-none-any.whl", "myapp", "0.1.0")],
        # Fake python-build-standalone tree. The launcher contract pins
        # python.exe at the dist root; we also include Lib/site.py to exercise
        # nested arcnames.
        "python_files": {
            "python.exe": b"FAKE_PYTHON_EXE_BYTES\x00\x01\x02",
            "python3.dll": b"FAKE_DLL\n",
            "Lib/site.py": b"# fake site.py\n",
            "Lib/os.py": b"# fake os.py\n",
            "Lib/encodings/__init__.py": b"# encodings\n",
        },
        "python_install_calls": [],
    }

    def fake_export(project_root: Path, output_file: Path, *, package=None, **_kwargs) -> None:
        state["calls"].append(("export", package))
        output_file.write_text("# fake reqs\n", encoding="utf-8")

    def fake_pip_install_target(
        project_root: Path,
        target_dir: Path,
        *,
        requirement: Path | None = None,
        wheel: Path | None = None,
        **_kwargs,
    ) -> None:
        state["calls"].append(("pip_install", requirement, wheel))
        target_dir.mkdir(parents=True, exist_ok=True)
        for arcname, content in state["stage_files"].items():
            dest = target_dir / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

    def fake_build_wheel(
        project_root: Path, out_dir: Path, *, all_packages=False, **_kwargs
    ) -> None:
        state["calls"].append(("build_wheel", all_packages))
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, name, version in state["wheels_to_make"]:
            _make_fake_wheel(out_dir / filename, name=name, version=version)

    def fake_python_install(install_dir: Path, *, version: str, **_kwargs) -> Path:
        state["python_install_calls"].append((install_dir, version))
        # Mirror uv's layout: a single child dir named for the cpython release.
        dist = install_dir / f"cpython-{version}.7-windows-x86_64-none"
        dist.mkdir(parents=True, exist_ok=True)
        for rel, content in state["python_files"].items():
            dest = dist / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return dist

    monkeypatch.setattr(builder.resolver, "export", fake_export)
    monkeypatch.setattr(builder.resolver, "pip_install_target", fake_pip_install_target)
    monkeypatch.setattr(builder.resolver, "build_wheel", fake_build_wheel)
    monkeypatch.setattr(builder.resolver, "python_install", fake_python_install)
    return state


def _make_config(
    project_root: Path, output_path: Path, *, bundle_python: bool = True
) -> BuildConfig:
    return BuildConfig(
        project_root=project_root,
        output_path=output_path,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="python.exe",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=True,
        python_version=None,
        bundle_python=bundle_python,
    )


def _read_env_json(path: Path) -> dict:
    with zipfile.ZipFile(path, "r") as zf:
        return json.loads(zf.read("env.json").decode("utf-8"))


def _launcher_recipe_fingerprint(path: Path, *, arcname_prefix: str = "_python/") -> str:
    """Independent re-derivation of the cross-language fingerprint from the
    produced archive's central directory — mirrors what the Rust launcher will
    do (D21/D22). Distinct code path from ``hashing.compute_python_fingerprint``
    on purpose so the test catches drift between producer and consumer.
    """
    import hashlib

    h = hashlib.sha256()
    with zipfile.ZipFile(path, "r") as zf:
        infos = [i for i in zf.infolist() if i.filename.startswith(arcname_prefix)]
    infos.sort(key=lambda i: i.filename.encode("utf-8"))
    for info in infos:
        h.update(info.filename.encode("utf-8"))
        h.update(b"\0")
        # CRC32 read from the central directory; little-endian 4-byte form.
        h.update((info.CRC & 0xFFFFFFFF).to_bytes(4, "little"))
        h.update(b"\0")
    return h.hexdigest()


# ---------- spec 02 §3 step 8.5: bundled Python lands as _python/ entries ----------


def test_bundle_adds_python_prefix_entries(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """The expected files appear at _python/<rel> arcnames."""
    config = _make_config(project_root, output_path, bundle_python=True)
    build(config)
    with zipfile.ZipFile(output_path, "r") as zf:
        names = set(zf.namelist())
    expected = {f"_python/{rel}" for rel in fake_resolver["python_files"]}
    assert expected <= names
    # python.exe at the prefix root (launcher contract).
    assert "_python/python.exe" in names


def test_bundle_python_install_invoked_with_version(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """When --python-version is unset, version is host major.minor."""
    import sys

    config = _make_config(project_root, output_path, bundle_python=True)
    build(config)
    assert len(fake_resolver["python_install_calls"]) == 1
    _install_dir, version = fake_resolver["python_install_calls"][0]
    assert version == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_no_bundle_means_no_python_install_invocation(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """Step 8.5 is gated on bundle_python — never runs otherwise."""
    out = tmp_path / "out" / "app.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)
    config = BuildConfig(
        project_root=project_root,
        output_path=out,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="/usr/bin/env python3",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=False,
        python_version=None,
        bundle_python=False,
    )
    build(config)
    assert fake_resolver["python_install_calls"] == []
    with zipfile.ZipFile(out, "r") as zf:
        for name in zf.namelist():
            assert not name.startswith("_python/"), name


# ---------- invariant: build_id MUST NOT depend on bundled Python (D21d) ----------


def test_bundle_does_not_change_build_id(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out_nobundle = tmp_path / "out" / "app_nobundle.exe"
    out_bundle = tmp_path / "out" / "app_bundle.exe"
    out_nobundle.parent.mkdir(parents=True, exist_ok=True)

    cfg_nobundle = _make_config(project_root, out_nobundle, bundle_python=False)
    build(cfg_nobundle)
    build_id_nobundle = _read_env_json(out_nobundle)["build_id"]

    cfg_bundle = _make_config(project_root, out_bundle, bundle_python=True)
    build(cfg_bundle)
    build_id_bundle = _read_env_json(out_bundle)["build_id"]

    assert build_id_bundle == build_id_nobundle, (
        "build_id must not change when --bundle-python is added; "
        "spec 02 §3 step 8.5 places python install AFTER compute_build_id"
    )


# ---------- invariant I11b: only _python/* and env.json change ----------


def test_invariant_i11b_only_python_prefix_and_envjson_differ(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out_nobundle = tmp_path / "out" / "app_nobundle.exe"
    out_bundle = tmp_path / "out" / "app_bundle.exe"
    out_nobundle.parent.mkdir(parents=True, exist_ok=True)
    build(_make_config(project_root, out_nobundle, bundle_python=False))
    build(_make_config(project_root, out_bundle, bundle_python=True))

    with zipfile.ZipFile(out_nobundle, "r") as zfa, zipfile.ZipFile(out_bundle, "r") as zfb:
        names_a = set(zfa.namelist())
        names_b = set(zfb.namelist())
        added = names_b - names_a
        # Every added entry begins with _python/.
        assert added
        assert all(n.startswith("_python/") for n in added), added
        # And the bundle build adds no other entries.
        assert names_b == names_a | added
        # For every common arcname except env.json, content must match.
        for name in names_a:
            if name == "env.json":
                continue
            assert zfa.read(name) == zfb.read(name), name


# ---------- env.json carries bundled_python (spec 05 §3.9) ----------


def test_bundle_writes_env_bundled_python_field(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    config = _make_config(project_root, output_path, bundle_python=True)
    build(config)
    env = _read_env_json(output_path)
    assert "bundled_python" in env
    bp = env["bundled_python"]
    assert bp["prefix"] == "_python/"
    assert bp["relative_python_exe"] == "python.exe"
    fp = bp["fingerprint"]
    assert isinstance(fp, str)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp), fp


def test_no_bundle_omits_env_bundled_python_field(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    out = tmp_path / "out" / "plain.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)
    config = BuildConfig(
        project_root=project_root,
        output_path=out,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="/usr/bin/env python3",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=False,
        python_version=None,
        bundle_python=False,
    )
    build(config)
    env = _read_env_json(out)
    assert "bundled_python" not in env


# ---------- cross-language fingerprint contract ----------


def test_bundle_fingerprint_matches_central_directory_recipe(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """The producer's fingerprint MUST equal the value an independent walker
    derives from the .exe's central directory (the Rust launcher's job).
    """
    config = _make_config(project_root, output_path, bundle_python=True)
    build(config)
    stamped = _read_env_json(output_path)["bundled_python"]["fingerprint"]
    derived = _launcher_recipe_fingerprint(output_path)
    assert stamped == derived


def test_bundle_fingerprint_is_deterministic_across_builds(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """Same python-files input → same fingerprint, two builds in a row."""
    out_a = tmp_path / "out" / "a.exe"
    out_b = tmp_path / "out" / "b.exe"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    build(_make_config(project_root, out_a, bundle_python=True))
    build(_make_config(project_root, out_b, bundle_python=True))
    fp_a = _read_env_json(out_a)["bundled_python"]["fingerprint"]
    fp_b = _read_env_json(out_b)["bundled_python"]["fingerprint"]
    assert fp_a == fp_b


def test_bundle_fingerprint_changes_when_python_changes(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """Different python-files input → different fingerprint."""
    out_a = tmp_path / "out" / "a.exe"
    out_b = tmp_path / "out" / "b.exe"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    build(_make_config(project_root, out_a, bundle_python=True))
    # Flip a byte in one python file.
    fake_resolver["python_files"]["Lib/site.py"] = b"# changed site.py\n"
    build(_make_config(project_root, out_b, bundle_python=True))
    fp_a = _read_env_json(out_a)["bundled_python"]["fingerprint"]
    fp_b = _read_env_json(out_b)["bundled_python"]["fingerprint"]
    assert fp_a != fp_b


def test_bundle_fingerprint_independent_of_build_id(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """Changing site-packages content changes build_id but not the python
    fingerprint (and vice versa). The two hashes are orthogonal."""
    out_a = tmp_path / "out" / "a.exe"
    out_b = tmp_path / "out" / "b.exe"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    build(_make_config(project_root, out_a, bundle_python=True))
    # Mutate site-packages (changes build_id) but keep python files unchanged.
    fake_resolver["stage_files"]["mypkg/cli.py"] = b"def main():\n    return 42\n"
    build(_make_config(project_root, out_b, bundle_python=True))
    env_a = _read_env_json(out_a)
    env_b = _read_env_json(out_b)
    assert env_a["build_id"] != env_b["build_id"]
    assert env_a["bundled_python"]["fingerprint"] == env_b["bundled_python"]["fingerprint"]


# ---------- hashing.compute_python_fingerprint unit ----------


def test_compute_python_fingerprint_matches_zlib_crc32_recipe(tmp_path: Path) -> None:
    """The hashing module's recipe matches the literal spec 02 §4a algorithm."""
    import hashlib

    from moonlit import hashing

    root = tmp_path / "py"
    root.mkdir()
    (root / "a.txt").write_bytes(b"hello\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01\x02\x03")

    h = hashlib.sha256()
    pairs = sorted(
        [
            (b"_python/a.txt", zlib.crc32(b"hello\n") & 0xFFFFFFFF),
            (b"_python/sub/b.bin", zlib.crc32(b"\x00\x01\x02\x03") & 0xFFFFFFFF),
        ],
        key=lambda t: t[0],
    )
    for arcname, crc in pairs:
        h.update(arcname)
        h.update(b"\0")
        h.update(crc.to_bytes(4, "little"))
        h.update(b"\0")
    expected = h.hexdigest()
    assert hashing.compute_python_fingerprint(root) == expected


def test_compute_python_fingerprint_empty_tree(tmp_path: Path) -> None:
    """Edge case: an empty tree yields the SHA-256 of the empty stream."""
    import hashlib

    from moonlit import hashing

    root = tmp_path / "empty"
    root.mkdir()
    expected = hashlib.sha256().hexdigest()
    assert hashing.compute_python_fingerprint(root) == expected


# ---------- defensive: bundle_python+windows_exe=False raises InternalError ----------


def test_bundle_without_windows_exe_internal_error(
    project_root: Path, output_path: Path, fake_resolver: dict
) -> None:
    """A direct BuildConfig caller that bypasses the CLI still gets stopped."""
    from moonlit.errors import InternalError

    out = output_path.with_suffix(".pyz")
    config = BuildConfig(
        project_root=project_root,
        output_path=out,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="/usr/bin/env python3",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=False,
        python_version=None,
        bundle_python=True,
    )
    with pytest.raises(InternalError, match="windows_exe"):
        build(config)
