"""Pin _bootstrap/runner.run to specs/03-bootstrap-runtime.md §7-§8.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the runner via direct import as a development-time TDD harness;
the e2e suite is the contract.

The autouse fixture isolates sys.path / sys.modules / MOONLIT_ENTRY_POINT
between tests because runner.run() mutates global interpreter state via
``site.addsitedir`` and ``importlib.import_module``.
"""

import os
import site
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from moonlit._bootstrap.environment import Environment
from moonlit._bootstrap.errors import CollisionError, EntryPointError
from moonlit._bootstrap.runner import run

# ---------- helpers / fixtures ----------


@pytest.fixture(autouse=True)
def _isolate_sys_state() -> Iterator[None]:
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


@pytest.fixture
def host_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake host site-packages: last on sys.path and reported by site.getsitepackages.

    Stubbing both site queries makes this the only host site dir, so the real
    entries already on sys.path stand in for stdlib / PYTHONPATH ahead of it.
    """
    host = tmp_path / "host" / "site-packages"
    host.mkdir(parents=True)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(host)])
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path / "no-user-site"))
    sys.path.append(str(host))
    return host


def stdlib_index() -> int:
    """Index of the real stdlib dir (where os.py lives) on this process's sys.path."""
    stdlib = os.path.normcase(os.path.dirname(os.__file__))
    return next(i for i, entry in enumerate(sys.path) if os.path.normcase(entry) == stdlib)


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
        "def main(*args, **kwargs):\n    assert args == () and kwargs == {}\n    return 0\n",
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
    with pytest.raises(EntryPointError, match=r"attribute Foo\.bar not found"):
        run(make_env("mymod_no_foo:Foo.bar"), site_dir)


def test_dotted_attribute_second_segment_missing_raises(tmp_path: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_bar", "class Foo:\n    pass\n")
    with pytest.raises(EntryPointError, match=r"attribute Foo\.bar not found"):
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


# ---------- sys.path placement: bundled ahead of host site dirs (D26) ----------


def test_bundled_module_wins_over_host_site_copy(tmp_path: Path, host_site: Path) -> None:
    # The D26 bug: addsitedir appends, so a stale host copy shadowed the bundled one.
    write_module(host_site, "shadowed_mod", "def main():\n    return 1\n")
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "shadowed_mod", "def main():\n    return 2\n")
    assert run(make_env("shadowed_mod:main"), site_dir) == 2


def test_site_dir_lands_immediately_before_first_host_site_dir(
    tmp_path: Path, host_site: Path
) -> None:
    # Everything ahead of the host dir (sys.path[0], PYTHONPATH, stdlib) keeps its
    # place; what host .pth files appended after it stays behind the bundled dir.
    trailing = str(tmp_path / "appended-by-host-pth")
    sys.path.append(trailing)
    ahead = sys.path[: sys.path.index(str(host_site))]
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_order", "def main():\n    return 0\n")
    run(make_env("mymod_order:main"), site_dir)
    assert sys.path == [*ahead, str(site_dir), str(host_site), trailing]


def test_user_site_dir_counts_as_host_site_dir(
    tmp_path: Path, host_site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user site precedes the system site dirs on sys.path, so it is the anchor.
    user_site = tmp_path / "user-site"
    user_site.mkdir()
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    sys.path.insert(sys.path.index(str(host_site)), str(user_site))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_user", "def main():\n    return 0\n")
    run(make_env("mymod_user:main"), site_dir)
    assert sys.path.index(str(site_dir)) == sys.path.index(str(user_site)) - 1


def test_host_site_dir_match_is_path_normalized(
    tmp_path: Path, host_site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # site.getsitepackages() and sys.path may spell the same directory differently.
    respelled = os.path.join(str(host_site), "..", host_site.name)
    monkeypatch.setattr(site, "getsitepackages", lambda: [respelled])
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_norm", "def main():\n    return 0\n")
    run(make_env("mymod_norm:main"), site_dir)
    assert sys.path.index(str(site_dir)) == sys.path.index(str(host_site)) - 1


def test_pth_added_dir_travels_with_site_dir(tmp_path: Path, host_site: Path) -> None:
    site_dir = make_site_dir(tmp_path)
    (site_dir / "extra").mkdir()
    (site_dir / "extra.pth").write_text("extra\n", encoding="utf-8")
    write_module(site_dir, "mymod_pth", "def main():\n    return 0\n")
    run(make_env("mymod_pth:main"), site_dir)
    start = sys.path.index(str(site_dir))
    assert sys.path[start : start + 3] == [
        str(site_dir),
        str(site_dir / "extra"),
        str(host_site),
    ]


def test_pth_import_line_insert_joins_the_bundled_block(tmp_path: Path, host_site: Path) -> None:
    # A bundled .pth `import` line can put an entry anywhere, not only at the tail;
    # it still moves with the block instead of jumping ahead of stdlib.
    site_dir = make_site_dir(tmp_path)
    front = site_dir / "front"
    front.mkdir()
    (site_dir / "front.pth").write_text(
        f"import sys; sys.path.insert(0, {str(front)!r})\n", encoding="utf-8"
    )
    write_module(site_dir, "mymod_pth_import", "def main():\n    return 0\n")
    run(make_env("mymod_pth_import:main"), site_dir)
    host_index = sys.path.index(str(host_site))
    assert sys.path[host_index - 2 : host_index] == [str(front), str(site_dir)]


def test_no_host_site_dir_on_sys_path_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. `python -S`: no host site dir is active, so there is nothing to shadow.
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path / "absent")])
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path / "absent-user"))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_host", "def main():\n    return 0\n")
    run(make_env("mymod_no_host:main"), site_dir)
    assert sys.path[-1] == str(site_dir)


def test_pythonpath_dir_named_site_packages_stays_ahead(tmp_path: Path, host_site: Path) -> None:
    # "Host" means listed by site.getsitepackages()/getusersitepackages(), never a
    # basename match: a PYTHONPATH entry that merely ends in site-packages keeps
    # its precedence over the bundled dir.
    pythonpath_dir = tmp_path / "pythonpath" / "site-packages"
    pythonpath_dir.mkdir(parents=True)
    sys.path.insert(1, str(pythonpath_dir))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_pythonpath", "def main():\n    return 0\n")
    run(make_env("mymod_pythonpath:main"), site_dir)
    assert sys.path.index(str(pythonpath_dir)) < sys.path.index(str(site_dir))


def test_host_site_dir_ahead_of_stdlib_is_not_the_anchor(tmp_path: Path, host_site: Path) -> None:
    # Docker's `PYTHONPATH=/opt/venv/.../site-packages`: site.py dedupes, so the host
    # dir sits only in its PYTHONPATH slot, ahead of the stdlib. Anchoring on it
    # would let a bundled module shadow the stdlib.
    sys.path.remove(str(host_site))
    sys.path.insert(1, str(host_site))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_stdlib", "def main():\n    return 0\n")
    run(make_env("mymod_stdlib:main"), site_dir)
    assert sys.path.index(str(site_dir)) > stdlib_index()


def test_stdlib_not_on_sys_path_searches_from_the_start(
    tmp_path: Path, host_site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "__file__", str(tmp_path / "elsewhere" / "os.py"))
    sys.path.remove(str(host_site))
    sys.path.insert(1, str(host_site))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_stdlib", "def main():\n    return 0\n")
    run(make_env("mymod_no_stdlib:main"), site_dir)
    assert sys.path.index(str(site_dir)) == sys.path.index(str(host_site)) - 1


def test_os_without_file_searches_from_the_start(
    tmp_path: Path, host_site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "__file__")
    sys.path.remove(str(host_site))
    sys.path.insert(1, str(host_site))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_no_os_file", "def main():\n    return 0\n")
    run(make_env("mymod_no_os_file:main"), site_dir)
    assert sys.path.index(str(site_dir)) == sys.path.index(str(host_site)) - 1


def test_bundled_pth_import_line_resolves_against_the_bundle(
    tmp_path: Path, host_site: Path
) -> None:
    # Bundled .pth `import` lines run inside addsitedir, so site_dir must already sit
    # ahead of the host dirs by then. This also pins that the .pth is still
    # processed when site_dir is already on sys.path.
    write_module(host_site, "pth_helper", "")
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "pth_helper", "")
    (site_dir / "helper.pth").write_text("import pth_helper\n", encoding="utf-8")
    write_module(site_dir, "mymod_pth_helper", "def main():\n    return 0\n")
    run(make_env("mymod_pth_helper:main"), site_dir)
    assert sys.modules["pth_helper"].__file__ == str(site_dir / "pth_helper.py")


def test_non_str_sys_path_entry_is_skipped(tmp_path: Path, host_site: Path) -> None:
    # Host startup code can leave junk such as None on sys.path; bare addsitedir
    # tolerated it, so the placement must too.
    sys.path.insert(sys.path.index(str(host_site)), cast(Any, None))
    site_dir = make_site_dir(tmp_path)
    write_module(site_dir, "mymod_junk", "def main():\n    return 0\n")
    run(make_env("mymod_junk:main"), site_dir)
    assert sys.path.index(str(site_dir)) == sys.path.index(str(host_site)) - 1


# ---------- exit-code attributes ----------


def test_collision_error_exit_code_is_1() -> None:
    assert CollisionError("x").exit_code == 1


def test_entry_point_error_exit_code_is_2() -> None:
    assert EntryPointError("x").exit_code == 2
