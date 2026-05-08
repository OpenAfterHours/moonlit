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
