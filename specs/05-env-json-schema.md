# env.json Schema (v1)

## 1. Location, encoding, role

- Path inside the .pyz: `env.json` at the archive root (top-level zip entry per D1).
- Encoding: UTF-8, no BOM.
- Top-level JSON value: object.
- Role: the bootstrap reads it to derive the cache key and the entry point; tooling reads it for human inspection. The bootstrap consumes only the four fields whose "Used by" column names it; everything else is informational.

**Threat model (v1).** `env.json` is **not authenticated**. A modified .pyz could ship a forged env.json and the bootstrap will trust it. Integrity verification is the `--no-modify` feature deferred to v0.2. The bootstrap must not auto-execute privileged behavior keyed solely on `name`.

## 2. Field table (schema_version 1)

| Field             | Type   | Required | Constraint                                      | Used by                          |
|-------------------|--------|----------|-------------------------------------------------|----------------------------------|
| `schema_version`  | int    | yes      | exactly `1`; not `bool`                         | bootstrap                        |
| `name`            | string | yes      | non-empty; PEP 508 regex (D11); raw (D5)        | bootstrap, tooling, cache key    |
| `build_id`        | string | yes      | `^[0-9a-f]{64}$` (lowercase)                    | bootstrap, cache key             |
| `entry_point`     | string | yes      | exactly one `:`; `module:attr` (see 3.4)        | bootstrap                        |
| `built_at`        | string | yes      | `%Y-%m-%dT%H:%M:%SZ` (D10)                      | tooling                          |
| `moonlit_version` | string | yes      | non-empty; PEP 440 (informational)              | tooling                          |
| `python_shebang`  | string | yes      | non-empty; no `\n`; no leading `#!`             | tooling                          |
| `python_version`  | string | no       | `^\d+\.\d+$` (major.minor only) — see §3.8      | bootstrap, tooling               |

"Non-empty" means `len(s) > 0`. Whitespace-only is non-empty by this definition; that is accepted and not a producer error.

The "Used by" column is the single source of truth.

## 3. Per-field validation rules

3.1 `schema_version` — `isinstance(v, int) and not isinstance(v, bool) and v == 1`. Future N>1 is rejected with a message that names N and suggests "upgrade moonlit to a version that supports env.json schema version N".

3.2 `name` — must match the PEP 508 literal **with `re.IGNORECASE`** (D11):
```python
import re
PEP508_NAME = re.compile(
    r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$",
    re.IGNORECASE,
)
```
The `re.IGNORECASE` flag is mandatory. Without it, all-lowercase names (e.g. `myapp`) are rejected — a bug. The value stored here is **raw** (D5): `env.json.name` is the value as authored in `[project].name`. **Consumers who need a normalized form perform the normalization themselves** — the bootstrap normalizes `name` per D5 (`re.sub(r"[-_.]+", "-", name).lower()`) when building the cache key (see `specs/04-cache-layout.md`). `ensure_ascii=False` permits non-ASCII names; homograph risk is accepted in v1.

3.3 `build_id` — `re.fullmatch(r"[0-9a-f]{64}", v)`. Producer recipe is `hashlib.sha256(...).hexdigest()`, which yields lowercase hex.

3.4 `entry_point` — exactly one `:`. The left side matches `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$`; the right side matches `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$`. No whitespace anywhere (regex contains no `\s`).

3.5 `built_at` — `datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")` must succeed. `fromisoformat` is **not** used.

3.6 `moonlit_version` — non-empty; informational only at bootstrap. Tooling validates against PEP 440 on demand. The producer reads `moonlit.__version__` and emits whatever it says, including local-version segments such as `0.1.0+local.dirty`. Empty string is rejected.

3.7 `python_shebang` — non-empty; no embedded newline; no leading `#!` (the build pipeline writes the `#!...\n` prefix outside the zip header — D1).

3.8 `python_version` (v1-optional) — must match `^\d+\.\d+$` when present, e.g. `"3.13"`. Stores the **target** Python's `major.minor` and matches the `cp<X><Y>` ABI tag of every wheel uv stages. Source-of-truth at build time: `BuildConfig.python_version` (set when the user passes `--python-version`, D20 cross-interpreter builds), falling back to the build host's `sys.version_info.major.minor`. The bootstrap compares this against the runtime interpreter's major.minor and exits 1 with a "built for X.Y, running A.B" message on mismatch — surfacing the real cause of the otherwise-mysterious `ModuleNotFoundError: No module named '<pkg>._core'` that occurs when a wheel's `.pyd` is silently skipped for ABI-tag mismatch. When the field is absent (older archives produced before this field's introduction) the bootstrap skips the check.

## 4. Validation algorithm (D8, consumer)

Bootstrap and tooling validate in this exact order. The first failure exits the bootstrap with code 1 (per D3 runtime enumeration); each step has the exact error message shown:

1. `env.json` member exists in the archive — `"env.json missing from archive"`.
2. Bytes decode as UTF-8 — `"env.json is not valid UTF-8"`.
3. `json.loads` succeeds — `"env.json is not valid JSON"`.
4. Top-level value is a `dict` — `"env.json must be a JSON object"`.
5. `schema_version` key present and `isinstance(v, int) and not isinstance(v, bool)` — `"env.json: schema_version missing or not an integer"`.
6. `schema_version == 1` — `"env.json: unsupported schema_version <N>; upgrade moonlit to a version that supports env.json schema version <N>"`.
7. All required fields present — `"env.json: missing required field '<field>'"`.
8. Each required field has the correct JSON type (per Section 2) — `"env.json: field '<field>' has wrong type (expected <T>)"`. JSON `null` for any required field is reported here, since `None` is not the expected type.
9. Each required field passes its format check (Section 3) — `"env.json: field '<field>' failed validation"`.
10. If `python_version` is present, validate its type (`"env.json: field 'python_version' has wrong type (expected string)"`) and format (`"env.json: field 'python_version' failed validation"`). When absent, skip — the field is optional.

Duplicate keys: `json.loads` silently keeps the last occurrence per stdlib semantics. v1 accepts this and does not install an `object_pairs_hook` to detect it. Documented, not enforced.

## 5. Producer responsibilities

- Compute `build_id` **before** `env.json` is written. **`env.json` bytes do not participate in `build_id`** — this is a load-bearing invariant; cache correctness depends on it (see CLAUDE.md "Invariants").
- Emit every required field; emit no reserved name (Section 7).
- `built_at`: `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")` (D10). Do **not** use `isoformat() + "Z"`.
- `moonlit_version`: read from `moonlit.__version__` and pass through unchanged.
- Serialize byte-stably:
  ```python
  payload = json.dumps(
      obj,
      indent=2,
      sort_keys=True,
      ensure_ascii=False,
      separators=(",", ": "),
  ) + "\n"
  ```
  The `separators` argument is pinned even though it matches the indent-mode default — explicit beats implicit. Exactly one trailing `\n`.
- Open the file in **binary** mode and write `payload.encode("utf-8")`. Do not rely on text-mode newline translation, which differs on Windows.

## 6. Consumer responsibilities

- Parse with `json.loads` after a UTF-8 decode of the zip member bytes.
- Validate per Section 4. Use values; ignore unknown fields (D9).
- The bootstrap consumes the fields marked "bootstrap" in Section 2. Other consumers read whatever they need from the same table.

## 7. Reserved field names and forward compatibility (D9)

Reserved: `hashes`, `compile_pyc`, `preamble`, `reproducible`.

- **Producer obligation (v1).** Producers MUST NOT emit any reserved field name. The build pipeline writes a fixed, closed object.
- **Consumer obligation (v1).** Consumers MUST ignore unknown fields, including any reserved name that appears in a future-built archive.
- **Graduation.** When a future v0.x release adds an optional field (e.g. `hashes` in v0.2), it moves from "reserved" to "v1-optional". Because v1 consumers were already required to ignore it, this is **not** a `schema_version` bump and v0.x producers MAY emit it without bumping.
- **Bumps.** `schema_version` increments only when a required field is renamed or removed, a field's type changes, or the bootstrap contract on field semantics changes.

## 8. Complete v1 example

```json
{
  "build_id": "a3f1c2d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00",
  "built_at": "2026-05-08T15:23:01Z",
  "entry_point": "myapp.cli:main",
  "moonlit_version": "0.1.0",
  "name": "myapp",
  "python_shebang": "/usr/bin/env python3",
  "python_version": "3.13",
  "schema_version": 1
}
```

The example terminates with a single `\n` byte not shown above.

## 9. Error matrix

| Condition                                         | Step | Bootstrap exit | Message                                                                                       |
|---------------------------------------------------|------|----------------|-----------------------------------------------------------------------------------------------|
| `env.json` not in archive                         | 1    | 1              | `env.json missing from archive`                                                               |
| Bytes not valid UTF-8                             | 2    | 1              | `env.json is not valid UTF-8`                                                                 |
| Not parseable JSON / empty file / truncated entry | 3    | 1              | `env.json is not valid JSON`                                                                  |
| Top-level not an object (array, string, number)   | 4    | 1              | `env.json must be a JSON object`                                                              |
| `schema_version` missing or non-int (incl. bool)  | 5    | 1              | `env.json: schema_version missing or not an integer`                                          |
| `schema_version` 0, negative, or >1               | 6    | 1              | `env.json: unsupported schema_version <N>; upgrade moonlit to a version that supports env.json schema version <N>` |
| Required field absent                             | 7    | 1              | `env.json: missing required field '<field>'`                                                  |
| Required field present but wrong JSON type / null | 8    | 1              | `env.json: field '<field>' has wrong type (expected <T>)`                                     |
| Required field fails format check                 | 9    | 1              | `env.json: field '<field>' failed validation`                                                 |
| `entry_point` contains whitespace                 | 9    | 1              | `env.json: field 'entry_point' failed validation`                                             |
| `python_version` present but non-string           | 10   | 1              | `env.json: field 'python_version' has wrong type (expected string)`                           |
| `python_version` present but bad format           | 10   | 1              | `env.json: field 'python_version' failed validation`                                          |
| `python_version` differs from runtime major.minor | n/a  | 1              | `this archive was built for Python <X.Y>, but you are running Python <A.B>; ...` (spec 03)    |
| Duplicate keys in JSON source                     | 3*   | 0 (accepted)   | last-wins per `json.loads`; not detected                                                      |
| Unknown extra field                               | n/a  | 0 (accepted)   | ignored (D9)                                                                                  |

`MOONLIT_ENTRY_POINT` empty string is treated as unset (D16); it does not feed into env.json validation. An invalid override value is rejected at runtime and exits with bootstrap code 2 (D3 runtime).

## 10. Out of scope

- A draft-2020-12 JSON Schema artifact at `specs/drafts/env.schema.json`. Defer until post-MVP.
- Authenticated env.json (`--no-modify`, signed manifests) — v0.2.
- Size limits on env.json — accepted as small in MVP; bootstrap reads it via `ZipFile.read("env.json")` into memory without bound.
