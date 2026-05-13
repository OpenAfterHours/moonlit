"""Pin builder behavior for ``--bundle-python`` (D21 folder-bundle redesign).

Mocks ``resolver.python_install`` to simulate uv writing a python-build-standalone
distribution into the tempdir. Asserts:

* The produced output is a **directory** containing
  ``<basename>.exe`` + ``<basename>.pyz`` + ``_python/`` (D21a).
* The inner ``.pyz``'s zip-entry set is byte-identical to a non-bundle build
  of the same project + same flags (invariant I11b).
* The launcher inside the bundle byte-equals the vendored launcher for the
  host architecture — no appended zip body.
* The python-build-standalone tree is copied verbatim into ``_python/``.
* ``build_id`` is byte-identical with or without ``--bundle-python`` —
  bundled Python MUST NOT enter the app's cache key (D21e).
* ``env.json`` does NOT carry a ``bundled_python`` field (D21h).
* ``--force`` rules: a moonlit-recognised bundle dir is overwritten;
  anything else at the output path is refused.
"""

import json
import zipfile
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
def bundle_path(tmp_path: Path) -> Path:
    """Default bundle output: a directory at tmp/out/app (no extension)."""
    out = tmp_path / "out" / "app"
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
        # Fake python-build-standalone tree. python.exe at the dist root is
        # required by _install_bundled_python; nested paths exercise the
        # _copy_python_tree recursion.
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


def _make_bundle_config(project_root: Path, output_path: Path) -> BuildConfig:
    return BuildConfig(
        project_root=project_root,
        output_path=output_path,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="python.exe",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=False,
        python_version=None,
        bundle_python=True,
    )


def _make_nonbundle_config(project_root: Path, output_path: Path) -> BuildConfig:
    """Pyz output matching the inner .pyz semantics of the bundle (same shebang)."""
    return BuildConfig(
        project_root=project_root,
        output_path=output_path,
        entry_point="myapp.cli:main",
        console_script=None,
        python_shebang="python.exe",
        package=None,
        force=False,
        verbosity=0,
        windows_exe=False,
        python_version=None,
        bundle_python=False,
    )


def _read_env_json(pyz: Path) -> dict:
    with zipfile.ZipFile(pyz, "r") as zf:
        return json.loads(zf.read("env.json").decode("utf-8"))


# ---------- output is a directory with the three expected children (D21a) ----------


def test_bundle_output_is_a_directory(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    build(_make_bundle_config(project_root, bundle_path))
    assert bundle_path.is_dir(), "expected a folder bundle, not a file"


def test_bundle_directory_contains_expected_children(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    build(_make_bundle_config(project_root, bundle_path))
    basename = bundle_path.name
    assert (bundle_path / f"{basename}.exe").is_file()
    assert (bundle_path / f"{basename}.pyz").is_file()
    assert (bundle_path / "_python").is_dir()
    assert (bundle_path / "_python" / "python.exe").is_file()


def test_bundle_directory_has_no_extra_children(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    build(_make_bundle_config(project_root, bundle_path))
    basename = bundle_path.name
    expected = {f"{basename}.exe", f"{basename}.pyz", "_python"}
    actual = {p.name for p in bundle_path.iterdir()}
    assert actual == expected


def test_bundle_basename_drives_inner_filenames(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """The directory's basename is reused for the launcher and the .pyz —
    this is the contract the launcher's sibling probe relies on (D22a)."""
    out = tmp_path / "out" / "shouter"
    out.parent.mkdir(parents=True)
    build(_make_bundle_config(project_root, out))
    assert (out / "shouter.exe").is_file()
    assert (out / "shouter.pyz").is_file()


# ---------- launcher is shipped verbatim (no appended zip) ----------


def test_bundle_launcher_byte_equals_vendored_launcher(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """The .exe in the folder is the vendored ``t-<arch>.exe`` verbatim. Crucially
    there is no appended zip — that's the AV-relevant property: it doesn't
    look like a self-extracting archive."""
    build(_make_bundle_config(project_root, bundle_path))
    expected = builder._load_launcher_bytes()
    actual = (bundle_path / f"{bundle_path.name}.exe").read_bytes()
    assert actual == expected


def test_bundle_launcher_is_not_a_zipfile(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """The launcher .exe has no trailing zip body — `zipfile.is_zipfile`
    must return False. This is the property Windows Defender ML scanners
    care about: PE with no embedded archive ≠ self-extracting trojan."""
    build(_make_bundle_config(project_root, bundle_path))
    exe = bundle_path / f"{bundle_path.name}.exe"
    assert not zipfile.is_zipfile(exe)


# ---------- inner .pyz parity with non-bundle build (invariant I11b) ----------


def test_inner_pyz_namelist_equals_nonbundle_namelist(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """The inner .pyz's set of zip entries is identical to what a non-bundle
    build of the same project + same flags produces (invariant I11b)."""
    bundle_out = tmp_path / "out" / "app"
    pyz_out = tmp_path / "out" / "plain.pyz"
    bundle_out.parent.mkdir(parents=True)
    build(_make_bundle_config(project_root, bundle_out))
    build(_make_nonbundle_config(project_root, pyz_out))
    inner = bundle_out / f"{bundle_out.name}.pyz"
    with zipfile.ZipFile(inner) as zf_bundle, zipfile.ZipFile(pyz_out) as zf_plain:
        assert set(zf_bundle.namelist()) == set(zf_plain.namelist())


def test_inner_pyz_content_equals_nonbundle_except_envjson(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """For every common arcname except ``env.json``, the inner .pyz bytes equal
    the non-bundle .pyz bytes. ``env.json`` differs only in its ``built_at``
    timestamp — the other fields are identical."""
    bundle_out = tmp_path / "out" / "app"
    pyz_out = tmp_path / "out" / "plain.pyz"
    bundle_out.parent.mkdir(parents=True)
    build(_make_bundle_config(project_root, bundle_out))
    build(_make_nonbundle_config(project_root, pyz_out))
    inner = bundle_out / f"{bundle_out.name}.pyz"
    with zipfile.ZipFile(inner) as zf_bundle, zipfile.ZipFile(pyz_out) as zf_plain:
        for name in zf_plain.namelist():
            if name == "env.json":
                continue
            assert zf_bundle.read(name) == zf_plain.read(name), name
        # env.json: built_at can differ; every other field must match.
        env_a = json.loads(zf_bundle.read("env.json").decode("utf-8"))
        env_b = json.loads(zf_plain.read("env.json").decode("utf-8"))
        env_a.pop("built_at", None)
        env_b.pop("built_at", None)
        assert env_a == env_b


# ---------- _python/ tree is copied verbatim ----------


def test_bundle_copies_python_tree_verbatim(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    build(_make_bundle_config(project_root, bundle_path))
    python_dir = bundle_path / "_python"
    for rel, content in fake_resolver["python_files"].items():
        # Use forward slashes in the test inputs but resolve via Path so
        # Windows uses backslashes natively.
        dest = python_dir.joinpath(*rel.split("/"))
        assert dest.is_file(), rel
        assert dest.read_bytes() == content, rel


def test_bundle_python_install_invoked_with_host_version(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """When --python-version is unset, version is host major.minor (D21c)."""
    import sys

    build(_make_bundle_config(project_root, bundle_path))
    assert len(fake_resolver["python_install_calls"]) == 1
    _install_dir, version = fake_resolver["python_install_calls"][0]
    assert version == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_no_bundle_means_no_python_install_invocation(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """Step 8.5 is gated on bundle_python — never runs otherwise."""
    out = tmp_path / "out" / "plain.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)
    build(_make_nonbundle_config(project_root, out))
    assert fake_resolver["python_install_calls"] == []


# ---------- invariant: build_id MUST NOT depend on bundled Python (D21e) ----------


def test_bundle_does_not_change_build_id(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """build_id is hashed over staged site-packages only — adding a bundled
    Python install (step 8.5, after compute_build_id) MUST NOT change it."""
    bundle_out = tmp_path / "out" / "app"
    pyz_out = tmp_path / "out" / "plain.pyz"
    bundle_out.parent.mkdir(parents=True)
    build(_make_bundle_config(project_root, bundle_out))
    build(_make_nonbundle_config(project_root, pyz_out))
    inner = bundle_out / f"{bundle_out.name}.pyz"
    assert _read_env_json(inner)["build_id"] == _read_env_json(pyz_out)["build_id"]


# ---------- env.json carries NO bundled_python field (D21h) ----------


def test_bundle_does_not_emit_bundled_python_field_in_env_json(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """The D21 redesign retired the env.json `bundled_python` sub-object —
    the bundled state is observable from the folder layout, not env.json."""
    build(_make_bundle_config(project_root, bundle_path))
    inner = bundle_path / f"{bundle_path.name}.pyz"
    env = _read_env_json(inner)
    assert "bundled_python" not in env


# ---------- --force / preflight rules (D21g) ----------


def test_bundle_refuses_to_overwrite_unrelated_directory_even_with_force(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """An existing directory at -o that doesn't look like a moonlit bundle
    is refused even with --force, so --force never turns into an `rm -rf`."""
    from moonlit.errors import OutputNotWritableError

    out = tmp_path / "out" / "app"
    out.parent.mkdir(parents=True)
    out.mkdir()
    (out / "important_user_file.txt").write_text("don't touch me", encoding="utf-8")

    cfg = _make_bundle_config(project_root, out)
    # Even with force=True, the unrelated dir must not be overwritten.
    cfg_force = BuildConfig(**{**cfg.__dict__, "force": True})
    with pytest.raises(OutputNotWritableError, match="not a moonlit bundle"):
        build(cfg_force)
    assert (out / "important_user_file.txt").is_file()


def test_bundle_refuses_existing_regular_file_at_output_path(
    project_root: Path, tmp_path: Path, fake_resolver: dict
) -> None:
    """A regular file at -o is refused — folder mode requires a directory
    target (or a free path)."""
    from moonlit.errors import OutputNotWritableError

    out = tmp_path / "out" / "app"
    out.parent.mkdir(parents=True)
    out.write_text("i'm a file, not a folder", encoding="utf-8")
    cfg = BuildConfig(**{**_make_bundle_config(project_root, out).__dict__, "force": True})
    with pytest.raises(OutputNotWritableError, match="not a directory"):
        build(cfg)


def test_bundle_overwrites_moonlit_bundle_dir_with_force(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """A previous moonlit bundle dir IS overwritten under --force."""
    build(_make_bundle_config(project_root, bundle_path))
    assert (bundle_path / f"{bundle_path.name}.exe").is_file()

    # Second build with --force should atomically replace the bundle.
    cfg = BuildConfig(**{**_make_bundle_config(project_root, bundle_path).__dict__, "force": True})
    build(cfg)
    assert (bundle_path / f"{bundle_path.name}.exe").is_file()
    assert (bundle_path / "_python" / "python.exe").is_file()


def test_bundle_refuses_existing_moonlit_bundle_dir_without_force(
    project_root: Path, bundle_path: Path, fake_resolver: dict
) -> None:
    """A moonlit-recognised bundle dir without --force triggers OutputExistsError."""
    from moonlit.errors import OutputExistsError

    build(_make_bundle_config(project_root, bundle_path))
    with pytest.raises(OutputExistsError, match="--force"):
        build(_make_bundle_config(project_root, bundle_path))
