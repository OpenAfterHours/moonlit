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
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(pyz)],
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
