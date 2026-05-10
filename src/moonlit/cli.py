"""moonlit CLI. Spec 01 (specs/01-cli.md) is the contract.

Top-level command groups, the ``build`` and ``info`` subcommands, the §4
preflight order (parse → project dir → ``uv`` on PATH → pyproject exists →
``uv.lock`` → workspace shape → entry-point syntax → output preflight →
pipeline), the §7 error-message format (``<ClassName>: <message>`` for
MoonlitError, ``error: <message>`` for parser-level), and the §9 SIGINT
handler (D18).

Most pipeline-level errors are raised by :mod:`moonlit.builder`; the CLI
raises only the early preflight subset (UvNotFoundError, NoLockfileError,
MalformedPyprojectError on missing pyproject) before delegating to
:func:`moonlit.builder.build`. The ``info`` subcommand is implemented
directly in this module since it has no pipeline.
"""

import re
import shutil
import signal
import sys
import traceback
import zipfile
from pathlib import Path

import click

from . import __version__
from ._bootstrap import environment as bootstrap_env
from ._bootstrap.errors import EnvJsonError as _BootstrapEnvJsonError
from .builder import BuildConfig, humanize_bytes
from .builder import build as run_build
from .errors import (
    BadArchiveError,
    MalformedPyprojectError,
    MoonlitError,
    NoLockfileError,
    UvNotFoundError,
)


class _MoonlitGroup(click.Group):
    """Click group that emits the spec §2.1 'no such subcommand' message."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is None:
            click.echo(f"error: no such subcommand: {cmd_name}", err=True)
            ctx.exit(2)
        return cmd


@click.group(
    cls=_MoonlitGroup,
    invoke_without_command=True,
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", message="moonlit %(version)s")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """moonlit — uv-powered Python zipapp builder."""
    if ctx.invoked_subcommand is None:
        # spec §2.1: no subcommand and no flag → top-level help to stderr, exit 2.
        click.echo(ctx.get_help(), err=True)
        ctx.exit(2)


@cli.command(
    name="build",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.argument("project", type=click.Path(), default=".")
@click.option(
    "-e",
    "--entry-point",
    "entry_point",
    default=None,
    help="Entry point baked into env.json (module:callable).",
)
@click.option(
    "-c",
    "--console-script",
    "console_script",
    default=None,
    help="Console-script name resolved from staged dist-info.",
)
@click.option(
    "-o",
    "--output-file",
    "output_file",
    required=True,
    type=click.Path(),
    help="Destination .pyz path.",
)
@click.option(
    "-p",
    "--python",
    "python_shebang",
    default="/usr/bin/env python3",
    help="Shebang line baked into env.json and prefixed to the .pyz.",
)
@click.option(
    "--package",
    "package",
    default=None,
    help="Workspace member to build (required iff [tool.uv.workspace] is set).",
)
@click.option(
    "--no-dev",
    "no_dev_flag",
    is_flag=True,
    default=False,
    help="Exclude dev-group dependencies (default behavior).",
)
@click.option(
    "--dev",
    "dev_flag",
    is_flag=True,
    default=False,
    help="Opt in to dev-group dependencies (mutually exclusive with --no-dev).",
)
@click.option(
    "--windows-exe",
    "windows_exe",
    is_flag=True,
    default=False,
    help="Produce a native Windows .exe (launcher + zipapp) instead of a .pyz.",
)
@click.option(
    "--python-version",
    "python_version",
    default=None,
    help=(
        "Target Python major.minor for cross-interpreter builds (e.g. 3.12). "
        "Threads through every uv invocation so wheels match that ABI; "
        "stamped into env.json. Default: build host's major.minor."
    ),
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Overwrite an existing regular-file output.",
)
@click.option("-q", "--quiet", "quiet", is_flag=True, default=False)
@click.option("-v", "--verbose", "verbose", is_flag=True, default=False)
@click.pass_context
def build_cmd(
    ctx: click.Context,
    project: str,
    entry_point: str | None,
    console_script: str | None,
    output_file: str,
    python_shebang: str,
    package: str | None,
    no_dev_flag: bool,
    dev_flag: bool,
    windows_exe: bool,
    python_version: str | None,
    force: bool,
    quiet: bool,
    verbose: bool,
) -> None:
    """Build a self-contained .pyz from a uv-managed project."""
    # spec §3 flag interactions (all → exit 2 via UsageError).
    if (entry_point is None) == (console_script is None):
        raise click.UsageError("exactly one of --entry-point/-e or --console-script/-c is required")
    if quiet and verbose:
        raise click.UsageError("--quiet and --verbose are mutually exclusive")
    if no_dev_flag and dev_flag:
        raise click.UsageError("--no-dev and --dev are mutually exclusive")
    if windows_exe and not output_file.lower().endswith(".exe"):
        # spec §3 rule 5 / D19b: --windows-exe demands an .exe output suffix.
        raise click.UsageError("--windows-exe requires --output-file to end in .exe")
    if python_version is not None:
        _validate_python_version(python_version)
    if (
        windows_exe
        and ctx.get_parameter_source("python_shebang") == click.core.ParameterSource.DEFAULT
    ):
        # D19c / D20: when --python-version is explicitly set, pivot the
        # default shebang to `py -X.Y` so the Windows PEP 397 launcher
        # selects the matching interpreter; otherwise keep the bare
        # `python.exe` default for the local-roundtrip case.
        if python_version is not None:
            python_shebang = f"py -{python_version}"
        else:
            python_shebang = "python.exe"
    _validate_shebang(python_shebang)

    # spec §4 step 2: PROJECT resolves to existing directory.
    project_root = Path(project).resolve(strict=False)
    if not project_root.is_dir():
        raise click.UsageError(f"PROJECT is not a directory: {project_root}")

    # spec §4 step 3: uv on PATH (exit 3).
    if shutil.which("uv") is None:
        raise UvNotFoundError("uv binary not found on PATH")

    # spec §4 step 4: pyproject.toml exists (exit 5).
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        raise MalformedPyprojectError(f"pyproject.toml not found: {pyproject}")

    # spec §4 step 5: uv.lock exists (exit 4).
    uvlock = project_root / "uv.lock"
    if not uvlock.is_file():
        raise NoLockfileError(f"uv.lock not found: {uvlock}")

    # Steps 6-9 happen inside builder.build() (workspace shape, entry-point
    # syntax, output preflight, pipeline).
    output_path = Path(output_file).resolve(strict=False)
    verbosity = 1 if verbose else (-1 if quiet else 0)
    config = BuildConfig(
        project_root=project_root,
        output_path=output_path,
        entry_point=entry_point,
        console_script=console_script,
        python_shebang=python_shebang,
        package=package,
        force=force,
        verbosity=verbosity,
        windows_exe=windows_exe,
        python_version=python_version,
    )
    sys.exit(run_build(config))


@cli.command(
    name="info",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.argument("pyz", type=click.Path())
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    default=False,
    help="Emit raw env.json bytes to stdout (no header).",
)
def info_cmd(pyz: str, json_mode: bool) -> None:
    """Print the env.json manifest of a moonlit-built .pyz."""
    pyz_path = _validate_info_target(pyz)

    if not zipfile.is_zipfile(pyz_path):
        raise BadArchiveError(f"not a zipfile: {pyz_path}")

    try:
        env = bootstrap_env.load(pyz_path)
    except _BootstrapEnvJsonError as exc:
        raise BadArchiveError(f"{pyz_path}: {exc}") from exc

    if json_mode:
        with zipfile.ZipFile(pyz_path, "r") as zf:
            sys.stdout.buffer.write(zf.read("env.json"))
        return

    _print_info(pyz_path, env)


def main() -> None:
    """Top-level entry point. Translates exceptions to spec §6 / D3 exit codes."""
    _install_sigint_handler()
    try:
        rv = cli(standalone_mode=False)
    except click.NoSuchOption as exc:
        click.echo(f"error: no such option: {exc.option_name}", err=True)
        sys.exit(2)
    except click.UsageError as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        sys.exit(2)
    except click.Abort:
        # spec §9: Click wraps KeyboardInterrupt as Abort under standalone_mode=False.
        # Exit 130 with no traceback (even under --verbose).
        sys.exit(130)
    except MoonlitError as exc:
        # spec §7: <ClassName>: <message>, single line, on stderr.
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        if _verbose_from_argv():
            traceback.print_exc(file=sys.stderr)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.exit(130)
    # ctx.exit(N) returns N rather than raising under standalone_mode=False;
    # propagate the code so the process exits with it.
    if isinstance(rv, int) and rv != 0:
        sys.exit(rv)


# ---------- private helpers ----------


def _install_sigint_handler() -> None:
    """Spec §9 / D18: SIGINT raises KeyboardInterrupt so finally blocks run.

    The actual cleanup of the build tempdir (D17) and any partial
    ``<output>.pyz.tmp.<pid>`` (D15) is performed by the existing finally
    blocks in :mod:`moonlit.builder`.
    """

    def handler(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, handler)
    except ValueError:
        # signal.signal raises ValueError off the main thread (e.g. inside
        # some test runners). Default Python behavior already raises
        # KeyboardInterrupt on SIGINT, so falling through is safe.
        pass


def _verbose_from_argv() -> bool:
    return any(arg in ("-v", "--verbose") for arg in sys.argv[1:])


def _validate_info_target(pyz: str) -> Path:
    """spec 01 §2.3 steps 1-2: PYZ resolves to an existing regular file."""
    pyz_path = Path(pyz).resolve(strict=False)
    if not pyz_path.exists():
        raise click.UsageError(f"PYZ does not exist: {pyz_path}")
    if not pyz_path.is_file():
        raise click.UsageError(f"PYZ is not a regular file: {pyz_path}")
    return pyz_path


def _print_info(pyz_path: Path, env: bootstrap_env.Environment) -> None:
    """spec 01 §2.3 default-mode output: header line + sorted field listing."""
    size = pyz_path.stat().st_size
    with zipfile.ZipFile(pyz_path, "r") as zf:
        n_entries = len(zf.infolist())
    click.echo(f"{pyz_path} ({humanize_bytes(size)}, {n_entries} entries)")
    fields = sorted(
        [
            ("build_id", env.build_id),
            ("built_at", env.built_at),
            ("entry_point", env.entry_point),
            ("moonlit_version", env.moonlit_version),
            ("name", env.name),
            ("python_shebang", env.python_shebang),
            ("schema_version", str(env.schema_version)),
        ]
    )
    width = max(len(name) for name, _ in fields)
    for name, value in fields:
        click.echo(f"  {name.ljust(width)}  {value}")


def _validate_shebang(shebang: str) -> None:
    """spec 02 §1: ASCII only, no NL/CR/NUL, encoded length ≤ 127 bytes."""
    if not shebang.isascii():
        raise click.UsageError("--python must contain only ASCII characters")
    if any(c in shebang for c in "\n\r\x00"):
        raise click.UsageError("--python must not contain newline, carriage-return, or NUL bytes")
    if len(shebang.encode("ascii")) > 127:
        raise click.UsageError("--python encoded length exceeds 127 bytes")


# Mirrors `_PYTHON_VERSION` in src/moonlit/_bootstrap/environment.py. Kept as a
# duplicate (rather than a private import from a stdlib-only sibling package)
# so the CLI's accept-set and env.json's accept-set are pinned by the same
# literal regardless of import direction. If the format ever changes, update
# both sites — there's a unit test that round-trips a value through the CLI
# into env.json so a divergence would surface.
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+$")


def _validate_python_version(value: str) -> None:
    """D20: --python-version must be major.minor only (matches cp<X><Y> ABI tag)."""
    if not _PYTHON_VERSION_RE.fullmatch(value):
        raise click.UsageError(f"--python-version must be major.minor (e.g. 3.12); got {value!r}")
