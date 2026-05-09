"""Workspace detection for [tool.uv.workspace] in pyproject.toml.

detect() enumerates members per specs/06-workspace-integration.md, validates
uniqueness post-PEP-503 normalization (D5/D12), and pre-validates workspace
shape so any uv subprocess later runs on a known-good configuration. Detection
is read-only with respect to the project tree.
"""

import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from moonlit.errors import MalformedPyprojectError


@dataclass(frozen=True)
class Workspace:
    """A detected uv workspace.

    ``root`` is the resolved project root. ``members`` maps each raw
    ``[project].name`` to the resolved member directory. To match a
    user-supplied ``--package`` value, normalize both sides via
    :func:`pep503_normalize` (D5/D12).
    """

    root: Path
    members: Mapping[str, Path]


def detect(project_root: Path) -> Workspace | None:
    """Parse ``project_root/pyproject.toml`` and return a Workspace or None."""
    data = _load_pyproject(project_root / "pyproject.toml")
    table = data.get("tool", {}).get("uv", {}).get("workspace")
    if table is None:
        return None
    members_globs = _read_str_list(table, "members")
    excludes_globs = _read_str_list(table, "exclude")
    excluded = _resolve_excludes(project_root, excludes_globs)

    collected: list[tuple[str, Path]] = []
    collected_paths: set[Path] = set()
    for raw_name, directory in _iter_glob_members(project_root, members_globs, excluded):
        collected.append((raw_name, directory))
        collected_paths.add(directory)

    root_resolved = project_root.resolve()
    root_name = _project_name(data)
    if root_name and root_resolved not in excluded and root_resolved not in collected_paths:
        collected.append((root_name, root_resolved))

    _enforce_unique_normalized_names([n for n, _ in collected])
    return Workspace(root=root_resolved, members=MappingProxyType(dict(collected)))


def pep503_normalize(name: str) -> str:
    """PEP 503 normalize a project name (D5)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------- private helpers, in detect()'s call order ----------


def _load_pyproject(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as exc:
        raise MalformedPyprojectError(f"pyproject.toml not found: {path}") from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise MalformedPyprojectError(f"pyproject.toml could not be parsed: {path}: {exc}") from exc


def _read_str_list(table: dict, key: str) -> list[str]:
    raw = table.get(key, [])
    if not isinstance(raw, list):
        raise MalformedPyprojectError(f"[tool.uv.workspace].{key} must be a list of strings")
    for item in raw:
        if not isinstance(item, str):
            raise MalformedPyprojectError(
                f"[tool.uv.workspace].{key} contains non-string element: {item!r}"
            )
    return raw


def _resolve_excludes(project_root: Path, patterns: list[str]) -> set[Path]:
    resolved: set[Path] = set()
    for pattern in patterns:
        # 3.13's Path.glob raises ValueError on patterns like "."; the literal
        # fallback below handles that case (spec 06 edge case 9: exclude = ["."]).
        try:
            for match in project_root.glob(pattern):
                resolved.add(match.resolve())
        except ValueError:
            pass
        literal = project_root / pattern
        if literal.exists():
            resolved.add(literal.resolve())
    return resolved


def _iter_glob_members(
    project_root: Path,
    patterns: list[str],
    excluded: set[Path],
) -> Iterator[tuple[str, Path]]:
    project_root_resolved = project_root.resolve()
    yielded_paths: set[Path] = set()
    for pattern in patterns:
        for match in project_root.glob(pattern):
            if not match.is_dir():
                continue
            resolved = match.resolve()
            if resolved in yielded_paths:
                continue
            if not _is_under(resolved, project_root_resolved):
                raise MalformedPyprojectError(f"workspace member outside project root: {resolved}")
            member_pyproject = match / "pyproject.toml"
            if not member_pyproject.exists():
                continue
            data = _load_pyproject(member_pyproject)
            name = _project_name(data)
            if not name:
                continue
            if resolved in excluded:
                continue
            yielded_paths.add(resolved)
            yield name, resolved


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _project_name(data: dict) -> str | None:
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _enforce_unique_normalized_names(names: list[str]) -> None:
    by_norm: dict[str, list[str]] = {}
    for raw in names:
        by_norm.setdefault(pep503_normalize(raw), []).append(raw)
    for raws in by_norm.values():
        if len(raws) > 1:
            joined = ", ".join(sorted(raws))
            raise MalformedPyprojectError(f"workspace has duplicate package names: {joined}")
