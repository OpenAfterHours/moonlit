"""``python -m moonlit`` shim — delegates to :func:`moonlit.cli.main`."""

from .cli import main

if __name__ == "__main__":
    main()
