"""End-to-end tests for ``moonlit pack`` (project-less PyPI packing, D25).

Packs real packages from PyPI via ``uv pip compile`` + ``uv pip install``,
then runs the produced ``.pyz`` and/or inspects its bundled site-packages.
No local ``pyproject.toml``/``uv.lock`` is involved — that is the whole point
of ``pack``.

Skipped if ``uv`` isn't on PATH (per CLAUDE.md). Individual tests skip if the
resolve/install step fails for what looks like a network reason, so the suite
stays green offline. Slower than the unit suite — it shells out to real ``uv``.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_UV_AVAILABLE = shutil.which("uv") is not None
pytestmark = pytest.mark.skipif(not _UV_AVAILABLE, reason="uv not on PATH")

_NETWORK_MARKERS = (
    "network",
    "failed to fetch",
    "could not connect",
    "error sending request",
    "temporary failure",
    "no solution found",
    "dns error",
)


def _run(argv: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _pack(*args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "moonlit", "pack", *args], timeout=timeout)


def _skip_if_network(proc: subprocess.CompletedProcess, what: str) -> None:
    """Skip (not fail) when a non-zero uv step looks network-related."""
    if proc.returncode == 0:
        return
    haystack = (proc.stdout + proc.stderr).lower()
    if any(marker in haystack for marker in _NETWORK_MARKERS):
        pytest.skip(f"{what} failed (network unavailable?):\n{proc.stdout}\n{proc.stderr}")


def test_pack_cowsay_runs_with_default_console_script(tmp_path: Path) -> None:
    # `pack cowsay` defaults to `-c cowsay` (D25e) — resolve the console script
    # named after the primary package, exactly as `uvx cowsay` would run it.
    out_pyz = tmp_path / "cowsay.pyz"
    build = _pack("cowsay", "-o", str(out_pyz))
    _skip_if_network(build, "uv pip compile/install")
    assert build.returncode == 0, f"pack failed:\n{build.stdout}\n{build.stderr}"
    assert out_pyz.is_file()
    assert build.stdout.strip().startswith("wrote ")

    # Run the produced .pyz with the SAME interpreter that built it (matching
    # ABI), so the D20c version check passes.
    run = _run([sys.executable, str(out_pyz), "-t", "packed by moonlit"], timeout=60)
    assert run.returncode == 0, f"pyz run failed:\n{run.stdout}\n{run.stderr}"
    assert "packed by moonlit" in run.stdout  # the speech bubble text
    assert "^__^" in run.stdout  # the cow


def test_pack_unions_all_three_sources_into_the_bundle(tmp_path: Path) -> None:
    # positional SPEC + --with + --with-requirements all land in site-packages.
    reqs = tmp_path / "extra-requirements.txt"
    reqs.write_text("idna\n", encoding="utf-8")
    out_pyz = tmp_path / "bundle.pyz"
    build = _pack(
        "cowsay",
        "--with",
        "six",
        "--with-requirements",
        str(reqs),
        # Entry point not executed here; any valid module:attr is fine.
        "-e",
        "cowsay:tux",
        "-o",
        str(out_pyz),
    )
    _skip_if_network(build, "uv pip compile/install")
    assert build.returncode == 0, f"pack failed:\n{build.stdout}\n{build.stderr}"

    with zipfile.ZipFile(out_pyz, "r") as zf:
        names = set(zf.namelist())
    # Primary package (positional).
    assert "site-packages/cowsay/__init__.py" in names
    # --with package (six ships as a single top-level module).
    assert "site-packages/six.py" in names
    # --with-requirements package.
    assert any(n.startswith("site-packages/idna/") for n in names)


def test_pack_requires_no_local_project(tmp_path: Path) -> None:
    # Run from an empty dir with no pyproject.toml / uv.lock: pack must not fail
    # on those grounds (I14). cwd is irrelevant to pack, but assert the artifact
    # builds anyway.
    empty = tmp_path / "empty"
    empty.mkdir()
    out_pyz = tmp_path / "six.pyz"
    build = _run(
        [
            sys.executable,
            "-m",
            "moonlit",
            "pack",
            "six",
            "-e",
            "six:python_2_unicode_compatible",
            "-o",
            str(out_pyz),
        ],
    )
    _skip_if_network(build, "uv pip compile/install")
    assert build.returncode == 0, f"pack failed:\n{build.stdout}\n{build.stderr}"
    assert out_pyz.is_file()
    assert "NoLockfileError" not in build.stderr
    assert "MalformedPyprojectError" not in build.stderr
