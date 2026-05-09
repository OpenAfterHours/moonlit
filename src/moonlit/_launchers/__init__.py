"""Vendored Windows launcher binaries for ``moonlit build --windows-exe``.

The .exe files in this package are pre-built artifacts of the ``launcher/``
Rust crate (one binary per Windows architecture: x86, x64, arm64, named
``t-<arch>.exe``). They are prepended to a Python zipapp by the build pipeline
to produce a native Windows-runnable executable.

Source code, build recipe, and license live under ``launcher/`` at the repo
root. To regenerate: ``cargo build --release --target <triple>`` per the
``launcher/README.md`` instructions, then copy the produced ``t.exe`` into
this directory under the appropriate ``t-<arch>.exe`` name.
"""
