"""Pin compute_build_id to D6 / specs/02-build-pipeline.md §4."""

import hashlib
from pathlib import Path

import pytest

from moonlit.hashing import compute_build_id


def test_returns_64_lowercase_hex(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"hello")
    digest = compute_build_id(tmp_path)
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # parses as hex; raises ValueError if not


def test_empty_directory_yields_sha256_of_empty_input(tmp_path: Path) -> None:
    assert compute_build_id(tmp_path) == hashlib.sha256(b"").hexdigest()


def test_single_file_matches_canonical_formula(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"hello")
    expected = hashlib.sha256(b"a.py\x00hello\x00").hexdigest()
    assert compute_build_id(tmp_path) == expected


def test_nested_file_uses_forward_slash_relpath(tmp_path: Path) -> None:
    # arch §10: relpaths are forward-slash regardless of host separator.
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "x.py").write_bytes(b"world")
    expected = hashlib.sha256(b"pkg/sub/x.py\x00world\x00").hexdigest()
    assert compute_build_id(tmp_path) == expected


def test_files_are_hashed_in_sorted_order(tmp_path: Path) -> None:
    # Creating "b.py" first must not change the result vs creating "a.py" first.
    (tmp_path / "b.py").write_bytes(b"two")
    (tmp_path / "a.py").write_bytes(b"one")
    expected = hashlib.sha256(b"a.py\x00one\x00b.py\x00two\x00").hexdigest()
    assert compute_build_id(tmp_path) == expected


def test_determinism_across_calls(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"alpha")
    (tmp_path / "b.py").write_bytes(b"beta")
    assert compute_build_id(tmp_path) == compute_build_id(tmp_path)


def test_top_level_pycache_excluded(tmp_path: Path) -> None:
    # D6 falsifier from arch §10.
    (tmp_path / "a.py").write_bytes(b"hello")
    baseline = compute_build_id(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-313.pyc").write_bytes(b"compiled")
    assert compute_build_id(tmp_path) == baseline


def test_nested_pycache_segment_excluded(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "x.py").write_bytes(b"src")
    baseline = compute_build_id(tmp_path)
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-313.pyc").write_bytes(b"compiled")
    assert compute_build_id(tmp_path) == baseline


def test_stray_pyc_file_excluded(tmp_path: Path) -> None:
    # `.pyc` is excluded by suffix, not just by being inside __pycache__.
    (tmp_path / "a.py").write_bytes(b"hello")
    baseline = compute_build_id(tmp_path)
    (tmp_path / "stray.pyc").write_bytes(b"compiled")
    assert compute_build_id(tmp_path) == baseline


def test_content_change_changes_digest(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    a = tmp_path_factory.mktemp("a")
    b = tmp_path_factory.mktemp("b")
    (a / "x.py").write_bytes(b"hello")
    (b / "x.py").write_bytes(b"world")
    assert compute_build_id(a) != compute_build_id(b)


def test_path_change_changes_digest(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    a = tmp_path_factory.mktemp("a")
    b = tmp_path_factory.mktemp("b")
    (a / "x.py").write_bytes(b"data")
    (b / "y.py").write_bytes(b"data")
    assert compute_build_id(a) != compute_build_id(b)


def test_empty_subdirectory_does_not_affect_digest(tmp_path: Path) -> None:
    # Directories themselves are never fed into the hash; only files are.
    (tmp_path / "a.py").write_bytes(b"hello")
    baseline = compute_build_id(tmp_path)
    (tmp_path / "empty_dir").mkdir()
    assert compute_build_id(tmp_path) == baseline


def test_binary_content_is_handled_byte_exactly(tmp_path: Path) -> None:
    payload = bytes(range(256))
    (tmp_path / "binary.bin").write_bytes(payload)
    expected = hashlib.sha256(b"binary.bin\x00" + payload + b"\x00").hexdigest()
    assert compute_build_id(tmp_path) == expected
