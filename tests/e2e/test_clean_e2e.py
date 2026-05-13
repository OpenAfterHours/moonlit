"""End-to-end tests for ``moonlit clean`` against a real bootstrap-populated cache.

Builds a fixture .pyz (with a real ``_bootstrap`` copy + a tiny user module),
runs it via ``python <pyz>`` with ``MOONLIT_ROOT`` pointed at a tmp dir to
populate the cache, then exercises ``python -m moonlit clean`` against that
same root. The contract is: ``--dry-run`` does not modify; ``--all`` reaps
the cache_key directory and its sibling ``.lock``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# tests/e2e/ is not a package; share helpers via sys.path injection.
sys.path.insert(0, str(Path(__file__).parent))
from test_bootstrap_e2e import make_test_pyz, run_pyz, valid_env


def _run_moonlit(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "moonlit", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _populate_cache(tmp_path: Path) -> tuple[Path, Path]:
    """Build + run a fixture .pyz so the bootstrap materializes a cache_key.

    Returns ``(cache_root, cache_key_dir)``.
    """
    cache_root = tmp_path / "moonlit_cache"
    build_id = "b" * 64
    pyz = make_test_pyz(
        tmp_path / "app.pyz",
        user_modules={"myapp.py": "def main():\n    return 0\n"},
        env_dict=valid_env(build_id=build_id),
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(cache_root)
    code, _stdout, stderr = run_pyz(pyz, env=env)
    assert code == 0, stderr
    cache_key_dir = cache_root / f"myapp_{build_id}"
    assert (cache_key_dir / "site-packages").is_dir(), (
        f"bootstrap did not populate {cache_key_dir}: {stderr}"
    )
    return cache_root, cache_key_dir


def test_clean_dry_run_then_real_against_real_built_pyz(tmp_path: Path) -> None:
    cache_root, cache_key_dir = _populate_cache(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(cache_root)

    # Dry run: nothing should change.
    code, stdout, stderr = _run_moonlit("clean", "--all", "--dry-run", env=env)
    assert code == 0, stderr
    assert "would delete" in stdout
    assert cache_key_dir.is_dir(), "dry run should not touch the FS"

    # Real run: the cache_key directory and its lock should be gone.
    code, stdout, stderr = _run_moonlit("clean", "--all", env=env)
    assert code == 0, stderr
    assert "deleted" in stdout
    assert not cache_key_dir.exists()
    # Any leftover entries are not our cache_key.
    if cache_root.is_dir():
        survivors = [p.name for p in cache_root.iterdir()]
        assert all(not p.startswith("myapp_") for p in survivors), survivors


def test_clean_no_flags_via_subprocess_exits_2(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOONLIT_")}
    env["MOONLIT_ROOT"] = str(tmp_path / "empty_root")
    code, _stdout, stderr = _run_moonlit("clean", env=env)
    assert code == 2
    assert "at least one of" in stderr
