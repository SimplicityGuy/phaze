# Phase 76 — compute-push-hardening — Security Audit

**Audit mode:** verify-mitigations (threat register authored at PLAN time; no new-threat scan)
**ASVS Level:** 1
**block_on:** high
**Threats closed:** 6/6
**threats_open:** 0
**Verdict:** SECURED

## Threat Verification

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| WR-01 (74-REVIEW) | Tampering / DoS | mitigate | CLOSED | `src/phaze/services/backends.py:668-672` — `_probe_availability` is a sequential `for backend in backends` loop that fully awaits each `await _probe_one(session, backend)` before the next. No `asyncio.gather` in code (only in the docstring at L662 describing the superseded design). Docstring L651-666 states a STRUCTURAL by-construction guarantee (no "arbiter"/"empirical"/"in practice" phrasing). Per-probe `asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)` (L644) and post-fan-out `session.rollback()` (L708) preserved unchanged. |
| T-76-01-SC | Tampering (uv installs) | accept | CLOSED | `git diff main...HEAD -- pyproject.toml uv.lock` empty — no dependency files changed. |
| AR-73-02 / T-73-13 / WR-04 | Tampering / EoP | mitigate | CLOSED | `src/phaze/routers/agent_push.py:240` — RMW serialized by `await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))` (CR-01 advisory-lock form, NOT `.with_for_update()`; no `.with_for_update()` present in code, only referenced in the explanatory comment L230). Reporter-auth gate intact: L216-223 raises 403 when `agent.id != backend.agent_ref`. CR-01 PUSHING-only CAS spill guard intact: L261 `update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)` with `rowcount == 0` idempotent no-op (L264-272). Cap boundary check `next_attempt > settings.push_max_attempts` at L248. |
| T-76-02-SC | Tampering (uv installs) | accept | CLOSED | Same phase diff — `pyproject.toml`/`uv.lock` unchanged. |
| AR-30-03 / Phase-30 REVIEW IN-01 | Tampering / Info Disclosure | mitigate | CLOSED | Both boundaries constrained with the canonical agent-id shape: `src/phaze/routers/tracklists.py:282` `agent_id: str = Query(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)`; `src/phaze/routers/pipeline_scans.py:153` `agent_id: Annotated[str, Query(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)]` with `Query` added to the fastapi import (L32). Malformed id → 422 at the HTTP boundary before `task_router.queue_for(agent_id)` / `session.get(Agent, agent_id)`. |
| T-76-03-SC | Tampering (uv installs) | accept | CLOSED | Same phase diff — `pyproject.toml`/`uv.lock` unchanged. |

## Accepted Risks Log

| Threat ID | Rationale | Verification |
|-----------|-----------|--------------|
| T-76-01-SC / T-76-02-SC / T-76-03-SC | No package installs in any Phase 76 plan (D-10). No legitimacy checkpoint required. | `git diff main...HEAD` touches only 4 implementation files, 4 test files, and planning docs — `pyproject.toml` and `uv.lock` are NOT in the changeset. |

## Unregistered Flags

None. SUMMARY `## Threat Flags` sections declare no new attack surface (76-03 explicitly: "the change reduces the input surface at existing boundaries; no new endpoints, auth paths, file access, or schema changes"). 76-01 and 76-02 introduce no new endpoints or auth paths.

## Notes

- The as-shipped mechanism for AR-73-02 / T-73-13 / WR-04 was corrected by CR-01 from `.with_for_update()` (which self-deadlocked the `apply_deterministic_key` before_enqueue hook on the same ledger row) to a transaction-scoped `pg_advisory_xact_lock(hashtext(ledger_key))`. The advisory-lock form is confirmed present in code; the row-lock form is confirmed absent from code. Same serialize-the-RMW intent, different lock space — verified per the threat register's CR-01 note.
- All verification is grep/read against implemented code, not documentation intent. Implementation and test files were not modified.
