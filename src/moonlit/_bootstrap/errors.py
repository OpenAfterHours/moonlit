"""Error hierarchy for the bootstrap.

Each subclass carries a stable ``exit_code`` per the D3 runtime enumeration
(specs/03-bootstrap-runtime.md §0). The bootstrap entry point catches
BootstrapError and translates ``exit_code`` into the process exit status.
"""


class BootstrapError(Exception):
    """Base class for bootstrap-internal failures."""

    exit_code: int = 1


class EnvJsonError(BootstrapError):
    """env.json missing, archive unreadable, or D8 validation failure (exit 1)."""

    exit_code = 1


class LockTimeoutError(BootstrapError):
    """Lock acquisition exceeded the wall-clock timeout (exit 3)."""

    exit_code = 3


class ExtractionError(BootstrapError):
    """Archive extraction failure or unsafe arcname (exit 1)."""

    exit_code = 1


class CollisionError(BootstrapError):
    """``_bootstrap`` collision in the staged site-packages tree (exit 1)."""

    exit_code = 1


class EntryPointError(BootstrapError):
    """Entry-point parse, import, attribute, or return-value coercion failure (exit 2)."""

    exit_code = 2


class ArchiveError(BootstrapError):
    """Archive resolution failed: empty sys.argv[0] or path is not a zipfile (exit 1)."""

    exit_code = 1


class PythonVersionMismatchError(BootstrapError):
    """Runtime Python's major.minor differs from the build-time Python (exit 1).

    The bundled wheels carry ``cp<X><Y>`` ABI tags from the build interpreter;
    a different runtime minor version skips them silently and surfaces as a
    ``ModuleNotFoundError`` on the first compiled-extension import. This
    error fails fast with both versions named so the user knows what to fix.
    """

    exit_code = 1
