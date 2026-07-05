---
phase: 71
slug: deployment-config-docs-n-lane-ui
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-05
audit_trail:
  - date: 2026-07-05
    run_by: gsd-security-auditor
    threats_total: 13
    closed: 13
    open: 0
    register_authored_at: plan-time
    result: SECURED
---

# Phase 71 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan-time across the five 71-0N-PLAN.md `<threat_model>` blocks;
> this audit VERIFIES each declared mitigation exists in the implemented code with file:line
> evidence (no blind scan for new threats).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| remote Kueue cluster → control-plane | `KueueBackend.is_available` makes a live kr8s call; a hung/slow cluster is untrusted latency crossing into the shared 5s poll | probe latency / availability bool |
| DB row values → rendered/logged output | `cloud_job` / registry values flow into the lane snapshot dicts and probe logs | backend id/kind/counts (secret-free by construction) |
| control row → routing decision | the `route_control.force_local` flag steers whether cloud/Kueue dispatch happens | boolean routing override |
| browser → POST /pipeline/routing/force-local | untrusted form input crosses into a state-changing write | `engage` boolean form field |
| internal reverse-proxy realm → endpoint | same internal-realm trust as all `/pipeline/*` controls (T-37-04) | operator toggle action |
| registry-declared lane values → rendered HTML / Alpine JS | `id`/`kind` and counts flow into the card template | operator-declared strings + ints |
| documentation → operator | docs describe secret handling but must never embed secret values | prose / config examples |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-71-01 | Information disclosure | lane snapshot dicts + card render + probe error logs | mitigate | 8-key-only lane dict (`backends.py:626-634`); probe-offline log emits `backend_id` ONLY, never config/SecretStr/token (`backends.py:571`); card renders only the eight scalars (`_lane_card.html:57-84`) | closed |
| T-71-02 | Denial of service | `_probe_availability` fan-out over a hung Kueue probe | mitigate | per-probe `asyncio.wait_for(_PROBE_TIMEOUT_SEC=1.5s)` + try/except→offline (`backends.py:569-573`); concurrent `asyncio.gather` bounds whole fan-out to ~one timeout (`backends.py:585`); WR-01 post-fan-out `session.rollback()` isolates a DB-layer probe error to one lane (`backends.py:622`) | closed |
| T-71-03 | Denial of service | snapshot / `get_route_control` raising into `/pipeline/stats` poll + drain | mitigate | snapshot never-500 degrade to `[]` with guarded double-rollback (`backends.py:637-643`); `get_route_control` degrade to `False` with guarded double-rollback, never raises (`route_control.py:38-47`) | closed |
| T-71-04 | Tampering | migration 031 seed / control row | mitigate | bound-param INSERT, no interpolation (`031_add_route_control.py:48-51`); `Boolean server_default false` (`031:40`, `route_control.py:32`); single `'global'` PK row | closed |
| T-71-05 | Tampering / XSS | lane id/kind in card + force-local pill/toast render | mitigate | Jinja autoescape on; lane `id`/`kind` render as HTML text, no Alpine JS context, no `|tojson` bypass needed (`_lane_card.html:57`); pill `hx-vals` interpolates only a server boolean (`_force_local_pill.html:22`); toast is server-constant text in a polite `aria-live` region (`_force_local_pill.html:39-42`) | closed |
| T-71-06 | Denial of service | initial render emitting OOB / missing DOM target | mitigate | `hx-swap-oob` gated behind `{% if oob %}` (`_analyze_lanes.html:20`); initial include passes no oob (`analyze_workspace.html:47`); poll re-includes with `oob=True` (`stats_bar.html:110`); UAT-01 hidden `#analyze-lanes` sink for non-Analyze stages (`_workspace_poll_seeds.html:102`) | closed |
| T-71-07 | Tampering (input) | force-local POST body | mitigate | `engage: Annotated[bool, Form()]` boolean coercion, no free-text (`routing.py:50`); DB column defaults false | closed |
| T-71-08 | Elevation of privilege (unintended cloud dispatch) | routing gate sites | mitigate | drain gate (`release_awaiting_cloud.py:130`); duration-router + backfill gates (`pipeline.py:396,718,793`); `select_backend` stays pure — zero `route_control` reference in `backend_selection.py` (grep-confirmed) | closed |
| T-71-10 | Spoofing / state lie | optimistic client mutation | mitigate | authoritative pill state from the JUST-COMMITTED row (`routing.py:70`); no optimistic client flip — template renders the server boolean only (`_force_local_pill.html:19,22`) | closed |
| T-71-11 | Information disclosure | runbook / configuration docs | mitigate | `_FILE`/`*_file` secrets referenced by NAME only; hermetic guard forbids literal PEM/token/inline-secret shapes and passes (`test_docs_beui03.py:110-126`, 9/9 green) | closed |
| T-71-12 | Tampering (stale guidance) | contradictory `cloud_target` docs | mitigate | `configuration.md` states `PHAZE_CLOUD_TARGET` was REMOVED in Phase 67 + carries the 1:1→`backends` equivalence; hermetic guard locks it (`test_docs_beui03.py:132-160`, green) | closed |
| T-71-09 | Denial of service (stall lever) | force-local toggle | accept | see Accepted Risks Log — reverts only to LOCAL (safe, reversible, no work lost), behind the internal realm, no new surface beyond the existing pause control | closed |
| T-71-SC | Tampering | package installs | accept | see Accepted Risks Log — zero packages installed this phase (milestone zero-new-deps); `git diff main...HEAD -- pyproject.toml uv.lock` is empty | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Unregistered Flags

None. No `## Threat Flags` section is present in any of the five 71-0N-SUMMARY.md files, and no new attack surface appeared during implementation that lacks a threat mapping. (The executor summaries carry `## Threat mitigations applied` narratives for T-71-01/02/03/05/06, all of which map to registered threats above.)

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-71-09 | T-71-09 | The force-local toggle only reverts routing to LOCAL — the safe direction. No in-flight work is lost, the action is fully reversible in one click, it sits behind the internal reverse-proxy realm (same trust boundary as the sibling per-stage pause/resume controls), and it adds no new attack surface beyond the existing pause lever. A misuse degrades throughput, not safety. | Robert (operator) | 2026-07-05 |
| AR-71-SC | T-71-SC | Zero packages were installed this phase (milestone zero-new-deps: docs + templates + one migration + one hermetic test). No dependency manifest changed (`git diff main...HEAD -- pyproject.toml uv.lock` empty), so there is no new supply-chain surface to vet. | Robert (operator) | 2026-07-05 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-05 | 13 | 13 | 0 | gsd-security-auditor |

Notes:
- Register authored at plan-time (all 5 plans carry `<threat_model>` blocks); this run verified each declared mitigation against the implemented code, not a blind vulnerability scan.
- WR-01 (code-review WARNING, fixed `fe1f0032`) STRENGTHENS T-71-02: the post-fan-out `session.rollback()` at `backends.py:622` extends per-lane isolation from probe timeouts to DB-layer probe errors.
- UAT-01 (fixed `1c0473b2`) added the hidden `#analyze-lanes` sink at `_workspace_poll_seeds.html:102`, reinforcing T-71-06 (no `htmx:oobErrorNoTarget` on non-Analyze stages / empty state).
- Docs threats (T-71-11/12) verified live: `uv run pytest tests/shared/core/test_docs_beui03.py` → 9 passed.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-05
