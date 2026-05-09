# moonlit-launcher

Native Windows launcher prepended to moonlit zipapps when built with
`moonlit build --windows-exe`. The launcher locates the running `.exe` on
disk, parses the PE section table to find the trailing data, reads a `#!`
shebang line, and runs `python.exe ... <self_path> ...` so Python executes
the trailing zip body as a zipapp.

This crate produces one binary, `t.exe` ("terminal" / console launcher).
The Python side (`src/moonlit/_launchers/`) vendors three pre-built copies
named by architecture: `t-x86.exe`, `t-x64.exe`, `t-arm64.exe`. The MVP
ships console-only — windowed (`w.exe`) variants are planned for later.

## Build

The launcher targets Windows. Two ABIs work; pick the one whose toolchain
you already have:

- **MSVC (`*-pc-windows-msvc`)** — needs Visual Studio Build Tools with the
  "Desktop development with C++" workload (~2 GB). Smaller binaries.
- **GNU (`*-pc-windows-gnu`)** — `rustup` bundles MinGW with this toolchain,
  so installation is self-contained inside `rustup`. Slightly larger binaries
  (~250 KiB vs ~20 KiB for MSVC).

The vendored binaries currently in `src/moonlit/_launchers/` were built with
the **GNU** toolchain. The CI workflow (planned) will switch to MSVC once a
Windows runner with VS Build Tools is configured.

```powershell
# GNU (recommended for setup-from-scratch)
rustup toolchain install stable-x86_64-pc-windows-gnu
cargo +stable-x86_64-pc-windows-gnu build --release --target x86_64-pc-windows-gnu

# Other arches (GNU): additional rustup targets
rustup target add i686-pc-windows-gnu --toolchain stable-x86_64-pc-windows-gnu
cargo +stable-x86_64-pc-windows-gnu build --release --target i686-pc-windows-gnu
# (aarch64 windows-gnu is partial; prefer aarch64-pc-windows-msvc once VS Build Tools are present)

# MSVC (smallest binaries, requires VS Build Tools)
cargo build --release --target x86_64-pc-windows-msvc
cargo build --release --target i686-pc-windows-msvc
cargo build --release --target aarch64-pc-windows-msvc
```

## Refresh the vendored binaries

After any source change in `launcher/`, regenerate the artifacts under
`src/moonlit/_launchers/`:

```powershell
$root = (Resolve-Path ..).Path
function Copy-Launcher($triple, $arch) {
    $src = "target\$triple\release\t.exe"
    $dst = "$root\src\moonlit\_launchers\t-$arch.exe"
    Copy-Item $src $dst -Force
    Write-Host "wrote $dst ($([math]::Round((Get-Item $dst).Length/1KB,1)) KiB)"
}
# Current vendored set (GNU toolchain):
cargo +stable-x86_64-pc-windows-gnu build --release --target x86_64-pc-windows-gnu
Copy-Launcher x86_64-pc-windows-gnu x64
```

CI (`.github/workflows/launchers.yml`, planned) will assert that the committed
binaries match a fresh `cargo build --release` of the current `launcher/`
revision.

## End-to-end smoke

```powershell
python tests\smoke_e2e.py
```

Hand-builds a tiny fake zipapp, prepends the vendored launcher, runs the
produced `.exe`, and asserts argv forwarding + exit-code forwarding.

## Tests

```powershell
cargo test --target x86_64-pc-windows-msvc
```

The unit tests exercise the PE-end parser, the shebang tokenizer, and the
Win32 command-line quoting rules without needing to launch a real process.

## Format on disk

A produced `.exe` looks like:

```
+------------------+--------------+-------------------+----------------------+
| <PE binary>      | b"#!"+sheb.. | b"\n"             | <zipfile body>       |
+------------------+--------------+-------------------+----------------------+
                   ^                                  ^
                   pe_end (computed at runtime)       Python's zipimport reads
                                                       the central directory
                                                       at the end of the file.
```

Compatible with distlib's launcher format: an .exe produced by either
implementation is interchangeable. We re-implement the algorithm rather than
vendor distlib's launcher so that the binary, license, and source all live
in the same repository.

## License

MIT — see [LICENSE](LICENSE.txt).
