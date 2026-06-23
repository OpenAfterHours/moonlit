# Plan: runtime cache self-GC (decision **D24**)

Status: **IMPLEMENTED** on branch `feat/runtime-cache-gc` (Phases 0–8 complete; 781 tests
green, ruff clean). Remaining: Phase 9 (minor-version bump + release note) is a release
decision left to the maintainer — the version in `pyproject.toml` is intentionally NOT bumped
yet. The locked decisions below were confirmed by the maintainer; specs under `specs/` now
carry the D24 contract.

## Problem

A produced `.pyz` / single-file `.exe` / `--bundle-python` folder extracts its bundled
`site-packages` to `<cache_root>/<cache_key>/site-packages/` on first run, where
`cache_key = f"{pep503(name)}_{build_id}"`. **Every rebuild changes the content → new
`build_id` → a brand-new cache dir** (often tens–hundreds of MB). Today these are immortal:

- `specs/04-cache-layout.md` §6: *"Never deleted automatically; manual reaping is the user's job."*
- `extract._sweep_old_siblings` only reaps `.old.<pid>` / `.tmp.<pid>` siblings of **its own**
  `cache_key` — never sibling cache dirs of other `build_id`s (verified).

The decisive constraint: **recipients of a `.pyz`/`.exe` usually do not have moonlit
installed**, so the build-time `moonlit clean` CLI cannot help them. The only moonlit code
that ships inside every artifact and runs on the recipient machine is the **stdlib-only
bootstrap** (`src/moonlit/_bootstrap/`, gated by `test_bootstrap_stdlib_only.py`, D7). The
reaper must live there.

## Locked decisions

| Knob | Value | Build flag | env.json | Runtime override |
|---|---|---|---|---|
| Enabled by default | **yes** | `--gc` / `--no-gc` | `gc.enabled` | `MOONLIT_NO_GC` |
| Keep latest N | **2** (current + 1 predecessor) | `--gc-keep-latest N` | `gc.keep_latest` | `MOONLIT_GC_KEEP_LATEST` |
| Age grace | **86400 s (24h)** | `--gc-grace <dur>` | `gc.grace_seconds` | `MOONLIT_GC_GRACE` |
| Control point | **env.json-carried + env-var override** | — | new `gc` object | — |
| Reap on `MOONLIT_FORCE_EXTRACT` | **yes** (one consistent slow-path rule) | — | — | — |

`keep_latest` floor is 1 (`--gc-keep-latest 1` gives the literal "leave only the most recent").

## Mechanism

New stdlib-only module `src/moonlit/_bootstrap/reap.py` (stepdown: `reap(...)` at top,
helpers below in call order). It **mirrors but never imports** `clean.py` (D7).

### Trigger — slow path only
Called from `extract.materialize`, immediately after `_sweep_old_siblings(site_parent)`
(currently `extract.py:64`) and **still inside the `with lock(lock_path):` block**. A warm
cache hit returns at `extract.py:44-45` *before the lock is ever acquired* → constructs no
reaper, scans nothing, emits nothing. **D14 warm fast path and §14 silent-on-success stay
byte-for-byte unchanged** (verified). Runs on cold first run and on `MOONLIT_FORCE_EXTRACT`.

### Selection — same-app keep-latest-N
1. `cache_root.iterdir()` once.
2. Classify each name (stdlib port of `clean.py._classify`/`_parse_cache_key`: split on last
   `_`, tail must `fullmatch(^[0-9a-f]{64}$)`, head non-empty). UNKNOWN names (e.g. future
   `v2/`, foreign dirs) left strictly alone.
3. Candidate set = `CACHE_ENTRY` dirs whose normalized name equals this app's
   (`re.sub(r"[-_.]+","-",env.name).lower()`, identical to `extract._cache_key`).
   **Cross-app caches are never touched.** Unambiguous: PEP 503 collapses every `_` to `-`,
   so the lone `_` in a well-formed key is the separator (verified).
4. Explicitly exclude the just-installed `cache_key` (also newest-mtime by construction →
   self-deletion structurally impossible; belt + suspenders).
5. Sort same-app group by `<key>/site-packages` **dir** `st_mtime` desc (stable against
   `.pyc` writes; fall back to key-dir mtime, then `0.0` — matches `clean._entry_mtime`).
   Keep newest `keep_latest`; the rest are deletable.
6. **Age grace:** drop any deletable whose site-packages mtime is newer than `now - grace`.
7. Orphan `.tmp`/`.old`/`.lock` reaping stays with `_sweep_old_siblings` (this key) and
   `moonlit clean` (cross-app). Auto-GC does **not** expand into orphan reaping.

### Safety — per-victim cooperative try-lock, best-effort
- For each deletable victim: `locking.try_acquire_nonblocking(<victim>.lock)`; on `None`
  (a live slow-path writer for that exact key) **skip**; on success `shutil.rmtree` the victim
  **while holding its lock**, then `release()` and `unlink(missing_ok=True)` the now-orphaned
  lock. Identical contract to `clean._delete_cache_entry` / D23. **No `--force` analogue** —
  an automatic GC has no human asserting quiescence.
- **The whole pass is wrapped in `try/except Exception` and swallowed.** A reap failure must
  never change the exit code or block `runner.run`. Per-victim `OSError` is caught, that
  victim is left intact, the pass continues. Diagnostics only under `MOONLIT_DEBUG`
  (`moonlit: pruned <key>` / `moonlit: skipped <key> (locked)`, stderr only).

### Coverage — one mechanism, all three shapes (verified)
- **Plain `.pyz`** — bootstrap reaps on slow-path extraction.
- **Single-file `--windows-exe`** — launcher prepends to the same zip body → same bootstrap.
- **`--bundle-python` folder** — launcher spawns `_python/python.exe -I <stem>.pyz`; the inner
  `.pyz` carries a byte-identical env.json (D21h) and still extracts app deps to the cache.
  The `_python/` tree lives in the bundle folder, not the cache, and is out of GC scope.

## env.json shape

Add ONE v1-optional top-level object (D9 graduation — **no `schema_version` bump**; v1
consumers already ignore unknown fields). env.json is written *after* `compute_build_id`
(builder.py:317 vs :306), so **env.json remains excluded from `build_id`** — cache keys,
build-id determinism, and I11/I11b bundle parity all preserved (verified).

```json
"gc": { "enabled": true, "keep_latest": 2, "grace_seconds": 86400 }
```

- **Producer** (`builder._build_env_dict`): always emit the `gc` object from new
  `BuildConfig.gc_*` fields, so env.json byte-shape stays stable. D21h preserved (identical
  bundle vs non-bundle).
- **Consumer** (`environment.py`): add `gc: dict | None = None` as the **last** dataclass
  field (after `python_version`) + `_read_optional_gc(parsed)` mirroring
  `_read_optional_python_version`: absent → `None` (bootstrap applies built-in defaults
  `enabled=True, keep_latest=2, grace_seconds=86400`); present → validate `enabled` is bool,
  `keep_latest` int (not bool) ≥ 1, `grace_seconds` int (not bool) ≥ 0; first violation raises
  `EnvJsonError`. Surfaced by `moonlit info`.
- **Runtime overrides:** `MOONLIT_NO_GC` (truthy disables regardless of env.json);
  `MOONLIT_GC_KEEP_LATEST` / `MOONLIT_GC_GRACE` (int; malformed value falls back to env.json,
  never errors — the bootstrap must not fail on a bad knob).

## Honest residual risk (document, do not hide)

A live **D14 fast-path reader of an older same-app build holds no lock** and is invisible to
the per-victim try-lock. If that build is older than the grace window, GC can delete its
`site-packages` out from under it — the same "undefined behavior" `specs/04 §13.3` already
names for `moonlit clean`, now triggered **automatically** rather than by a human.

This is **bounded, not eliminated**, by: keep-latest ≥ 2 + 24h grace + same-app scope +
`MOONLIT_NO_GC`. Corrections from adversarial verification:
- **Windows is NOT a reliable backstop.** A pure-Python reader holds no file handle, so its
  idle site-packages can be `rmtree`'d on Windows too. Safety rests *only* on
  try-lock + keep-latest + grace + same-app scope; platform `rmtree` behavior is incidental.
- **A shared reader-lock IS feasible** (`msvcrt.LK_NBRLCK`, `fcntl.flock(LOCK_SH)`) and would
  fully close the hazard — but it adds a lock acquire to *every warm run*, breaking the
  "never on the fast path / zero extra work" contract. **Deliberately out of scope**; recorded
  as a future hardening option.

Other residuals: stdlib classifier in `reap.py` duplicates `clean.py`'s (guarded by a parity
test); mtime is a fragile recency signal under backup/rsync/clock-skew (shared with
`clean.py`); first-run latency gains an `iterdir` + stats + try-locks + a possibly-large
rmtree under the lock (bounded; runs after the app's own dir is installed); silent best-effort
reclaim (recipient sees no freed disk on a swallowed error — `MOONLIT_DEBUG` only); shared
`MOONLIT_ROOT` across users could cross-reap (already unsupported per MVP).

## Implementation (strict TDD — failing test first each phase)

0. **Gate baseline** — run `test_bootstrap_stdlib_only.py` green; `reap.py` must keep it green
   (imports limited to `os, re, shutil, time, pathlib` + in-package `.locking`, `.environment`).
1. **env.json consumer** — `test_environment.py::{test_gc_field_absent_defaults_none,
   test_gc_field_present_parses, test_gc_wrong_type_raises,
   test_gc_keep_latest_out_of_range_raises, test_old_archive_without_gc_still_loads}` → add
   `gc` field + `_read_optional_gc`, wire into `load()`.
2. **env.json producer** — `test_builder_env_dict.py::{test_env_dict_stamps_gc_from_config,
   test_no_gc_flag_disables_in_env_json, test_env_json_not_in_build_id_after_gc_added}` → add
   `gc_enabled/gc_keep_latest/gc_grace_seconds` to `BuildConfig`, emit `gc` in
   `_build_env_dict`. Assert build_id unchanged for identical staging.
3. **CLI wiring** — `test_cli.py::{test_gc_flags_thread_into_buildconfig, test_no_gc_flag_default_on,
   test_gc_grace_duration_parsed}` → add `--gc/--no-gc`, `--gc-keep-latest`, `--gc-grace`
   (reuse `clean._parse_duration`; importing `clean` is fine in build-time CLI). `--no-gc` wins
   over the other two (stamped but inert; no exit-2 contradiction).
4. **Reaper core** — `test_reap.py::{test_keeps_keep_latest_newest_same_name,
   test_never_reaps_other_app_names, test_excludes_just_installed_key,
   test_skips_entry_whose_lock_is_held, test_grace_skips_recent_entry,
   test_disabled_when_gc_enabled_false, test_disabled_by_MOONLIT_NO_GC,
   test_errors_are_swallowed_best_effort, test_ignores_unknown_and_orphan_names,
   test_classifier_matches_clean_parse_cache_key, test_unlinks_victim_lock_after_successful_reap,
   test_keep_latest_env_override}` → implement `reap.py`. Use `os.utime` for explicit mtimes.
5. **Wire into extract** — `test_extract.py::{test_slow_path_invokes_reap_after_install,
   test_fast_path_does_not_invoke_reap, test_force_extract_invokes_reap,
   test_reap_failure_does_not_break_materialize}` → resolve knobs (env.json defaults overridden
   by `MOONLIT_NO_GC`/`MOONLIT_GC_KEEP_LATEST`/`MOONLIT_GC_GRACE`), call `reap(...)` right after
   `_sweep_old_siblings`, inside the slow-path lock only.
6. **Stdlib gate re-run** — `test_bootstrap_stdlib_only.py` over `reap.py`; confirm no
   non-stdlib import and no `os.rename` outside `atomic_replace_dir`.
7. **e2e (skip if uv absent)** — `test_bootstrap_e2e.py::{test_repeated_builds_reap_old_cache,
   test_no_gc_env_var_preserves_old_entries}`: build fixture twice with different content → two
   cache_keys, run the newer artifact, assert oldest-beyond-keep_latest is gone AND the app
   still exits 0 with expected stdout.
8. **Specs + docs** — apply Spec changes below.
9. **Release gating** — minor-version bump (e.g. v0.5.0) with the contract change announced;
   advertise `--no-gc` / `MOONLIT_NO_GC`.

## Spec changes (Phase 8)

- **`specs/04-cache-layout.md`** §6 amend "Never deleted automatically" (same-app entries MAY
  be auto-pruned by the app's own bootstrap on the cold slow path; `moonlit clean` stays the
  only cross-app/whole-cache/orphan tool); add **§12.2 "Automatic self-GC (D24)"** (policy,
  bounded-not-eliminated hazard, cross-ref §13.3); add a §11 safe-to-delete row.
- **`specs/03-bootstrap-runtime.md`** §2 step 10 + §6 (reap after sweep, under lock); §9
  env-var table (+ 3 `MOONLIT_GC*` vars; update D16 count); §12 (confirm no new stdlib module);
  §14 (GC silent on success, `MOONLIT_DEBUG`-only, never paints a progress line).
- **`specs/05-env-json-schema.md`** §2/§3 (`gc` object), §4 (validate-when-present step), §7
  (graduate `gc` to v1-optional, reserve the name), §8 (example), §9 (error-matrix rows).
- **`specs/01-cli.md`** §2.2 (`--gc/--no-gc`, `--gc-keep-latest`, `--gc-grace`), §3 (stamp-only,
  `--no-gc` makes the others inert).
- **`specs/CROSS_CUTTING_DECISIONS.md`** add **D24** (loosens 04 §6; automatic analogue of D23
  with human-quiescence replaced by grace+lock+same-app; D13/D14 read-side unchanged); update
  D16 (+3 vars); D9 (graduate `gc`).
- **`CLAUDE.md`** env-var section (+3 vars); Invariants (cache no longer append-only for an
  app's own keys; live/just-installed key never selected; reaper mirrors but never imports
  `clean.py`).
- **Changelog/release note** — the "never deleted automatically" contract changes; advertise
  opt-outs.

## Verification provenance

Designed via a 4-angle design panel + 8 adversarial verifiers + synthesis. Verifiers refuted
two initial assumptions (msvcrt-has-no-shared-lock; Windows-rmtree-is-a-backstop) — both folded
into the safety model above. All other crux claims confirmed (folder-bundle coverage, slow-path
preserves fast path, no schema bump, unambiguous cache-key parsing, env.json out of build_id,
no existing auto-GC today).

Post-implementation: a 3-dimension adversarial code review (concurrency/safety, policy/validation,
spec-impl-doc consistency) over the diff found **no logic or concurrency bugs** in `reap.py` or the
wiring. The only confirmed findings were stale doc absolutes ("exactly four env vars" in specs 00/03,
"only the four fields" in spec 05, a "Since moonlit 0.5" version pin) — all corrected. Implementation
verified by 781 passing tests (24 in `test_reap.py`, e2e reaping under real subprocess `.pyz` runs)
and a clean `ruff check`.
