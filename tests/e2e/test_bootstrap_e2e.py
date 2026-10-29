"""End-to-end tests for the bootstrap (per arch §2 contract).

Each test hand-crafts a fixture .pyz containing a real ``_bootstrap`` copy
from ``src/moonlit/_bootstrap/``, plus user modules under ``site-packages/``
and a valid ``env.json``. The pyz is then run via ``python <pyz>`` as a
subprocess with ``MOONLIT_ROOT`` pointing at an isolated tmp dir; stdout,
stderr, and exit code are asserted.

This is the contract test mode for the bootstrap. The unit tests in
``tests/unit/test_environment.py``, ``test_locking.py``, ``test_extract.py``,
``test_runner.py``, and ``test_bootstrap_orchestrator.py`` are dev-time
TDD harnesses; this suite is what arch §2 names as the supported test mode.
"""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SRC = REPO_ROOT / "src" / "moonlit" / "_bootstrap"
TEMPLATE_SRC = REPO_ROOT / "src" / "moonlit" / "_templates" / "main_py.tmpl"


# ---------- pyz construction helpers ----------


def _read_main_template_bytes() -> bytes:
    text = TEMPLATE_SRC.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.encode("utf-8")


def _iter_bootstrap_files() -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for src in sorted(BOOTSTRAP_SRC.rglob("*")):
        if not src.is_file():
            continue
        if "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(BOOTSTRAP_SRC).as_posix()
        files.append((rel, src.read_bytes()))
    return files


def _serialize_env_json(env_dict: dict) -> bytes:
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


def valid_env(
    *,
    name: str = "myapp",
    build_id: str | None = None,
    entry_point: str = "myapp:main",
) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "build_id": build_id or ("a" * 64),
        "entry_point": entry_point,
        "built_at": "2026-05-09T00:00:00Z",
        "moonlit_version": "0.1.0",
        "python_shebang": "/usr/bin/env python3",
    }


def make_test_pyz(
    path: Path,
    *,
    user_modules: dict[str, str | bytes] | None = None,
    env_dict: dict | None = None,
    omit_env_json: bool = False,
) -> Path:
    """Hand-craft a .pyz with a real _bootstrap copy + user modules + env.json."""
    user_modules = user_modules or {}
    env_dict = env_dict or valid_env()
    with open(path, "wb") as fp:
        fp.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("__main__.py", _read_main_template_bytes())
            if not omit_env_json:
                zf.writestr("env.json", _serialize_env_json(env_dict))
            for rel, content in _iter_bootstrap_files():
                zf.writestr(f"_bootstrap/{rel}", content)
            for arcname, content in user_modules.items():
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                zf.writestr(f"site-packages/{arcname}", data)
    return path


# ---------- subprocess helpers ----------


def isolated_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """Inherit parent env, strip MOONLIT_*, point MOONLIT_ROOT at a tmp dir."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(tmp_path / "moonlit_cache")
    env.update(overrides)
    return env


def run_pyz(
    pyz: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    interpreter: Path | str = sys.executable,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [str(interpreter), str(pyz)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------- happy-path bootstrapping ----------


def test_pyz_runs_user_main_and_prints_output(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    print('hello from myapp')\n    return 0\n"},
    )
    code, stdout, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 0, stderr
    assert "hello from myapp" in stdout


def test_extraction_is_silent_under_pipe(tmp_path: Path) -> None:
    # Spec 03 §14: the unpacking indicator is TTY-gated. Under a pipe
    # (subprocess capture), a cold-cache extraction must leave no progress
    # frames or escape sequences on stderr.
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    print('hi')\n    return 0\n"},
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 0, stderr
    assert "\x1b[2K" not in stderr
    assert not any(frame in stderr for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_user_int_return_becomes_exit_code(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 7\n"},
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 7, stderr


def test_user_int_above_255_is_masked(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 256\n"},
    )
    code, _, _ = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 0  # 256 & 0xFF == 0


def test_user_sys_exit_propagates(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "import sys\ndef main():\n    sys.exit(42)\n"},
    )
    code, _, _ = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 42


def test_user_exception_exits_1_with_traceback(tmp_path: Path) -> None:
    # Spec 03 §10: user-code exceptions propagate; Python's default excepthook
    # prints the traceback and the process exits 1.
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    raise RuntimeError('boom')\n"},
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 1
    assert "RuntimeError" in stderr
    assert "boom" in stderr


def test_dotted_module_entry_point(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={
            "mypkg/__init__.py": "",
            "mypkg/cli.py": "def main():\n    print('from cli')\n    return 0\n",
        },
        env_dict=valid_env(entry_point="mypkg.cli:main"),
    )
    code, stdout, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 0, stderr
    assert "from cli" in stdout


def test_dotted_attribute_entry_point(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={
            "myapp.py": (
                "class Foo:\n"
                "    @staticmethod\n"
                "    def bar():\n"
                "        print('Foo.bar')\n"
                "        return 0\n"
            ),
        },
        env_dict=valid_env(entry_point="myapp:Foo.bar"),
    )
    code, stdout, _ = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 0
    assert "Foo.bar" in stdout


# ---------- cache fast path ----------


def test_second_run_hits_cache(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    print('hello')\n    return 0\n"},
    )
    env = isolated_env(tmp_path)
    code1, out1, _ = run_pyz(pyz, env=env)
    code2, out2, _ = run_pyz(pyz, env=env)
    assert code1 == 0 == code2
    assert out1 == out2
    cache = Path(env["MOONLIT_ROOT"])
    assert cache.is_dir()
    site_dirs = list(cache.rglob("site-packages"))
    assert len(site_dirs) == 1


# ---------- env vars ----------


def test_moonlit_force_extract_re_extracts(tmp_path: Path) -> None:
    original = "def main():\n    print('original')\n    return 0\n"
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": original},
    )
    env = isolated_env(tmp_path)

    # 1) First run populates the cache.
    code, _, _ = run_pyz(pyz, env=env)
    assert code == 0

    # 2) Mutate the cached myapp.py.
    cache = Path(env["MOONLIT_ROOT"])
    cached_files = list(cache.rglob("myapp.py"))
    assert len(cached_files) == 1, cached_files
    cached_main = cached_files[0]
    mutated = "def main():\n    print('mutated')\n    return 99\n"
    cached_main.write_text(mutated, encoding="utf-8")

    # 3) Without FORCE_EXTRACT: cache hit, the bootstrap does NOT touch the
    # cached source. We assert via file content rather than re-running the
    # script: CPython's `.pyc` invalidation compares only second-resolution
    # mtime + size (per importlib._bootstrap_external), so on a fast runner
    # where the mutation lands in the same wall-clock second as the first
    # run's `.pyc` AND the byte count is unchanged, Python serves the stale
    # bytecode. That's a Python behavior, orthogonal to moonlit's contract.
    run_pyz(pyz, env=env)
    assert cached_main.read_text(encoding="utf-8") == mutated

    # 4) With FORCE_EXTRACT: re-extraction under the lock restores the
    # archive's original source content (atomic_replace_dir wipes the old
    # cache including any `__pycache__`, so the next import recompiles fresh).
    env_force = dict(env)
    env_force["MOONLIT_FORCE_EXTRACT"] = "1"
    code, stdout, _ = run_pyz(pyz, env=env_force)
    assert code == 0
    assert "original" in stdout
    assert cached_main.read_text(encoding="utf-8") == original


def test_moonlit_force_extract_zero_is_truthy(tmp_path: Path) -> None:
    # Spec 03 §9: '0' is non-empty hence truthy. Surprising but documented.
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    print('orig')\n    return 0\n"},
    )
    env = isolated_env(tmp_path)
    run_pyz(pyz, env=env)  # populate
    cached_main = next(Path(env["MOONLIT_ROOT"]).rglob("myapp.py"))
    cached_main.write_text("def main():\n    print('mutated')\n    return 5\n", encoding="utf-8")

    env_zero = dict(env)
    env_zero["MOONLIT_FORCE_EXTRACT"] = "0"  # non-empty → truthy
    code, stdout, _ = run_pyz(pyz, env=env_zero)
    assert code == 0
    assert "orig" in stdout


def test_moonlit_entry_point_overrides(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={
            "real.py": "def main():\n    print('real')\n    return 0\n",
            "fake.py": "def main():\n    print('fake')\n    return 0\n",
        },
        env_dict=valid_env(entry_point="real:main"),
    )
    env = isolated_env(tmp_path, MOONLIT_ENTRY_POINT="fake:main")
    code, stdout, _ = run_pyz(pyz, env=env)
    assert code == 0
    assert "fake" in stdout
    assert "real" not in stdout


def test_moonlit_root_directs_cache(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
    )
    custom_cache = tmp_path / "very_custom"
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(custom_cache)
    code, _, _ = run_pyz(pyz, env=env)
    assert code == 0
    assert custom_cache.is_dir()
    assert any(custom_cache.iterdir())


def test_moonlit_debug_prints_traceback(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "broken.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
        omit_env_json=True,
    )
    env = isolated_env(tmp_path, MOONLIT_DEBUG="1")
    code, _, stderr = run_pyz(pyz, env=env)
    assert code == 1
    assert "moonlit:" in stderr
    assert "Traceback" in stderr


def test_no_moonlit_debug_no_traceback(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "broken.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
        omit_env_json=True,
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 1
    assert "moonlit:" in stderr
    assert "Traceback" not in stderr


# ---------- error paths (D3 runtime exit codes) ----------


def test_missing_env_json_exits_1(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "broken.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
        omit_env_json=True,
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 1
    assert "env.json missing" in stderr


def test_invalid_entry_point_format_in_env_json_exits_1(tmp_path: Path) -> None:
    # Validation in environment.load (D8 step 9) catches before runner runs.
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    pass\n"},
        env_dict=valid_env(entry_point="invalid_no_colon"),
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 1
    assert "entry_point" in stderr
    assert "failed validation" in stderr


def test_module_not_importable_exits_2(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    pass\n"},
        env_dict=valid_env(entry_point="missing_module:main"),
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 2
    assert "cannot import" in stderr


def test_attribute_not_found_exits_2(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "# no main here\n"},
        env_dict=valid_env(entry_point="myapp:main"),
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 2
    assert "attribute main not found" in stderr


def test_uncoercible_return_value_exits_2(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return [1, 2, 3]\n"},
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 2
    assert "uncoercible" in stderr


def test_bootstrap_collision_exits_1(tmp_path: Path) -> None:
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={
            "_bootstrap/marker.py": "# stowaway\n",
            "myapp.py": "def main():\n    return 0\n",
        },
    )
    code, _, stderr = run_pyz(pyz, env=isolated_env(tmp_path))
    assert code == 1
    assert "_bootstrap collision" in stderr


# ---------- bundled site-packages precedence (D26) ----------


_REPORT_SHADOWPKG = (
    "import importlib.metadata\n"
    "import shadowpkg\n"
    "def main():\n"
    "    print(f'module={shadowpkg.VERSION}')\n"
    "    print(f\"metadata={importlib.metadata.version('shadowpkg')}\")\n"
    "    return 0\n"
)


def _shadowpkg_files(version: str) -> dict[str, str]:
    """A one-module distribution ``shadowpkg`` plus its dist-info, site-packages-relative."""
    return {
        "shadowpkg.py": f"VERSION = {version!r}\n",
        f"shadowpkg-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: shadowpkg\nVersion: {version}\n"
        ),
    }


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _make_venv(venv_dir: Path) -> tuple[Path, Path]:
    """Create a stdlib venv; return (its python, the site-packages it reports)."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], check=True, timeout=60
    )
    if os.name == "nt":
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"
    # Ask the venv rather than hard-coding lib/python3.X vs Lib layouts.
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    return python, Path(purelib)


def test_bundled_distribution_beats_stale_host_copy(tmp_path: Path) -> None:
    # The venv running the .pyz holds a stale shadowpkg 1.0; the bundled 2.0 must
    # win for both the import and its distribution metadata.
    venv_python, venv_site = _make_venv(tmp_path / "venv")
    _write_tree(venv_site, _shadowpkg_files("1.0"))
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={**_shadowpkg_files("2.0"), "myapp.py": _REPORT_SHADOWPKG},
    )
    env = isolated_env(tmp_path)
    env.pop("PYTHONPATH", None)  # PYTHONPATH outranks the bundle by design.
    code, stdout, stderr = run_pyz(pyz, env=env, interpreter=venv_python)
    assert code == 0, stderr
    assert stdout.splitlines() == ["module=2.0", "metadata=2.0"]


def test_pythonpath_outranks_bundled_distribution(tmp_path: Path) -> None:
    # The documented operator override: PYTHONPATH sits ahead of the bundled tree.
    override = tmp_path / "override"
    _write_tree(override, {"shadowpkg.py": "VERSION = 'override'\n"})
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={**_shadowpkg_files("2.0"), "myapp.py": _REPORT_SHADOWPKG},
    )
    code, stdout, stderr = run_pyz(pyz, env=isolated_env(tmp_path, PYTHONPATH=str(override)))
    assert code == 0, stderr
    assert "module=override" in stdout


def test_host_site_dir_on_pythonpath_cannot_lift_bundle_above_stdlib(tmp_path: Path) -> None:
    # Docker pattern: PYTHONPATH names the venv's own site-packages. site.py dedupes,
    # so that dir sits only in its PYTHONPATH slot, ahead of the stdlib. The bundled
    # stdlib-named module must not win, and the venv copy of shadowpkg keeps
    # precedence because PYTHONPATH outranks the bundle.
    venv_python, venv_site = _make_venv(tmp_path / "venv")
    _write_tree(venv_site, _shadowpkg_files("1.0"))
    probe_argparse = (
        "import argparse\n"
        "print('argparse=bundled' if hasattr(argparse, 'BUNDLED') else 'argparse=stdlib')\n"
    )
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={
            **_shadowpkg_files("2.0"),
            "argparse.py": "BUNDLED = True\n",
            "myapp.py": probe_argparse + _REPORT_SHADOWPKG,
        },
    )
    env = isolated_env(tmp_path, PYTHONPATH=str(venv_site))
    code, stdout, stderr = run_pyz(pyz, env=env, interpreter=venv_python)
    assert code == 0, stderr
    assert stdout.splitlines() == ["argparse=stdlib", "module=1.0", "metadata=1.0"]


# ---------- arch §10 invariants under real subprocess execution ----------


def test_only_site_packages_prefix_is_extracted_to_cache(tmp_path: Path) -> None:
    # D1: only the site-packages/ prefix lands in the cache; _bootstrap/, env.json,
    # and __main__.py stay in the archive.
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
    )
    env = isolated_env(tmp_path)
    run_pyz(pyz, env=env)
    cache = Path(env["MOONLIT_ROOT"])
    site_dirs = list(cache.rglob("site-packages"))
    assert len(site_dirs) == 1
    site_dir = site_dirs[0]
    # Cache should contain only the user's myapp.py (plus .pyc bytecode that
    # CPython auto-generates on first import — not extracted by the bootstrap).
    extracted = {
        p.name
        for p in site_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    assert extracted == {"myapp.py"}
    assert not (site_dir.parent / "_bootstrap").exists()
    assert not (site_dir / "_bootstrap").exists()
    assert not (site_dir / "env.json").exists()


def test_cache_key_uses_pep503_normalized_name(tmp_path: Path) -> None:
    # D5: cache_key = f"{normalized_name}_{build_id}"; raw env.json.name is "My_App.Name".
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
        env_dict=valid_env(name="My_App.Name", build_id="b" * 64),
    )
    env = isolated_env(tmp_path)
    run_pyz(pyz, env=env)
    cache = Path(env["MOONLIT_ROOT"])
    expected_key_dir = cache / f"my-app-name_{'b' * 64}"
    assert expected_key_dir.is_dir()


# ---------- runtime cache self-GC (spec 04 §12.2 / D24) ----------


def _gc_env(build_id: str, *, keep_latest: int = 1, grace_seconds: int = 0) -> dict:
    e = valid_env(build_id=build_id)
    e["gc"] = {"enabled": True, "keep_latest": keep_latest, "grace_seconds": grace_seconds}
    return e


def test_repeated_builds_reap_old_cache(tmp_path: Path) -> None:
    # Two builds of the same app (different build_ids); running the second reaps
    # the first, keeping only the most recent (keep_latest=1, grace 0).
    user = {"myapp.py": "def main():\n    print('ran')\n    return 0\n"}
    pyz_v1 = make_test_pyz(tmp_path / "v1.pyz", user_modules=user, env_dict=_gc_env("a" * 64))
    pyz_v2 = make_test_pyz(tmp_path / "v2.pyz", user_modules=user, env_dict=_gc_env("b" * 64))
    env = isolated_env(tmp_path)
    cache = Path(env["MOONLIT_ROOT"])

    code1, out1, err1 = run_pyz(pyz_v1, env=env)
    assert code1 == 0, err1
    assert "ran" in out1
    assert (cache / f"myapp_{'a' * 64}").is_dir()

    code2, out2, err2 = run_pyz(pyz_v2, env=env)
    assert code2 == 0, err2
    assert "ran" in out2
    assert (cache / f"myapp_{'b' * 64}").is_dir()  # the new build survives
    assert not (cache / f"myapp_{'a' * 64}").exists()  # the prior build was reaped


def test_no_gc_env_var_preserves_old_entries(tmp_path: Path) -> None:
    # MOONLIT_NO_GC is the recipient escape hatch: it disables reaping even when
    # the artifact was built with GC on.
    user = {"myapp.py": "def main():\n    return 0\n"}
    pyz_v1 = make_test_pyz(tmp_path / "v1.pyz", user_modules=user, env_dict=_gc_env("a" * 64))
    pyz_v2 = make_test_pyz(tmp_path / "v2.pyz", user_modules=user, env_dict=_gc_env("b" * 64))
    env = isolated_env(tmp_path, MOONLIT_NO_GC="1")
    cache = Path(env["MOONLIT_ROOT"])

    assert run_pyz(pyz_v1, env=env)[0] == 0
    assert run_pyz(pyz_v2, env=env)[0] == 0
    assert (cache / f"myapp_{'a' * 64}").is_dir()  # preserved — GC suppressed
    assert (cache / f"myapp_{'b' * 64}").is_dir()


def test_gc_reaps_silently(tmp_path: Path) -> None:
    # §14 silent-on-success: reaping emits nothing on stdout/stderr by default.
    user = {"myapp.py": "def main():\n    return 0\n"}
    pyz_v1 = make_test_pyz(tmp_path / "v1.pyz", user_modules=user, env_dict=_gc_env("a" * 64))
    pyz_v2 = make_test_pyz(tmp_path / "v2.pyz", user_modules=user, env_dict=_gc_env("b" * 64))
    env = isolated_env(tmp_path)
    run_pyz(pyz_v1, env=env)
    _code, out, err = run_pyz(pyz_v2, env=env)
    assert out == ""
    assert "pruned" not in err
    assert err == ""
