//! moonlit zipapp launcher.
//!
//! This binary serves two output shapes produced by `moonlit build`:
//!
//! 1. **Single-file `--windows-exe`** (no `--bundle-python`): the launcher PE
//!    is prepended to `b"#!<python_shebang>\n"` and a zip body. On-disk:
//!
//!    ```text
//!    <this PE binary><b"#!"><python_shebang><b"\n"><zip body>
//!    ```
//!
//!    At run time we walk the PE section table to find the trailing data,
//!    read the shebang, and `CreateProcessW(<interp> <args>... <self_path> <our_args>...)`.
//!    Python's zipapp / zipimport machinery tolerates leading bytes before
//!    the ZIP central directory, so passing the .exe path as `argv[0]` to
//!    Python makes Python execute the trailing zip as a zipapp.
//!
//! 2. **Folder bundle (`--bundle-python`)**: the launcher PE is a standalone
//!    file in a bundle folder produced by moonlit:
//!
//!    ```text
//!    <output>\
//!    ├── <basename>.exe     ← this binary (no appended data)
//!    ├── <basename>.pyz     ← the application zipapp
//!    └── _python\python.exe ← the bundled CPython interpreter
//!    ```
//!
//!    At run time we probe for the two sibling files; if both exist we
//!    `CreateProcessW(<self_dir>\_python\python.exe -I <self_dir>\<basename>.pyz <our_args>...)`.
//!    Nothing is extracted, no fingerprint, no lock, no AV-tripping heuristics
//!    — the bundled Python on disk IS the cache.
//!
//! If the sibling probe finds nothing, the launcher falls through to the
//! PE-end + shebang path. That preserves backward-compat for users who
//! manually rename or move the launcher out of its folder, and is also how
//! the single-file `--windows-exe` case dispatches.
//!
//! Stays small and dependency-light on purpose: every byte ships in front of
//! every produced `.exe`.

#![windows_subsystem = "console"]

use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::mem;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::process;
use std::ptr;

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, TRUE};
use windows_sys::Win32::System::LibraryLoader::GetModuleFileNameW;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, WaitForSingleObject, INFINITE,
    PROCESS_INFORMATION, STARTUPINFOW,
};

/// Conventional Windows "command not found" exit code; we reuse it for any
/// failure path that prevents handing control to Python.
const EXIT_LAUNCH_FAILED: u32 = 9009;

/// Default shebang when the trailing data has no `#!` line. Mirrors PEP 397's
/// `py.exe` selecting the latest Python 3.
const DEFAULT_SHEBANG: &str = "py -3";

fn main() {
    let exit = match run() {
        Ok(code) => code,
        Err(e) => {
            eprintln!("moonlit launcher: {}", e);
            EXIT_LAUNCH_FAILED
        }
    };
    process::exit(exit as i32);
}

// ---------- top-level orchestration ----------

fn run() -> Result<u32, String> {
    let self_path = self_path()?;
    // D22a: folder-bundle probe. If we sit next to `_python\python.exe` and
    // a matching `<stem>.pyz`, dispatch the bundled interpreter directly.
    if let Some(folder) = detect_folder_bundle(&self_path) {
        return spawn_folder_bundle(&folder);
    }
    // Otherwise we're a single-file launcher prepended to a zipapp. Walk the
    // PE section table to find the trailing data, read the shebang, dispatch.
    let mut file = File::open(&self_path)
        .map_err(|e| format!("cannot open self ({}): {e}", self_path.display()))?;
    let pe_end = find_pe_end(&mut file)?;
    let shebang_line = read_shebang_line(&mut file, pe_end)?;
    let (interpreter, extra_args) = parse_shebang(&shebang_line);
    spawn_interpreter(&interpreter, &extra_args, &self_path)
}

// ---------- step 1: where am I? ----------

fn self_path() -> Result<PathBuf, String> {
    let mut buf: Vec<u16> = vec![0; 1024];
    loop {
        let n = unsafe { GetModuleFileNameW(ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) };
        if n == 0 {
            return Err(format!("GetModuleFileNameW failed: GetLastError={}", unsafe { GetLastError() }));
        }
        if (n as usize) < buf.len() {
            buf.truncate(n as usize);
            return Ok(PathBuf::from(OsString::from_wide(&buf)));
        }
        // Buffer was too small (truncated); double and retry.
        buf.resize(buf.len() * 2, 0);
    }
}

// ---------- folder-bundle probe (D22a) ----------

/// A located folder bundle: the bundled `python.exe`, the application `.pyz`,
/// and the running launcher (for forwarding `argv[0]` semantics if ever needed).
pub(crate) struct FolderBundle {
    pub python_exe: PathBuf,
    pub app_pyz: PathBuf,
}

/// If `self_path` sits in a folder bundle (sibling `_python\python.exe` AND
/// sibling `<stem>.pyz`), return their paths; otherwise None.
///
/// "Stem" is the file name with the final extension removed. We do not
/// require any specific extension on `self_path` itself — the launcher is
/// typically `<basename>.exe` but stem-based resolution does not care.
pub(crate) fn detect_folder_bundle(self_path: &Path) -> Option<FolderBundle> {
    let self_dir = self_path.parent()?;
    let stem = self_path.file_stem()?;
    let python_exe = self_dir.join("_python").join("python.exe");
    let mut app_pyz = self_dir.join(stem);
    app_pyz.set_extension("pyz");
    if python_exe.is_file() && app_pyz.is_file() {
        Some(FolderBundle { python_exe, app_pyz })
    } else {
        None
    }
}

/// `CreateProcessW(<python_exe> -I <app_pyz> <forwarded args>)`, wait, forward
/// the child's exit code. No env-block manipulation; the parent's environment
/// is inherited as-is.
fn spawn_folder_bundle(folder: &FolderBundle) -> Result<u32, String> {
    let our_args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let mut parts: Vec<OsString> = Vec::with_capacity(3 + our_args.len());
    parts.push(folder.python_exe.as_os_str().to_os_string());
    // `-I` (isolated mode) implies `-E -s`: ignore PYTHONPATH and user
    // site-packages. The bundled interpreter runs as if freshly installed.
    parts.push(OsString::from("-I"));
    parts.push(folder.app_pyz.as_os_str().to_os_string());
    parts.extend(our_args);
    let parts_refs: Vec<&OsStr> = parts.iter().map(|s| s.as_os_str()).collect();
    let mut cmdline_w = build_cmdline_w(&parts_refs);

    let mut si: STARTUPINFOW = unsafe { mem::zeroed() };
    si.cb = mem::size_of::<STARTUPINFOW>() as u32;
    let mut pi: PROCESS_INFORMATION = unsafe { mem::zeroed() };

    let ok = unsafe {
        CreateProcessW(
            ptr::null(),               // lpApplicationName: NULL → search PATH for first cmdline token
            cmdline_w.as_mut_ptr(),    // lpCommandLine (writable per Win32 contract)
            ptr::null_mut(),
            ptr::null_mut(),
            TRUE,                      // bInheritHandles → inherit stdio
            0,
            ptr::null(),               // lpEnvironment (inherit)
            ptr::null(),               // lpCurrentDirectory (inherit)
            &si,
            &mut pi,
        )
    };
    if ok == 0 {
        let err = unsafe { GetLastError() };
        return Err(format!(
            "cannot launch bundled python ({}): GetLastError={}",
            folder.python_exe.display(),
            err
        ));
    }
    unsafe {
        WaitForSingleObject(pi.hProcess, INFINITE);
        let mut code: u32 = 0;
        GetExitCodeProcess(pi.hProcess, &mut code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        Ok(code)
    }
}

// ---------- step 2: find the end of the PE image ----------

/// Return the file offset of the first byte after the PE image.
///
/// Walks the section table and returns `max(PointerToRawData + SizeOfRawData)`
/// across all sections with a non-zero raw pointer. Sections with
/// `PointerToRawData == 0` (e.g. uninitialized .bss-style) carry no file bytes
/// and do not contribute to the trailing-data offset.
fn find_pe_end(file: &mut File) -> Result<u64, String> {
    let mut dos = [0u8; 64];
    file.seek(SeekFrom::Start(0)).map_err(io_err)?;
    file.read_exact(&mut dos).map_err(io_err)?;
    if &dos[0..2] != b"MZ" {
        return Err("not a PE file: missing 'MZ' signature".into());
    }
    let e_lfanew = u32_le(&dos[0x3C..0x40]);

    file.seek(SeekFrom::Start(e_lfanew as u64)).map_err(io_err)?;
    let mut nt_header_top = [0u8; 24];
    file.read_exact(&mut nt_header_top).map_err(io_err)?;
    if &nt_header_top[0..4] != b"PE\0\0" {
        return Err("not a PE file: missing 'PE' signature".into());
    }
    // IMAGE_FILE_HEADER follows the 4-byte signature.
    // Layout:
    //   +0  Machine               (u16)
    //   +2  NumberOfSections      (u16)
    //   +4  TimeDateStamp         (u32)
    //   +8  PointerToSymbolTable  (u32)
    //   +12 NumberOfSymbols       (u32)
    //   +16 SizeOfOptionalHeader  (u16)
    //   +18 Characteristics       (u16)
    let file_header = &nt_header_top[4..24];
    let num_sections = u16_le(&file_header[2..4]) as usize;
    let opt_header_size = u16_le(&file_header[16..18]) as u64;

    // Skip the optional header to land on the section table.
    let section_table_start = e_lfanew as u64 + 24 + opt_header_size;
    file.seek(SeekFrom::Start(section_table_start)).map_err(io_err)?;

    let mut pe_end: u64 = section_table_start + 40 * num_sections as u64;
    for _ in 0..num_sections {
        let mut section = [0u8; 40];
        file.read_exact(&mut section).map_err(io_err)?;
        // IMAGE_SECTION_HEADER fields we care about:
        //   +16 SizeOfRawData     (u32)
        //   +20 PointerToRawData  (u32)
        let size_raw = u32_le(&section[16..20]) as u64;
        let ptr_raw = u32_le(&section[20..24]) as u64;
        if ptr_raw == 0 {
            continue;
        }
        let section_end = ptr_raw + size_raw;
        if section_end > pe_end {
            pe_end = section_end;
        }
    }
    Ok(pe_end)
}

// ---------- step 3: read & parse the shebang line ----------

/// Read a single line of bytes starting at `offset`. The line excludes the
/// trailing `\n` (and `\r` if present). Up to 4 KiB is consumed; the result
/// is decoded as UTF-8 (lossy on bad sequences, since shebang content is
/// expected to be ASCII Python paths).
fn read_shebang_line(file: &mut File, offset: u64) -> Result<String, String> {
    file.seek(SeekFrom::Start(offset)).map_err(io_err)?;
    let mut buf = vec![0u8; 4096];
    let n = file.read(&mut buf).map_err(io_err)?;
    let slice = &buf[..n];
    let end = slice.iter().position(|&b| b == b'\n').unwrap_or(slice.len());
    let mut line = &slice[..end];
    if line.last() == Some(&b'\r') {
        line = &line[..line.len() - 1];
    }
    Ok(String::from_utf8_lossy(line).into_owned())
}

/// Tokenize a shebang line into `(interpreter, extra_args)`.
///
/// Strips a leading `#!`. Empty/whitespace-only input returns the
/// `DEFAULT_SHEBANG` fallback. Tokens are split on ASCII whitespace; we do not
/// honor shell-style quoting because the producer (moonlit's `--python` flag)
/// rejects strings containing newline / NUL / non-ASCII (specs/02-build-pipeline.md
/// §1) and the shebang is not user-typed at runtime.
fn parse_shebang(line: &str) -> (OsString, Vec<OsString>) {
    let trimmed = line.trim().strip_prefix("#!").unwrap_or(line.trim()).trim();
    let source = if trimmed.is_empty() { DEFAULT_SHEBANG } else { trimmed };
    let mut tokens = source.split_ascii_whitespace();
    let interpreter = OsString::from(tokens.next().unwrap_or("py"));
    let extra: Vec<OsString> = tokens.map(OsString::from).collect();
    (interpreter, extra)
}

// ---------- step 4: spawn Python ----------

fn spawn_interpreter(
    interpreter: &OsStr,
    extra_args: &[OsString],
    self_path: &Path,
) -> Result<u32, String> {
    let our_args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let mut parts: Vec<&OsStr> = Vec::with_capacity(2 + extra_args.len() + our_args.len());
    parts.push(interpreter);
    for a in extra_args {
        parts.push(a.as_os_str());
    }
    parts.push(self_path.as_os_str());
    for a in &our_args {
        parts.push(a.as_os_str());
    }
    let mut cmdline_w = build_cmdline_w(&parts);

    let mut si: STARTUPINFOW = unsafe { mem::zeroed() };
    si.cb = mem::size_of::<STARTUPINFOW>() as u32;
    let mut pi: PROCESS_INFORMATION = unsafe { mem::zeroed() };

    let ok = unsafe {
        CreateProcessW(
            ptr::null(),               // lpApplicationName: NULL → search PATH for first cmdline token
            cmdline_w.as_mut_ptr(),    // lpCommandLine (writable per Win32 contract)
            ptr::null_mut(),           // lpProcessAttributes
            ptr::null_mut(),           // lpThreadAttributes
            TRUE,                      // bInheritHandles → inherit stdio
            0,                         // dwCreationFlags
            ptr::null(),               // lpEnvironment (inherit)
            ptr::null(),               // lpCurrentDirectory (inherit)
            &si,
            &mut pi,
        )
    };
    if ok == 0 {
        let err = unsafe { GetLastError() };
        return Err(format!(
            "cannot launch interpreter '{}': GetLastError={}",
            interpreter.to_string_lossy(),
            err
        ));
    }

    unsafe {
        WaitForSingleObject(pi.hProcess, INFINITE);
        let mut code: u32 = 0;
        GetExitCodeProcess(pi.hProcess, &mut code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        Ok(code)
    }
}

/// Build a Win32 CommandLineToArgvW-compatible UTF-16 command line.
///
/// Each argument is quoted per Microsoft's parsing rules (CommandLineToArgvW):
///   * wrap in `"..."` if the argument is empty or contains whitespace, `"`, etc.
///   * within quotes, double any backslashes that immediately precede a `"`,
///     and escape `"` itself as `\"`.
pub(crate) fn build_cmdline_w(parts: &[&OsStr]) -> Vec<u16> {
    let mut out: Vec<u16> = Vec::new();
    for (i, p) in parts.iter().enumerate() {
        if i > 0 {
            out.push(b' ' as u16);
        }
        push_quoted_w(&mut out, p);
    }
    out.push(0);
    out
}

fn push_quoted_w(out: &mut Vec<u16>, arg: &OsStr) {
    let wide: Vec<u16> = arg.encode_wide().collect();
    let needs_quote = wide.is_empty()
        || wide.iter().any(|&c| {
            let b = c as u32;
            b == b' ' as u32 || b == b'\t' as u32 || b == b'\n' as u32 || b == b'\x0b' as u32 || b == b'"' as u32
        });
    if !needs_quote {
        out.extend_from_slice(&wide);
        return;
    }
    out.push(b'"' as u16);
    let mut i = 0;
    while i < wide.len() {
        let c = wide[i];
        if c == b'\\' as u16 {
            // Count consecutive backslashes.
            let mut j = i;
            while j < wide.len() && wide[j] == b'\\' as u16 {
                j += 1;
            }
            let backslashes = j - i;
            if j == wide.len() {
                // Trailing run before the closing quote: double them.
                for _ in 0..backslashes * 2 {
                    out.push(b'\\' as u16);
                }
            } else if wide[j] == b'"' as u16 {
                // Run before a quote: double them, then emit \" for the quote.
                for _ in 0..backslashes * 2 {
                    out.push(b'\\' as u16);
                }
                out.push(b'\\' as u16);
                out.push(b'"' as u16);
                j += 1;
            } else {
                // Run not before a quote: emit verbatim.
                for _ in 0..backslashes {
                    out.push(b'\\' as u16);
                }
            }
            i = j;
        } else if c == b'"' as u16 {
            out.push(b'\\' as u16);
            out.push(b'"' as u16);
            i += 1;
        } else {
            out.push(c);
            i += 1;
        }
    }
    out.push(b'"' as u16);
}

// ---------- byte-order helpers ----------

fn u16_le(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}

fn u32_le(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

fn io_err<E: std::fmt::Display>(e: E) -> String {
    format!("io: {e}")
}

// ---------- tests ----------

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;
    use std::fs;
    use std::io::Write;

    /// Build a minimal valid PE file in memory with `n` sections; each section
    /// has the given `(ptr_raw, size_raw)`. Returns the full bytes plus the
    /// expected pe_end (max of ptr+size, ignoring zero-ptr sections).
    fn make_fake_pe(sections: &[(u32, u32)]) -> (Vec<u8>, u64) {
        let e_lfanew: u32 = 0x80;
        let opt_header_size: u16 = 0xF0;
        let num_sections: u16 = sections.len() as u16;

        let mut buf = vec![0u8; e_lfanew as usize];
        buf[0] = b'M';
        buf[1] = b'Z';
        buf[0x3C..0x40].copy_from_slice(&e_lfanew.to_le_bytes());

        // NT signature + IMAGE_FILE_HEADER (20 bytes) + IMAGE_OPTIONAL_HEADER
        buf.extend_from_slice(b"PE\0\0");
        let mut file_header = [0u8; 20];
        file_header[2..4].copy_from_slice(&num_sections.to_le_bytes());
        file_header[16..18].copy_from_slice(&opt_header_size.to_le_bytes());
        buf.extend_from_slice(&file_header);
        buf.extend(std::iter::repeat(0u8).take(opt_header_size as usize));

        // Section table: 40 bytes per section.
        for (ptr, size) in sections {
            let mut section = [0u8; 40];
            section[16..20].copy_from_slice(&size.to_le_bytes());
            section[20..24].copy_from_slice(&ptr.to_le_bytes());
            buf.extend_from_slice(&section);
        }

        let pe_end_floor = e_lfanew as u64 + 24 + opt_header_size as u64 + 40 * num_sections as u64;
        let computed = sections
            .iter()
            .filter(|(p, _)| *p != 0)
            .map(|(p, s)| *p as u64 + *s as u64)
            .fold(pe_end_floor, u64::max);
        (buf, computed)
    }

    fn write_pe_to_tempfile(bytes: &[u8]) -> std::fs::File {
        // Per-test unique path so parallel test threads don't race over the
        // same file. (Using std::process::id() alone would collide.)
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let id = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "moonlit-launcher-test-{}-{}.bin",
            std::process::id(),
            id
        ));
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .read(true)
            .write(true)
            .open(&path)
            .expect("create temp");
        f.write_all(bytes).expect("write");
        f
    }

    fn unique_bundle_dir(tag: &str) -> PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let id = COUNTER.fetch_add(1, Ordering::Relaxed);
        let p = std::env::temp_dir().join(format!(
            "moonlit-launcher-bundle-{tag}-{}-{}",
            std::process::id(),
            id
        ));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).expect("create bundle dir");
        p
    }

    fn touch(path: &Path) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("mkdir -p");
        }
        fs::write(path, b"").expect("touch");
    }

    #[test]
    fn pe_end_matches_max_section_end() {
        let (bytes, expected) = make_fake_pe(&[(0x200, 0x100), (0x400, 0x200), (0x800, 0x80)]);
        let mut f = write_pe_to_tempfile(&bytes);
        let pe_end = find_pe_end(&mut f).unwrap();
        // Largest section ends at 0x800 + 0x80 = 0x880.
        assert_eq!(pe_end, expected);
        assert_eq!(pe_end, 0x880);
    }

    #[test]
    fn pe_end_ignores_zero_pointer_sections() {
        // Second section has ptr=0 (uninitialized); it must not contribute.
        let (bytes, expected) = make_fake_pe(&[(0x200, 0x100), (0x000, 0xFFFF), (0x400, 0x100)]);
        let mut f = write_pe_to_tempfile(&bytes);
        let pe_end = find_pe_end(&mut f).unwrap();
        assert_eq!(pe_end, expected);
        assert_eq!(pe_end, 0x500);
    }

    #[test]
    fn pe_end_for_no_sections_falls_back_to_section_table_end() {
        // Even with zero sections, pe_end is at least just past the (empty)
        // section table — so trailing data lands in a sensible place.
        let (bytes, expected) = make_fake_pe(&[]);
        let mut f = write_pe_to_tempfile(&bytes);
        let pe_end = find_pe_end(&mut f).unwrap();
        assert_eq!(pe_end, expected);
    }

    #[test]
    fn rejects_non_pe_input() {
        // 64-byte non-PE blob: passes the DOS-header read, fails the 'MZ' check.
        let mut bytes = [0u8; 64];
        bytes[..4].copy_from_slice(b"junk");
        let mut f = write_pe_to_tempfile(&bytes);
        let err = find_pe_end(&mut f).unwrap_err();
        assert!(err.contains("'MZ'"), "unexpected error: {err}");
    }

    #[test]
    fn rejects_input_shorter_than_dos_header() {
        let mut f = write_pe_to_tempfile(b"too short");
        let err = find_pe_end(&mut f).unwrap_err();
        assert!(err.starts_with("io:"), "expected io error, got: {err}");
    }

    #[test]
    fn parse_shebang_strips_hashbang_and_tokenizes() {
        let (interp, args) = parse_shebang("#!python.exe -X utf8");
        assert_eq!(interp, OsString::from("python.exe"));
        assert_eq!(args, vec![OsString::from("-X"), OsString::from("utf8")]);
    }

    #[test]
    fn parse_shebang_handles_no_args() {
        let (interp, args) = parse_shebang("#!python");
        assert_eq!(interp, OsString::from("python"));
        assert!(args.is_empty());
    }

    #[test]
    fn parse_shebang_falls_back_when_empty() {
        let (interp, args) = parse_shebang("");
        assert_eq!(interp, OsString::from("py"));
        assert_eq!(args, vec![OsString::from("-3")]);
    }

    #[test]
    fn parse_shebang_falls_back_when_only_hashbang() {
        let (interp, args) = parse_shebang("#!");
        assert_eq!(interp, OsString::from("py"));
        assert_eq!(args, vec![OsString::from("-3")]);
    }

    #[test]
    fn parse_shebang_tolerates_no_hashbang_prefix() {
        // If the producer (or a test fixture) skipped the `#!`, we still
        // tokenize the line — same effect as if it were present.
        let (interp, args) = parse_shebang("python.exe -u");
        assert_eq!(interp, OsString::from("python.exe"));
        assert_eq!(args, vec![OsString::from("-u")]);
    }

    fn cmdline_to_string(parts: &[&OsStr]) -> String {
        let v = build_cmdline_w(parts);
        let v = &v[..v.len().saturating_sub(1)]; // strip NUL
        String::from_utf16_lossy(v)
    }

    #[test]
    fn build_cmdline_simple_args() {
        let s = cmdline_to_string(&[OsStr::new("python.exe"), OsStr::new("-X"), OsStr::new("utf8")]);
        assert_eq!(s, "python.exe -X utf8");
    }

    #[test]
    fn build_cmdline_quotes_args_with_spaces() {
        let s = cmdline_to_string(&[OsStr::new("python.exe"), OsStr::new(r"C:\Program Files\app.pyz")]);
        assert_eq!(s, r#"python.exe "C:\Program Files\app.pyz""#);
    }

    #[test]
    fn build_cmdline_escapes_embedded_quotes() {
        let s = cmdline_to_string(&[OsStr::new("python.exe"), OsStr::new(r#"a"b"#)]);
        assert_eq!(s, r#"python.exe "a\"b""#);
    }

    #[test]
    fn build_cmdline_doubles_backslashes_only_before_quotes() {
        // Backslashes not adjacent to a quote are emitted verbatim.
        let s = cmdline_to_string(&[OsStr::new(r"C:\path\to\thing")]);
        assert_eq!(s, r"C:\path\to\thing");
    }

    #[test]
    fn build_cmdline_doubles_trailing_backslashes_before_closing_quote() {
        // An arg that NEEDS quoting and ends with `\` must double its trailing
        // backslashes so the closing `"` is not consumed by `\"` parsing.
        let s = cmdline_to_string(&[OsStr::new(r"a b\")]);
        assert_eq!(s, r#""a b\\""#);
    }

    #[test]
    fn build_cmdline_quotes_empty_arg() {
        let s = cmdline_to_string(&[OsStr::new("python.exe"), OsStr::new("")]);
        assert_eq!(s, r#"python.exe """#);
    }

    // ---------- folder-bundle probe ----------

    #[test]
    fn detect_folder_bundle_returns_some_when_siblings_present() {
        // Layout:
        //   <dir>/foo.exe        (the launcher; doesn't need to be a real PE)
        //   <dir>/foo.pyz
        //   <dir>/_python/python.exe
        let dir = unique_bundle_dir("happy");
        let exe = dir.join("foo.exe");
        touch(&exe);
        touch(&dir.join("foo.pyz"));
        touch(&dir.join("_python").join("python.exe"));

        let bundle = detect_folder_bundle(&exe).expect("bundle should be detected");
        assert!(bundle.python_exe.ends_with("_python\\python.exe")
            || bundle.python_exe.ends_with("_python/python.exe"));
        assert!(bundle.app_pyz.ends_with("foo.pyz"));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn detect_folder_bundle_returns_none_when_python_missing() {
        let dir = unique_bundle_dir("no-python");
        let exe = dir.join("foo.exe");
        touch(&exe);
        touch(&dir.join("foo.pyz"));
        // No _python/ directory at all.
        assert!(detect_folder_bundle(&exe).is_none());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn detect_folder_bundle_returns_none_when_pyz_missing() {
        let dir = unique_bundle_dir("no-pyz");
        let exe = dir.join("foo.exe");
        touch(&exe);
        // Note: missing foo.pyz sibling.
        touch(&dir.join("_python").join("python.exe"));
        assert!(detect_folder_bundle(&exe).is_none());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn detect_folder_bundle_uses_self_stem_for_pyz_name() {
        // A launcher renamed to `BAR.exe` looks for `BAR.pyz`, not `foo.pyz`.
        // Filesystem case sensitivity differs on Windows; here we just check
        // that a mismatched-stem `.pyz` is rejected.
        let dir = unique_bundle_dir("stem");
        let exe = dir.join("bar.exe");
        touch(&exe);
        touch(&dir.join("foo.pyz")); // wrong stem
        touch(&dir.join("_python").join("python.exe"));
        assert!(detect_folder_bundle(&exe).is_none());

        // With the right stem name, it does match.
        touch(&dir.join("bar.pyz"));
        assert!(detect_folder_bundle(&exe).is_some());
        fs::remove_dir_all(&dir).ok();
    }
}
