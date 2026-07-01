---
phase: 61-full-record-k-agents
plan: 04
subsystem: admin-ui
tags: [record-03, agents-page, cloudjob, kueue, htmx, jinja2, degrade-safe, kdeploy-04]

# Dependency graph
requires:
  - phase: 61-full-record-k-agents
    plan: 01
    provides: "test_record_palette_agents.py RED scaffold (test_compute_lane_liveness_states + test_agents_two_sections_never_dead); seed_cloud_jobs fixture; classify_compute_lanes() signature home in phaze.services.agent_liveness"
  - phase: 29-agents-page
    provides: "admin_agents.py _load_agents/page/table_partial + agent_liveness.classify/sort_key + agents_table.html 5s self-poll (reused verbatim as Section 1)"
provides:
  - "classify_compute_lanes(session) -> tuple[str, int] — read-only, degrade-safe CloudJob aggregation (ACTIVE/WAITING/IDLE, never DEAD)"
  - "Two-section Agents page: Section 1 heartbeating agents (local/A1) + Section 2 ephemeral k8s compute/burst lanes, both riding the existing 5s _table self-poll"
  - "admin/partials/compute_lanes.html — the Section 2 lane card + ephemeral NOTE"
affects: [61-05-empty-state]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Degrade-safe read-only count aggregation in a services module (mirrors pipeline.get_inadmissible_count try/except → default), injected server-side onto the render"
    - "Single HTMX swap target wrapping TWO sub-sections so both refresh on one poll (two sibling roots would duplicate on outerHTML)"

key-files:
  created:
    - "src/phaze/templates/admin/partials/compute_lanes.html"
  modified:
    - "src/phaze/services/agent_liveness.py"
    - "src/phaze/routers/admin_agents.py"
    - "src/phaze/templates/admin/agents.html"
    - "src/phaze/templates/admin/partials/agents_table.html"

key-decisions:
  - "Section 2 lives INSIDE the single #agents-table-section swap target (not a sibling section) — hx-swap=outerHTML would duplicate two sibling roots on each poll. One root wrapping both sub-sections = both refresh on the existing 5s loop, zero new hx-trigger (RESEARCH OQ-1)."
  - "NOTE prose lowercases 'dead' ('never as a perpetually-dead agent') vs the UI-SPEC's 'perpetually-DEAD' — the Wave-0 contract test asserts no 'DEAD' substring anywhere in Section 2 (the never-DEAD invariant). Meaning is preserved; the executable gate wins over editorial casing."
  - "oob_counts stays off (plan interfaces / Pitfall 5); live refresh rides the _table poll rather than the UI-SPEC's /pipeline/stats OOB fanout — the plan overrides the UI-SPEC on transport."

requirements-completed: [RECORD-03]

# Metrics
duration: ~10min
completed: 2026-07-01
---

# Phase 61 Plan 04: Two-section Agents page (heartbeating + ephemeral compute lanes) Summary

**Turned the Agents page into the RECORD-03 two-section surface — Section 1 the existing heartbeating agents (local/A1) reused verbatim, Section 2 a distinct "Compute / burst lanes · ephemeral" k8s section driven by a new read-only `classify_compute_lanes()` CloudJob aggregation (Active/Waiting/Idle, never a perpetually-DEAD agent), both refreshing on the existing single 5s self-poll.**

## Performance

- **Duration:** ~10 min
- **Completed:** 2026-07-01
- **Tasks:** 2
- **Files:** 5 (1 created, 4 modified) + 1 SUMMARY

## Accomplishments
- **Task 1 — `classify_compute_lanes()` (TDD green).** Added `async def classify_compute_lanes(session) -> tuple[str, int]` to `services/agent_liveness.py`, mirroring the degrade-safe `try/except → default` count shape of `pipeline.get_inadmissible_count`/`get_cloud_phase_counts`. Precedence: `("ACTIVE", running)` when ≥1 `CloudJob.status == running`; else `("WAITING", waiting)` when ≥1 `submitted` AND `inadmissible`; else `("IDLE", 0)`. On any `SQLAlchemyError` it rolls back and returns `("IDLE", 0)` — a DB hiccup NEVER paints the lane DEAD/red (T-61-08 / KDEPLOY-04 / D-07).
- **Task 2 — two-section page + router wiring.** Restructured `agents_table.html`'s single `#agents-table-section` swap target to wrap Section 1 (the verbatim `_load_agents` grid + footers; `classify`/`sort_key` untouched) and Section 2 (`compute_lanes.html`). New `compute_lanes.html` renders the `⎈ k8s burst` lane card with the LOCKED dot+label per state (emerald "ACTIVE · {n} workloads" / amber "WAITING · quota" `role="alert"` / gray "IDLE"), an in-flight breakdown when Active/Waiting, and the amber ephemeral NOTE. Wired `admin_agents.py` to call `classify_compute_lanes(session)` and inject `(compute_lane_state, compute_lane_count)` into BOTH the full page and the `/_table` partial contexts, so Section 2 rides the existing 5s poll with no new loop. Replaced the static Phase-56 k8s note with this LIVE section.

## Task Commits

Each task was committed atomically:

1. **Task 1: classify_compute_lanes() degrade-safe CloudJob liveness** — `15af548` (feat)
2. **Task 2: two-section Agents page — heartbeating + ephemeral compute lanes** — `1a02f8e` (feat)

## Files Created/Modified
- `src/phaze/services/agent_liveness.py` — added `classify_compute_lanes()` + `ComputeLaneState` literal; updated the module docstring (the classify/sort_key pure-function note now scopes to those two; documents the one DB-touching degrade-safe read).
- `src/phaze/routers/admin_agents.py` — imports + calls `classify_compute_lanes`; injects `compute_lane_state`/`compute_lane_count` into `page` and `table_partial`.
- `src/phaze/templates/admin/partials/compute_lanes.html` — NEW Section 2 lane card + NOTE (`id="compute-lanes"`).
- `src/phaze/templates/admin/partials/agents_table.html` — swap target now wraps Section 1 card + `{% include compute_lanes.html %}`; Section 1 gains a visible "Agents · heartbeating" eyebrow (was sr-only "Registered agents").
- `src/phaze/templates/admin/agents.html` — removed the static Phase-56 k8s note; the two-section layout (incl. the literal "Compute / burst lanes" reference) now renders through the polled partial.

## Decisions Made
- **Section 2 nests inside the single swap target, not as a sibling section.** `hx-swap="outerHTML"` on `#agents-table-section` replaces that one element with the whole returned fragment; two sibling roots would accumulate/duplicate on each poll tick. One root wrapping both sub-sections keeps a single 5s loop refreshing both (RESEARCH OQ-1, plan interfaces).
- **NOTE copy lowercases "dead".** The UI-SPEC §Copywriting NOTE reads "never as a perpetually-DEAD agent"; the Wave-0 contract test `test_agents_two_sections_never_dead` asserts `"DEAD" not in` the Section-2 fragment (the never-DEAD invariant, KDEPLOY-04). Rendered as "perpetually-dead agent" — identical meaning, satisfies the executable gate. See Deviations.
- **Transport = the `_table` poll, not the `/pipeline/stats` OOB fanout.** Plan interfaces pin `oob_counts` off (Pitfall 5) and route the live refresh through the existing self-poll; this plan follows the plan over the UI-SPEC's OOB suggestion.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking conflict] UI-SPEC NOTE copy vs. the never-DEAD contract test**
- **Found during:** Task 2
- **Issue:** The verbatim UI-SPEC NOTE ("never as a perpetually-**DEAD** agent") contains the uppercase substring `DEAD`, which the Wave-0 gate `test_agents_two_sections_never_dead` forbids anywhere after the `id="compute-lanes"` marker (`assert "DEAD" not in compute_section`). Emitting the verbatim copy would fail the plan's own Task-2 verify.
- **Fix:** Lowercased the single word in the rendered prose → "never as a perpetually-dead agent." Meaning and the KDEPLOY-04 intent are preserved; the never-DEAD-state invariant now holds literally.
- **Files modified:** `src/phaze/templates/admin/partials/compute_lanes.html`
- **Commit:** `1a02f8e`

## Issues Encountered
- Running `pytest tests/test_record_palette_agents.py -k "compute or agents"` (the plan's verification line) sweeps the ENTIRE file, because the filename `test_record_palette_agents` contains the substring "agents" — pytest `-k` matches the module name in the node id. In this isolated Wave-2 worktree (base = 61-01 only), that pulls in the still-RED tests owned by the not-yet-landed sibling plans (61-02 record, 61-03 palette/`distinct_artists`, 61-05 empty-state), which fail as expected. The two RECORD-03 tests this plan owns both pass; they turn green here and the full-file sweep goes green once the sibling Wave-2 plans land.
- The combined run of `test_record_palette_agents.py` + `test_routers/test_admin_agents.py` surfaced pre-existing cross-file fixture-isolation IntegrityErrors on `ck_agents_kind_enum` (agent seeding). Both files pass in isolation; unrelated to this plan's additive changes (matches the known "Local full-suite colima flake" note).

## Verification
- `test_compute_lane_liveness_states` — PASS (IDLE→WAITING→ACTIVE precedence + counts).
- `test_agents_two_sections_never_dead` — PASS (`id="compute-lanes"` present, a state rendered, no "DEAD" in Section 2).
- `tests/test_routers/test_admin_agents.py` — 14 passed (Section 1 reuse verbatim: 5-state pills incl. DEAD in Section 1, kind badges, sort order, empty state, BLOCKER-2 all intact).
- `tests/test_shell_routes.py` — passed (no regression from the template restructure).
- `grep -rni "dead" src/phaze/templates/admin/` — the only rendered "DEAD" is Section 1's `_status_pill.html` (a real heartbeating agent's dead state); Section 2 emits none.
- `ruff check` + `ruff format` + `mypy` (strict) — clean on both edited source files; pre-commit passed on both commits (no `--no-verify`).

## Threat Surface
- **T-61-08 (false-alarm DoS)** mitigated: `classify_compute_lanes` degrades to `("IDLE", 0)` on `SQLAlchemyError` (rollback + warn) — never DEAD/red.
- **T-61-01 (tampering)** mitigated: all DB-sourced values (`compute_lane_count`) render through Jinja autoescape; status cells carry a word (ACTIVE/WAITING/IDLE), not hue-only (WCAG 1.4.1).
- No new security surface beyond the threat model.

## Known Stubs
None — Section 2 is wired to live `CloudJob` counts via `classify_compute_lanes`; no placeholder/hardcoded lane data.

## Self-Check: PASSED
- `src/phaze/templates/admin/partials/compute_lanes.html` present on disk.
- `classify_compute_lanes` present in `agent_liveness.py`; `compute_lane_state` injected in `admin_agents.py`; "Compute / burst lanes" present in `agents.html`.
- Both task commits present in git log (`15af548`, `1a02f8e`).
- Both RECORD-03 tests pass; admin_agents + shell_routes regressions green (20 passed combined).

---
*Phase: 61-full-record-k-agents*
*Completed: 2026-07-01*
