# Phase 85: EXECUTED-Gate Revival - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-10
**Phase:** 85-executed-gate-revival
**Areas discussed:** applied() predicate & placement, Which apply-outcomes count, Retroactive activation & rollout, Gate-by-gate vs uniform swap

---

## applied() predicate — source of truth

| Option | Description | Selected |
|--------|-------------|----------|
| `proposals.status=='executed'` | Single predicate, no file.state read, the exact column Phase 86 makes sole authority. Helper in services/stage_status.py. Includes UNCHANGED, excludes FAILED for free. | ✓ |
| `execution_log` completed EXISTS | EXISTS over execution_log.status=='completed' (+ sha256_verified). More literal, heavier join, audit-log semantics. | |
| Both (AND) | proposals.status=='executed' AND execution_log completed. Strictest, couples two tables, false-negative risk. | |

**User's choice:** `proposals.status=='executed'`
**Notes:** Aligns with Phase 86 promoting proposals.status to sole authority; keeps predicate expressed purely over the proposals table.

---

## Which apply-outcomes count

**Determined by predicate choice (D-01).** proposals.status=='executed' automatically includes UNCHANGED files (both MOVED and UNCHANGED collapse to 'executed' at the proposal layer) and excludes FAILED. MOVED-only was rejected as self-defeating (would require a file.state read). Idempotency preserved via the existing `completed_subq` anti-join.

**User's choice:** Predicate-derived — UNCHANGED in, FAILED out.

---

## Retroactive activation & rollout

| Option | Description | Selected |
|--------|-------------|----------|
| Ship live + paginate unbounded lists | No flag (writing is operator-triggered → display-only risk). Add LIMIT/pagination to review.py:422 / tags.py:174. Live-UAT: lists populate, manual tag-write succeeds. | ✓ |
| Ship live, no pagination changes | Simplest; accept large backlog load; defer pagination to Phase 87. | |
| Behind a config flag (default off) | Gate revival behind e.g. PHAZE_TAG_WRITE_ENABLED, flip after UAT. Extra machinery for an already-manual path. | |

**User's choice:** Ship live + paginate unbounded lists
**Notes:** Surfacing the previously-invisible applied-file backlog is the intended fix; the only real hazard is unbounded list queries at 200K scale.

---

## Gate-by-gate vs uniform swap

| Option | Description | Selected |
|--------|-------------|----------|
| Uniform, including UI badge | One shared applied() fragment + helper across all functional gates AND proposal_row.html badge — no file.state reader survives into Phase 90. | ✓ |
| Functional gates only; defer badge to Phase 87 | Swap the functional gates; leave proposal_row.html reading file.state for Phase 87. Leaves one file.state reader temporarily. | |

**User's choice:** Uniform, including UI badge
**Notes:** Avoids leaving a stray file.state reader for the Phase 90 column drop to trip over.

---

## Claude's Discretion

- Exact `applied()` helper name/signature and SQL-fragment form.
- Pagination page-size / parameterization on the unbounded lists (follow in-tree idiom).
- Badge label wording ("Executed" vs "Applied").

## Deferred Ideas

- Delete FileRecord.state / `_TERMINAL_FILE_STATES` cascade; proposals.status sole authority → Phase 86.
- Broader operator visibility (stage matrix, retry, eligibility trace) → Phase 87.
- Drop files.state / FileState enum → Phase 90.
- Reviewed todo `analysis-completed-at-backfill.md` — not folded (belongs to Phase 79/90 shadow-gate work).
