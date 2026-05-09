"""D7 gate: enforce stdlib-only imports under src/moonlit/_bootstrap/.

Per specs/03-bootstrap-runtime.md §12 and specs/CROSS_CUTTING_DECISIONS.md D7,
the bootstrap runs before staged site-packages reaches sys.path, so it must
import only from the Python 3.13 standard library. There is no runtime
self-check; this test is the gate, run in CI on every push.

This file also enforces that os.rename appears only inside the documented
D4 atomic_replace_dir protocol (specs/03-bootstrap-runtime.md §6, §11).

The gate runs over the real src/moonlit/_bootstrap/ tree AND over synthetic
ASTs so the gate's own correctness is pinned even before any bootstrap code
exists.
"""

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

BOOTSTRAP_ROOT = Path(__file__).resolve().parents[2] / "src" / "moonlit" / "_bootstrap"

# Per spec 03 §6, atomic_replace_dir is the only function that may use os.rename.
_OS_RENAME_ALLOWED_FUNCTIONS = frozenset({"atomic_replace_dir"})


# ---------- AST helpers ----------


def _bootstrap_files() -> list[Path]:
    if not BOOTSTRAP_ROOT.is_dir():
        return []
    return sorted(BOOTSTRAP_ROOT.rglob("*.py"))


def _absolute_imports(tree: ast.AST) -> set[str]:
    """Top-level module names from absolute Import / ImportFrom statements."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                continue  # relative; D7 ignores these
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _walk_calls_with_parent(
    tree: ast.AST,
) -> Iterator[tuple[str | None, ast.Call]]:
    """Yield (innermost-enclosing-function-name | None, Call-node) for every Call."""

    def recurse(node: ast.AST, enclosing: str | None) -> Iterator[tuple[str | None, ast.Call]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                yield (enclosing, child)
            new_enclosing = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else enclosing
            )
            yield from recurse(child, new_enclosing)

    yield from recurse(tree, None)


def _is_os_rename(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "rename"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _os_rename_calls_outside_d4(tree: ast.AST) -> list[tuple[int, str | None]]:
    """[(lineno, enclosing-fn) for each os.rename call outside D4]."""
    out: list[tuple[int, str | None]] = []
    for enclosing, call in _walk_calls_with_parent(tree):
        if not _is_os_rename(call.func):
            continue
        if enclosing in _OS_RENAME_ALLOWED_FUNCTIONS:
            continue
        out.append((call.lineno, enclosing))
    return out


# ---------- gate: applied to real _bootstrap/ files ----------


def test_bootstrap_modules_import_only_stdlib() -> None:
    violations: list[str] = []
    for path in _bootstrap_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = sorted(n for n in _absolute_imports(tree) if n not in sys.stdlib_module_names)
        if bad:
            violations.append(f"  {path.name}: {bad}")
    assert not violations, "Bootstrap modules with non-stdlib imports (D7):\n" + "\n".join(
        violations
    )


def test_no_os_rename_outside_d4_in_bootstrap() -> None:
    violations: list[str] = []
    for path in _bootstrap_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _os_rename_calls_outside_d4(tree)
        if bad:
            violations.append(f"  {path.name}: {bad}")
    assert not violations, (
        "os.rename used outside the D4 atomic_replace_dir protocol "
        f"(allowed: {sorted(_OS_RENAME_ALLOWED_FUNCTIONS)}):\n" + "\n".join(violations)
    )


# ---------- gate self-tests: synthetic ASTs pin the gate's own correctness ----------


def test_gate_flags_third_party_import_statement() -> None:
    tree = ast.parse("import click\nimport os\nimport sys\n")
    bad = sorted(n for n in _absolute_imports(tree) if n not in sys.stdlib_module_names)
    assert bad == ["click"]


def test_gate_flags_third_party_from_import() -> None:
    tree = ast.parse("from click import command\n")
    bad = sorted(n for n in _absolute_imports(tree) if n not in sys.stdlib_module_names)
    assert bad == ["click"]


def test_gate_ignores_intra_package_relative_imports() -> None:
    tree = ast.parse("from . import foo\nfrom ..bar import baz\nfrom .x.y import z\n")
    assert _absolute_imports(tree) == set()


def test_gate_handles_dotted_stdlib_imports() -> None:
    tree = ast.parse("from collections.abc import Iterator\nimport importlib.resources\n")
    names = _absolute_imports(tree)
    assert names <= sys.stdlib_module_names
    assert "collections" in names
    assert "importlib" in names


def test_gate_flags_os_rename_at_module_level() -> None:
    tree = ast.parse("import os\nos.rename('a', 'b')\n")
    bad = _os_rename_calls_outside_d4(tree)
    assert len(bad) == 1
    assert bad[0][1] is None  # enclosing function is None (module level)


def test_gate_flags_os_rename_in_disallowed_function() -> None:
    tree = ast.parse("import os\ndef sneaky():\n    os.rename('a', 'b')\n")
    bad = _os_rename_calls_outside_d4(tree)
    assert len(bad) == 1
    assert bad[0][1] == "sneaky"


def test_gate_allows_os_rename_inside_atomic_replace_dir() -> None:
    src = "import os\ndef atomic_replace_dir(src, dst, pid):\n    os.rename(src, dst)\n"
    assert _os_rename_calls_outside_d4(ast.parse(src)) == []


def test_gate_uses_innermost_enclosing_function() -> None:
    # An os.rename call inside a nested helper inside atomic_replace_dir
    # is still a violation — the helper is the innermost enclosing function.
    src = (
        "import os\n"
        "def atomic_replace_dir(src, dst, pid):\n"
        "    def helper():\n"
        "        os.rename(src, dst)\n"
        "    helper()\n"
    )
    bad = _os_rename_calls_outside_d4(ast.parse(src))
    assert len(bad) == 1
    assert bad[0][1] == "helper"


def test_gate_does_not_match_pathlib_rename_or_os_replace() -> None:
    src = "import os\nfrom pathlib import Path\nPath('x').rename('y')\nos.replace('a', 'b')\n"
    assert _os_rename_calls_outside_d4(ast.parse(src)) == []
