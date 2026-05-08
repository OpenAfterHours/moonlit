"""Pin workspace.detect to specs/06-workspace-integration.md."""

from pathlib import Path

import pytest

from moonlit import errors
from moonlit.workspace import Workspace, detect, pep503_normalize


# ---------- helpers ----------


def write_root_pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def make_member(root: Path, dirname: str, project_name: str | None) -> Path:
    member = root / dirname
    member.mkdir(parents=True, exist_ok=True)
    if project_name is None:
        body = "[project]\n"
    elif project_name == "":
        body = '[project]\nname = ""\n'
    else:
        body = f'[project]\nname = "{project_name}"\n'
    (member / "pyproject.toml").write_text(body, encoding="utf-8")
    return member


# ---------- pep503_normalize (D5) ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("foo", "foo"),
        ("FOO", "foo"),
        ("My-Pkg", "my-pkg"),
        ("my_pkg", "my-pkg"),
        ("my.pkg", "my-pkg"),
        ("My_Pkg", "my-pkg"),
        ("my___pkg", "my-pkg"),
        ("my-_-pkg", "my-pkg"),
        ("My.._pkg", "my-pkg"),
        ("MyPkg", "mypkg"),
    ],
)
def test_pep503_normalize_examples(raw: str, expected: str) -> None:
    assert pep503_normalize(raw) == expected


# ---------- non-workspace detection ----------


def test_no_uv_workspace_table_returns_none(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, '[project]\nname = "single"\n')
    assert detect(tmp_path) is None


def test_other_uv_settings_without_workspace_returns_none(tmp_path: Path) -> None:
    write_root_pyproject(
        tmp_path,
        '[project]\nname = "single"\n[tool.uv]\nmanaged = true\n',
    )
    assert detect(tmp_path) is None


# ---------- workspace shape ----------


def test_workspace_with_glob_collects_members(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    make_member(tmp_path, "packages/greeter", "greeter")
    make_member(tmp_path, "packages/shouter", "shouter")
    ws = detect(tmp_path)
    assert isinstance(ws, Workspace)
    assert set(ws.members) == {"greeter", "shouter"}
    assert ws.members["greeter"] == (tmp_path / "packages" / "greeter").resolve()
    assert ws.members["shouter"] == (tmp_path / "packages" / "shouter").resolve()


def test_root_with_project_is_a_member(tmp_path: Path) -> None:
    # spec 06 edge case 1.
    write_root_pyproject(
        tmp_path,
        '[project]\nname = "umbrella"\n[tool.uv.workspace]\nmembers = ["packages/*"]\n',
    )
    make_member(tmp_path, "packages/leaf", "leaf")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"umbrella", "leaf"}
    assert ws.members["umbrella"] == tmp_path.resolve()


def test_root_excluded_via_dot(tmp_path: Path) -> None:
    # spec 06 edge case 9.
    write_root_pyproject(
        tmp_path,
        (
            '[project]\nname = "umbrella"\n'
            '[tool.uv.workspace]\nmembers = ["packages/*"]\nexclude = ["."]\n'
        ),
    )
    make_member(tmp_path, "packages/leaf", "leaf")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"leaf"}


def test_exclude_removes_matched_member(tmp_path: Path) -> None:
    write_root_pyproject(
        tmp_path,
        (
            '[tool.uv.workspace]\n'
            'members = ["packages/*"]\nexclude = ["packages/skipme"]\n'
        ),
    )
    make_member(tmp_path, "packages/keep", "keep")
    make_member(tmp_path, "packages/skipme", "skipme")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"keep"}


def test_empty_workspace_returns_empty_members(tmp_path: Path) -> None:
    # spec 06 edge case 15.
    write_root_pyproject(tmp_path, "[tool.uv.workspace]\nmembers = []\nexclude = []\n")
    ws = detect(tmp_path)
    assert ws is not None
    assert dict(ws.members) == {}


def test_workspace_with_only_root_member(tmp_path: Path) -> None:
    write_root_pyproject(
        tmp_path,
        '[project]\nname = "umbrella"\n[tool.uv.workspace]\nmembers = []\n',
    )
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"umbrella"}


# ---------- skipping behavior ----------


def test_member_dir_without_pyproject_is_skipped(tmp_path: Path) -> None:
    # spec 06 edge case 3.
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    (tmp_path / "packages" / "no_project").mkdir(parents=True)
    make_member(tmp_path, "packages/has_project", "has_project")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"has_project"}


def test_member_without_name_is_skipped(tmp_path: Path) -> None:
    # spec 06 edge case 5.
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    make_member(tmp_path, "packages/no_name", None)
    make_member(tmp_path, "packages/with_name", "with_name")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"with_name"}


def test_member_with_empty_name_is_skipped(tmp_path: Path) -> None:
    # spec 06 edge case 5 (empty-name variant).
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    make_member(tmp_path, "packages/empty_name", "")
    make_member(tmp_path, "packages/has_name", "has_name")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"has_name"}


def test_glob_matching_a_file_is_skipped(tmp_path: Path) -> None:
    # spec 06 edge case 7.
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "stray.txt").write_text("not a member", encoding="utf-8")
    make_member(tmp_path, "packages/real", "real")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"real"}


# ---------- error paths ----------


def test_missing_pyproject_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, "not = valid = toml\n")
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_members_not_a_list_raises(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = "packages/*"\n')
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_members_contains_non_string_raises(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, "[tool.uv.workspace]\nmembers = [42]\n")
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_exclude_not_a_list_raises(tmp_path: Path) -> None:
    write_root_pyproject(
        tmp_path, '[tool.uv.workspace]\nmembers = []\nexclude = "x"\n'
    )
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_member_outside_project_root_raises(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # spec 06 edge case 8.
    parent = tmp_path_factory.mktemp("parent")
    project_root = parent / "proj"
    project_root.mkdir()
    sibling = parent / "outsider"
    sibling.mkdir()
    (sibling / "pyproject.toml").write_text(
        '[project]\nname = "outsider"\n', encoding="utf-8"
    )
    write_root_pyproject(
        project_root, '[tool.uv.workspace]\nmembers = ["../outsider"]\n'
    )
    with pytest.raises(errors.MalformedPyprojectError):
        detect(project_root)


def test_member_pyproject_parse_failure_raises(tmp_path: Path) -> None:
    # spec 06 edge case 4.
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    bad = tmp_path / "packages" / "bad"
    bad.mkdir(parents=True)
    (bad / "pyproject.toml").write_text("not = valid = toml\n", encoding="utf-8")
    with pytest.raises(errors.MalformedPyprojectError):
        detect(tmp_path)


def test_duplicate_normalized_names_raises_with_raw_names(tmp_path: Path) -> None:
    # spec 06 edge case 6 / step 7.
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    make_member(tmp_path, "packages/a", "Greeter")
    make_member(tmp_path, "packages/b", "greeter")
    with pytest.raises(errors.MalformedPyprojectError) as excinfo:
        detect(tmp_path)
    msg = str(excinfo.value)
    assert "duplicate" in msg.lower()
    assert "Greeter" in msg
    assert "greeter" in msg


# ---------- returned shape ----------


def test_workspace_root_field_is_resolved(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, "[tool.uv.workspace]\nmembers = []\n")
    ws = detect(tmp_path)
    assert ws is not None
    assert ws.root == tmp_path.resolve()


def test_member_paths_are_resolved(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    make_member(tmp_path, "packages/keep", "keep")
    ws = detect(tmp_path)
    assert ws is not None
    assert ws.members["keep"] == (tmp_path / "packages" / "keep").resolve()


def test_workspace_is_frozen_dataclass(tmp_path: Path) -> None:
    write_root_pyproject(tmp_path, "[tool.uv.workspace]\nmembers = []\n")
    ws = detect(tmp_path)
    assert ws is not None
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        ws.root = Path("/elsewhere")  # type: ignore[misc]


def test_overlapping_globs_do_not_create_phantom_duplicates(tmp_path: Path) -> None:
    # If two patterns both match the same dir, it appears once, not as a duplicate.
    write_root_pyproject(
        tmp_path, '[tool.uv.workspace]\nmembers = ["packages/*", "packages/foo"]\n'
    )
    make_member(tmp_path, "packages/foo", "foo")
    ws = detect(tmp_path)
    assert ws is not None
    assert set(ws.members) == {"foo"}
