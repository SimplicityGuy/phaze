# Phase 89: Legacy Scan-Path Deletion & Sentinel Reattribution - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-11
**Phase:** 89-legacy-scan-path-deletion-sentinel-reattribution
**Areas discussed:** Reattribution target, Router disposition, Default + test seed, Verify & downgrade

---

## Reattribution Target

First pass surfaced a clarifying question from the user ("is this using the name of the fileserver?"), answered by clarifying that `Agent.id` is a string PK (operator-chosen, e.g. `nox`) and `name` is a separate label — the FK reattribution writes the target's `id`. Re-asked with that framing.

| Option | Description | Selected |
|--------|-------------|----------|
| Auto: sole fileserver, else abort | Query non-revoked `kind='fileserver'`; 1 → use it; 0 or >1 → abort, >1 accepts explicit `-x reattribute_to=<id>` override | ✓ |
| Explicit id via alembic -x / env | Operator must always supply the target id; abort if unset/invalid | |
| Auto with explicit override | Hybrid; explicit id wins, else auto-detect sole fileserver | |

**User's choice:** Auto: sole fileserver, else abort (selected preview retained the `>1 → ABORT: pass -x reattribute_to=<id>` escape hatch, so the ambiguous case accepts an explicit override).
**Notes:** In current prod exactly one real fileserver (nox) exists, so the auto path resolves with no operator input. Reattribution scope is all legacy-owned files + scan_batches (incl. the 012 `status='live'` sentinel batch), per LEGACY-02.

---

## Router Disposition

| Option | Description | Selected |
|--------|-------------|----------|
| Delete whole file + unregister | Remove routers/scan.py (POST + GET), drop include_router, delete test_scan.py | ✓ (conditional) |
| Surgical: keep GET only | Delete POST + run_scan import, keep GET status endpoint | |
| Verify callers first | Confirm no external consumer before deciding; default to full delete | (folded in) |

**User's choice:** Free-text — "if it's no longer needed, remove it and its tests."
**Notes:** Resolved as full delete of routers/scan.py + tests, contingent on research/planning confirming the GET status endpoint has no live consumer (no template/JS ref; sanity-check homelab/monitoring). No known external poller.

---

## Default + Test Seed

| Option | Description | Selected |
|--------|-------------|----------|
| Drop default, explicit everywhere | Remove Python default on both models; agent_id required; conftest seeds a real fileserver; ~10 tests repoint their constant | ✓ |
| Repoint default to real agent | Keep a Python default but point to a real id | |
| Drop DB default, keep test shim | Drop default in source, add a conftest factory to minimize test edits | |

**User's choice:** Drop default, explicit everywhere.
**Notes:** During analysis, confirmed migration 012 added `agent_id` as nullable with NO DB `server_default` — so "drop the default" (LEGACY-03) is a pure model-code change; the Alembic migration needs no `ALTER COLUMN … DROP DEFAULT`.

---

## Verify & Downgrade

| Option | Description | Selected |
|--------|-------------|----------|
| Hard-abort + irreversible downgrade | Strict COUNT=0 assert in one txn, else rollback; downgrade raises NotImplementedError (ownership unrecoverable) | ✓ |
| Hard-abort + best-effort downgrade | Same strict upgrade; downgrade re-creates the sentinel row but does not un-reattribute | |
| Warn-only verification | Log warning, proceed to DELETE anyway — rejected: RESTRICT FK makes it impossible | |

**User's choice:** Hard-abort + irreversible downgrade.
**Notes:** Single transaction — UPDATE files/scan_batches → assert zero remaining legacy-owned rows (else RAISE) → DELETE sentinel. downgrade() documents that per-row legacy ownership is lost once merged into the target.

## Claude's Discretion

- Migration revision number (assigned at plan time; slots after the latest branch migration).
- Bulk UPDATE batching/lock strategy for the ~11,428-file corpus.
- Exact wording of the abort messages and the NotImplementedError reason.

## Deferred Ideas

- Rollout/release sequencing (tag, homelab redeploy timing) — operational, handled at ship time.
- Future scan-batch status API for surviving `scan_directory` batches — build fresh on the pipeline surface if needed.
- Reviewed-not-folded todos: `analysis-completed-at-backfill.md` and `wr-01-review-builder-limit-before-filter.md` (keyword collisions, unrelated to sentinel retirement).
