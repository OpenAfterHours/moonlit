"""End-to-end test of `moonlit build --windows-exe --bundle-python` (D21/D22).

Builds a real `greeter`+`shouter` workspace, asks moonlit to embed a CPython
interpreter inside the produced .exe, then runs the .exe with **PATH cleared**
to prove that the bundled interpreter is what's actually doing the work.

Slow — shells out to ``uv python install`` which downloads ~30 MiB of
python-build-standalone the first time, and to ``uv build`` for the wheels.
Skip if uv isn't on PATH; skip on non-Windows (Phase 1 of the feature is
Windows-only by design).
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_UV_AVAILABLE = shutil.which("uv") is not None
_WINDOWS = sys.platform == "win32"

pytestmark = [
    pytest.mark.skipif(not _UV_AVAILABLE, reason="uv not on PATH"),
    pytest.mark.skipif(not _WINDOWS, reason="bundled-Python phase 1 is Windows-only"),
]


def _make_demo_workspace(root: Path) -> Path:
    """Mirror of tests/e2e/test_workspace_demo.py::_make_demo_workspace."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n', encoding="utf-8"
    )
    greeter = root / "packages" / "greeter"
    (greeter / "src" / "greeter").mkdir(parents=True)
    (greeter / "pyproject.toml").write_text(
        "[project]\n"
        'name = "greeter"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/greeter"]\n',
        encoding="utf-8",
    )
    (greeter / "src" / "greeter" / "__init__.py").write_text(
        'def greet():\n    return "hello from greeter"\n',
        encoding="utf-8",
    )
    shouter = root / "packages" / "shouter"
    (shouter / "src" / "shouter").mkdir(parents=True)
    (shouter / "pyproject.toml").write_text(
        "[project]\n"
        'name = "shouter"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["greeter"]\n'
        "\n"
        "[tool.uv.sources]\n"
        "greeter = {workspace = true}\n"
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/shouter"]\n',
        encoding="utf-8",
    )
    (shouter / "src" / "shouter" / "__init__.py").write_text("", encoding="utf-8")
    (shouter / "src" / "shouter" / "cli.py").write_text(
        "from greeter import greet\n\ndef main():\n    print(greet().upper())\n    return 0\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def demo_workspace(tmp_path: Path) -> Path:
    root = _make_demo_workspace(tmp_path / "moonlit-demo")
    proc = subprocess.run(
        ["uv", "lock"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"uv lock failed (network unavailable?):\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    assert (root / "uv.lock").exists()
    return root


def _path_stripped_env(local_appdata_override: Path) -> dict[str, str]:
    """Env for running the bundled .exe with PATH cleared.

    The whole point of bundling is to work without Python on PATH. We
    deliberately strip every variable except the minimal Windows essentials
    (SystemRoot, SYSTEMDRIVE, COMSPEC, TEMP/TMP) so a system Python cannot
    "rescue" the run. LOCALAPPDATA is redirected to a per-test directory so
    no real bundled-Python cache from a previous run can satisfy the
    launcher; the first run MUST exercise the full extract path.
    """
    keep = ("SystemRoot", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "USERPROFILE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = ""
    env["LOCALAPPDATA"] = str(local_appdata_override)
    # Strip any MOONLIT_* override so we exercise the default cache layout.
    return env


def test_bundle_python_runs_with_path_cleared(demo_workspace: Path, tmp_path: Path) -> None:
    """The full D21/D22 happy path: build → run with PATH="" → expected stdout."""
    out_exe = tmp_path / "out" / "shouter.exe"
    out_exe.parent.mkdir(parents=True)

    build_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "moonlit",
            "build",
            str(demo_workspace),
            "--package",
            "shouter",
            "-e",
            "shouter.cli:main",
            "--windows-exe",
            "--bundle-python",
            "-o",
            str(out_exe),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build_proc.returncode == 0, (
        f"moonlit build failed:\nstdout={build_proc.stdout}\nstderr={build_proc.stderr}"
    )
    assert out_exe.is_file()
    # MZ header → PE binary in front (launcher prepended).
    assert out_exe.read_bytes()[:2] == b"MZ"

    # The zip body contains _python/ entries plus env.json with bundled_python.
    with zipfile.ZipFile(out_exe, "r") as zf:
        names = zf.namelist()
        assert any(n.startswith("_python/") for n in names)
        assert "_python/python.exe" in names
        env = zf.read("env.json").decode("utf-8")
    assert '"bundled_python"' in env
    assert '"fingerprint"' in env

    # Run with PATH cleared and LOCALAPPDATA redirected.
    local_appdata = tmp_path / "fake-localappdata"
    local_appdata.mkdir()
    env = _path_stripped_env(local_appdata)
    run_proc = subprocess.run(
        [str(out_exe)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert run_proc.returncode == 0, (
        f"bundled exe failed:\nstdout={run_proc.stdout}\nstderr={run_proc.stderr}"
    )
    assert "HELLO FROM GREETER" in run_proc.stdout

    # First-run extraction populated the per-fingerprint cache.
    moonlit_python_root = local_appdata / "moonlit" / "python"
    assert moonlit_python_root.is_dir()
    fingerprint_dirs = [p for p in moonlit_python_root.iterdir() if p.is_dir()]
    assert len(fingerprint_dirs) == 1, fingerprint_dirs
    fp_dir = fingerprint_dirs[0]
    # The cache key is a 64-hex SHA-256, matches the env.json fingerprint, and
    # the dispatched python.exe is present.
    assert len(fp_dir.name) == 64
    assert (fp_dir / "python.exe").is_file()

    # Second run is a fast-path cache hit — no re-extract, same output.
    run_proc2 = subprocess.run(
        [str(out_exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert run_proc2.returncode == 0
    assert "HELLO FROM GREETER" in run_proc2.stdout
