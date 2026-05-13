"""Pin ``moonlit clean`` to specs/01-cli.md §2.4 and specs/04-cache-layout.md §12.1.

Layout-version 1; tests are unit-scale (fake cache roots under ``tmp_path``).
The e2e harness against a real-built .pyz lives in
``tests/e2e/test_clean_e2e.py``.
"""

from __future__ import annotations

import os
import re
import time
from datetime import timedelta
from pathlib import Path

import pytest

from moonlit import clean as clean_mod
from moonlit import errors
from moonlit._bootstrap import environment as bootstrap_env
from moonlit._bootstrap import locking as bootstrap_locking

# ---------- helpers ----------

# 64-char hex stand-ins for build_ids. They differ in the first 8 chars so the
# table truncation in --dry-run output is observable.
HEX_A = "a1b2c3d4" + "0" * 56
HEX_B = "5e6f7890" + "1" * 56
HEX_C = "deadbeef" + "2" * 56
HEX_D = "c0ffee00" + "3" * 56


def _make_cache_entry(
    cache_root: Path,
    cache_key: str,
    *,
    mtime: float | None = None,
    n_files: int = 1,
    file_size: int = 256,
) -> Path:
    """Create ``<cache_root>/<cache_key>/site-packages/`` with ``n_files`` files."""
    sp = cache_root / cache_key / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (sp / f"f{i}.dat").write_bytes(b"x" * file_size)
    if mtime is not None:
        os.utime(sp, (mtime, mtime))
    return sp.parent


def _make_orphan_tmp(cache_root: Path, cache_key: str, pid: int, *, file_size: int = 64) -> Path:
    p = cache_root / f".{cache_key}.tmp.{pid}"
    p.mkdir(parents=True, exist_ok=True)
    (p / "leak.dat").write_bytes(b"y" * file_size)
    return p


def _make_orphan_old(cache_root: Path, cache_key: str, pid: int, *, file_size: int = 64) -> Path:
    p = cache_root / f".{cache_key}.old.{pid}"
    p.mkdir(parents=True, exist_ok=True)
    (p / "leak.dat").write_bytes(b"z" * file_size)
    return p


def _make_lock_file(cache_root: Path, cache_key: str) -> Path:
    p = cache_root / f"{cache_key}.lock"
    p.touch()
    return p


# ---------- shared cache-root resolver (step 2) ----------


def test_clean_uses_same_cache_root_as_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """specs/04-cache-layout.md §12.1 / D23: both consumers MUST resolve to the same path."""
    monkeypatch.delenv("MOONLIT_ROOT", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert clean_mod._resolve_cache_root() == bootstrap_env.resolve_cache_root()


def test_clean_respects_moonlit_root_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MOONLIT_ROOT", str(tmp_path))
    assert clean_mod._resolve_cache_root() == tmp_path.resolve()


# ---------- new error classes ----------


def test_clean_refused_error_has_exit_code_14() -> None:
    assert errors.CleanRefusedError.exit_code == 14
    assert issubclass(errors.CleanRefusedError, errors.MoonlitError)


def test_clean_io_error_has_exit_code_15() -> None:
    assert errors.CleanIOError.exit_code == 15
    assert issubclass(errors.CleanIOError, errors.MoonlitError)


# ---------- _parse_cache_key (step 3) ----------


def test_parse_cache_key_returns_name_and_build_id() -> None:
    assert clean_mod._parse_cache_key(f"myapp_{HEX_A}") == ("myapp", HEX_A)


def test_parse_cache_key_handles_underscore_in_name() -> None:
    # PEP-503 normalized form collapses underscores, but the cache_key on disk
    # can be re-parsed only with split-on-last-underscore semantics.
    assert clean_mod._parse_cache_key(f"my-pkg_{HEX_B}") == ("my-pkg", HEX_B)


def test_parse_cache_key_rejects_no_underscore() -> None:
    assert clean_mod._parse_cache_key("noseparator") is None


def test_parse_cache_key_rejects_non_64hex_suffix() -> None:
    assert clean_mod._parse_cache_key("myapp_short") is None
    assert clean_mod._parse_cache_key(f"myapp_{'a' * 63}") is None  # 63
    assert clean_mod._parse_cache_key(f"myapp_{'a' * 65}") is None  # 65
    assert clean_mod._parse_cache_key(f"myapp_{'g' * 64}") is None  # not hex
    assert clean_mod._parse_cache_key(f"myapp_{'A' * 64}") is None  # uppercase
    assert clean_mod._parse_cache_key(f"_{HEX_A}") is None  # empty name


# ---------- _classify (step 3) ----------


def test_classify_cache_entry() -> None:
    name = f"myapp_{HEX_A}"
    c = clean_mod._classify(name)
    assert c.kind == clean_mod._Kind.CACHE_ENTRY
    assert c.cache_key == name
    assert c.pid is None


def test_classify_lock_file() -> None:
    name = f"myapp_{HEX_A}.lock"
    c = clean_mod._classify(name)
    assert c.kind == clean_mod._Kind.LOCK
    assert c.cache_key == f"myapp_{HEX_A}"
    assert c.pid is None


def test_classify_tmp_orphan() -> None:
    name = f".myapp_{HEX_A}.tmp.42"
    c = clean_mod._classify(name)
    assert c.kind == clean_mod._Kind.TMP
    assert c.cache_key == f"myapp_{HEX_A}"
    assert c.pid == 42


def test_classify_old_orphan() -> None:
    name = f".myapp_{HEX_A}.old.7"
    c = clean_mod._classify(name)
    assert c.kind == clean_mod._Kind.OLD
    assert c.cache_key == f"myapp_{HEX_A}"
    assert c.pid == 7


def test_classify_unknown_random_name() -> None:
    c = clean_mod._classify("v2")
    assert c.kind == clean_mod._Kind.UNKNOWN
    c = clean_mod._classify("README.md")
    assert c.kind == clean_mod._Kind.UNKNOWN


def test_classify_unknown_lock_without_valid_cache_key() -> None:
    # Trailing .lock alone is not enough — the prefix must parse as a cache_key.
    c = clean_mod._classify("notakey.lock")
    assert c.kind == clean_mod._Kind.UNKNOWN


def test_classify_unknown_tmp_without_pid_digits() -> None:
    c = clean_mod._classify(f".myapp_{HEX_A}.tmp.abc")
    assert c.kind == clean_mod._Kind.UNKNOWN


def test_classify_unknown_dotfile_not_matching_orphan_pattern() -> None:
    c = clean_mod._classify(".DS_Store")
    assert c.kind == clean_mod._Kind.UNKNOWN


# ---------- _parse_duration (step 4) ----------


@pytest.mark.parametrize(
    "value,expected_seconds",
    [
        ("30s", 30),
        ("5m", 5 * 60),
        ("2h", 2 * 3600),
        ("7d", 7 * 86400),
        ("1s", 1),
    ],
)
def test_parse_duration_accepts_units(value: str, expected_seconds: int) -> None:
    assert clean_mod._parse_duration(value) == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "30",  # no unit
        "d",  # no number
        "30x",  # bad unit
        "0d",  # zero
        "-5d",  # negative
        "1.5h",  # non-integer
        "1h30m",  # compound (not supported in v0.3)
        "1 d",  # whitespace
    ],
)
def test_parse_duration_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        clean_mod._parse_duration(value)


# ---------- _scan_cache_root (step 4) ----------


def test_scan_cache_root_returns_empty_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert clean_mod._scan_cache_root(missing) == []


def test_scan_cache_root_returns_empty_when_empty(tmp_path: Path) -> None:
    assert clean_mod._scan_cache_root(tmp_path) == []


def test_scan_cache_root_classifies_cache_entries(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}", mtime=1_000_000.0)
    _make_cache_entry(tmp_path, f"other_{HEX_B}", mtime=2_000_000.0)
    entries = clean_mod._scan_cache_root(tmp_path)
    cache_entries = [e for e in entries if e.classified.kind == clean_mod._Kind.CACHE_ENTRY]
    assert len(cache_entries) == 2
    by_key = {e.name: e for e in cache_entries}
    assert by_key[f"myapp_{HEX_A}"].mtime == pytest.approx(1_000_000.0, abs=2)
    assert by_key[f"other_{HEX_B}"].mtime == pytest.approx(2_000_000.0, abs=2)


def test_scan_cache_root_picks_up_orphans_and_locks(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    _make_cache_entry(tmp_path, cache_key)
    _make_orphan_tmp(tmp_path, cache_key, 42)
    _make_orphan_old(tmp_path, cache_key, 7)
    _make_lock_file(tmp_path, cache_key)
    entries = clean_mod._scan_cache_root(tmp_path)
    kinds = sorted(e.classified.kind.value for e in entries)
    assert kinds == ["cache_entry", "lock", "old", "tmp"]


def test_scan_cache_root_skips_unknown_silently(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    (tmp_path / "v2").mkdir()  # reserved future subdir
    (tmp_path / "README.txt").write_text("junk", encoding="utf-8")
    entries = clean_mod._scan_cache_root(tmp_path)
    assert len(entries) == 1
    assert entries[0].classified.kind == clean_mod._Kind.CACHE_ENTRY


# ---------- _apply_filters (step 4) ----------


def _config(**kwargs) -> clean_mod.CleanConfig:
    """Build a CleanConfig with defaults; tests override specific fields."""
    defaults = {
        "cache_root": Path("/tmp/unused"),
        "all_": False,
        "older_than": None,
        "keep_latest": None,
        "name_pattern": None,
        "force": False,
        "dry_run": False,
        "show_sizes": False,
        "verbosity": 0,
    }
    defaults.update(kwargs)
    return clean_mod.CleanConfig(**defaults)


def test_apply_filters_no_flags_returns_empty(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    entries = clean_mod._scan_cache_root(tmp_path)
    assert clean_mod._apply_filters(entries, _config()) == []


def test_apply_filters_all_selects_every_cache_entry(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    _make_cache_entry(tmp_path, f"other_{HEX_B}")
    _make_lock_file(tmp_path, f"myapp_{HEX_A}")  # not a cache entry
    entries = clean_mod._scan_cache_root(tmp_path)
    result = clean_mod._apply_filters(entries, _config(all_=True))
    assert {e.name for e in result} == {f"myapp_{HEX_A}", f"other_{HEX_B}"}


def test_apply_filters_older_than_uses_site_packages_mtime(tmp_path: Path) -> None:
    now = time.time()
    _make_cache_entry(tmp_path, f"old_{HEX_A}", mtime=now - 10 * 86400)  # 10 days
    _make_cache_entry(tmp_path, f"new_{HEX_B}", mtime=now - 1 * 3600)  # 1 hour
    entries = clean_mod._scan_cache_root(tmp_path)
    result = clean_mod._apply_filters(
        entries, _config(older_than=timedelta(days=7), all_=False), now=now
    )
    assert {e.name for e in result} == {f"old_{HEX_A}"}


def test_apply_filters_name_glob_matches_normalized_name(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    _make_cache_entry(tmp_path, f"my-pkg_{HEX_B}")
    _make_cache_entry(tmp_path, f"other_{HEX_C}")
    entries = clean_mod._scan_cache_root(tmp_path)
    result = clean_mod._apply_filters(entries, _config(name_pattern="my*"))
    assert {e.name for e in result} == {f"myapp_{HEX_A}", f"my-pkg_{HEX_B}"}


def test_apply_filters_keep_latest_groups_by_name(tmp_path: Path) -> None:
    base = time.time()
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}", mtime=base - 3000)  # oldest myapp
    _make_cache_entry(tmp_path, f"myapp_{HEX_B}", mtime=base - 2000)
    _make_cache_entry(tmp_path, f"myapp_{HEX_C}", mtime=base - 1000)  # newest myapp
    _make_cache_entry(tmp_path, f"other_{HEX_D}", mtime=base - 5000)
    entries = clean_mod._scan_cache_root(tmp_path)
    # Keep latest 1 per name: HEX_C (myapp) and HEX_D (other) kept; A & B deletable.
    result = clean_mod._apply_filters(entries, _config(keep_latest=1))
    deleted_names = {e.name for e in result}
    assert deleted_names == {f"myapp_{HEX_A}", f"myapp_{HEX_B}"}


def test_apply_filters_keep_latest_zero_deletes_all_in_matched_groups(tmp_path: Path) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    _make_cache_entry(tmp_path, f"myapp_{HEX_B}")
    entries = clean_mod._scan_cache_root(tmp_path)
    result = clean_mod._apply_filters(entries, _config(keep_latest=0))
    assert {e.name for e in result} == {f"myapp_{HEX_A}", f"myapp_{HEX_B}"}


def test_apply_filters_intersects_older_than_and_name(tmp_path: Path) -> None:
    now = time.time()
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}", mtime=now - 10 * 86400)  # old + matches
    _make_cache_entry(tmp_path, f"myapp_{HEX_B}", mtime=now - 1 * 3600)  # new + matches
    _make_cache_entry(tmp_path, f"other_{HEX_C}", mtime=now - 10 * 86400)  # old + no match
    entries = clean_mod._scan_cache_root(tmp_path)
    result = clean_mod._apply_filters(
        entries,
        _config(older_than=timedelta(days=7), name_pattern="myapp"),
        now=now,
    )
    assert {e.name for e in result} == {f"myapp_{HEX_A}"}


# ---------- try-lock + delete (step 5) ----------


def test_walk_size_returns_total_bytes(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x" * 100)
    (d / "sub").mkdir()
    (d / "sub" / "b.bin").write_bytes(b"y" * 250)
    assert clean_mod._walk_size(d) == 350


def test_walk_size_missing_dir_is_zero(tmp_path: Path) -> None:
    assert clean_mod._walk_size(tmp_path / "nope") == 0


def test_delete_cache_entry_deletes_dir_and_lock(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    cache_dir = _make_cache_entry(tmp_path, cache_key, n_files=2, file_size=128)
    lock_path = _make_lock_file(tmp_path, cache_key)
    entries = clean_mod._scan_cache_root(tmp_path)
    entry = next(e for e in entries if e.name == cache_key)
    freed, reason = clean_mod._delete_cache_entry(entry, force=False)
    assert reason is None
    assert freed >= 256  # 2 files * 128 bytes
    assert not cache_dir.exists()
    assert not lock_path.exists()


def test_delete_cache_entry_skips_when_lock_held(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    cache_dir = _make_cache_entry(tmp_path, cache_key, n_files=1, file_size=64)
    lock_path = tmp_path / f"{cache_key}.lock"
    # Simulate a concurrent holder: open and lock the file.
    held_fd = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert held_fd is not None
    try:
        entries = clean_mod._scan_cache_root(tmp_path)
        entry = next(e for e in entries if e.name == cache_key)
        freed, reason = clean_mod._delete_cache_entry(entry, force=False)
        assert freed == 0
        assert reason == "locked"
        assert cache_dir.exists()
        assert lock_path.exists()
    finally:
        bootstrap_locking.release(held_fd, lock_path)


def test_delete_cache_entry_force_overrides_held_lock(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    cache_dir = _make_cache_entry(tmp_path, cache_key, n_files=1, file_size=64)
    lock_path = tmp_path / f"{cache_key}.lock"
    held_fd = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert held_fd is not None
    try:
        entries = clean_mod._scan_cache_root(tmp_path)
        entry = next(e for e in entries if e.name == cache_key)
        freed, reason = clean_mod._delete_cache_entry(entry, force=True)
        assert reason is None
        assert freed >= 64
        assert not cache_dir.exists()
        # The lock file is left alone when --force overrode a live holder.
        # On Windows the holder still owns the byte range; we don't pull the rug.
    finally:
        bootstrap_locking.release(held_fd, lock_path)


def test_try_acquire_nonblocking_round_trip(tmp_path: Path) -> None:
    """Sanity-check the bootstrap helper used by clean."""
    lock_path = tmp_path / "x.lock"
    fd1 = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert isinstance(fd1, int)
    fd2 = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert fd2 is None  # contended
    bootstrap_locking.release(fd1, lock_path)
    fd3 = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert isinstance(fd3, int)
    bootstrap_locking.release(fd3, lock_path)


# ---------- orphan reap (step 6) ----------


def test_select_orphans_picks_tmp_old_when_cache_key_missing(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    _make_orphan_tmp(tmp_path, cache_key, 42)
    _make_orphan_old(tmp_path, cache_key, 7)
    entries = clean_mod._scan_cache_root(tmp_path)
    orphans = clean_mod._select_orphans(entries, deletion_set=set())
    kinds = sorted(o.classified.kind.value for o in orphans)
    assert kinds == ["old", "tmp"]


def test_select_orphans_picks_tmp_old_when_cache_key_in_deletion_set(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    _make_cache_entry(tmp_path, cache_key)
    _make_orphan_tmp(tmp_path, cache_key, 42)
    entries = clean_mod._scan_cache_root(tmp_path)
    orphans = clean_mod._select_orphans(entries, deletion_set={cache_key})
    assert {o.classified.kind for o in orphans} == {clean_mod._Kind.TMP}


def test_select_orphans_skips_tmp_old_when_cache_key_kept(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    _make_cache_entry(tmp_path, cache_key)
    _make_orphan_tmp(tmp_path, cache_key, 42)
    entries = clean_mod._scan_cache_root(tmp_path)
    orphans = clean_mod._select_orphans(entries, deletion_set=set())
    assert orphans == []


def test_select_orphans_dangling_lock_no_cache_dir(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    _make_lock_file(tmp_path, cache_key)
    entries = clean_mod._scan_cache_root(tmp_path)
    orphans = clean_mod._select_orphans(entries, deletion_set=set())
    assert [o.classified.kind for o in orphans] == [clean_mod._Kind.LOCK]


def test_select_orphans_skips_lock_when_cache_dir_present_and_kept(tmp_path: Path) -> None:
    cache_key = f"myapp_{HEX_A}"
    _make_cache_entry(tmp_path, cache_key)
    _make_lock_file(tmp_path, cache_key)
    entries = clean_mod._scan_cache_root(tmp_path)
    orphans = clean_mod._select_orphans(entries, deletion_set=set())
    assert orphans == []


def test_delete_orphan_tmp(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    p = _make_orphan_tmp(tmp_path, cache_key, 42, file_size=512)
    entries = clean_mod._scan_cache_root(tmp_path)
    tmp = next(e for e in entries if e.classified.kind == clean_mod._Kind.TMP)
    freed, reason = clean_mod._delete_orphan(tmp)
    assert reason is None
    assert freed >= 512
    assert not p.exists()


def test_delete_orphan_old(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    p = _make_orphan_old(tmp_path, cache_key, 7, file_size=256)
    entries = clean_mod._scan_cache_root(tmp_path)
    old = next(e for e in entries if e.classified.kind == clean_mod._Kind.OLD)
    freed, reason = clean_mod._delete_orphan(old)
    assert reason is None
    assert freed >= 256
    assert not p.exists()


def test_delete_orphan_unheld_lock_is_unlinked(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    p = _make_lock_file(tmp_path, cache_key)
    entries = clean_mod._scan_cache_root(tmp_path)
    lock = next(e for e in entries if e.classified.kind == clean_mod._Kind.LOCK)
    freed, reason = clean_mod._delete_orphan(lock)
    assert reason is None
    assert freed >= 0
    assert not p.exists()


def test_delete_orphan_held_lock_is_skipped(tmp_path: Path) -> None:
    cache_key = f"ghost_{HEX_A}"
    p = _make_lock_file(tmp_path, cache_key)
    held_fd = bootstrap_locking.try_acquire_nonblocking(p)
    assert held_fd is not None
    try:
        entries = clean_mod._scan_cache_root(tmp_path)
        lock = next(e for e in entries if e.classified.kind == clean_mod._Kind.LOCK)
        freed, reason = clean_mod._delete_orphan(lock)
        assert reason == "locked"
        assert freed == 0
        assert p.exists()
    finally:
        bootstrap_locking.release(held_fd, p)


# ---------- orchestrator + output (step 7) ----------


def test_clean_returns_zero_when_cache_root_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no_such_root"
    rc = clean_mod.clean(_config(cache_root=missing, all_=True))
    assert rc == 0
    out, _err = capsys.readouterr()
    assert "deleted 0 entries" in out
    assert "freed 0 B" in out


def test_clean_all_deletes_every_cache_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    _make_cache_entry(tmp_path, f"other_{HEX_B}")
    rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True))
    assert rc == 0
    assert not (tmp_path / f"myapp_{HEX_A}").exists()
    assert not (tmp_path / f"other_{HEX_B}").exists()
    out, _err = capsys.readouterr()
    assert re.search(r"deleted 2 entries, freed \S+ \S+", out)


def test_clean_dry_run_does_not_modify_filesystem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    _make_cache_entry(tmp_path, f"other_{HEX_B}")
    rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True, dry_run=True))
    assert rc == 0
    assert (tmp_path / f"myapp_{HEX_A}").exists()
    assert (tmp_path / f"other_{HEX_B}").exists()
    out, _err = capsys.readouterr()
    assert re.search(r"would delete 2 entries, would free \S+ \S+", out)


def test_clean_table_emitted_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    clean_mod.clean(_config(cache_root=tmp_path, all_=True))
    out, err = capsys.readouterr()
    # Header columns
    for col in ("ACTION", "NAME", "BUILD_ID", "AGE", "SIZE", "PATH"):
        assert col in err
    # myapp row
    assert "myapp" in err
    # Truncated build_id appears as a separate column (followed by whitespace).
    assert re.search(rf"\b{HEX_A[:8]}\b\s", err)
    # Trailer is on stdout, NOT in the table
    assert "deleted" in out
    assert "deleted" not in err


def test_clean_verbose_shows_full_build_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    clean_mod.clean(_config(cache_root=tmp_path, all_=True, verbosity=1))
    _out, err = capsys.readouterr()
    assert HEX_A in err  # full hex visible


def test_clean_quiet_suppresses_table_but_keeps_trailer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    clean_mod.clean(_config(cache_root=tmp_path, all_=True, verbosity=-1))
    out, err = capsys.readouterr()
    assert "ACTION" not in err  # table suppressed
    assert "myapp" not in err
    assert "deleted 1 entries" in out  # trailer preserved


def test_clean_dry_run_keep_action_shows_dash_size_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = time.time()
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}", mtime=base - 1000)
    _make_cache_entry(tmp_path, f"myapp_{HEX_B}", mtime=base - 2000)
    # keep-latest 1 → HEX_A (newer) kept, HEX_B deleted.
    clean_mod.clean(
        _config(cache_root=tmp_path, keep_latest=1, dry_run=True, show_sizes=False)
    )
    _out, err = capsys.readouterr()
    # Find the keep row for HEX_A: SIZE column should be —
    keep_line = next(line for line in err.splitlines() if line.startswith("keep"))
    assert "—" in keep_line


def test_clean_show_sizes_populates_keep_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = time.time()
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}", mtime=base - 1000, file_size=2048)
    _make_cache_entry(tmp_path, f"myapp_{HEX_B}", mtime=base - 2000)
    clean_mod.clean(
        _config(
            cache_root=tmp_path, keep_latest=1, dry_run=True, show_sizes=True
        )
    )
    _out, err = capsys.readouterr()
    keep_line = next(line for line in err.splitlines() if line.startswith("keep"))
    assert "—" not in keep_line  # size populated
    # Must contain a humanized size unit
    assert any(unit in keep_line for unit in ("B", "KiB", "MiB"))


def test_clean_skip_row_shown_for_held_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_key = f"myapp_{HEX_A}"
    _make_cache_entry(tmp_path, cache_key)
    lock_path = tmp_path / f"{cache_key}.lock"
    held_fd = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert held_fd is not None
    try:
        rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True))
        assert rc == errors.CleanRefusedError.exit_code  # 14
        _out, err = capsys.readouterr()
        assert "skip" in err
        assert "(locked)" in err
        # Cache dir remained
        assert (tmp_path / cache_key).exists()
    finally:
        bootstrap_locking.release(held_fd, lock_path)


def test_clean_force_overrides_held_lock_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_key = f"myapp_{HEX_A}"
    cache_dir = _make_cache_entry(tmp_path, cache_key)
    lock_path = tmp_path / f"{cache_key}.lock"
    held_fd = bootstrap_locking.try_acquire_nonblocking(lock_path)
    assert held_fd is not None
    try:
        rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True, force=True))
        assert rc == 0
        assert not cache_dir.exists()
    finally:
        bootstrap_locking.release(held_fd, lock_path)


def test_clean_reaps_orphans_when_cache_key_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_key = f"ghost_{HEX_A}"
    tmp = _make_orphan_tmp(tmp_path, cache_key, 42)
    old = _make_orphan_old(tmp_path, cache_key, 7)
    lock = _make_lock_file(tmp_path, cache_key)
    rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True))
    assert rc == 0
    assert not tmp.exists()
    assert not old.exists()
    assert not lock.exists()


def test_clean_io_error_returns_15(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(clean_mod.shutil, "rmtree", boom)
    rc = clean_mod.clean(_config(cache_root=tmp_path, all_=True))
    assert rc == errors.CleanIOError.exit_code  # 15


def test_clean_table_includes_orphan_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_key = f"ghost_{HEX_A}"
    _make_orphan_tmp(tmp_path, cache_key, 42)
    _make_orphan_old(tmp_path, cache_key, 7)
    _make_lock_file(tmp_path, cache_key)
    clean_mod.clean(_config(cache_root=tmp_path, all_=True))
    _out, err = capsys.readouterr()
    # Each orphan kind shows as an "orphan" row.
    assert err.count("\norphan") >= 3 or err.count("orphan ") >= 3


def test_clean_no_matches_returns_zero_with_empty_trailer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_cache_entry(tmp_path, f"myapp_{HEX_A}")
    rc = clean_mod.clean(_config(cache_root=tmp_path, name_pattern="nosuch"))
    assert rc == 0
    out, _err = capsys.readouterr()
    assert "deleted 0 entries" in out
