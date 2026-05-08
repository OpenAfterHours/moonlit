"""Pin _bootstrap.bootstrap orchestrator to specs/03-bootstrap-runtime.md §2/§3/§10.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the orchestrator via direct import as a development-time TDD
harness. The contract test mode is e2e via subprocess; that suite is
built once the build-time pipeline can produce a real .pyz.
"""

import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit._bootstrap import _resolve_cache_root, bootstrap


# ---------- helpers / fixtures ----------


@pytest.fixture(autouse=True)
def _isolate_sys_state() -> None:
    saved_path = sys.path[:]
    saved_modules = set(sys.modules.keys())
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules.keys()):
            if name not in saved_modules:
                del sys.modules[name]


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONLIT_ENTRY_POINT", raising=False)
    monkeypatch.delenv("MOONLIT_FORCE_EXTRACT", raising=False)
    monkeypatch.delenv("MOONLIT_DEBUG", raising=False)


@pytest.fixture(autouse=True)
def _safe_default_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tests testing the default resolution will monkeypatch.delenv("MOONLIT_ROOT").
    monkeypatch.setenv("MOONLIT_ROOT", str(tmp_path / "_default_isolated_cache"))


def valid_env_dict() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "myapp",
        "build_id": "a" * 64,
        "entry_point": "myapp:main",
        "built_at": "2026-01-01T00:00:00Z",
        "moonlit_version": "0.1.0",
        "python_shebang": "/usr/bin/env python3",
    }


def make_pyz(
    path: Path,
    env_dict: dict[str, Any] | None,
    site_packages: dict[str, str | bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", "")
        if env_dict is not None:
            zf.writestr("env.json", json.dumps(env_dict).encode("utf-8"))
        for arcname, content in (site_packages or {}).items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(f"site-packages/{arcname}", data)
    return path


def set_argv(monkeypatch: pytest.MonkeyPatch, archive: Path | str) -> None:
    monkeypatch.setattr(sys, "argv", [str(archive)])


# ---------- happy path ----------


def test_returns_zero_on_successful_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "def main():\n    return 0\n"},
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 0


def test_returns_user_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "def main():\n    return 42\n"},
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 42


# ---------- step 1: archive resolution ----------


def test_empty_argv0_returns_1_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", [""])
    assert bootstrap() == 1
    err = capsys.readouterr().err
    assert "moonlit: " in err
    assert "cannot locate zipapp" in err


def test_non_zipfile_path_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    not_zip = tmp_path / "not.pyz"
    not_zip.write_text("not a zip", encoding="utf-8")
    set_argv(monkeypatch, not_zip)
    assert bootstrap() == 1
    err = capsys.readouterr().err
    assert "not a moonlit zipapp" in err


# ---------- step 3: env.json validation propagates ----------


def test_missing_env_json_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(tmp_path / "app.pyz", env_dict=None)
    set_argv(monkeypatch, archive)
    assert bootstrap() == 1
    assert "env.json missing" in capsys.readouterr().err


def test_malformed_env_json_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "app.pyz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("env.json", b"{not json}")
    set_argv(monkeypatch, archive)
    assert bootstrap() == 1
    assert "not valid JSON" in capsys.readouterr().err


# ---------- runner errors propagate with correct exit codes ----------


def test_bad_entry_point_module_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        {**valid_env_dict(), "entry_point": "missing_module:main"},
        {"myapp.py": "def main():\n    return 0\n"},
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 2
    assert "cannot import" in capsys.readouterr().err


def test_bad_entry_point_attr_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        {**valid_env_dict(), "entry_point": "myapp:missing_attr"},
        {"myapp.py": "# no attr here\n"},
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 2
    assert "attribute missing_attr not found" in capsys.readouterr().err


def test_bootstrap_collision_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {
            "myapp.py": "def main():\n    return 0\n",
            "_bootstrap/marker.py": "# stowaway\n",
        },
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 1
    assert "_bootstrap collision" in capsys.readouterr().err


# ---------- spec §10: user-code exceptions propagate ----------


def test_user_code_exception_is_not_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spec §10: user-code exceptions propagate normally; Python's default
    # excepthook handles them, not our BootstrapError catch.
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "def main():\n    raise RuntimeError('user code error')\n"},
    )
    set_argv(monkeypatch, archive)
    with pytest.raises(RuntimeError, match="user code error"):
        bootstrap()


def test_user_code_systemexit_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SystemExit is not a BootstrapError; it propagates untouched.
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "import sys\ndef main():\n    sys.exit(7)\n"},
    )
    set_argv(monkeypatch, archive)
    with pytest.raises(SystemExit) as excinfo:
        bootstrap()
    assert excinfo.value.code == 7


# ---------- MOONLIT_DEBUG (spec §10) ----------


def test_moonlit_debug_prints_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(tmp_path / "app.pyz", env_dict=None)
    set_argv(monkeypatch, archive)
    monkeypatch.setenv("MOONLIT_DEBUG", "1")
    bootstrap()
    err = capsys.readouterr().err
    assert "moonlit: env.json" in err
    assert "Traceback" in err


def test_no_moonlit_debug_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(tmp_path / "app.pyz", env_dict=None)
    set_argv(monkeypatch, archive)
    bootstrap()
    err = capsys.readouterr().err
    assert "moonlit:" in err
    assert "Traceback" not in err


def test_empty_moonlit_debug_is_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # D16: empty after os.environ.get is treated as unset.
    archive = make_pyz(tmp_path / "app.pyz", env_dict=None)
    set_argv(monkeypatch, archive)
    monkeypatch.setenv("MOONLIT_DEBUG", "")
    bootstrap()
    err = capsys.readouterr().err
    assert "Traceback" not in err


# ---------- error messages start with moonlit: prefix ----------


def test_error_messages_start_with_moonlit_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = make_pyz(tmp_path / "app.pyz", env_dict=None)
    set_argv(monkeypatch, archive)
    bootstrap()
    err = capsys.readouterr().err
    # Spec §10: print a single line `moonlit: <message>` to stderr.
    assert err.startswith("moonlit: ")


# ---------- MOONLIT_ROOT integration ----------


def test_moonlit_root_overrides_cache_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_cache = tmp_path / "custom_cache"
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "def main():\n    return 0\n"},
    )
    monkeypatch.setenv("MOONLIT_ROOT", str(custom_cache))
    set_argv(monkeypatch, archive)
    assert bootstrap() == 0
    assert (custom_cache / f"myapp_{'a' * 64}" / "site-packages").is_dir()


# ---------- _resolve_cache_root unit tests (spec §3) ----------


def test_resolve_cache_root_honors_moonlit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setenv("MOONLIT_ROOT", str(custom))
    assert _resolve_cache_root() == custom.resolve()


def test_resolve_cache_root_expands_user_in_moonlit_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONLIT_ROOT", "~/custom_moonlit_root")
    resolved = _resolve_cache_root()
    assert "~" not in str(resolved)
    assert resolved.name == "custom_moonlit_root"


def test_resolve_cache_root_default_on_windows_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOONLIT_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(os, "name", "nt")
    assert _resolve_cache_root() == Path(str(tmp_path / "appdata")) / "moonlit"


def test_resolve_cache_root_falls_back_to_home_when_localappdata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOONLIT_ROOT", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    assert _resolve_cache_root() == Path.home() / ".moonlit"


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific cache-root branch")
def test_resolve_cache_root_uses_home_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOONLIT_ROOT", raising=False)
    assert _resolve_cache_root() == Path.home() / ".moonlit"


def test_resolve_cache_root_empty_moonlit_root_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D16: empty MOONLIT_ROOT is treated as unset; the default branch applies
    # for whichever platform the test is running on.
    monkeypatch.setenv("MOONLIT_ROOT", "")
    if os.name == "nt":
        appdata = tmp_path / "appdata"
        monkeypatch.setenv("LOCALAPPDATA", str(appdata))
        assert _resolve_cache_root() == appdata / "moonlit"
    else:
        assert _resolve_cache_root() == Path.home() / ".moonlit"


# ---------- end-to-end env-var integration ----------


def test_moonlit_entry_point_override_through_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),  # entry_point=myapp:main
        {
            "myapp.py": "def main():\n    return 5\n",
            "other.py": "def main():\n    return 99\n",
        },
    )
    monkeypatch.setenv("MOONLIT_ENTRY_POINT", "other:main")
    set_argv(monkeypatch, archive)
    assert bootstrap() == 99


def test_moonlit_force_extract_re_extracts_through_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_pyz(
        tmp_path / "app.pyz",
        valid_env_dict(),
        {"myapp.py": "def main():\n    return 0\n"},
    )
    set_argv(monkeypatch, archive)
    assert bootstrap() == 0

    cache_root = Path(os.environ["MOONLIT_ROOT"])
    site_dir = cache_root / f"myapp_{'a' * 64}" / "site-packages"
    (site_dir / "myapp.py").write_text(
        "def main():\n    return 11\n", encoding="utf-8"
    )

    monkeypatch.setenv("MOONLIT_FORCE_EXTRACT", "1")
    bootstrap()
    assert (
        (site_dir / "myapp.py").read_text(encoding="utf-8")
        == "def main():\n    return 0\n"
    )
