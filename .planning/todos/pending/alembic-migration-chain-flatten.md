---
title: Flatten the Alembic migration chain (001-039) into a single baseline migration
created: 2026-07-14
severity: minor
type: engineering-debt
found_by: design session 2026-07-14 (brainstorming; superpowers spec approved)
owner: next milestone
design_spec: docs/superpowers/specs/2026-07-14-alembic-baseline-flatten-design.md
blocks: null
resolves_phase: 102
precondition: "prod at Alembic 039 (SATISFIED as of 2026-07-14 deploy) — re-confirm via read-only PG probe immediately before merge"
---

# Flatten the Alembic migration chain into a single baseline

Collapse the 39-file linear chain (`alembic/versions/001`→`039`) into **one**
baseline migration, using Alembic's documented **"Prune Old Migration Files"**
pattern. Full approved design in `docs/superpowers/specs/2026-07-14-alembic-baseline-flatten-design.md`.

## Why now-safe

Prod is the only persistent Alembic-managed DB and is **at `039`** as of the
2026-07-14 deploy. Reusing revision id `039` as the new base (`down_revision =
None`) makes prod a **no-op** on next `upgrade head` — zero manual stamp, zero
DDL, zero risk. Ephemeral CI/test DBs build from scratch.

## Scope when promoted (candidate phase breakdown)

1. **Build the baseline** — run the current chain on the 5433 DB →
   `pg_dump --schema-only` + data dump of the two seed tables → author
   `039_baseline_schema.py` (`revision="039"`, `down_revision=None`) embedding
   the dumped DDL via `op.execute` + bound-param seed `INSERT`s
   (`pipeline_stage_control`, `route_control`). `downgrade()` drops everything.
2. **Prove fidelity (merge gate)** — schema-equivalence `diff` between the
   pre-flatten chain output and the baseline output must be **empty**; seed rows
   byte-identical. Capture as VERIFICATION evidence.
3. **Delete** the 39 migration files + the ~22 per-migration test files
   (`tests/integration/test_migrations/test_*.py` + 2 in `tests/shared/core/`);
   **add** `test_baseline_schema.py` preserving the durable invariants (033 XOR
   CHECK, seeds present, partial indexes, search-vector/gin, enums, expected
   tables/columns; clean upgrade-from-empty + `downgrade base` round-trip;
   `--autogenerate` empty diff).
4. **Re-verify** the 90% coverage gate; export both DB URLs for the 5433 harness
   (5432/5433 footgun).

## Hard gate before merge

Re-confirm prod `alembic_version.version_num == '039'` via the read-only PG probe
(`ssh datum@lux.lan`, `BEGIN TRANSACTION READ ONLY`). If not at 039, hold.

## Out of scope

No schema change (byte-identical to the `039` chain output). Independent of the
3 post-deploy cloud-burst bugs.
