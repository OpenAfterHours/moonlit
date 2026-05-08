"""Pin _bootstrap/runner.run to specs/03-bootstrap-runtime.md §7-§8.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the runner via direct import as a development-time TDD harness;
the e2e suite is the contract.

The autouse fixture isolates sys.path / sys.modules / MOONLIT_ENTRY_POINT
between tests because runner.run() mutates global interpreter state via
``site.addsitedir`` and ``importlib.import_module``.
"""

import sys
from pathlib import Path

import pytest

from moonlit._bootstrap.environment import Environment
from moonlit._bootstrap.errors import CollisionError, EntryPointError
from moonlit._bootstrap.runner import run


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
def _clean_entry_point_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONLIT_ENTRY_POINT", raising=False)


def make_env(entry_point: str) -> Environment:
    return Environment(
        schema_version=1,
        name="myapp",
        build_id="a" * 64,
        entry_point=entry_point,
        built_at="2026-05-08T15:23:01Z",
        moonlit_version="0.1.0",
        python_shebang="/usr/bin/env python3",
    )


def make_site_dir(tmp_path: Path) -> Path:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    return site_dir


def write_module(site_dir: Path, name: str, body: str) -> None:
    (site_dir / f"{name}.py").write_text(body, encoding="utf-8")


def write_package(site_dir: Path, pkg: str, modules: dict[str, str]) -> None:
    pkg_dir = site_dir / pkg
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    for name, body in modules.items():
        (pkg_dir / f"{name}.py").write_text(body, encoding="utf-8")


# ---------- happy path: return-value coercion ----------


def test_returns_zero_when_entry_point_returns_none(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_none", "def main():\n    return None\n")
    assert run(make_env("mymod_none:main"), site_dir) == 0


def test_int_return_passes_through(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_int", "def main():\n    return 42\n")
    assert run(make_env("mymod_int:main"), site_dir) == 42


def test_int_return_above_255_is_masked(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_big", "def main():\n    return 256\n")
    assert run(make_env("mymod_big:main"), site_dir) == 0


def test_int_return_negative_is_masked(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_neg", "def main():\n    return -1\n")
    # -1 & 0xFF == 255
    assert run(make_env("mymod_neg:main"), site_dir) == 255


def test_bool_return_coerced_via_int_branch(tmp_path: Path) -> None:
    # spec §8: bool is int in Python; True → 1, False → 0. No separate bool branch.
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_true", "def main():\n    return True\n")
    write_module(site_dir, "mymod_false", "def main():\n    return False\n")
    assert run(make_env("mymod_true:main"), site_dir) == 1
    assert run(make_env("mymod_false:main"), site_dir) == 0


def test_str_return_that_parses_as_int_is_coerced(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_str_int", "def main():\n    return '42'\n")
    assert run(make_env("mymod_str_int:main"), site_dir) == 42


# ---------- happy path: entry-point resolution ----------


def test_dotted_attribute_walks_getattr(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(
        site_dir,
        "mymod_dotted",
        "class Foo:\n    @staticmethod\n    def bar():\n        return 7\n",
    )
    assert run(make_env("mymod_dotted:Foo.bar"), site_dir) == 7


def test_dotted_module_imports_package(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_package(site_dir, "mypkg", {"cli": "def main():\n    return 11\n"})
    assert run(make_env("mypkg.cli:main"), site_dir) == 11


def test_moonlit_entry_point_env_var_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "real_mod", "def main():\n    return 5\n")
    write_module(site_dir, "fake_mod", "def main():\n    return 99\n")
    monkeypatch.setenv("MOONLIT_ENTRY_POINT", "fake_mod:main")
    assert run(make_env("real_mod:main"), site_dir) == 99


def test_empty_moonlit_entry_point_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D16: empty string is treated as unset.
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "real_fallback", "def main():\n    return 5\n")
    monkeypatch.setenv("MOONLIT_ENTRY_POINT", "")
    assert run(make_env("real_fallback:main"), site_dir) == 5


def test_entry_point_called_with_no_arguments(tmp_path: Path) -> None:
    # spec §8: 'invoked with no positional or keyword arguments — obj()'.
    site_dir = make_site_dir(tmp_path)
    write_module(
        site_dir,
        "mymod_noargs",
        "def main(*args, **kwargs):\n"
        "    assert args == () and kwargs == {}\n"
        "    return 0\n",
    )
    assert run(make_env("mymod_noargs:main"), site_dir) == 0


# ---------- entry-point parse errors (spec §8) ----------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no_colon",
        "two:colons:here",
        ":missing_module",
        "missing_attr:",
        "::",
        ":",
        "a:b.",
        "a:.b",
        "a:b..c",
    ],
)
def test_invalid_entry_point_format_raises(tmp_path: Path, value: str) -> None:
    site_dir = make_site_dir(tmp_path)
    with pytest.raises(EntryPointError, match="invalid entry point"):
        run(make_env(value), site_dir)


def test_invalid_entry_point_message_includes_value(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    with pytest.raises(EntryPointError) as excinfo:
        run(make_env("two:colons:here"), site_dir)
    assert "two:colons:here" in str(excinfo.value)


# ---------- import / attribute errors ----------


def test_module_not_importable_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    with pytest.raises(EntryPointError, match="cannot import"):
        run(make_env("does_not_exist_module:main"), site_dir)


def test_module_not_importable_message_includes_module(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    with pytest.raises(EntryPointError) as excinfo:
        run(make_env("missing_xyz:main"), site_dir)
    assert "missing_xyz" in str(excinfo.value)


def test_attribute_not_found_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_main", "# no main here\n")
    with pytest.raises(EntryPointError, match="attribute main not found"):
        run(make_env("mymod_no_main:main"), site_dir)


def test_dotted_attribute_first_segment_missing_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_foo", "# nothing\n")
    with pytest.raises(EntryPointError, match="attribute Foo.bar not found"):
        run(make_env("mymod_no_foo:Foo.bar"), site_dir)


def test_dotted_attribute_second_segment_missing_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_bar", "class Foo:\n    pass\n")
    with pytest.raises(EntryPointError, match="attribute Foo.bar not found"):
        run(make_env("mymod_no_bar:Foo.bar"), site_dir)


# ---------- return-value coercion errors ----------


def test_uncoercible_list_return_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_list", "def main():\n    return [1, 2, 3]\n")
    with pytest.raises(EntryPointError, match="uncoercible"):
        run(make_env("mymod_list:main"), site_dir)


def test_uncoercible_string_return_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_str", "def main():\n    return 'hello'\n")
    with pytest.raises(EntryPointError, match="uncoercible"):
        run(make_env("mymod_str:main"), site_dir)


def test_uncoercible_message_includes_type(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_dict", "def main():\n    return {'a': 1}\n")
    with pytest.raises(EntryPointError) as excinfo:
        run(make_env("mymod_dict:main"), site_dir)
    assert "dict" in str(excinfo.value)


# ---------- _bootstrap collision check (spec §7) ----------


def test_bootstrap_collision_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    (site_dir / "_bootstrap").mkdir()
    with pytest.raises(CollisionError, match="_bootstrap collision"):
        run(make_env("anything:main"), site_dir)


@pytest.mark.parametrize("name", ["_bootstrap", "_Bootstrap", "_BOOTSTRAP", "_BootStrap"])
def test_case_folded_bootstrap_collision_raises(tmp_path: Path, name: str) -> None:
    # Each test gets its own tmp_path so case-insensitive FS doesn't collide.
    site_dir = make_site_dir(tmp_path)
    (site_dir / name).mkdir()
    with pytest.raises(CollisionError):
        run(make_env("anything:main"), site_dir)


def test_bootstrap_as_a_file_also_collides(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    (site_dir / "_bootstrap").write_text("# stowaway\n", encoding="utf-8")
    with pytest.raises(CollisionError, match="_bootstrap collision"):
        run(make_env("anything:main"), site_dir)


def test_collision_aborts_before_addsitedir(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    (site_dir / "_bootstrap").mkdir()
    assert str(site_dir) not in sys.path
    with pytest.raises(CollisionError):
        run(make_env("anything:main"), site_dir)
    assert str(site_dir) not in sys.path


# ---------- side effects: addsitedir ----------


def test_run_adds_site_dir_to_sys_path(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_sidefx", "def main():\n    return 0\n")
    assert str(site_dir) not in sys.path
    run(make_env("mymod_sidefx:main"), site_dir)
    assert str(site_dir) in sys.path


# ---------- exit-code attributes ----------


def test_collision_error_exit_code_is_1() -> None:
    assert CollisionError("x").exit_code == 1


def test_entry_point_error_exit_code_is_2() -> None:
    assert EntryPointError("x").exit_code == 2
