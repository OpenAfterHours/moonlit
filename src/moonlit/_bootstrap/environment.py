"""Validate and load env.json from a moonlit zipapp.

Implements the D8 9-step ordered validation (specs/05-env-json-schema.md §4)
and the per-field format checks (§3). Every failure raises EnvJsonError; the
bootstrap entry point translates it to runtime exit code 1 (D3).
"""

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import EnvJsonError

_REQUIRED_FIELDS: tuple[str, ...] = (
    "name",
    "build_id",
    "entry_point",
    "built_at",
    "moonlit_version",
    "python_shebang",
)

# D11: PEP 508 name regex. The re.IGNORECASE flag is mandatory.
_PEP508_NAME = re.compile(
    r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$",
    re.IGNORECASE,
)

_BUILD_ID = re.compile(r"^[0-9a-f]{64}$")

# entry_point each side: dotted Python identifier; no whitespace.
_ENTRY_POINT_SIDE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# python_version: "<major>.<minor>" only (matches the cp<X><Y> wheel ABI tag).
_PYTHON_VERSION = re.compile(r"^\d+\.\d+$")


@dataclass(frozen=True)
class Environment:
    schema_version: int
    name: str
    build_id: str
    entry_point: str
    built_at: str
    moonlit_version: str
    python_shebang: str
    # Optional v1 field per spec 05 §7. Absent when an older moonlit produced
    # the archive; the bootstrap skips the version check in that case.
    python_version: str | None = None


def load(archive_path: str | Path) -> Environment:
    """Read env.json from ``archive_path``, validate per D8, return Environment."""
    raw = _read_env_bytes(archive_path)
    text = _decode_utf8(raw)
    parsed = _parse_json(text)
    _ensure_dict(parsed)
    _check_schema_version(parsed)
    _check_required_fields_present(parsed)
    _check_required_field_types(parsed)
    _check_field_formats(parsed)
    python_version = _read_optional_python_version(parsed)
    return Environment(
        schema_version=parsed["schema_version"],
        name=parsed["name"],
        build_id=parsed["build_id"],
        entry_point=parsed["entry_point"],
        built_at=parsed["built_at"],
        moonlit_version=parsed["moonlit_version"],
        python_shebang=parsed["python_shebang"],
        python_version=python_version,
    )


# ---------- D8 steps, in load()'s call order ----------


def _read_env_bytes(archive_path: str | Path) -> bytes:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                return zf.read("env.json")
            except KeyError as exc:
                raise EnvJsonError("env.json missing from archive") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise EnvJsonError(f"archive unreadable: {archive_path}") from exc


def _decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvJsonError("env.json is not valid UTF-8") from exc


def _parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnvJsonError("env.json is not valid JSON") from exc


def _ensure_dict(parsed: object) -> None:
    if not isinstance(parsed, dict):
        raise EnvJsonError("env.json must be a JSON object")


def _check_schema_version(parsed: dict) -> None:
    if "schema_version" not in parsed:
        raise EnvJsonError("env.json: schema_version missing or not an integer")
    v = parsed["schema_version"]
    # bool is a subclass of int in Python; reject explicitly per D8 step 5.
    if not isinstance(v, int) or isinstance(v, bool):
        raise EnvJsonError("env.json: schema_version missing or not an integer")
    if v != 1:
        raise EnvJsonError(
            f"env.json: unsupported schema_version {v}; "
            f"upgrade moonlit to a version that supports env.json schema version {v}"
        )


def _check_required_fields_present(parsed: dict) -> None:
    for field in _REQUIRED_FIELDS:
        if field not in parsed:
            raise EnvJsonError(f"env.json: missing required field '{field}'")


def _check_required_field_types(parsed: dict) -> None:
    for field in _REQUIRED_FIELDS:
        value = parsed[field]
        # bool is rejected here because every string-typed field below
        # is declared "string" in spec §2; True/False are not strings.
        if not isinstance(value, str):
            raise EnvJsonError(f"env.json: field '{field}' has wrong type (expected string)")


def _check_field_formats(parsed: dict) -> None:
    if not _PEP508_NAME.match(parsed["name"]):
        raise EnvJsonError("env.json: field 'name' failed validation")
    if not _BUILD_ID.fullmatch(parsed["build_id"]):
        raise EnvJsonError("env.json: field 'build_id' failed validation")
    if not _is_valid_entry_point(parsed["entry_point"]):
        raise EnvJsonError("env.json: field 'entry_point' failed validation")
    if not _is_valid_built_at(parsed["built_at"]):
        raise EnvJsonError("env.json: field 'built_at' failed validation")
    if not parsed["moonlit_version"]:
        raise EnvJsonError("env.json: field 'moonlit_version' failed validation")
    if not _is_valid_shebang(parsed["python_shebang"]):
        raise EnvJsonError("env.json: field 'python_shebang' failed validation")


def _is_valid_entry_point(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2:
        return False
    lhs, rhs = parts
    return bool(_ENTRY_POINT_SIDE.fullmatch(lhs) and _ENTRY_POINT_SIDE.fullmatch(rhs))


def _is_valid_built_at(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_valid_shebang(value: str) -> bool:
    if not value:
        return False
    if "\n" in value:
        return False
    if value.startswith("#!"):
        return False
    return True


def _read_optional_python_version(parsed: dict) -> str | None:
    """Read and validate the optional ``python_version`` field.

    Per spec 05 §7 / D9 this field is v1-optional: when absent the bootstrap
    skips the version check (older archives keep working). When present it
    must be a string matching ``<major>.<minor>``.
    """
    if "python_version" not in parsed:
        return None
    value = parsed["python_version"]
    if not isinstance(value, str):
        raise EnvJsonError("env.json: field 'python_version' has wrong type (expected string)")
    if not _PYTHON_VERSION.fullmatch(value):
        raise EnvJsonError("env.json: field 'python_version' failed validation")
    return value
