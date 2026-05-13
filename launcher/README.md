# moonlit-launcher

Native Windows launcher used by two `moonlit build` modes:

1. **`--windows-exe`** (single-file): the launcher PE is prepended to a `#!shebang\n` line and a zip body; at runtime it walks the PE section table, reads the shebang, and runs `python.exe ... <self_path> ...` so Python executes the trailing zip as a zipapp.
2. **`--bundle-python`** (folder bundle): the launcher PE sits in a folder alongside `<basename>.pyz` and `_python\python.exe`; at runtime it probes for those siblings and, if both exist, spawns the bundled interpreter directly. No on-disk extraction, no fingerprint, no AV-tripping heuristics.

This crate produces one binary, `t.exe` ("terminal" / console launcher). The Python side (`src/moonlit/_launchers/`) vendors three pre-built copies named by architecture: `t-x86.exe`, `t-x64.exe`, `t-arm64.exe`. The MVP ships console-only — windowed (`w.exe`) variants are planned for later.

## Build

The launcher targets Windows. Two ABIs work; pick the one whose toolchain you already have:

- **MSVC (`*-pc-windows-msvc`)** — needs Visual Studio Build Tools with the "Desktop development with C++" workload (~2 GB). Smaller binaries.
- **GNU (`*-pc-windows-gnu`)** — `rustup` bundles MinGW with this toolchain, so installation is self-contained inside `rustup`. Slightly larger binaries.

The vendored binaries currently in `src/moonlit/_launchers/` were built with the **GNU** toolchain. A CI workflow (planned) will switch to MSVC once a Windows runner with VS Build Tools is configured.

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

After any source change in `launcher/`, regenerate the artifacts under `src/moonlit/_launchers/`:

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

CI (`.github/workflows/launchers.yml`, planned) will assert that the committed binaries match a fresh `cargo build --release` of the current `launcher/` revision.

## End-to-end smoke

```powershell
python tests\smoke_e2e.py
```

Hand-builds a tiny fake zipapp, prepends the vendored launcher, runs the produced `.exe`, and asserts argv forwarding + exit-code forwarding.

## Tests

```powershell
cargo test --target x86_64-pc-windows-msvc
```

The unit tests exercise the PE-end parser, the shebang tokenizer, the Win32 command-line quoting rules, and the folder-bundle sibling probe — all without needing to launch a real process.

## Dispatch order at runtime

`run()` in `src/main.rs` tries two paths, in order:

1. **Folder-bundle probe (D22a)**. Compute `self_dir = parent(self_path)` and `stem = file_stem(self_path)`. If `self_dir\_python\python.exe` exists AND `self_dir\<stem>.pyz` exists, `CreateProcessW("self_dir\_python\python.exe -I self_dir\<stem>.pyz <forwarded args>")` and forward the exit code. The parent's environment is inherited as-is — no env-block manipulation.

2. **PE-end + shebang fallback**. If the probe finds nothing, the launcher must be a single-file `--windows-exe` artifact: open `self_path`, walk the PE section table to find `pe_end`, read the `#!...` line that follows, tokenize it, and `CreateProcessW(<interp> <args>... <self_path> <our_args>...)`. Python's zipimport reads the trailing zip from `self_path` regardless of leading bytes.

The two paths share `build_cmdline_w` and the same wait/exit-code semantics.

## On-disk shapes

**Single-file (`--windows-exe`)**:

```
+------------------+--------------+-------------------+----------------------+
| <PE binary>      | b"#!"+sheb.. | b"\n"             | <zipfile body>       |
+------------------+--------------+-------------------+----------------------+
                   ^                                  ^
                   pe_end (computed at runtime)       Python's zipimport reads
                                                       the central directory
                                                       at the end of the file.
```

Compatible with distlib's launcher format: an .exe produced by either implementation is interchangeable.

**Folder bundle (`--bundle-python`)**:

```
<output>\
├── <basename>.exe        ← this binary, no appended zip
├── <basename>.pyz        ← the application zipapp (same body as a non-bundle build)
└── _python\
    ├── python.exe        ← the bundled CPython interpreter (python-build-standalone)
    ├── python3XX.dll
    └── Lib\…
```

This shape avoids the runtime-extraction pattern that triggered `Trojan:Win32/Wacatac.B!ml` detections in v0.3.0 — nothing is decompressed at runtime, the launcher is just a process shim.

## License

MIT — see [LICENSE](LICENSE.txt).
