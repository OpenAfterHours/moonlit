# moonlit specs

These are the foundational contracts for the moonlit project. They go together: each spec covers one concern, and `CROSS_CUTTING_DECISIONS.md` resolves issues that span multiple specs.

Read in order:

| #  | File | What it covers |
|----|------|---------------|
| 00 | [00-architecture.md](00-architecture.md) | The system as a whole — boundaries between build-time and runtime, module decomposition with public surfaces, runtime artifact contract, lifecycle phases, cross-cutting invariants with falsifiers, spec map. **Start here.** |
| 01 | [01-cli.md](01-cli.md) | The `moonlit` command-line surface — flags, exit codes (build-time enumeration), preflight order, stdout/stderr semantics, signal handling, stability guarantees. |
| 02 | [02-build-pipeline.md](02-build-pipeline.md) | The 10-step build pipeline, exact `uv` argv per step, build_id computation (deterministic, excludes `__pycache__`/`*.pyc`), atomic .pyz output via temp-then-replace, error→exit-code map, tempdir lifecycle, cross-platform notes, edge cases with test IDs. |
| 03 | [03-bootstrap-runtime.md](03-bootstrap-runtime.md) | What runs *inside* every produced `.pyz` — runtime exit-code enumeration, fast-path/slow-path semantics, lock protocol, extraction protocol with `.old.<pid>` rename trick, sys.path setup, entry-point resolution, env-var surface, stdlib-only constraint enforced via `tests/unit/test_bootstrap_stdlib_only.py`. |
| 04 | [04-cache-layout.md](04-cache-layout.md) | On-disk cache layout under `MOONLIT_ROOT` — directory tree, naming conventions, lifecycle, safe-to-delete table, locking semantics, atomic-rename semantics, edge cases. |
| 05 | [05-env-json-schema.md](05-env-json-schema.md) | The `env.json` wire format embedded in every `.pyz` — field table, validation rules, schema versioning policy, reserved-field forward-compat policy, complete example, error matrix. |
| 06 | [06-workspace-integration.md](06-workspace-integration.md) | How moonlit interprets `[tool.uv.workspace]`, `--package` semantics with PEP-503 normalization, intra-workspace dep handling via `uv build --all-packages`, validation timing, edge cases. |
| —  | [CROSS_CUTTING_DECISIONS.md](CROSS_CUTTING_DECISIONS.md) | **Binding** decisions resolving contradictions across specs (D1–D18). Where any individual spec disagrees with this document, this document wins. |

## How these were built

The specs went through one round of authoring, one devil's-advocate critique round, and one revision round. The v1 drafts and the critiques are preserved in [`drafts/`](drafts/) for audit; the polished v2 specs are at the top level of this directory.

Cross-cutting issues surfaced by the critique round (e.g. zip arcname layout, workspace transitive deps, exit-code namespace collisions, `os.replace` directory-on-Windows) are resolved in `CROSS_CUTTING_DECISIONS.md` rather than in any single spec.

## Status

All specs are at v2 and self-consistent against `CROSS_CUTTING_DECISIONS.md`. They have not yet been implemented — the next step is to lay down the `src/moonlit/` skeleton against `IMPLEMENTATION_PLAN.md` (in the repo root) and the architecture spec.

## Style conventions

- **Contract style.** "X happens when Y." Not "X should happen when Y."
- **Falsifiable invariants.** Every claim names what observation would refute it.
- **No emoji.** Plain markdown.
- **Stable references.** When a spec mentions another, it cites the file path: `specs/03-bootstrap-runtime.md`, not "the bootstrap spec."
- **Decisions doc wins.** When any spec disagrees with `CROSS_CUTTING_DECISIONS.md`, the decision is binding and the spec is the bug.
