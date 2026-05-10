# Getting started

## Prerequisites

- Python 3.13 or later.
- `uv` on `PATH`. Install per the [uv install guide](https://docs.astral.sh/uv/getting-started/installation/).
- A uv-managed project with both a `pyproject.toml` and a `uv.lock` (run `uv lock` if missing).

## Install

```sh
uv tool install moonlit
moonlit --help
```

Alternative installers:

```sh
pipx install moonlit
# or
pip install --user moonlit
```

From source (for hacking on moonlit itself):

```sh
git clone https://github.com/OpenAfterHours/moonlit.git
cd moonlit
uv sync
uv run moonlit --help
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

## Cross-interpreter builds

Native-extension wheels (e.g. `msgspec`, `numpy`, `pydantic-core`) carry `cp<X><Y>` ABI tags and only load on the matching Python `major.minor`. By default `moonlit build` targets the build host's interpreter — the `.pyz` will only run on that exact `major.minor`. To target a different Python, pass `--python-version <X.Y>`:

```sh
# Build for Python 3.12 from a 3.13 dev box.
uv run moonlit build --python-version 3.12 -e myapp.cli:main -o myapp-py312.pyz
```

The flag threads through every `uv` invocation (`export`, `pip install --target`, `build`). uv auto-fetches a managed standalone CPython 3.12 to its cache if no 3.12 is locally installed; subsequent builds reuse it. Set `UV_PYTHON_DOWNLOADS=never` to opt out (and surface "missing interpreter" as a build-time error instead).

The chosen version is stamped into `env.json.python_version`. At runtime the bootstrap compares it against the recipient's `sys.version_info.major.minor` and exits 1 with a clear message on mismatch — surfacing the real cause of the otherwise-mysterious `ModuleNotFoundError: No module named '<pkg>._core'` that occurs when a `.pyd` is silently skipped because its ABI tag doesn't match the running Python:

```
moonlit: this archive was built for Python 3.12, but you are running Python 3.13;
install a Python 3.12 interpreter or rebuild with `moonlit build --python <python-3.12>`
```

For `--windows-exe` builds, combining `--python-version <X.Y>` with the default `-p` automatically pivots the launcher's shebang to `py -<X.Y>` so the Windows PEP 397 launcher selects the matching Python on the recipient's machine. Pass `-p` explicitly to override.

```sh
# Produces test-out.exe with shebang `py -3.12`.
uv run moonlit build --windows-exe --python-version 3.12 -e myapp.cli:main -o myapp.exe
```

!!! note "Recipients still need a matching Python installed"
    Like [shiv](https://github.com/linkedin/shiv), the produced `.pyz`/`.exe` does **not** bundle the Python interpreter — only the dependency closure. The recipient needs a Python whose `major.minor` matches the build's target ABI on `PATH` (or registered with `py.exe` on Windows). Multi-version-in-one-artifact (one `.pyz` that runs on multiple Pythons) is **not** supported; build one artifact per target version.

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
| `moonlit: this archive was built for Python X.Y, but you are running Python A.B` (runtime exit 1) | Recipient's Python `major.minor` doesn't match the wheels' ABI tag | Install a Python `X.Y` matching the build (or use `py.exe`/`uv python install`); or rebuild with `moonlit build --python-version A.B …`. |

The full exit-code map is in the [CLI reference](cli-reference.md).
