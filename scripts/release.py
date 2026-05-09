#!/usr/bin/env python3
"""Bump the moonlit version, run pre-flight checks, then commit and tag.

Usage:
    uv run python scripts/release.py patch
    uv run python scripts/release.py minor
    uv run python scripts/release.py major
    uv run python scripts/release.py 0.2.3        # explicit X.Y.Z
    uv run python scripts/release.py patch --dry-run
    uv run python scripts/release.py patch --skip-tests        # NOT recommended
    uv run python scripts/release.py patch --skip-lint
    uv run python scripts/release.py patch --skip-build

What it does (in order):

  1. Reads the current version from pyproject.toml.
  2. Pre-flight: working tree must be clean, on the configured release
     branch, and the target tag (vX.Y.Z) must not already exist.
  3. Runs `uv run pytest` and `uv run ruff check` against the CURRENT code.
     If anything fails, no version is bumped and no files change.
  4. Runs `uv build` to verify packaging works at the current version.
  5. Edits the three canonical version locations:
       - pyproject.toml             (version = "X.Y.Z")
       - src/moonlit/__init__.py    (__version__ = "X.Y.Z")
       - overrides/home.html        (OpenAfterHours · vX.Y.Z subtitle)
  6. Runs `uv lock` so uv.lock reflects the new version.
  7. Commits the four updated files as `chore: release vX.Y.Z`.
  8. Creates an annotated tag `vX.Y.Z`.
  9. Prints the commands to run for actual release (push + publish).

Pushing and publishing are intentionally NOT automated — those are the
irreversible steps and should be a deliberate human action.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------------ constants

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_BRANCH = "master"

PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "moonlit" / "__init__.py"
HOMEPAGE = REPO_ROOT / "overrides" / "home.html"

# Files touched by the bump. Used for `git add` and the diff summary.
BUMP_FILES = [PYPROJECT, INIT_PY, HOMEPAGE, REPO_ROOT / "uv.lock"]


# ---------------------------------------------------------------- entry point


def main() -> int:
    args = parse_args()

    current = read_current_version()
    target = compute_target(current, args.bump)

    print(f"current: {current}")
    print(f"target : {target}")
    print()

    require_clean_tree()
    require_release_branch()
    require_tag_unused(target)

    if not args.skip_tests:
        run("uv run pytest", description="pytest")
    if not args.skip_lint:
        run("uv run ruff check src tests", description="ruff check")
    if not args.skip_build:
        run("uv build", description="uv build (current version sanity check)")

    if args.dry_run:
        print()
        print(f"--dry-run: would bump {current} -> {target} and commit + tag.")
        return 0

    print()
    print(f"=== bumping {current} -> {target} ===")
    bump_pyproject(target)
    bump_init_py(target)
    bump_homepage(target)
    run("uv lock", description="uv lock")

    print()
    show_diff_stat()

    print()
    print("=== committing + tagging ===")
    commit_release(target)
    tag_release(target)

    print()
    print_next_steps(target)
    return 0


# ----------------------------------------------------------- argument parsing


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="release.py",
        description="Bump version, run checks, commit, and tag.",
    )
    p.add_argument(
        "bump",
        help='"major" | "minor" | "patch" | explicit "X.Y.Z"',
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan; change nothing.")
    p.add_argument("--skip-tests", action="store_true", help="Skip pytest (not recommended).")
    p.add_argument("--skip-lint", action="store_true", help="Skip ruff check.")
    p.add_argument("--skip-build", action="store_true", help="Skip uv build sanity check.")
    return p.parse_args()


# ----------------------------------------------------------- version handling


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(s: str) -> Version:
    m = _VERSION_RE.match(s)
    if not m:
        die(f"version {s!r} is not in X.Y.Z form (pre-release suffixes are unsupported here)")
    return Version(int(m[1]), int(m[2]), int(m[3]))


def read_current_version() -> Version:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data["project"]["version"]
    return parse_version(raw)


def compute_target(current: Version, bump: str) -> Version:
    if bump == "major":
        target = Version(current.major + 1, 0, 0)
    elif bump == "minor":
        target = Version(current.major, current.minor + 1, 0)
    elif bump == "patch":
        target = Version(current.major, current.minor, current.patch + 1)
    else:
        target = parse_version(bump)
        if target <= current:
            die(f"explicit version {target} is not strictly greater than current {current}")
    return target


# ------------------------------------------------------------ pre-flight gates


def require_clean_tree() -> None:
    out = capture("git status --porcelain")
    if out.strip():
        print("Working tree is not clean:", file=sys.stderr)
        print(out, file=sys.stderr)
        die("commit or stash before releasing")


def require_release_branch() -> None:
    branch = capture("git rev-parse --abbrev-ref HEAD").strip()
    if branch != RELEASE_BRANCH:
        die(f"on branch {branch!r}; releases must come from {RELEASE_BRANCH!r}")


def require_tag_unused(target: Version) -> None:
    tag = f"v{target}"
    out = capture(f"git tag --list {tag}").strip()
    if out:
        die(f"tag {tag} already exists")


# ----------------------------------------------------------------- file edits


def bump_pyproject(target: Version) -> None:
    """Update the [project] version line, preserving formatting."""
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{target}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        die('could not find a top-level `version = "..."` line in pyproject.toml')
    PYPROJECT.write_text(new_text, encoding="utf-8")
    print(f'  pyproject.toml         -> version = "{target}"')


def bump_init_py(target: Version) -> None:
    text = INIT_PY.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'^__version__\s*=\s*"[^"]+"',
        f'__version__ = "{target}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        die('could not find `__version__ = "..."` in src/moonlit/__init__.py')
    INIT_PY.write_text(new_text, encoding="utf-8")
    print(f'  src/moonlit/__init__.py -> __version__ = "{target}"')


def bump_homepage(target: Version) -> None:
    """Update the `OpenAfterHours · vX.Y.Z` subtitle in the landing template."""
    text = HOMEPAGE.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"OpenAfterHours · v\d+\.\d+\.\d+",
        f"OpenAfterHours · v{target}",
        text,
        count=1,
    )
    if n != 1:
        # Not fatal — homepage may be restructured later. Warn loudly.
        print(
            "  WARNING: did not find 'OpenAfterHours · vX.Y.Z' in overrides/home.html "
            "— skipping homepage bump",
            file=sys.stderr,
        )
        return
    HOMEPAGE.write_text(new_text, encoding="utf-8")
    print(f"  overrides/home.html    -> OpenAfterHours · v{target}")


# -------------------------------------------------------------------- git ops


def show_diff_stat() -> None:
    paths = " ".join(str(p.relative_to(REPO_ROOT).as_posix()) for p in BUMP_FILES)
    print("--- diff stat ---")
    run(f"git diff --stat -- {paths}", description=None, echo=False)


def commit_release(target: Version) -> None:
    paths = [str(p.relative_to(REPO_ROOT).as_posix()) for p in BUMP_FILES]
    subprocess.run(["git", "add", *paths], check=True, cwd=REPO_ROOT)
    subprocess.run(
        ["git", "commit", "-m", f"chore: release v{target}"],
        check=True,
        cwd=REPO_ROOT,
    )
    print(f"  committed: chore: release v{target}")


def tag_release(target: Version) -> None:
    tag = f"v{target}"
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", f"moonlit {tag}"],
        check=True,
        cwd=REPO_ROOT,
    )
    print(f"  tagged: {tag}")


# --------------------------------------------------------------- next-steps blurb


def print_next_steps(target: Version) -> None:
    tag = f"v{target}"
    print("=== next steps (run when you're ready) ===")
    print()
    print("  # push the release commit + tag — the release.yml workflow takes")
    print("  # it from there: tests, builds, publishes to PyPI via OIDC, and")
    print("  # creates a GitHub Release with auto-generated notes.")
    print(f"  git push origin {RELEASE_BRANCH}")
    print(f"  git push origin {tag}")
    print()
    print("  # follow the run at:")
    print("  #   https://github.com/OpenAfterHours/moonlit/actions/workflows/release.yml")
    print()
    print("  # to undo before pushing:")
    print(f"  git tag -d {tag}")
    print("  git reset --hard HEAD~1")


# ------------------------------------------------------------------ subprocess


def run(cmd: str, *, description: str | None, echo: bool = True) -> None:
    """Run a shell command; abort on non-zero exit."""
    if description and echo:
        print(f"=== {description} ===")
    if echo:
        print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        die(f"command failed (exit {result.returncode}): {cmd}")
    if echo:
        print()


def capture(cmd: str) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
