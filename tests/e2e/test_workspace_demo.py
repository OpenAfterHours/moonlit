"""End-to-end test of the canonical demo workspace (CLAUDE.md / spec 02 §10).

Builds a real ``greeter``+``shouter`` uv workspace, runs the moonlit CLI
against it (spawning real ``uv`` subprocesses for export, build-wheel, and
pip-install-target), then runs the produced ``.pyz`` and asserts that
``shouter.cli:main`` correctly imports the ``greeter`` workspace dep and
prints "HELLO FROM GREETER".

Skipped if ``uv`` isn't on PATH (per CLAUDE.md "Skip if uv isn't on PATH").
This test is slower than the rest of the suite — it shells out to uv for
``uv lock``, ``uv export``, ``uv build``, and several ``uv pip install``s.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_UV_AVAILABLE = shutil.which("uv") is not None
pytestmark = pytest.mark.skipif(not _UV_AVAILABLE, reason="uv not on PATH")


def _make_demo_workspace(root: Path) -> Path:
    """Build the canonical greeter+shouter workspace at ``root``."""
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
        "def greet():\n"
        '    return "hello from greeter"\n',
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
        "from greeter import greet\n"
        "\n"
        "def main():\n"
        "    print(greet().upper())\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return root


def _run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _isolated_runtime_env(cache_root: Path) -> dict[str, str]:
    """Inherit parent env, strip MOONLIT_*, point MOONLIT_ROOT at a tmp dir."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(cache_root)
    return env


@pytest.fixture
def demo_workspace(tmp_path: Path) -> Path:
    """Workspace + ``uv lock`` ready for moonlit to build."""
    root = _make_demo_workspace(tmp_path / "moonlit-demo")
    proc = _run_subprocess(["uv", "lock"], cwd=root)
    if proc.returncode != 0:
        pytest.skip(
            f"uv lock failed (network unavailable?):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    assert (root / "uv.lock").exists()
    return root


def test_canonical_demo_shouter_imports_greeter(
    demo_workspace: Path, tmp_path: Path
) -> None:
    output_pyz = tmp_path / "shouter.pyz"

    # Step 1: invoke the moonlit CLI as a subprocess against the demo workspace.
    build_proc = _run_subprocess(
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
            "-o",
            str(output_pyz),
        ],
    )
    assert build_proc.returncode == 0, (
        f"moonlit build failed:\n"
        f"stdout={build_proc.stdout}\nstderr={build_proc.stderr}"
    )
    assert output_pyz.is_file()
    # Spec 01 §8: success line on stdout.
    assert build_proc.stdout.strip().startswith("wrote ")
    assert "entries)" in build_proc.stdout

    # Step 2: run the produced .pyz with an isolated cache.
    cache_root = tmp_path / "cache"
    runtime_env = _isolated_runtime_env(cache_root)
    run_proc = _run_subprocess(
        [sys.executable, str(output_pyz)], env=runtime_env, timeout=60
    )
    assert run_proc.returncode == 0, (
        f"pyz run failed:\nstdout={run_proc.stdout}\nstderr={run_proc.stderr}"
    )
    assert "HELLO FROM GREETER" in run_proc.stdout

    # The cache populated under the per-test MOONLIT_ROOT.
    assert cache_root.is_dir()
    site_dirs = list(cache_root.rglob("site-packages"))
    assert len(site_dirs) == 1
    site_dir = site_dirs[0]
    # Both workspace members landed in site-packages (D2 transitive deps).
    assert (site_dir / "greeter" / "__init__.py").is_file()
    assert (site_dir / "shouter" / "cli.py").is_file()
    # Cache key is PEP-503 normalized name + build_id (D5).
    assert site_dir.parent.name.startswith("shouter_")

    # Step 3: re-run with MOONLIT_FORCE_EXTRACT — same output, archive content
    # restored under the lock.
    force_env = dict(runtime_env)
    force_env["MOONLIT_FORCE_EXTRACT"] = "1"
    force_proc = _run_subprocess(
        [sys.executable, str(output_pyz)], env=force_env, timeout=60
    )
    assert force_proc.returncode == 0
    assert "HELLO FROM GREETER" in force_proc.stdout

    # Step 4: third run is a cache hit (D14 fast path) — same output again.
    hit_proc = _run_subprocess(
        [sys.executable, str(output_pyz)], env=runtime_env, timeout=60
    )
    assert hit_proc.returncode == 0
    assert "HELLO FROM GREETER" in hit_proc.stdout


def test_canonical_demo_negative_unknown_package(
    demo_workspace: Path, tmp_path: Path
) -> None:
    # Spec invariant I5 / spec 01 exit-5 path: --package nonexistent → exit 5.
    output_pyz = tmp_path / "out.pyz"
    proc = _run_subprocess(
        [
            sys.executable,
            "-m",
            "moonlit",
            "build",
            str(demo_workspace),
            "--package",
            "nonexistent",
            "-e",
            "x:y",
            "-o",
            str(output_pyz),
        ],
    )
    assert proc.returncode == 5
    assert "UnknownPackageError:" in proc.stderr
    assert "greeter" in proc.stderr
    assert "shouter" in proc.stderr
    assert not output_pyz.exists()


def test_canonical_demo_negative_missing_uv_lock(tmp_path: Path) -> None:
    # Spec exit 4: missing uv.lock.
    root = _make_demo_workspace(tmp_path / "no_lock")
    # No uv lock here.
    output_pyz = tmp_path / "out.pyz"
    proc = _run_subprocess(
        [
            sys.executable,
            "-m",
            "moonlit",
            "build",
            str(root),
            "--package",
            "shouter",
            "-e",
            "shouter.cli:main",
            "-o",
            str(output_pyz),
        ],
    )
    assert proc.returncode == 4
    assert "NoLockfileError:" in proc.stderr
