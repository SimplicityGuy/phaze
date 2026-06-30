---
phase: 58
slug: enrich-analyze-workspaces
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-30
---

# Phase 58 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Phase 58 (enrich-analyze-workspaces) is a presentation-only phase: Jinja2 workspace
fragments (Discover / Metadata / Fingerprint / Analyze) rendered into the v7.0 shell's
`#stage-workspace` swap target, FastAPI shell-router context loads, and ONE new read-only
service query (`get_analyze_stage_files`). NO new endpoint, payload, enqueue path, auth
surface, schema, or package install. The threat register was authored at plan time across
the four plans (`register_authored_at_plan_time: true`); it is consolidated and deduped
below. Each `mitigate` disposition has been verified to exist in the implemented code
(grep-confirmed against the cited file:line, not accepted on documentation or intent); the
single `accept` disposition (T-58-SC) is logged in Accepted Risks.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| browser → shell route | rail navigation + the 5s poll cross here; `{stage}` is the only path-shaped input | URL path segment |
| browser → POST triggers | SCAN / RECOVER / EXTRACT ALL / FINGERPRINT ALL bulk-enqueue actions | existing dedup-keyed endpoints |
| DB → render | scan paths, agent names, file paths, tags, window counts, `cloud_phase` flow into workspace/file-table cells | strings + server-computed ints |
| /pipeline/stats poll → DOM | server-computed OOB count seeds land on pre-mounted `dag-seed-<key>` placeholders | numeric ints |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-57-01 | Tampering | shell.py STAGE_PARTIALS stage resolution | mitigate | `STAGE_PARTIALS` is a static literal dict (`shell.py:55-77`); `stage_partial` always comes from `STAGE_PARTIALS[stage]` (`shell.py:99,105`), never spliced/concatenated into a template path; unknown stage 404s (`shell.py:149-150`, `if stage not in STAGE_PARTIALS: raise HTTPException(status_code=404)`) | CLOSED |
| T-58-XSS | Tampering / Info-disclosure | DB-derived path/agent/tag cells in the workspace + file tables | mitigate | Jinja autoescape on; `_file_table.html:44` renders `{{ cell.text }}` (autoescaped) in `font-mono text-xs truncate` cells with `title="{{ cell.title }}"`; NO `\| safe` on any user/DB-derived value — the only `\| safe` token in the Phase-58 partials is the `_file_table.html:13` doc comment that FORBIDS it; lane/window values are server-computed ints / static strings | CLOSED |
| T-58-POLL | DoS (self-inflicted) | chrome poll + reused recent-scans self-poll | mitigate | Exactly ONE `hx-get="/pipeline/stats"` poll element in shell chrome (`shell.html:188`) outside `#stage-workspace`, with `hx-trigger="every 5s [document.visibilityState === 'visible']"` (`shell.html:189`) + a `visibilitychange` foreground-resume listener (`shell.html:253`); the reused recent-scans surface renders through the generic `_file_table.html` with its self-poll STRIPPED; no Phase-58 workspace partial carries a real `hx-trigger="every"`/`setInterval` (grep matches are all explanatory comments) | CLOSED |
| T-58-SEED | Tampering (UX integrity) | notYetEnriched / computeOnline OOB seed targets | mitigate | `_workspace_poll_seeds.html:67` pre-mounts `<p id="dag-seed-notYetEnriched">` and `:40` `<p id="dag-seed-computeOnline">` so the derived OOB seeds land (htmx OOB swaps apply only to pre-existing ids — no silent stick-at-0); both keys also seeded to int `0` in the shell's own Alpine store (`shell.html:139`, W-1 fix) | CLOSED |
| T-58-ENQ | DoS (self-inflicted) | EXTRACT ALL / FINGERPRINT ALL / RECOVER bulk enqueue | mitigate | R-4 busy-gate + confirm on each existing dedup-keyed endpoint: metadata `hx-post="/pipeline/extract-metadata"` + `hx-confirm` + `:disabled="$store.pipeline.metadataBusy > 0"` (`metadata_workspace.html:28-31`); fingerprint `hx-post="/pipeline/fingerprint"` + `hx-confirm` + `:disabled="...fingerprintBusy > 0"` (`fingerprint_workspace.html:26-29`); RECOVER `hx-post="/pipeline/recover"` + `hx-confirm` + `:disabled="...agentBusy > 0"` (`discover_workspace.html:27-31`); no new enqueue path | CLOSED |
| T-58-DEGRADE | DoS (self-inflicted) | get_analyze_stage_files on the hot 5s poll | mitigate | Degrade-safe nested SAVEPOINT: `async with session.begin_nested():` (`services/pipeline.py:791`) wrapping the read; `except Exception: logger.warning("analyze_stage_files_degraded", ...); return []` (`services/pipeline.py:811-813`) — never 500s the page/poll (mirrors `count_active_agents`) | CLOSED |
| T-58-ALERT | Tampering (UX integrity) | Kueue quota-wait vs Inadmissible distinction | mitigate | The fault distinction is preserved by VERBATIM reuse: `inadmissible_card.html:22` and `localqueue_card.html:22` carry `role="alert"`; `admission_state_card.html` carries NO `role="alert"` (0 occurrences) — the recoverable-vs-fault distinction is not collapsed | CLOSED |
| T-58-SC | Tampering (supply chain) | npm/pip installs | accept | Phase 58 installs NO packages (no registry interaction). See Accepted Risks. | CLOSED (accepted) |

---

## Accepted Risks

### T-58-SC — Supply-chain (no installs this phase)

**Disposition:** accept. Phase 58 is a presentation-only IA rewrite over existing
routers/services/templates. It adds ZERO dependencies — no `pyproject.toml` /
`uv.lock` change, no npm/pip/registry interaction (RESEARCH Package Legitimacy Audit:
N/A). The only build artifact touched is the gitignored, locally-regenerated
`src/phaze/static/css/app.css` (`just tailwind`, not committed). There is therefore no
new supply-chain attack surface introduced by this phase. Accepted as the disposition the
threat register declares; no mitigation code is expected or required.

---

## Unregistered Flags

None. The `## Threat Flags` sections of the plan summaries report **None** (58-03,
58-04 explicit; 58-01, 58-02 introduce no new endpoint/auth/file-access/schema surface).
No new attack surface appeared during implementation that lacks a threat-register mapping.

---

## Audit Trail

- Register source: `58-01-PLAN.md` … `58-04-PLAN.md` `<threat_model>` blocks (deduped).
- Verification method: grep-confirmed against implemented source (file:line cited per row);
  no mitigation accepted on documentation/intent.
- Implementation files verified read-only (no edits): `routers/shell.py`,
  `services/pipeline.py`, `templates/shell/shell.html`,
  `templates/pipeline/partials/{_workspace_scaffold,_file_table,_workspace_poll_seeds,discover_workspace,metadata_workspace,fingerprint_workspace,analyze_workspace,_lane_card,admission_state_card,inadmissible_card,localqueue_card}.html`.
- Cross-checked against `58-VERIFICATION.md` (status passed, 5/5, live UAT 2026-06-30).
- Config: asvs_level 1, block_on high. Threats open: 0 → no block.

_Audited: 2026-06-30 — gsd-security-auditor_
