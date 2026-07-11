---
phase: 86
slug: proposals-cutover
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-11
---

# Phase 86 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Phase 86 (SIDECAR-03) deletes the redundant `FileRecord.state` review-decision cascade
so `proposals.status` becomes the sole authority. The phase adds **no** endpoint, no input
path, and no new dependency — it removes internal writes and adds tests. The threat surface is
therefore dominated by *regression* risks in existing controls (cross-tenant guard, strict body
parsing, the pending-upsert conflict guard) rather than new attack surface. All 15 plan-time
threats are verified CLOSED by direct source inspection.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| agent → API (`PATCH /api/internal/agent/proposals/{id}/state`) | The one contract-adjacent surface: an authenticated file-server agent PATCHes a proposal outcome. Untrusted body validated by the unchanged `ProposalStatePatch` schema. | Proposal outcome (`file_state`, optional `current_path`) — agent-scoped, low sensitivity |
| worker/service → DB | `store_proposals` / `update_proposal_status` / `bulk_update_status` write proposal rows; callers validated upstream, no external input crosses here | Proposal status rows (internal) |
| future source edit → anti-drift guard | A re-added `FileRecord.state` cascade must be caught before merge by the AST source-scan guard | Source AST (build-time only) |
| test-suite → CI review-bucket gate | A red `tests/review` bucket blocks the phase PR | Test results (build-time only) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-86-01 | Tampering | `store_proposals` upsert — removing the state block could weaken the pending conflict guard | mitigate | Only the file-load-and-guard block deleted; `on_conflict_do_update(index_where=status=='pending')` intact — verified `proposal.py:341-346`, partial index `uq_proposals_file_id_pending` is sole protection | closed |
| T-86-02 | Repudiation | `proposals.status` remains the sole review-decision authority | accept | `update_proposal_status` still writes `proposals.status`; `execution_log` unchanged; no audit-trail regression | closed |
| T-86-03 | Elevation of Privilege | cross-tenant guard (`agent_proposals.py`) | mitigate | `file_record.agent_id != agent.id` → 403 block **untouched** — verified present `agent_proposals.py:68-70`; code review independently confirmed; 403/401 guard tests green | closed |
| T-86-04 | Tampering | `ProposalStatePatch` request shape (`file_state` + `_require_path_when_moved` validator) | mitigate | `schemas/agent_proposals.py` **not edited** in phase (empty diff); `extra="forbid"` (line 24) + validator (line 32) preserved | closed |
| T-86-05 | Tampering | dropping `current_path` at the apply-outcome site would silently lose the moved-file path | mitigate | Only the `file_record.state =` limb deleted; `current_path` write kept — verified `agent_proposals.py:116`; positive test asserts persistence on a moved PATCH | closed |
| T-86-06 | Tampering (self) | a toothless anti-drift guard (wrong repo root / grep blind to multi-line SQLAlchemy) | mitigate | Existence-assert resolved paths (`parents[2]` root fixed); `ast.walk` over Call args+keywords; permanently-encoded `test_guard_flags_*` RED suite. Initially partial (WR-01); **fully closed by 86-05** (base-kind-agnostic scanner + mutation-verified RED cases) | closed |
| T-86-04-01 | Tampering | test edit weakens coverage | mitigate | Only the assertion of the DELETED cascade removed; positive assertions (`pg_insert` called, `session.execute` awaited) preserved — `store_proposals` real behavior stays covered | closed |
| T-86-04-02 | Repudiation | "bucket green" claimed but not run | mitigate | Full `uv run pytest tests/review -q` run required and executed → 428 passed, 0 failed (was 1 failed) — closes the exact false-claim Gap 1 | closed |
| T-86-05-01 | Tampering | reintroduced chained-attr cascade evades guard | mitigate | `_state_writes`/`_state_reads` broadened to base-kind-agnostic `.state`; `test_guard_flags_chained_attr_string_write` encoded + mutation-verified RED→restore→GREEN | closed |
| T-86-05-02 | Tampering | two-step ORM idiom evades guard | mitigate | `_orm_row_bound_names` binds locals fetched via `.scalar_one_or_none()`-family; `test_guard_flags_two_step_orm_idiom_write` encoded + mutation-verified | closed |
| T-86-05-03 | Repudiation | toothless GREEN guard test | mitigate | Each new case mutation-verified RED→restore→GREEN, evidence recorded in 86-05-SUMMARY; verifier independently reproduced RED→GREEN | closed |
| T-86-05-04 | Denial of Service (false positive) | broadened scan over-fires on `.status`/`.id`/prose | accept-with-check | `.attr == "state"` keying preserved; four existing GREEN false-positive checks + three real-source guards stay GREEN — verified | closed |
| T-86-SC (86-01) | Tampering | npm/pip/cargo installs | accept | Zero package installs in this phase (RESEARCH § Package Legitimacy Audit: N/A) — no supply-chain surface | closed |
| T-86-SC (86-02) | Tampering | npm/pip/cargo installs | accept | Zero package installs this phase | closed |
| T-86-SC (86-03) | Tampering | npm/pip/cargo installs | accept | Zero package installs this phase | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-86-01 | T-86-02 | `proposals.status` is the single review-decision authority by design (SIDECAR-03); the removed `FileRecord.state` mirror was redundant and drift-prone. No audit-trail regression — `execution_log` unchanged. | Robert Wlodarczyk | 2026-07-11 |
| AR-86-02 | T-86-SC | Phase performs zero package installs (deletion + tests only); no supply-chain surface introduced. | Robert Wlodarczyk | 2026-07-11 |
| AR-86-03 | T-86-05-04 | Broadened AST scanner keys strictly on `.attr == "state"`; four false-positive GREEN checks (`.status`, `body.file_state`, `.id`, docstrings) plus three real-source guards verified GREEN, so over-firing is bounded. | Robert Wlodarczyk | 2026-07-11 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-11 | 15 | 15 | 0 | /gsd:secure-phase (short-circuit — plan-time register, all mitigations verified in source) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-11
