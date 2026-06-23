"""Pin _bootstrap/reap.reap to specs/04-cache-layout.md §12.2 and D24.

Runtime cache self-GC: after a fresh slow-path extraction, the bootstrap reaps
OLDER cache entries of the SAME app, keeping the newest ``keep_latest`` and
only those past the age grace, never crossing app boundaries, never the
just-installed key, never a lock-held entry, and never raising.

NB on test mode: like test_extract.py / test_environment.py these unit tests
drive the logic via direct import as a dev-time TDD harness; the e2e suite is
the contract.
"""

import os
from pathlib import Path

import pytest

from moonlit import clean as clean_mod
from moonlit._bootstrap import locking, reap
from moonlit._bootstrap.environment import Environment

# ---------- helpers ----------

NOW = 1_000_000_000.0
DAY = 86400.0
H64 = {c: c * 64 for c in "0123456789abcdef"}


def env_with(
    *,
    name: str = "myapp",
    build_id: str,
    gc: dict | None = None,
) -> Environment:
    return Environment(
        schema_version=1,
        name=name,
        build_id=build_id,
        entry_point="myapp.cli:main",
        built_at="2026-05-08T15:23:01Z",
        moonlit_version="0.1.0",
        python_shebang="/usr/bin/env python3",
        gc=gc,
    )


def make_entry(cache_root: Path, cache_key: str, *, mtime: float) -> Path:
    """Create <cache_root>/<cache_key>/site-packages/ with one file and an mtime."""
    sp = cache_root / cache_key / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "f.py").write_text("x", encoding="utf-8")
    os.utime(sp, (mtime, mtime))
    return cache_root / cache_key


def gc(enabled: bool = True, keep_latest: int = 2, grace_seconds: int = 0) -> dict:
    # grace defaults to 0 in tests so age never interferes unless a test opts in.
    return {"enabled": enabled, "keep_latest": keep_latest, "grace_seconds": grace_seconds}


@pytest.fixture(autouse=True)
def _clear_gc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MOONLIT_NO_GC", "MOONLIT_GC_KEEP_LATEST", "MOONLIT_GC_GRACE", "MOONLIT_DEBUG"):
        monkeypatch.delenv(var, raising=False)


# ---------- keep-latest selection ----------


def test_keeps_keep_latest_newest_same_name(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old1, old2 = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old1}", mtime=NOW - 10 * DAY)
    make_entry(cache_root, f"myapp_{old2}", mtime=NOW - 20 * DAY)

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=2)), cache_root, now=NOW)

    assert (cache_root / f"myapp_{cur}").is_dir()
    assert (cache_root / f"myapp_{old1}").is_dir()  # 2nd-newest survives keep_latest=2
    assert not (cache_root / f"myapp_{old2}").exists()  # oldest reaped


def test_keep_latest_one_reaps_all_predecessors(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old1, old2 = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old1}", mtime=NOW - 10 * DAY)
    make_entry(cache_root, f"myapp_{old2}", mtime=NOW - 20 * DAY)

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert (cache_root / f"myapp_{cur}").is_dir()
    assert not (cache_root / f"myapp_{old1}").exists()
    assert not (cache_root / f"myapp_{old2}").exists()


# ---------- cross-app isolation ----------


def test_never_reaps_other_app_names(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)
    other = make_entry(cache_root, f"otherapp_{H64['c']}", mtime=NOW - 99 * DAY)

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert not (cache_root / f"myapp_{old}").exists()
    assert other.is_dir(), "a different app's cache must never be reaped"


def test_normalized_name_matches_same_app(tmp_path: Path) -> None:
    # D5: env.name "My_App" normalizes to "my-app"; the on-disk key uses the
    # normalized form. The reaper must match the same app across name spellings.
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"my-app_{cur}", mtime=NOW)
    make_entry(cache_root, f"my-app_{old}", mtime=NOW - 10 * DAY)

    reap.reap(env_with(name="My_App", build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert (cache_root / f"my-app_{cur}").is_dir()
    assert not (cache_root / f"my-app_{old}").exists()


# ---------- never the just-installed key ----------


def test_excludes_just_installed_key(tmp_path: Path) -> None:
    # Even when the current key is NOT the newest by mtime, it is never reaped.
    cache_root = tmp_path / "cache"
    cur, newer, old = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW - 10 * DAY)  # current, not newest
    make_entry(cache_root, f"myapp_{newer}", mtime=NOW)  # newest by mtime
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 100 * DAY)

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert (cache_root / f"myapp_{cur}").is_dir(), "current key must never be reaped"
    assert not (cache_root / f"myapp_{old}").exists()


# ---------- cooperative liveness ----------


def test_skips_entry_whose_lock_is_held(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)

    lock_path = cache_root / f"myapp_{old}.lock"
    fd = locking.try_acquire_nonblocking(lock_path)
    assert fd is not None
    try:
        reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)
        assert (cache_root / f"myapp_{old}").is_dir(), "lock-held entry must be skipped"
    finally:
        locking.release(fd, lock_path)


# ---------- age grace ----------


def test_grace_skips_recent_entry(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 60)  # 1 minute ago

    reap.reap(
        env_with(build_id=cur, gc=gc(keep_latest=1, grace_seconds=int(DAY))),
        cache_root,
        now=NOW,
    )
    assert (cache_root / f"myapp_{old}").is_dir(), "entry within grace must be skipped"


def test_grace_reaps_entry_past_window(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 2 * DAY)  # well past 24h

    reap.reap(
        env_with(build_id=cur, gc=gc(keep_latest=1, grace_seconds=int(DAY))),
        cache_root,
        now=NOW,
    )
    assert not (cache_root / f"myapp_{old}").exists()


# ---------- enabled / disabled gating ----------


def test_disabled_when_gc_enabled_false(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)

    reap.reap(env_with(build_id=cur, gc=gc(enabled=False, keep_latest=1)), cache_root, now=NOW)
    assert (cache_root / f"myapp_{old}").is_dir()


def test_disabled_by_moonlit_no_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)

    monkeypatch.setenv("MOONLIT_NO_GC", "1")
    reap.reap(env_with(build_id=cur, gc=gc(enabled=True, keep_latest=1)), cache_root, now=NOW)
    assert (cache_root / f"myapp_{old}").is_dir()


def test_defaults_apply_when_gc_field_absent(tmp_path: Path) -> None:
    # Old archive: env.gc is None → built-in defaults (enabled, keep 2). With
    # grace defaulting to 24h, an entry 10 days old past keep_latest is reaped.
    cache_root = tmp_path / "cache"
    cur, old1, old2 = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old1}", mtime=NOW - 10 * DAY)
    make_entry(cache_root, f"myapp_{old2}", mtime=NOW - 20 * DAY)

    reap.reap(env_with(build_id=cur, gc=None), cache_root, now=NOW)

    assert (cache_root / f"myapp_{cur}").is_dir()
    assert (cache_root / f"myapp_{old1}").is_dir()  # keep_latest default 2
    assert not (cache_root / f"myapp_{old2}").exists()


# ---------- runtime env overrides ----------


def test_keep_latest_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "cache"
    cur, old1, old2 = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old1}", mtime=NOW - 10 * DAY)
    make_entry(cache_root, f"myapp_{old2}", mtime=NOW - 20 * DAY)

    # env.json says keep 2; the recipient override pins keep 1.
    monkeypatch.setenv("MOONLIT_GC_KEEP_LATEST", "1")
    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=2)), cache_root, now=NOW)

    assert (cache_root / f"myapp_{cur}").is_dir()
    assert not (cache_root / f"myapp_{old1}").exists()
    assert not (cache_root / f"myapp_{old2}").exists()


def test_grace_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 60)  # 1 min ago

    # env.json grace is 0; recipient raises it to a day → recent entry spared.
    monkeypatch.setenv("MOONLIT_GC_GRACE", str(int(DAY)))
    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1, grace_seconds=0)), cache_root, now=NOW)
    assert (cache_root / f"myapp_{old}").is_dir()


@pytest.mark.parametrize("bad", ["", "abc", "-1", "0", "1.5", "2x"])
def test_malformed_keep_latest_env_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    # A malformed override must never crash and must fall back to env.json.
    cache_root = tmp_path / "cache"
    cur, old1, old2 = H64["a"], H64["b"], H64["c"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old1}", mtime=NOW - 10 * DAY)
    make_entry(cache_root, f"myapp_{old2}", mtime=NOW - 20 * DAY)

    monkeypatch.setenv("MOONLIT_GC_KEEP_LATEST", bad)
    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=2)), cache_root, now=NOW)
    # Fell back to keep 2 → second-newest survives.
    assert (cache_root / f"myapp_{old1}").is_dir()
    assert not (cache_root / f"myapp_{old2}").exists()


# ---------- best-effort: never raise, never break the run ----------


def test_errors_are_swallowed_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(reap.shutil, "rmtree", boom)
    # Must not raise; the run continues even if reclaim fails.
    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)
    assert (cache_root / f"myapp_{old}").is_dir()  # rmtree failed → entry survives


def test_missing_cache_root_is_noop(tmp_path: Path) -> None:
    cache_root = tmp_path / "does-not-exist"
    reap.reap(env_with(build_id=H64["a"], gc=gc(keep_latest=1)), cache_root, now=NOW)
    assert not cache_root.exists()


# ---------- unknown / orphan entries are left alone ----------


def test_ignores_unknown_and_orphan_names(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)

    # Future reserved subtree, foreign dir, and orphan staging of another key.
    (cache_root / "v2").mkdir()
    (cache_root / "not-a-key").mkdir()
    orphan_tmp = cache_root / f".otherapp_{H64['c']}.tmp.123"
    orphan_tmp.mkdir()
    orphan_old = cache_root / f".otherapp_{H64['c']}.old.456"
    orphan_old.mkdir()

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert not (cache_root / f"myapp_{old}").exists()  # eligible cache entry reaped
    assert (cache_root / "v2").is_dir()  # reserved subtree untouched
    assert (cache_root / "not-a-key").is_dir()  # foreign dir untouched
    assert orphan_tmp.is_dir()  # orphan reaping is clean's job, not the bootstrap's
    assert orphan_old.is_dir()


# ---------- lock-file hygiene ----------


def test_unlinks_victim_lock_after_successful_reap(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cur, old = H64["a"], H64["b"]
    make_entry(cache_root, f"myapp_{cur}", mtime=NOW)
    make_entry(cache_root, f"myapp_{old}", mtime=NOW - 10 * DAY)
    lock_path = cache_root / f"myapp_{old}.lock"
    lock_path.touch()

    reap.reap(env_with(build_id=cur, gc=gc(keep_latest=1)), cache_root, now=NOW)

    assert not (cache_root / f"myapp_{old}").exists()
    assert not lock_path.exists(), "the reaped entry's now-orphaned lock should be unlinked"


# ---------- classifier parity with clean.py (guards drift; D7 forbids import) ----------


def test_classifier_matches_clean_parse_cache_key() -> None:
    names = [
        f"myapp_{H64['a']}",  # valid
        f"my-app_{H64['b']}",  # valid (hyphenated)
        f"a_b_{H64['c']}",  # head with an underscore: rfind splits on the last
        "nounderscore",  # no separator
        f"x_{'g' * 64}",  # non-hex tail
        f"_{H64['d']}",  # empty head
        f"myapp_{'a' * 63}",  # tail too short
        f"myapp_{'a' * 65}",  # tail too long
        f"MyApp_{H64['e']}",  # uppercase head (round-tripped on disk)
    ]
    for name in names:
        assert reap._parse_cache_key(name) == clean_mod._parse_cache_key(name), name
