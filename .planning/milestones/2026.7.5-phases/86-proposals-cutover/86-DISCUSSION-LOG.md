# Phase 86: Proposals Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-10
**Phase:** 86-proposals-cutover
**Areas discussed:** Deletion scope, Apply HTTP contract, Regression proof, Gate + _TERMINAL disposal

---

## Deletion scope

| Option | Description | Selected |
|--------|-------------|----------|
| All four now | Delete sites 1-3 AND the apply-outcome `file.state` limb (`agent_proposals.py:115`), keeping `current_path`. Truest to "proposals.status is SOLE authority"; one coherent seam; larger blast radius (touches agent PATCH contract). | ✓ |
| Core now, apply-outcome → Phase 90 | Delete sites 1-3 only; leave the apply-outcome joint-write as a dead dual-write for Phase 90. Smallest blast radius, no HTTP-contract change. | |
| You decide | Recommend based on small-blast-radius requirement + contract entanglement. | |

**User's choice:** All four now
**Notes:** Chosen despite the larger blast radius so no proposal-lifecycle `file.state` writer survives into Phase 90 and the "proposals seam" is self-contained. `current_path` write at site 4 must survive.

---

## Apply HTTP contract

| Option | Description | Selected |
|--------|-------------|----------|
| Echo the request's file_state | Response echoes `body.file_state` without reading `file_record.state`; wire contract byte-identical; idempotent branch also stops reading the column. | ✓ |
| Derive from proposals.status | Map `proposals.status` → `moved`/`unchanged` via `_FILE_FOLLOW`; re-derives a distinction only `file.state` carried; field is unused anyway. | |
| Drop the field | Remove `file_state` from `ProposalStateResponse`; agent-facing schema change for no functional gain. | |

**User's choice:** Echo the request's file_state
**Notes:** The only caller (`execution.py:205`) discards the response, so echoing the request value is honest and zero-churn. Request-side `file_state` field stays (drives `current_path` requirement + move).

---

## Regression proof

| Option | Description | Selected |
|--------|-------------|----------|
| Behavioral + mutation-verified | Integration test of the real bug scenario (stale `store_proposals` batch on an applied file → `applied(f)` stays True, independent session) + apply-PATCH test (keeps `current_path`, echoes request) + each guard mutation-checked (break→RED→restore), documented. | ✓ |
| Behavioral only | Same behavioral tests, no documented mutation-verify. | |
| Per-site unit tests | Unit-test each deleted writer in isolation; doesn't prove the end-to-end MOVED-clobber scenario. | |

**User's choice:** Behavioral + mutation-verified
**Notes:** Consistent with prior burn on toothless guards — a GREEN guard proves nothing until it has failed once. Assert from an INDEPENDENT session per the get_session-never-commits rule.

---

## Gate + _TERMINAL disposal

| Option | Description | Selected |
|--------|-------------|----------|
| Leave gate as-is; delete _TERMINAL fully | Do not modify `shadow_compare.py` INVARIANTS (six proposal invariants stay green, now historical-only); fully delete `_TERMINAL_FILE_STATES` + guard + newly-unused import. | ✓ |
| Leave gate; add a historical-only doc note | Same deletions + a one-line comment marking the six invariants historical-only; touches the standing gate file. | |
| You decide | Recommend lightest-touch option. | |

**User's choice:** Leave gate as-is; delete _TERMINAL fully
**Notes:** Minimal touch to the standing gate; the historical-only nature is inherent, not a defect. The gate stays green by implication direction.

---

## Claude's Discretion

- Exact wording of the request-derived echo on the idempotent replay branch (`None` vs `body.file_state`).
- Test file placement / bucket (must pass via `just test-bucket <bucket>` in isolation).
- Whether `store_proposals` still needs to load the `FileRecord` at all after the state write is gone (likely delete the dead load).

## Deferred Ideas

- Enrich/cloud/dedup/ingestion `.state=` writers + dropping `files.state` / `FileState` enum / `ix_files_state` → Phase 90 (MIG-04).
- Operator UI derivation of review/apply status from `proposals` → Phase 87 (UI-01..05).
- `store_proposals` duplicate-pending-insert on an already-executed file — not a Phase 86 concern (derived propose-pending prevents the call; `applied(f)` unaffected). Noted as a truth for the researcher.
- Reviewed-not-folded todos: `analysis-completed-at-backfill.md` (→ Phase 79/90), `wr-01-review-builder-limit-before-filter.md` (→ Phase 85/87 tag/CUE builders).
