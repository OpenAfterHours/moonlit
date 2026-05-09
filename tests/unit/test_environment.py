"""Pin _bootstrap/environment.load to specs/05-env-json-schema.md (D8).

NB on test mode: specs/00-architecture.md §2 reserves e2e-via-subprocess
as the contract test mode for the bootstrap. These unit tests exercise the
validation logic via direct import as a development-time TDD harness;
the e2e suite (built once the full bootstrap exists) is the contract.
"""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moonlit._bootstrap.environment import Environment, load
from moonlit._bootstrap.errors import EnvJsonError

# ---------- helpers ----------


def valid_env() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "myapp",
        "build_id": ("a3f1c2d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"),
        "entry_point": "myapp.cli:main",
        "built_at": "2026-05-08T15:23:01Z",
        "moonlit_version": "0.1.0",
        "python_shebang": "/usr/bin/env python3",
    }


def make_pyz(tmp_path: Path, env_bytes: bytes | None) -> Path:
    pyz = tmp_path / "app.pyz"
    with zipfile.ZipFile(pyz, "w") as zf:
        zf.writestr("__main__.py", "")
        if env_bytes is not None:
            zf.writestr("env.json", env_bytes)
    return pyz


def make_pyz_with(tmp_path: Path, env_dict: dict[str, Any]) -> Path:
    return make_pyz(tmp_path, json.dumps(env_dict).encode("utf-8"))


# ---------- happy path ----------


def test_load_returns_environment_for_valid_json(tmp_path: Path) -> None:
    pyz = make_pyz_with(tmp_path, valid_env())
    env = load(pyz)
    assert isinstance(env, Environment)
    assert env.schema_version == 1
    assert env.name == "myapp"
    assert env.build_id == valid_env()["build_id"]
    assert env.entry_point == "myapp.cli:main"
    assert env.built_at == "2026-05-08T15:23:01Z"
    assert env.moonlit_version == "0.1.0"
    assert env.python_shebang == "/usr/bin/env python3"


def test_environment_is_frozen(tmp_path: Path) -> None:
    # `dataclasses.FrozenInstanceError` is a subclass of AttributeError, so
    # AttributeError covers the frozen-dataclass and the no-such-attribute cases.
    pyz = make_pyz_with(tmp_path, valid_env())
    env = load(pyz)
    with pytest.raises(AttributeError):
        env.name = "other"  # type: ignore[misc]


def test_unknown_fields_are_ignored_d9(tmp_path: Path) -> None:
    # D9: forward-compatibility — consumers ignore unknown / reserved fields.
    env_dict = {**valid_env(), "future_extra_key": [1, 2, 3], "hashes": {"x": "y"}}
    pyz = make_pyz_with(tmp_path, env_dict)
    env = load(pyz)
    assert env.name == "myapp"


# ---------- step 1: env.json existence / archive readability ----------


def test_missing_env_json_raises(tmp_path: Path) -> None:
    pyz = make_pyz(tmp_path, env_bytes=None)
    with pytest.raises(EnvJsonError, match="missing from archive"):
        load(pyz)


def test_archive_not_a_zipfile_raises(tmp_path: Path) -> None:
    not_a_zip = tmp_path / "not.pyz"
    not_a_zip.write_bytes(b"this is not a zip file")
    with pytest.raises(EnvJsonError):
        load(not_a_zip)


# ---------- step 2: UTF-8 decode ----------


def test_invalid_utf8_raises(tmp_path: Path) -> None:
    pyz = make_pyz(tmp_path, env_bytes=b"\xff\xfe\xfd not utf-8")
    with pytest.raises(EnvJsonError, match="not valid UTF-8"):
        load(pyz)


# ---------- step 3: JSON parse ----------


@pytest.mark.parametrize("payload", [b"{not json}", b"{", b"", b"   ", b'{"a": 1,}'])
def test_invalid_json_raises(tmp_path: Path, payload: bytes) -> None:
    pyz = make_pyz(tmp_path, env_bytes=payload)
    with pytest.raises(EnvJsonError, match="not valid JSON"):
        load(pyz)


# ---------- step 4: top-level dict ----------


@pytest.mark.parametrize(
    "payload",
    [b"[1, 2]", b'"a string"', b"42", b"3.14", b"true", b"false", b"null"],
)
def test_top_level_not_an_object_raises(tmp_path: Path, payload: bytes) -> None:
    pyz = make_pyz(tmp_path, env_bytes=payload)
    with pytest.raises(EnvJsonError, match="must be a JSON object"):
        load(pyz)


# ---------- step 5: schema_version int and not bool ----------


def test_schema_version_missing_raises(tmp_path: Path) -> None:
    env = valid_env()
    del env["schema_version"]
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="schema_version missing or not an integer"):
        load(pyz)


@pytest.mark.parametrize("value", ["1", True, False, None, 1.0, [1], {"v": 1}])
def test_schema_version_wrong_type_raises(tmp_path: Path, value: Any) -> None:
    env = {**valid_env(), "schema_version": value}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="schema_version missing or not an integer"):
        load(pyz)


# ---------- step 6: schema_version == 1 ----------


@pytest.mark.parametrize("v", [0, 2, -1, 99])
def test_unsupported_schema_version_raises(tmp_path: Path, v: int) -> None:
    env = {**valid_env(), "schema_version": v}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match=f"unsupported schema_version {v}"):
        load(pyz)


# ---------- step 7: required fields present ----------


_REQUIRED_NON_VERSION_FIELDS = [
    "name",
    "build_id",
    "entry_point",
    "built_at",
    "moonlit_version",
    "python_shebang",
]


@pytest.mark.parametrize("field", _REQUIRED_NON_VERSION_FIELDS)
def test_missing_required_field_raises(tmp_path: Path, field: str) -> None:
    env = valid_env()
    del env[field]
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match=f"missing required field '{field}'"):
        load(pyz)


# ---------- step 8: types ----------


@pytest.mark.parametrize("field", _REQUIRED_NON_VERSION_FIELDS)
@pytest.mark.parametrize("value", [123, None, [], {}, True, 1.5])
def test_field_wrong_type_raises(tmp_path: Path, field: str, value: Any) -> None:
    env = {**valid_env(), field: value}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match=f"field '{field}' has wrong type"):
        load(pyz)


# ---------- step 9: format ----------


@pytest.mark.parametrize("name", ["valid", "valid-name", "MyApp", "my_pkg", "1abc", "a"])
def test_valid_name_passes(tmp_path: Path, name: str) -> None:
    env = {**valid_env(), "name": name}
    pyz = make_pyz_with(tmp_path, env)
    assert load(pyz).name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "with space",
        "_underscore",
        "trailing-",
        "-leading",
        ".leading-dot",
        "trailing.",
    ],
)
def test_invalid_name_raises(tmp_path: Path, name: str) -> None:
    env = {**valid_env(), "name": name}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'name' failed validation"):
        load(pyz)


@pytest.mark.parametrize(
    "build_id",
    [
        "",
        "abc",
        "z" * 64,
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase hex
    ],
)
def test_invalid_build_id_raises(tmp_path: Path, build_id: str) -> None:
    env = {**valid_env(), "build_id": build_id}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'build_id' failed validation"):
        load(pyz)


@pytest.mark.parametrize(
    "ep",
    [
        "module:func",
        "pkg.module:func",
        "pkg.sub.mod:cls.method",
        "_underscore:_callable",
    ],
)
def test_valid_entry_point_passes(tmp_path: Path, ep: str) -> None:
    env = {**valid_env(), "entry_point": ep}
    pyz = make_pyz_with(tmp_path, env)
    assert load(pyz).entry_point == ep


@pytest.mark.parametrize(
    "ep",
    [
        "",
        "no_colon",
        "two:colons:here",
        ":nostart",
        "noend:",
        "with space:func",
        "func: spaced",
        "1bad:func",
        "module:1bad",
        "module:.leading",
        "module..double:func",
        "module:func.",
    ],
)
def test_invalid_entry_point_raises(tmp_path: Path, ep: str) -> None:
    env = {**valid_env(), "entry_point": ep}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'entry_point' failed validation"):
        load(pyz)


@pytest.mark.parametrize(
    "ts",
    [
        "2026-05-08T15:23:01",  # missing Z
        "2026-05-08T15:23:01.123Z",  # microseconds
        "2026-05-08 15:23:01Z",  # space instead of T
        "garbage",
        "",
        "2026-13-08T15:23:01Z",  # invalid month
    ],
)
def test_invalid_built_at_raises(tmp_path: Path, ts: str) -> None:
    env = {**valid_env(), "built_at": ts}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'built_at' failed validation"):
        load(pyz)


def test_empty_moonlit_version_raises(tmp_path: Path) -> None:
    env = {**valid_env(), "moonlit_version": ""}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'moonlit_version' failed validation"):
        load(pyz)


@pytest.mark.parametrize(
    "shebang",
    [
        "",
        "#!/usr/bin/env python3",
        "/usr/bin/env\npython3",
    ],
)
def test_invalid_shebang_raises(tmp_path: Path, shebang: str) -> None:
    env = {**valid_env(), "python_shebang": shebang}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'python_shebang' failed validation"):
        load(pyz)


# ---------- D8 ordering: earlier failures shadow later ones ----------


def test_dict_check_runs_before_schema_version(tmp_path: Path) -> None:
    pyz = make_pyz(tmp_path, env_bytes=b"[]")
    with pytest.raises(EnvJsonError, match="must be a JSON object"):
        load(pyz)


def test_schema_check_runs_before_required_fields(tmp_path: Path) -> None:
    # schema_version bad AND required fields missing → schema error wins.
    env = {"schema_version": "wrong", "name": "myapp"}
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="schema_version"):
        load(pyz)


def test_required_check_runs_before_type_check(tmp_path: Path) -> None:
    env = valid_env()
    del env["name"]
    env["build_id"] = 12345  # also wrong type
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="missing required field 'name'"):
        load(pyz)


def test_type_check_runs_before_format_check(tmp_path: Path) -> None:
    env = {**valid_env(), "build_id": 12345, "name": "ok-name"}  # type fails
    pyz = make_pyz_with(tmp_path, env)
    with pytest.raises(EnvJsonError, match="field 'build_id' has wrong type"):
        load(pyz)
