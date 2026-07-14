---
phase: 89-legacy-scan-path-deletion-sentinel-reattribution
audited: 2026-07-11
asvs_level: 2
block_on: high
register_authored_at_plan_time: true
threats_total: 8
threats_closed: 8
threats_open: 0
status: secured
---

# SECURITY.md — Phase 89: Legacy Scan Path Deletion, Sentinel Reattribution

**Phase:** 89 — legacy-scan-path-deletion-sentinel-reattribution
**Audited:** 2026-07-11
**ASVS Level:** 2
**Block on:** high
**Threats Closed:** 8/8 (6 mitigate + 2 accept)
**Register authored:** plan time (`register_authored_at_plan_time: true`) — each mitigation verified in shipped code, not intent.

## Threat Verification

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-89-01-01 | Elevation/Tampering | mitigate | CLOSED | `src/phaze/routers/scan.py`, `src/phaze/services/ingestion.py`, `src/phaze/schemas/scan.py` all deleted (test -f fails). No `scan` in the `main.py` router-import tuple (L16-47) and no `include_router(scan.router)`. Surviving ingest `src/phaze/routers/agent_files.py` stamps `data["agent_id"] = agent.id` (L110) and `agent_id=agent.id` (L154) from `Depends(get_authenticated_agent)` (L61/L164) — never from the body; batch ownership re-checked against `agent.id` (L85). `grep api/v1/scan src/phaze` returns nothing. |
| T-89-01-02 | Tampering | mitigate | CLOSED | `src/phaze/models/file.py:88-92` and `src/phaze/models/scan_batch.py:29-33` — `agent_id` `mapped_column` has NO `default=` kwarg; retains `nullable=False` + `ForeignKey("agents.id", ondelete="RESTRICT")`. `grep 'default="legacy-application-server"' src/phaze/models/` returns nothing. |
| T-89-02-01 | Tampering | mitigate | CLOSED | `alembic/versions/038_retire_legacy_sentinel.py` — override read via `context.get_x_argument` (L125), validated against `agents` (exists + `kind='fileserver'` + `revoked_at IS NULL`) BEFORE use via `_VALIDATE_OVERRIDE` (L79) + `.bindparams(id=override)` (L127); invalid → `RuntimeError` (L128-129). Reattribution SQL parameterized on `:target` via `.bindparams(target=...)` (L152/L160/L161). Grep confirms no f-string SQL interpolation (`{target}`/`{override}` absent). |
| T-89-02-02 | Tampering/Repudiation | mitigate | CLOSED | `_resolve_target` (L119-138): auto-detect 0 rows → raise (L133-134), >1 → raise with `-x reattribute_to` guidance (L135-137), exactly 1 → use. COUNT=0 gate `_COUNT_REMAINING` asserted before sentinel DELETE (L163-165). Single transaction rolls back on any raise. |
| T-89-02-03 | DoS/data-loss | mitigate | CLOSED | Whole `upgrade()` body (L141-167) runs in one Alembic transaction; every abort branch (invalid override, 0/>1 fileserver, CR-01 collision guard L153-158, COUNT!=0 L164-165) raises before `_DELETE_SENTINEL` (L167) is reached. |
| T-89-02-04 | DoS | mitigate | CLOSED | Reattribute-before-delete ordering: `_REATTRIBUTE_FILES`/`_REATTRIBUTE_SCAN_BATCHES` (L160-161) run before `_DELETE_SENTINEL` (L167); COUNT=0 gate (L163-165) proves no `files`/`scan_batches` still reference the sentinel, so the RESTRICT FK is satisfiable at DELETE time. |
| T-89-02-05 | Tampering | mitigate | CLOSED | `_DELETE_LEGACY_LIVE_BATCH` (L84, `status = 'live'`) executed FIRST (L147), before the bulk `UPDATE scan_batches` — prevents a second live row for the target under `uq_scan_batches_agent_id_live`. |
| T-89-01-SC | Tampering (supply chain) | accept | CLOSED | Accepted risk — see log below. |
| T-89-02-SC | Tampering (supply chain) | accept | CLOSED | Accepted risk — see log below. |

## Accepted Risks Log

| Threat ID | Component | Rationale | Accepted |
|-----------|-----------|-----------|----------|
| T-89-01-SC | package installs (Plan 01) | Plan 01 is pure source deletion + model-code edit — no packages installed. RESEARCH Package Legitimacy Audit = N/A. No new dependency attack surface. | 2026-07-11 |
| T-89-02-SC | package installs (Plan 02) | Plan 02 uses only existing `alembic`/`sqlalchemy` (no new deps). RESEARCH Package Legitimacy Audit = N/A. No new dependency attack surface. | 2026-07-11 |

## Unregistered Flags

None. Neither `89-01-SUMMARY.md` nor `89-02-SUMMARY.md` declares a `## Threat Flags` section; no new attack surface appeared during implementation that lacks a threat mapping.

## Notes (advisory, out of audit scope)

The code-review gate (`89-REVIEW.md`) recorded two critical findings, both dispositioned before this audit:
- **CR-01** (composite-UQ `uq_files_agent_id_original_path` reattribution collision) — FIXED. The `_FILES_PATH_COLLISION` pre-flight guard (038 L95-100, L152-158) aborts with operator guidance + rollback instead of an opaque `IntegrityError`. This strengthens T-89-02-03/04 and is present in the audited code.
- **CR-02** (038 aborts `upgrade head` on a fileserver-less fresh DB) — ACCEPTED as designed (locked decision D-01: 0 fileservers → abort). Not a threat-register item; not a security gap. Advisory WR-01/02/03 and IN-01/02 remain open as future polish and do not affect any declared mitigation.

---
_Audited by gsd-security-auditor. Implementation files were not modified._
