"""End-to-end test of `moonlit build --bundle-python` (D21 folder-bundle redesign).

Builds a real `greeter`+`shouter` workspace, asks moonlit to bundle a CPython
interpreter into a folder alongside the application zipapp, then runs the
folder's launcher .exe with **PATH cleared** to prove that the bundled
interpreter — not a system Python — is doing the work.

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


def _path_stripped_env() -> dict[str, str]:
    """Env for running the bundled launcher with PATH cleared.

    The whole point of bundling is to work without Python on PATH. We
    deliberately strip every variable except the minimal Windows essentials
    (SystemRoot, SYSTEMDRIVE, COMSPEC, TEMP/TMP) so a system Python cannot
    "rescue" the run. LOCALAPPDATA is preserved because the bootstrap inside
    the inner .pyz uses it for its own site-packages extract cache (D5/D6) —
    that's the moonlit per-build cache, not the bundled-Python cache (which
    no longer exists in the D21 redesign).
    """
    keep = ("SystemRoot", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = ""
    # Strip any MOONLIT_* override so we exercise the default cache layout.
    return env


def test_bundle_python_runs_with_path_cleared(demo_workspace: Path, tmp_path: Path) -> None:
    """D21 happy path: build → run launcher with PATH="" → expected stdout."""
    out_dir = tmp_path / "out" / "shouter"
    out_dir.parent.mkdir(parents=True)

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
            "--bundle-python",
            "-o",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build_proc.returncode == 0, (
        f"moonlit build failed:\nstdout={build_proc.stdout}\nstderr={build_proc.stderr}"
    )

    # 1. Output is a directory with the expected three children.
    assert out_dir.is_dir()
    basename = out_dir.name
    launcher = out_dir / f"{basename}.exe"
    app_pyz = out_dir / f"{basename}.pyz"
    python_dir = out_dir / "_python"
    assert launcher.is_file(), f"missing launcher .exe: {launcher}"
    assert app_pyz.is_file(), f"missing app .pyz: {app_pyz}"
    assert python_dir.is_dir(), f"missing _python/ dir: {python_dir}"
    assert (python_dir / "python.exe").is_file()

    # 2. Launcher is a PE with no appended zip — this is the AV-relevant win.
    assert launcher.read_bytes()[:2] == b"MZ"
    assert not zipfile.is_zipfile(launcher), (
        "launcher .exe must NOT contain a trailing zip body — that's the "
        "AV-trippy self-extracting-archive pattern the D21 redesign retired."
    )

    # 3. Inner .pyz IS a zipfile with the standard moonlit layout.
    assert zipfile.is_zipfile(app_pyz)
    with zipfile.ZipFile(app_pyz, "r") as zf:
        names = zf.namelist()
        assert "env.json" in names
        assert "__main__.py" in names
        assert any(n.startswith("site-packages/") for n in names)
        # env.json no longer carries a bundled_python field (D21h).
        env_text = zf.read("env.json").decode("utf-8")
    assert '"bundled_python"' not in env_text

    # 4. Run the launcher with PATH cleared and verify the bundled Python ran.
    env = _path_stripped_env()
    run_proc = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert run_proc.returncode == 0, (
        f"bundled exe failed:\nstdout={run_proc.stdout}\nstderr={run_proc.stderr}"
    )
    assert "HELLO FROM GREETER" in run_proc.stdout

    # 5. Second run is a fast-path cache hit on the inner bootstrap — same
    # output, no failure.
    run_proc2 = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert run_proc2.returncode == 0
    assert "HELLO FROM GREETER" in run_proc2.stdout
