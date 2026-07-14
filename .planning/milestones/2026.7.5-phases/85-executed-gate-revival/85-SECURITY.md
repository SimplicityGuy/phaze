---
phase: 85
slug: executed-gate-revival
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-10
---

# Phase 85 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Built from the 4 plan-time `<threat_model>` blocks (85-01..04) and their SUMMARY threat flags
> (`register_authored_at_plan_time: true`). All plan-time threats verified CLOSED with mitigations
> confirmed present in source.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| agent → control-plane `PATCH /proposals/{id}/state` | authenticated agent sets `proposals.status`; the write source the revived `applied()` predicate reads | proposal apply-outcome (`executed`/`failed`) |
| operator click → `POST /tags/{id}/write` → filesystem | operator-triggered mutagen tag write on `file.current_path`, now gated by `applied()` | audio-file tag bytes on disk |
| operator click → `POST /cue/{id}/generate` → filesystem | operator-triggered CUE write on `file.current_path`, now gated by `applied()` | `.cue` file bytes on disk |
| operator poll/render → review list builders | read-only render over the newly-visible applied backlog; DoS surface is the unbounded list | file/proposal/metadata rows |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-85-01 | Spoofing/Tampering | `applied()` predicate reachable by a `proposals.status='executed'` write (85-01) | accept | Reaching `executed` requires the apply path's copy→verify→delete to succeed BEFORE the proposal PATCH; an IO failure forces `status='failed'`. PATCH route is `Depends(get_authenticated_agent)` (unchanged). Reading the column adds no new surface. Spoofing needs a compromised authenticated agent — out of scope for a single-operator private-network tool. | closed |
| T-85-02 | Tampering/Integrity | choice of predicate source | mitigate | `applied_clause()`/`is_applied()` read `proposals.status=='executed'` (transactionally coupled to `current_path`), NEVER the best-effort `execution_log` or `files.state` — verified `src/phaze/services/stage_status.py:143,161`. Closes "tag/CUE write to a stale/deleted path" at the predicate level. | closed |
| T-85-03 | Elevation/Path traversal | mutagen write on `file.current_path` (85-02) | accept | Path-containment guard `tasks/execution.py::_resolve_and_check_containment` (T-26-11-S1) is unchanged / out of scope. Reviving the dead gate adds no new write surface — the writer was already wired; only the dead predicate changed. | closed |
| T-85-04 | Denial of Service | `bulk_write_no_discrepancies` unbounded loop over a large newly-visible applied backlog (85-02) | mitigate | `.limit(_MAX_BULK_TAG_WRITE)` (=2000, D-03) bounds the operator-triggered one-shot loop — verified `src/phaze/routers/tags.py:44,427`. | closed |
| T-85-05 | Elevation/Path traversal | `write_cue_file` on `Path(file.current_path)` (85-03) | accept | Apply-path containment guard unchanged; reviving the CUE gate adds no new write surface (writer already wired). | closed |
| T-85-06 | Denial of Service | `get_tagwrite_review_rows` / `get_cue_review_cards` rendering a large first-time-visible applied backlog (85-04) | mitigate | `.limit(_MAX_REVIEW_ROWS)` (=2000, D-03) caps both builders; the eligible half also breaks at the same bound — verified `src/phaze/services/review.py:57,122,238,271`. A fixed cap (no operator-supplied `page_size`) is a stronger DoS control than a `Query(le=100)` bound. | closed |
| T-85-SC | Tampering (supply chain) | package installs | N/A | Zero new packages this phase (milestone constraint); `pyproject.toml`/`uv.lock` dependency set unchanged. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

> **Note — WR-01 is NOT a security gap.** The Phase 85 code review flagged that the `.limit()` in the
> tag/CUE bulk builders is applied *before* the Python qualifier filter (starvation at 200K scale,
> tracked as follow-up debt). This is a functional-correctness/usability issue — the DoS mitigations
> T-85-04/T-85-06 still hold because the `.limit(2000)` bounds the query result regardless of ordering.
> No threat is reopened by WR-01.

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-85-01 | T-85-01 | Spoofing the `executed` write requires a compromised authenticated agent; single-operator private-network deployment posture (consistent with prior AR-27-*/AR-37-* dispositions). Read-only column access adds no surface. | Operator | 2026-07-10 |
| AR-85-02 | T-85-03 | mutagen tag-write path containment is enforced by the unchanged apply-path guard; this phase revives a dead predicate over an already-wired writer, adding no new write surface. | Operator | 2026-07-10 |
| AR-85-03 | T-85-05 | CUE-write path containment identical rationale to AR-85-02; writer already wired, containment guard unchanged. | Operator | 2026-07-10 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-10 | 7 | 7 | 0 | secure-phase (short-circuit: threats_open=0 + plan-time register; mitigations independently grep-verified in source) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-10
