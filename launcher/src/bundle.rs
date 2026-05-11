//! Bundled-Python launcher path (D21 / D22).
//!
//! When a `.exe` produced by `moonlit build --windows-exe --bundle-python`
//! runs, the central directory of its trailing zip contains entries whose
//! filenames start with `_python/`. This module:
//!
//!   1. Scans the central directory for those entries.
//!   2. Computes a per-bundle SHA-256 **fingerprint** over the sorted
//!      `(arcname, crc32_le)` pairs. The producer (Python-side
//!      `hashing.compute_python_fingerprint`) must agree byte-for-byte with
//!      this value — it's the cross-language contract pinned in
//!      `specs/02-build-pipeline.md` §4a.
//!   3. Extracts the entries to `%LOCALAPPDATA%\moonlit\python\<fingerprint>\`
//!      under a `LockFileEx` per-fingerprint lock with the same parameters as
//!      the Python-side D13 protocol. Idempotent and concurrency-safe.
//!   4. Spawns `<cache>\python.exe -I <self_path> <forwarded args>` with
//!      `MOONLIT_BUNDLED_PYTHON=<fingerprint>` injected into the child env.
//!      The `-I` flag isolates the bundled interpreter from any host-Python
//!      environment leaks; the env var lets the moonlit bootstrap skip the
//!      python-version mismatch check (spec 03 §2 step 4a carve-out).
//!
//! When no `_python/*` entries are found, `detect_bundle` returns `None` and
//! the launcher falls back to its historical shebang path.
//!
//! Stdlib-only (Rust side); no `zip` crate. The only added deps are
//! `miniz_oxide` for deflate and `sha2` for SHA-256.

use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::mem;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::ptr;
use std::time::Instant;

use sha2::{Digest, Sha256};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_LOCK_VIOLATION, GENERIC_READ, GENERIC_WRITE,
    HANDLE, INVALID_HANDLE_VALUE, TRUE,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, LockFileEx, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
    LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, OPEN_ALWAYS,
};
use windows_sys::Win32::System::IO::OVERLAPPED;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, Sleep, WaitForSingleObject,
    CREATE_UNICODE_ENVIRONMENT, INFINITE, PROCESS_INFORMATION, STARTUPINFOW,
};

use crate::build_cmdline_w;

const PYTHON_PREFIX: &[u8] = b"_python/";
const LOCK_TIMEOUT_SECS: u64 = 60;
const LOCK_POLL_MS: u32 = 50;
// Conservative cap on a single decompressed file. python-build-standalone
// rarely ships anything past ~50 MiB; 256 MiB leaves plenty of head room and
// caps maliciously-crafted CRCs that promise tiny compressed sizes but
// inflate to gigabytes.
const INFLATE_LIMIT: usize = 256 * 1024 * 1024;

// ============================================================================
// public entry points (used by main.rs::run)
// ============================================================================

/// If the trailing zip contains any `_python/*` entries, return a `Bundle`
/// describing them; otherwise return `None`. Non-fatal on absence of a valid
/// zip (the launcher's non-bundle path is the historical default and remains
/// the contract for plain .pyz/.exe inputs).
pub fn detect_bundle(file: &mut File) -> Result<Option<Bundle>, String> {
    let entries = match read_central_directory(file) {
        Ok(e) => e,
        Err(_) => return Ok(None),
    };
    let mut python_entries: Vec<ZipEntry> = entries
        .into_iter()
        .filter(|e| e.filename.starts_with(PYTHON_PREFIX))
        .collect();
    if python_entries.is_empty() {
        return Ok(None);
    }
    python_entries.sort_by(|a, b| a.filename.cmp(&b.filename));
    let fingerprint = compute_fingerprint(&python_entries);
    Ok(Some(Bundle {
        entries: python_entries,
        fingerprint,
    }))
}

/// Resolve `%LOCALAPPDATA%\moonlit\python\<fingerprint>\` for the given
/// fingerprint. Errors when `LOCALAPPDATA` is unset.
pub fn bundled_cache_dir(fingerprint: &str) -> Result<PathBuf, String> {
    let local = std::env::var_os("LOCALAPPDATA")
        .ok_or_else(|| String::from("LOCALAPPDATA is not set; bundled Python cache unreachable"))?;
    let mut p = PathBuf::from(local);
    p.push("moonlit");
    p.push("python");
    p.push(fingerprint);
    Ok(p)
}

/// Idempotent first-run extract. Double-checked under a per-fingerprint lock
/// (mirrors `_bootstrap/locking.py` D13 plus D14 fast-path semantics).
pub fn ensure_extracted(file: &mut File, bundle: &Bundle, cache: &Path) -> Result<(), String> {
    // Fast path (no lock): if python.exe is already there, extraction is done.
    if cache.join("python.exe").is_file() {
        return Ok(());
    }
    let parent = cache
        .parent()
        .ok_or_else(|| format!("cache path has no parent: {}", cache.display()))?;
    fs::create_dir_all(parent)
        .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;

    let lock_name = format!("{}.lock", bundle.fingerprint);
    let lock_path = parent.join(&lock_name);
    let _lock = acquire_lock(&lock_path)?;

    // Re-check under the lock (D14 slow path).
    if cache.join("python.exe").is_file() {
        return Ok(());
    }

    let pid = std::process::id();
    let tmp = parent.join(format!("{}.tmp.{}", bundle.fingerprint, pid));
    // Clean any stale tmp dir from a crashed previous run.
    if tmp.exists() {
        let _ = fs::remove_dir_all(&tmp);
    }
    fs::create_dir_all(&tmp)
        .map_err(|e| format!("cannot create {}: {e}", tmp.display()))?;

    let extract_result = extract_entries(file, &bundle.entries, &tmp);
    if let Err(e) = extract_result {
        // Best-effort cleanup so a half-extracted tmp doesn't pollute the cache.
        let _ = fs::remove_dir_all(&tmp);
        return Err(e);
    }

    // Swap into place. If the target already exists (race we lost, or a
    // partial install), rename it aside first.
    if cache.exists() {
        let old = parent.join(format!("{}.old.{}", bundle.fingerprint, pid));
        let _ = fs::rename(cache, &old);
    }
    fs::rename(&tmp, cache).map_err(|e| {
        format!("cannot publish bundled Python at {}: {e}", cache.display())
    })?;
    Ok(())
}

/// Spawn `<cache>\python.exe -I <self_path> <forwarded args>` with
/// `MOONLIT_BUNDLED_PYTHON=<fingerprint>` set in the child env. Returns the
/// child's exit code.
pub fn spawn_bundled_python(
    cache: &Path,
    fingerprint: &str,
    self_path: &Path,
) -> Result<u32, String> {
    let python_exe = cache.join("python.exe");
    if !python_exe.is_file() {
        return Err(format!(
            "bundled python.exe not found after extract: {}",
            python_exe.display()
        ));
    }

    let our_args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let mut parts: Vec<OsString> = Vec::with_capacity(3 + our_args.len());
    parts.push(python_exe.into_os_string());
    parts.push(OsString::from("-I"));
    parts.push(self_path.as_os_str().to_os_string());
    parts.extend(our_args);
    let parts_refs: Vec<&OsStr> = parts.iter().map(|s| s.as_os_str()).collect();
    let mut cmdline_w = build_cmdline_w(&parts_refs);

    let env_block_w = build_env_block_w("MOONLIT_BUNDLED_PYTHON", fingerprint);

    let mut si: STARTUPINFOW = unsafe { mem::zeroed() };
    si.cb = mem::size_of::<STARTUPINFOW>() as u32;
    let mut pi: PROCESS_INFORMATION = unsafe { mem::zeroed() };

    let ok = unsafe {
        CreateProcessW(
            ptr::null(),
            cmdline_w.as_mut_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            TRUE,
            CREATE_UNICODE_ENVIRONMENT,
            env_block_w.as_ptr() as *const _,
            ptr::null(),
            &si,
            &mut pi,
        )
    };
    if ok == 0 {
        let err = unsafe { GetLastError() };
        return Err(format!("CreateProcessW for bundled python failed: {err}"));
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

// ============================================================================
// types
// ============================================================================

pub struct Bundle {
    pub entries: Vec<ZipEntry>,
    pub fingerprint: String,
}

#[derive(Clone)]
pub struct ZipEntry {
    pub filename: Vec<u8>,         // UTF-8 bytes from the central directory.
    pub local_header_offset: u64,  // absolute file offset of the LFH.
    pub compressed_size: u64,
    /// Recorded uncompressed size from the central directory. Not consulted
    /// during extraction (we cap inflated output at `INFLATE_LIMIT` instead),
    /// but parsed for completeness in case a future feature needs it.
    #[allow(dead_code)]
    pub uncompressed_size: u64,
    pub compression_method: u16,   // 0=stored, 8=deflate.
    pub crc32: u32,
}

// ============================================================================
// zip central-directory walk (no external `zip` crate)
// ============================================================================

const EOCD_SIGNATURE: &[u8; 4] = b"PK\x05\x06";
const CD_FILE_HEADER_SIGNATURE: &[u8; 4] = b"PK\x01\x02";
const ZIP64_EOCD_LOCATOR_SIGNATURE: &[u8; 4] = b"PK\x06\x07";
const ZIP64_EOCD_SIGNATURE: &[u8; 4] = b"PK\x06\x06";
// Maximum size of the EOCD record plus its comment; the EOCD must lie within
// the last 22 + 0xFFFF = 65557 bytes of any conformant zip.
const EOCD_SEARCH_CAP: u64 = 65557;

/// Walk the central directory and return one ZipEntry per file. Tolerates
/// zip64 if the cd-offset/count are zip64-encoded.
fn read_central_directory(file: &mut File) -> Result<Vec<ZipEntry>, String> {
    let (cd_offset, cd_size, expected_count) = find_central_directory_extent(file)?;

    file.seek(SeekFrom::Start(cd_offset))
        .map_err(io_err)?;
    let mut cd = vec![0u8; cd_size as usize];
    file.read_exact(&mut cd).map_err(io_err)?;

    let mut entries: Vec<ZipEntry> = Vec::with_capacity(expected_count);
    let mut cursor = 0usize;
    while cursor + 46 <= cd.len() {
        if &cd[cursor..cursor + 4] != CD_FILE_HEADER_SIGNATURE {
            break;
        }
        let compression_method = u16_le(&cd[cursor + 10..cursor + 12]);
        let crc32 = u32_le(&cd[cursor + 16..cursor + 20]);
        let compressed_size_field = u32_le(&cd[cursor + 20..cursor + 24]);
        let uncompressed_size_field = u32_le(&cd[cursor + 24..cursor + 28]);
        let name_len = u16_le(&cd[cursor + 28..cursor + 30]) as usize;
        let extra_len = u16_le(&cd[cursor + 30..cursor + 32]) as usize;
        let comment_len = u16_le(&cd[cursor + 32..cursor + 34]) as usize;
        let lfh_offset_field = u32_le(&cd[cursor + 42..cursor + 46]);

        let name_start = cursor + 46;
        let name_end = name_start + name_len;
        let extra_end = name_end + extra_len;
        let comment_end = extra_end + comment_len;
        if comment_end > cd.len() {
            return Err("truncated central directory".into());
        }
        let filename = cd[name_start..name_end].to_vec();

        // Zip64 extras: if any of compressed/uncompressed/offset is 0xFFFFFFFF,
        // the real value lives in a Zip64 extra block (tag 0x0001) in the
        // extra field. Pull them out if present.
        let (compressed_size, uncompressed_size, lfh_offset) = resolve_zip64(
            &cd[name_end..extra_end],
            compressed_size_field,
            uncompressed_size_field,
            lfh_offset_field,
        )?;

        entries.push(ZipEntry {
            filename,
            local_header_offset: lfh_offset,
            compressed_size,
            uncompressed_size,
            compression_method,
            crc32,
        });
        cursor = comment_end;
    }
    Ok(entries)
}

fn find_central_directory_extent(file: &mut File) -> Result<(u64, u64, usize), String> {
    let file_size = file.seek(SeekFrom::End(0)).map_err(io_err)?;
    let search_start = file_size.saturating_sub(EOCD_SEARCH_CAP).max(0);
    let search_len = file_size - search_start;
    file.seek(SeekFrom::Start(search_start)).map_err(io_err)?;
    let mut tail = vec![0u8; search_len as usize];
    file.read_exact(&mut tail).map_err(io_err)?;

    // Scan backwards for the EOCD signature.
    let mut eocd_pos: Option<usize> = None;
    if tail.len() >= 22 {
        for i in (0..=tail.len() - 22).rev() {
            if &tail[i..i + 4] == EOCD_SIGNATURE {
                eocd_pos = Some(i);
                break;
            }
        }
    }
    let eocd = eocd_pos.ok_or_else(|| String::from("EOCD signature not found"))?;
    let eocd_bytes = &tail[eocd..eocd + 22];
    let count_in_eocd = u16_le(&eocd_bytes[10..12]) as usize;
    let cd_size_field = u32_le(&eocd_bytes[12..16]);
    let cd_offset_field = u32_le(&eocd_bytes[16..20]);

    if count_in_eocd != 0xFFFF
        && cd_size_field != 0xFFFFFFFF
        && cd_offset_field != 0xFFFFFFFF
    {
        return Ok((cd_offset_field as u64, cd_size_field as u64, count_in_eocd));
    }

    // Zip64 path: locate the Zip64 EOCD locator (20 bytes) just before the
    // regular EOCD, read the absolute offset of the Zip64 EOCD record, and
    // pull cd offset/size/count from there.
    if eocd < 20 {
        return Err("zip64 indicator set but no locator room".into());
    }
    let locator = &tail[eocd - 20..eocd];
    if &locator[0..4] != ZIP64_EOCD_LOCATOR_SIGNATURE {
        return Err("zip64 EOCD locator signature missing".into());
    }
    let zip64_eocd_offset = u64_le(&locator[8..16]);
    file.seek(SeekFrom::Start(zip64_eocd_offset)).map_err(io_err)?;
    let mut zip64_eocd = [0u8; 56];
    file.read_exact(&mut zip64_eocd).map_err(io_err)?;
    if &zip64_eocd[0..4] != ZIP64_EOCD_SIGNATURE {
        return Err("zip64 EOCD signature missing".into());
    }
    let zip64_count = u64_le(&zip64_eocd[32..40]) as usize;
    let zip64_cd_size = u64_le(&zip64_eocd[40..48]);
    let zip64_cd_offset = u64_le(&zip64_eocd[48..56]);
    Ok((zip64_cd_offset, zip64_cd_size, zip64_count))
}

fn resolve_zip64(
    extras: &[u8],
    compressed_field: u32,
    uncompressed_field: u32,
    lfh_offset_field: u32,
) -> Result<(u64, u64, u64), String> {
    let mut compressed = compressed_field as u64;
    let mut uncompressed = uncompressed_field as u64;
    let mut lfh_offset = lfh_offset_field as u64;
    let zip64_needed = compressed_field == 0xFFFFFFFF
        || uncompressed_field == 0xFFFFFFFF
        || lfh_offset_field == 0xFFFFFFFF;
    if !zip64_needed {
        return Ok((compressed, uncompressed, lfh_offset));
    }
    let mut cursor = 0;
    while cursor + 4 <= extras.len() {
        let tag = u16_le(&extras[cursor..cursor + 2]);
        let size = u16_le(&extras[cursor + 2..cursor + 4]) as usize;
        let payload_start = cursor + 4;
        let payload_end = payload_start + size;
        if payload_end > extras.len() {
            return Err("truncated extra field".into());
        }
        if tag == 0x0001 {
            // Zip64 fields are present in the order: uncompressed, compressed,
            // local-header-offset, disk-number-start — but only for those that
            // were 0xFFFFFFFF in the regular CD record. Order follows the same
            // sequence.
            let payload = &extras[payload_start..payload_end];
            let mut sub = 0;
            if uncompressed_field == 0xFFFFFFFF {
                uncompressed = u64_le(&payload[sub..sub + 8]);
                sub += 8;
            }
            if compressed_field == 0xFFFFFFFF {
                compressed = u64_le(&payload[sub..sub + 8]);
                sub += 8;
            }
            if lfh_offset_field == 0xFFFFFFFF {
                lfh_offset = u64_le(&payload[sub..sub + 8]);
            }
            break;
        }
        cursor = payload_end;
    }
    Ok((compressed, uncompressed, lfh_offset))
}

// ============================================================================
// fingerprint (D21/D22 cross-language contract)
// ============================================================================

fn compute_fingerprint(entries: &[ZipEntry]) -> String {
    let mut hasher = Sha256::new();
    for entry in entries {
        hasher.update(&entry.filename);
        hasher.update([0u8]);
        hasher.update(entry.crc32.to_le_bytes());
        hasher.update([0u8]);
    }
    hex_lower(&hasher.finalize())
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0F) as usize] as char);
    }
    out
}

// ============================================================================
// extract: read each entry's local file header, decompress, write to disk
// ============================================================================

fn extract_entries(file: &mut File, entries: &[ZipEntry], dest: &Path) -> Result<(), String> {
    for entry in entries {
        let rel_bytes = &entry.filename[PYTHON_PREFIX.len()..];
        if rel_bytes.is_empty() {
            continue;
        }
        let rel = std::str::from_utf8(rel_bytes)
            .map_err(|e| format!("invalid UTF-8 in arcname: {e}"))?;
        if rel.split('/').any(|seg| seg == ".." || seg.is_empty()) {
            return Err(format!("rejected unsafe arcname: {rel}"));
        }
        let out_path = join_relative_posix(dest, rel);
        if let Some(parent) = out_path.parent() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
        }
        let data = read_entry_data(file, entry)?;
        write_atomic(&out_path, &data)?;
    }
    Ok(())
}

fn join_relative_posix(dest: &Path, rel: &str) -> PathBuf {
    let mut p = dest.to_path_buf();
    for seg in rel.split('/') {
        p.push(seg);
    }
    p
}

fn read_entry_data(file: &mut File, entry: &ZipEntry) -> Result<Vec<u8>, String> {
    file.seek(SeekFrom::Start(entry.local_header_offset))
        .map_err(io_err)?;
    let mut header = [0u8; 30];
    file.read_exact(&mut header).map_err(io_err)?;
    if &header[0..4] != b"PK\x03\x04" {
        return Err(format!(
            "bad local file header at offset {}",
            entry.local_header_offset
        ));
    }
    let name_len = u16_le(&header[26..28]) as u64;
    let extra_len = u16_le(&header[28..30]) as u64;
    let data_offset = entry.local_header_offset + 30 + name_len + extra_len;
    file.seek(SeekFrom::Start(data_offset)).map_err(io_err)?;

    let mut compressed = vec![0u8; entry.compressed_size as usize];
    file.read_exact(&mut compressed).map_err(io_err)?;

    match entry.compression_method {
        0 => Ok(compressed),
        8 => miniz_oxide::inflate::decompress_to_vec_with_limit(&compressed, INFLATE_LIMIT)
            .map_err(|e| format!("inflate failed: {:?}", e)),
        m => Err(format!("unsupported compression method: {m}")),
    }
}

fn write_atomic(path: &Path, data: &[u8]) -> Result<(), String> {
    // Per-file write: data is fully buffered (Python files are small), open
    // the destination directly, write, fsync. The directory-level swap is
    // what makes the *whole tree* atomic; per-file atomicity is overkill for
    // contents that will only be observed once the tree is renamed in.
    let mut f = File::create(path)
        .map_err(|e| format!("cannot create {}: {e}", path.display()))?;
    f.write_all(data)
        .map_err(|e| format!("write {}: {e}", path.display()))?;
    // Best-effort sync; do not fail extraction if the FS rejects it.
    let _ = f.sync_data();
    Ok(())
}

// ============================================================================
// lock (Win32 LockFileEx, mirrors D13 parameters)
// ============================================================================

struct LockGuard {
    handle: HANDLE,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        if self.handle != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

fn acquire_lock(lock_path: &Path) -> Result<LockGuard, String> {
    let wide: Vec<u16> = lock_path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            ptr::null(),
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        let err = unsafe { GetLastError() };
        return Err(format!(
            "CreateFileW({}) for lock failed: {err}",
            lock_path.display()
        ));
    }
    let started = Instant::now();
    loop {
        let mut overlapped: OVERLAPPED = unsafe { mem::zeroed() };
        let ok = unsafe {
            LockFileEx(
                handle,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if ok != 0 {
            return Ok(LockGuard { handle });
        }
        let err = unsafe { GetLastError() };
        // ERROR_LOCK_VIOLATION (33) is the contended-but-retryable case.
        if err != ERROR_LOCK_VIOLATION {
            unsafe {
                CloseHandle(handle);
            }
            return Err(format!("LockFileEx failed: {err}"));
        }
        if started.elapsed().as_secs() >= LOCK_TIMEOUT_SECS {
            unsafe {
                CloseHandle(handle);
            }
            return Err(format!(
                "lock acquisition timed out ({LOCK_TIMEOUT_SECS}s) at {}",
                lock_path.display()
            ));
        }
        unsafe { Sleep(LOCK_POLL_MS) };
    }
}

// ============================================================================
// child-process env block (UTF-16, double-NUL terminated)
// ============================================================================

fn build_env_block_w(extra_name: &str, extra_value: &str) -> Vec<u16> {
    // Copy the parent's env, then override / append `extra_name=extra_value`.
    let mut entries: Vec<(OsString, OsString)> = std::env::vars_os().collect();
    entries.retain(|(k, _)| {
        // Case-insensitive comparison: Windows env-var names are
        // case-insensitive (PATH == path == Path), so we drop any pre-existing
        // entry that collides regardless of casing before pushing ours.
        !os_str_eq_ignore_case(k.as_os_str(), OsStr::new(extra_name))
    });
    entries.push((OsString::from(extra_name), OsString::from(extra_value)));

    let mut block: Vec<u16> = Vec::new();
    for (key, value) in entries {
        for c in key.encode_wide() {
            block.push(c);
        }
        block.push(b'=' as u16);
        for c in value.encode_wide() {
            block.push(c);
        }
        block.push(0);
    }
    // Final NUL — required by CreateProcessW for a UTF-16 env block.
    block.push(0);
    block
}

fn os_str_eq_ignore_case(a: &OsStr, b: &OsStr) -> bool {
    let av: Vec<u16> = a.encode_wide().map(ascii_to_lower_u16).collect();
    let bv: Vec<u16> = b.encode_wide().map(ascii_to_lower_u16).collect();
    av == bv
}

fn ascii_to_lower_u16(c: u16) -> u16 {
    if c >= b'A' as u16 && c <= b'Z' as u16 {
        c + 0x20
    } else {
        c
    }
}

// ============================================================================
// helpers (le ints / io errors)
// ============================================================================

fn u16_le(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}

fn u32_le(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

fn u64_le(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

fn io_err<E: std::fmt::Display>(e: E) -> String {
    format!("io: {e}")
}

// ============================================================================
// tests (pure functions only — extraction/lock are covered by E2E)
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// Standard CRC-32 (IEEE 802.3) — same polynomial as Python's `zlib.crc32`.
    /// Used to compute correct CRCs in hand-built test zips so the bundle
    /// walker accepts them.
    fn crc32(data: &[u8]) -> u32 {
        let mut table = [0u32; 256];
        for i in 0..256u32 {
            let mut c = i;
            for _ in 0..8 {
                if c & 1 != 0 {
                    c = 0xEDB88320 ^ (c >> 1);
                } else {
                    c >>= 1;
                }
            }
            table[i as usize] = c;
        }
        let mut crc: u32 = 0xFFFFFFFF;
        for b in data {
            crc = table[((crc ^ *b as u32) & 0xFF) as usize] ^ (crc >> 8);
        }
        crc ^ 0xFFFFFFFF
    }

    /// Build a minimal valid PK\x05\x06 EOCD-trailed zip with the given entries,
    /// each stored uncompressed. Returns the byte buffer.
    fn make_stored_zip(entries: &[(&[u8], &[u8])]) -> Vec<u8> {
        let mut out: Vec<u8> = Vec::new();
        let mut local_offsets: Vec<u32> = Vec::new();

        for (name, data) in entries {
            local_offsets.push(out.len() as u32);
            // Local file header.
            out.extend_from_slice(b"PK\x03\x04");
            out.extend_from_slice(&20u16.to_le_bytes()); // version needed
            out.extend_from_slice(&0u16.to_le_bytes()); // gp flags
            out.extend_from_slice(&0u16.to_le_bytes()); // method = stored
            out.extend_from_slice(&0u16.to_le_bytes()); // mod time
            out.extend_from_slice(&0u16.to_le_bytes()); // mod date
            let crc = crc32(data);
            out.extend_from_slice(&crc.to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes()); // compressed
            out.extend_from_slice(&(data.len() as u32).to_le_bytes()); // uncompressed
            out.extend_from_slice(&(name.len() as u16).to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes()); // extra len
            out.extend_from_slice(name);
            out.extend_from_slice(data);
        }
        let cd_offset = out.len() as u32;
        for ((name, data), lfh_offset) in entries.iter().zip(local_offsets.iter()) {
            // Central directory file header.
            out.extend_from_slice(b"PK\x01\x02");
            out.extend_from_slice(&20u16.to_le_bytes()); // version made by
            out.extend_from_slice(&20u16.to_le_bytes()); // version needed
            out.extend_from_slice(&0u16.to_le_bytes()); // gp flags
            out.extend_from_slice(&0u16.to_le_bytes()); // method
            out.extend_from_slice(&0u16.to_le_bytes()); // mod time
            out.extend_from_slice(&0u16.to_le_bytes()); // mod date
            let crc = crc32(data);
            out.extend_from_slice(&crc.to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
            out.extend_from_slice(&(name.len() as u16).to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes()); // extra len
            out.extend_from_slice(&0u16.to_le_bytes()); // comment len
            out.extend_from_slice(&0u16.to_le_bytes()); // disk number
            out.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
            out.extend_from_slice(&0u32.to_le_bytes()); // external attrs
            out.extend_from_slice(&lfh_offset.to_le_bytes());
            out.extend_from_slice(name);
        }
        let cd_size = out.len() as u32 - cd_offset;
        // EOCD.
        out.extend_from_slice(b"PK\x05\x06");
        out.extend_from_slice(&0u16.to_le_bytes()); // disk number
        out.extend_from_slice(&0u16.to_le_bytes()); // cd start disk
        out.extend_from_slice(&(entries.len() as u16).to_le_bytes()); // entries on disk
        out.extend_from_slice(&(entries.len() as u16).to_le_bytes()); // total entries
        out.extend_from_slice(&cd_size.to_le_bytes());
        out.extend_from_slice(&cd_offset.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // comment len
        out
    }

    /// Write `bytes` to a unique temp file and return an open File over it.
    fn make_tempfile(bytes: &[u8]) -> File {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let id = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "moonlit-bundle-test-{}-{}.bin",
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

    #[test]
    fn detect_bundle_returns_none_when_no_python_entries() {
        let zip = make_stored_zip(&[
            (b"site-packages/x.py", b"data1"),
            (b"__main__.py", b"data2"),
        ]);
        let mut f = make_tempfile(&zip);
        let bundle = detect_bundle(&mut f).unwrap();
        assert!(bundle.is_none(), "non-bundle zip detected as bundle");
    }

    #[test]
    fn detect_bundle_finds_python_prefix_entries() {
        let zip = make_stored_zip(&[
            (b"site-packages/x.py", b"data1"),
            (b"_python/python.exe", b"fake exe"),
            (b"_python/Lib/site.py", b"# site\n"),
            (b"__main__.py", b"data2"),
        ]);
        let mut f = make_tempfile(&zip);
        let bundle = detect_bundle(&mut f).unwrap().expect("bundle expected");
        assert_eq!(bundle.entries.len(), 2);
        // Sorted lex on UTF-8 bytes:
        assert_eq!(bundle.entries[0].filename, b"_python/Lib/site.py");
        assert_eq!(bundle.entries[1].filename, b"_python/python.exe");
        assert_eq!(bundle.fingerprint.len(), 64);
        for c in bundle.fingerprint.chars() {
            assert!(c.is_ascii_hexdigit() && !c.is_ascii_uppercase());
        }
    }

    #[test]
    fn fingerprint_matches_python_reference_value() {
        // Inputs match test_compute_python_fingerprint_matches_zlib_crc32_recipe
        // in tests/unit/test_builder_bundle_python.py. The expected hex below
        // was computed externally with Python's `hashlib.sha256` + `zlib.crc32`
        // and pinned here so a divergence between this Rust path and the
        // Python producer fails the test directly — the cross-language
        // contract (D21/D22).
        let zip = make_stored_zip(&[
            (b"_python/a.txt", b"hello\n"),
            (b"_python/sub/b.bin", b"\x00\x01\x02\x03"),
        ]);
        let mut f = make_tempfile(&zip);
        let bundle = detect_bundle(&mut f).unwrap().unwrap();
        assert_eq!(
            bundle.fingerprint,
            "1f313bf96a9f469b129669902a707607d5823d82b9f57a0409f4b58460397302"
        );
    }

    #[test]
    fn hex_lower_produces_64_chars_for_sha256() {
        let mut h = Sha256::new();
        h.update(b"abc");
        let out = hex_lower(&h.finalize());
        assert_eq!(out.len(), 64);
        // Known SHA-256 of "abc".
        assert_eq!(
            out,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn read_central_directory_handles_multiple_entries_with_correct_offsets() {
        let zip = make_stored_zip(&[
            (b"a.txt", b"AAA"),
            (b"_python/b.bin", b"BBBB"),
            (b"_python/c.py", b"CCCCC"),
        ]);
        let mut f = make_tempfile(&zip);
        let entries = read_central_directory(&mut f).unwrap();
        assert_eq!(entries.len(), 3);
        // Local-header offsets must monotonically increase in the zip we built.
        let offs: Vec<u64> = entries.iter().map(|e| e.local_header_offset).collect();
        assert!(offs.windows(2).all(|w| w[0] < w[1]));
    }

    #[test]
    fn missing_eocd_returns_err_but_detect_bundle_returns_none() {
        let mut f = make_tempfile(&[0u8; 200]);
        assert!(read_central_directory(&mut f).is_err());
        // detect_bundle softens the error: a malformed/short zip is treated
        // as "not a bundle" so plain .pyz inputs still hit the shebang path.
        let mut f2 = make_tempfile(&[0u8; 200]);
        assert!(detect_bundle(&mut f2).unwrap().is_none());
    }

    #[test]
    fn extract_entries_writes_files_with_correct_content() {
        let zip = make_stored_zip(&[
            (b"_python/a.txt", b"hello world"),
            (b"_python/Lib/x.py", b"# inner\n"),
        ]);
        let mut f = make_tempfile(&zip);
        let bundle = detect_bundle(&mut f).unwrap().unwrap();
        let dest = std::env::temp_dir().join(format!(
            "moonlit-extract-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dest);
        fs::create_dir_all(&dest).unwrap();
        extract_entries(&mut f, &bundle.entries, &dest).unwrap();
        let a = fs::read(dest.join("a.txt")).unwrap();
        let x = fs::read(dest.join("Lib").join("x.py")).unwrap();
        assert_eq!(a, b"hello world");
        assert_eq!(x, b"# inner\n");
        fs::remove_dir_all(&dest).unwrap();
    }

    #[test]
    fn extract_rejects_dotdot_segments() {
        // Hand-craft a CD entry with `..` in the name to confirm the
        // zip-slip guard. We call extract_entries directly with a forged
        // ZipEntry; the underlying zip body need not be readable because the
        // guard fires before any local-header read.
        let fake_entry = ZipEntry {
            filename: b"_python/../escape.txt".to_vec(),
            local_header_offset: 0,
            compressed_size: 0,
            uncompressed_size: 0,
            compression_method: 0,
            crc32: 0,
        };
        let dest = std::env::temp_dir().join(format!(
            "moonlit-zipslip-test-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dest).ok();
        let mut f = make_tempfile(&[0u8; 30]);
        let err = extract_entries(&mut f, &[fake_entry], &dest).unwrap_err();
        assert!(err.contains("unsafe arcname"), "unexpected: {err}");
        let _ = fs::remove_dir_all(&dest);
    }

    #[test]
    fn env_block_overrides_existing_value_case_insensitively() {
        // Sanity: the produced block contains MOONLIT_BUNDLED_PYTHON=<value>
        // and no duplicate key under any casing.
        std::env::set_var("MOONLIT_BUNDLED_PYTHON", "OLD_VALUE");
        let block = build_env_block_w("MOONLIT_BUNDLED_PYTHON", "NEW_VALUE");
        let decoded = String::from_utf16_lossy(&block);
        let nul_terminated: Vec<&str> = decoded.split('\0').collect();
        let matches: Vec<&&str> = nul_terminated
            .iter()
            .filter(|s| s.to_ascii_lowercase().starts_with("moonlit_bundled_python="))
            .collect();
        assert_eq!(matches.len(), 1, "got {matches:?}");
        assert!(matches[0].ends_with("=NEW_VALUE"));
        std::env::remove_var("MOONLIT_BUNDLED_PYTHON");
    }
}

