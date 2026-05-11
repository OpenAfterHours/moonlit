"""Error hierarchy for the moonlit CLI.

Every MoonlitError subclass carries a stable ``exit_code`` class attribute.
The top-level CLI catches MoonlitError and translates it into the process
exit status. The exit-code map is pinned in
specs/CROSS_CUTTING_DECISIONS.md (D3) and specs/01-cli.md §6.
"""


class MoonlitError(Exception):
    """Base class for all user-translatable moonlit failures."""

    exit_code: int = 1


class UvNotFoundError(MoonlitError):
    exit_code = 3


class NoLockfileError(MoonlitError):
    exit_code = 4


class NotAWorkspaceError(MoonlitError):
    exit_code = 5


class UnknownPackageError(MoonlitError):
    exit_code = 5


class MissingPackageError(MoonlitError):
    exit_code = 5


class MalformedPyprojectError(MoonlitError):
    exit_code = 5


class BadEntryPointError(MoonlitError):
    exit_code = 6


class ConsoleScriptNotFoundError(MoonlitError):
    exit_code = 6


class OutputExistsError(MoonlitError):
    exit_code = 7


class OutputNotWritableError(MoonlitError):
    exit_code = 7


class ExportError(MoonlitError):
    exit_code = 8


class StagingError(MoonlitError):
    exit_code = 9


class WheelArtifactError(MoonlitError):
    exit_code = 10


class InternalError(MoonlitError):
    exit_code = 11


class BadArchiveError(MoonlitError):
    """Input ``.pyz`` is not a valid moonlit archive (exit 12)."""

    exit_code = 12


class PythonBundleError(MoonlitError):
    """``--bundle-python`` failure: ``uv python install`` non-zero, or the
    resulting install dir doesn't have exactly one distribution child (D21c).
    """

    exit_code = 13
