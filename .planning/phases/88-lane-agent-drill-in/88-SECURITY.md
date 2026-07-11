# SECURITY.md — Phase 88 (lane-agent-drill-in)

**Audited:** 2026-07-11
**ASVS Level:** 1
**Register:** authored at plan time (register_authored_at_plan_time: true) — each declared mitigation verified against implemented code.
**Disposition:** SECURED — 11/11 threats CLOSED. The 1 non-blocking WARNING (guard-coverage gap on T-88-10) was RESOLVED during this secure-phase run — see the WARNING section.
**block_on:** high — no high-severity open threat; phase may ship.

Implementation files were treated as READ-ONLY; no implementation file was modified during this audit.

---

## Threat Verification (mitigate)

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-88-01 | Tampering/Info-disclosure (`?lane=`/`?agent=` reflected into highlight + aria-label) | mitigate | CLOSED | Raw param never reflected directly. Router resolves via lookup-in-known-set: `routers/pipeline.py:758` (`selected_lane = lane if any(one.get("id") == lane for one in lanes) else None`) and `routers/admin_agents.py:70-76` (`_resolve_selected_agent`), threaded at `:141`,`:176`. Templates compare only the resolved value: `_lane_card.html:64,71` (`selected_lane == lane.id`), `agents_table.html:67,74`. All ids/kinds/names Jinja-autoescaped — no `|safe`/`|tojson` (grep-confirmed: only comment mentions). aria-labels use autoescaped `lane.kind`/`lane.id`/`agent.name`. |
| T-88-03 | Elevation/Info-disclosure (IDOR/path, `{backend_id}`) | mitigate | CLOSED | `routers/pipeline.py:809-823` — `get_backend_lane_snapshot` then `next((one for one in lanes if one["id"] == backend_id), None)`; unknown id → 200 "Lane offline" HTML fragment (`_lane_detail.html:117-124`), never 500/JSON/HTTPException, never a raw-param-driven read. |
| T-88-04 | Info-disclosure (secret leak in `_lane_detail.html`) | mitigate | CLOSED | Only secret-free snapshot scalars rendered: `_lane_detail.html:45-99` renders `lane.{kind,id,rank,cap,in_flight,available,quota_wait,inadmissible}` + CloudJob `file_id.hex[:8]`/`updated_at`. No `backend.config`/`SecretStr`/kube token. Helpers `backends.py:815-866` return only CloudJob scalars + broker counts (module comment `:805-808`). |
| T-88-05 | Tampering (reflected XSS, `backend_id`/`lane.kind`) | mitigate | CLOSED | Jinja autoescape on; no `|safe`/`|tojson` in `_lane_detail.html` / `_lane_card.html` (grep-confirmed; matches are only in `never |safe` comments). |
| T-88-06 | DoS (unbounded read on 5s tick, completions/queue depths) | mitigate | CLOSED | `backends.py:812` `LANE_RECENT_N = 20`; `get_lane_recent_completions:828-836` `WHERE status==succeeded ORDER BY updated_at DESC LIMIT limit` + guarded rollback → `[]` (`:838-844`); `get_lane_queue_depths:858-866` per-tier `try/except → 0`. No whole-corpus scan. |
| T-88-07 | Elevation/Info-disclosure (IDOR/path, `{agent_id}`) | mitigate | CLOSED | `routers/admin_agents.py:210` `agent = await session.get(Agent, agent_id)`; None → friendly empty fragment at **200** (`:211-220`, `_agent_activity.html:24-29`), never 500/JSON. `Agent.id` is a String column so an unknown id returns None (no UUID-cast 500). WR-01 fix (200 not 404) present + documented at `:200-204`. |
| T-88-08 | DoS (unbounded per-agent aggregate/scans/queues on 5s tick) | mitigate | CLOSED | `_agent_stage_buckets` (`services/pipeline.py:365-415`) one indexed inner-subquery `GROUP BY stage_status_case` per stage (NOT row materialization), wrapped in `session.begin_nested()` SAVEPOINT degrade to all-zero (`:407-411`, CR-01 fix). `get_agent_recent_scans:454-471` `LIMIT` + `begin_nested()` → `[]`. `get_agent_lane_depths:423-451` per-lane `try/except → 0`. SAVEPOINT (not plain rollback) preserves the caller's loaded `agent` ORM object. |
| T-88-09 | Tampering (reflected XSS, `agent.name`/`agent_id`/`agent.kind`) | mitigate | CLOSED | Jinja autoescape on; `_agent_activity.html` renders `agent.name`/`agent.id` (`:56,62,76`) with no `|safe`/`|tojson` (grep-confirmed; only in `never |safe` comment `:22`). |
| T-88-10 | Info-disclosure (stale/raw-state leak, agent grouping source) | CLOSED (see WARNING) | CLOSED | Substantive mitigation present: `_agent_stage_buckets` composes the derived `stage_status_case` (`services/pipeline.py:396-400`), never `FileRecord.state`; endpoint passes only `buckets`/`queue_depths`/`recent_scans`/`agent` (no FileRecord); `_agent_activity.html` renders no `.state` (grep-confirmed; only in comment `:23`). Guard `tests/shared/test_no_raw_state_render.py` present and self-tested — but see WARNING below (coverage gap). |

## Threat Verification (accept)

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-88-02 | DoS (5s poll re-render carrying the param) | accept (low) | CLOSED | Acceptance rationale holds: O(1) membership check over the already-loaded snapshot/agent list, no extra query — `pipeline.py:758` iterates `lanes` (already fetched `:755`), `admin_agents.py:76` iterates `agents` (already fetched `:94-95`). Existing poll bound unchanged. |
| T-88-SC | Tampering (package installs) | accept (none) | CLOSED | Zero packages installed across all three plans (`88-01/02/03-SUMMARY.md` frontmatter `tech-stack.added: []`). No supply-chain surface introduced. |

---

## WARNING — guard-coverage gap on T-88-10 (non-blocking) — ✅ RESOLVED

**Resolution (commit pending this secure-phase run):** `_SCANNED_DIRS` now includes `templates/admin/partials` (`test_no_raw_state_render.py:36-40`), so the guard covers the new agent-activity surface. Mutation-verified: planting `{{ agent.state }}` in `_agent_activity.html` turns the guard RED; removing it restores GREEN. The durability gap below is closed — retained for the audit trail.

**Classification:** WARNING (durability/regression gap, not a live disclosure). Does not meet `block_on: high`.

The declared T-88-10 mitigation cites the regression guard `tests/shared/test_no_raw_state_render.py`. That guard scans only `_SCANNED_DIRS = (templates/pipeline/partials, templates/record)` (`test_no_raw_state_render.py:36-39`). The new agent body `_agent_activity.html` lives in `templates/admin/partials/` — **outside the scanned set**. The guard therefore does NOT cover the new attack surface: a future edit adding `{{ f.state }}` (or a cell-dict `{'text': f.state}`) to `_agent_activity.html` would leave the guard GREEN.

The **live** threat is closed today — the template genuinely renders no `.state` and the endpoint passes no `FileRecord` — so this is a coverage gap in the regression teeth, not an open info-disclosure. Consistent with the project maxim "'Closed' in SECURITY.md ≠ tested; mutation-test your guard tests."

**Suggested remediation (owner: implementation, follow-up):** extend `_SCANNED_DIRS` to include `templates/admin/partials` (or the specific agent-activity path) and re-run the mutation check (plant `{{ f.state }}` in `_agent_activity.html`, confirm RED, restore). This is a test-file change only; it does not modify implementation.

---

## Code-review fix confirmation (commit 3665d328)

The audit confirms the REVIEW.md fixes are present in the audited tree:
- **CR-01** (T-88-08 SAVEPOINT): `_agent_stage_buckets` uses `async with session.begin_nested()` (`services/pipeline.py:407`), not a plain rollback — the caller's loaded `agent` is not expired.
- **CR-02** (self-removing own-tick): `_agent_activity.html:150-161` — the 5s poll is on a dedicated child with `x-effect="if (armed && !open ...) window.htmx.remove($el)"`, matching `_lane_detail.html:109-115`; the body root no longer carries the poll.
- **WR-01** (200 not-found): `agent_activity` returns the not-found fragment at 200 (`admin_agents.py:216-220`); documented at `:200-204`.

---

## Unregistered Flags

None. `88-02-SUMMARY.md ## Threat Flags` = "None"; `88-03-SUMMARY.md ## Threat Surface` = "No new surface beyond the plan's `<threat_model>` … No threat_flag needed"; `88-01-SUMMARY.md` declares no new network/auth/schema surface. No new attack surface appeared during implementation without a threat mapping.

---

## Accepted Risks Log

| Risk | Disposition | Rationale |
|------|-------------|-----------|
| T-88-02 — 5s poll re-render carrying `?lane=`/`?agent=` | accept (low) | O(1) membership check against already-loaded lists; no extra query; poll bound unchanged. |
| T-88-SC — supply-chain via package installs | accept (none) | Zero packages installed this phase (all three plans `tech-stack.added: []`). |
| Operator console has no authentication | accept (documented posture) | Private-LAN admin console; documented at `admin_agents.py:16-19` (consistent with pipeline.py precedent). Pre-existing, not introduced by Phase 88. |
