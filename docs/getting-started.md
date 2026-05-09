# Getting started

## Prerequisites

- Python 3.13 or later.
- `uv` on `PATH`. Install per the [uv install guide](https://docs.astral.sh/uv/getting-started/installation/).
- A uv-managed project with both a `pyproject.toml` and a `uv.lock` (run `uv lock` if missing).

## Install

`moonlit` is not yet on PyPI. Install from source:

```sh
git clone <moonlit-repo> moonlit
cd moonlit
uv sync
uv run moonlit --help
```

For one-shot use without a clone:

```sh
uv tool install --from <moonlit-repo> moonlit
moonlit --help
```

## Single-package walkthrough

Given a project rooted at `myapp/`:

```
myapp/
├── pyproject.toml          # [project].name = "myapp"; build-system = hatchling
├── uv.lock
└── src/myapp/
    ├── __init__.py
    └── cli.py              # def main() -> int: print("hello"); return 0
```

Build the zipapp:

```sh
cd myapp
uv run moonlit build -e myapp.cli:main -o myapp.pyz
```

You should see one line on stdout:

```
wrote myapp.pyz (X.Y MiB, N entries)
```

Run it:

```sh
python ./myapp.pyz
```

Output:

```
hello
```

The first run extracts the bundled `site-packages/` to a per-build cache; subsequent runs of the same `.pyz` hit the cache directly without unpacking.

## Workspace walkthrough

`moonlit` understands `[tool.uv.workspace]`. Set up two members:

```
moonlit-demo/
├── pyproject.toml          # [tool.uv.workspace] members = ["packages/*"]
├── uv.lock
└── packages/
    ├── greeter/
    │   ├── pyproject.toml  # [project].name = "greeter"
    │   └── src/greeter/__init__.py    # def greet(): return "hello from greeter"
    └── shouter/
        ├── pyproject.toml  # [project].name = "shouter"
        │                   # [tool.uv.sources] greeter = {workspace = true}
        └── src/shouter/cli.py
```

The contents of `shouter/src/shouter/cli.py`:

```python
from greeter import greet

def main():
    print(greet().upper())
    return 0
```

Build the zipapp from the `shouter` member:

```sh
cd moonlit-demo
uv lock
uv run moonlit build --package shouter -e shouter.cli:main -o shouter.pyz
```

Run it:

```sh
python ./shouter.pyz
```

Output:

```
HELLO FROM GREETER
```

The `greeter` workspace dep was built and bundled automatically by `uv build --all-packages` plus the per-wheel install loop. No edits to `requirements.txt` are needed; the lockfile is the source of truth.

!!! warning "Windows PowerShell 5.1: UTF-8 BOM gotcha"
    On Windows PowerShell 5.1 (the default), `Set-Content -Encoding utf8` writes UTF-8 *with* a BOM, and `tomllib` rejects the BOM with `Invalid statement (at line 1, column 1)`, surfacing as `MalformedPyprojectError` (exit 5). To author `pyproject.toml` files from PowerShell 5.1, use one of:

    - `[System.IO.File]::WriteAllText($path, $content, (New-Object System.Text.UTF8Encoding $false))` — explicit no-BOM UTF-8, works in PS 5.1 and PS 7+.
    - `Set-Content -Encoding ascii $path` — fine for ASCII-only content.
    - `Out-File -Encoding utf8NoBOM $path` — PS 7+ only.

## Verifying the runtime contract

After running `python ./shouter.pyz` once, the cache should exist:

=== "Windows"

    ```pwsh
    ls $env:LOCALAPPDATA\moonlit
    ```

=== "POSIX"

    ```sh
    ls ~/.moonlit
    ```

You'll see one directory named `<normalized-name>_<build_id>`, where `<normalized-name>` is the PEP 503 normalization of the target's `[project].name` (lowercase, runs of `.`/`-`/`_` collapsed to `-`) and `<build_id>` is a 64-character hex SHA-256 digest of the staged tree.

To force re-extraction (useful when verifying a build):

```sh
MOONLIT_FORCE_EXTRACT=1 python ./shouter.pyz
```

To redirect the cache to a different location:

```sh
MOONLIT_ROOT=/tmp/moonlit-cache python ./shouter.pyz
```

For more on the runtime, see [Runtime](runtime.md).

## Common errors

| Symptom | Cause | Fix |
|---|---|---|
| `UvNotFoundError: uv binary not found on PATH` (exit 3) | `uv` isn't installed or not on `PATH` | Install uv per the link above. |
| `NoLockfileError: uv.lock not found` (exit 4) | The project root has no `uv.lock` | Run `uv lock` first. |
| `MissingPackageError: --package is required for uv workspaces` (exit 5) | The project is a workspace but you didn't pass `--package` | Pass `--package <member>`. |
| `UnknownPackageError: --package 'X' not in workspace; members: ...` (exit 5) | Typo in `--package` value | Match against the listed member names (PEP 503 normalized). |
| `OutputExistsError: output already exists; pass --force to overwrite` (exit 7) | The output `.pyz` is already on disk | Pass `--force`, or delete it. |

The full exit-code map is in the [CLI reference](cli-reference.md).
