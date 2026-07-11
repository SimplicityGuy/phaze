---
phase: 88
slug: lane-agent-drill-in
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-11
---

# Phase 88 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (via `uv run`) + httpx AsyncClient for endpoint tests |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`); buckets `tests/buckets.json` |
| **Quick run command** | `uv run pytest tests/analyze/routers/test_lane_detail.py tests/agents/routers/test_agent_activity.py tests/integration/test_agent_stage_buckets.py` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~90 seconds (bucketed subset far less) |

**Test-DB footgun (MEMORY):** export BOTH `TEST_DATABASE_URL` (5433) and `MIGRATIONS_TEST_DATABASE_URL` — `just test-bucket` does not export the migration URL by default. The per-agent `GROUP BY stage_status_case` aggregate MUST run against real Postgres (5433), not SQLite, or Pitfall 2 (`GroupingError` — cannot GROUP BY the CASE expression directly) won't be caught. Routers' `get_session` never commits — assert from an INDEPENDENT session.

---

## Sampling Rate

- **After every task commit:** Run the quick command (stage-status aggregate + drill-in endpoint tests)
- **After every plan wave:** Run the full suite
- **Before `/gsd:verify-work`:** Full suite green + 90% coverage floor met
- **Max feedback latency:** ~90 seconds

---

## Per-Task Verification Map

> Task IDs are assigned at plan time (three D-08 seams: shared pane shell + triggers; lane body; agent body). Requirement-level rows below; the nyquist auditor refines to per-task rows after plans exist.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 88-01-XX | 88-01 | 1 | DRILL-03 | — | Trigger is `role=button` + Enter/Space; pane `role=region` non-modal; Esc dismiss clears `?param`; focus returns by stable id after a poll swap; both self-polls carry the pushed `?lane=`/`?agent=` via `hx-vals` (shell.html + agents_table.html) | api + markup assertion | `uv run pytest tests/analyze/routers/test_lane_drill_survival.py tests/agents/routers/test_agent_drill_survival.py` | ❌ W0 | ⬜ pending |
| 88-02-XX | 88-02 | 2 | DRILL-01 | — | `GET /pipeline/lanes/{backend_id}` returns kind-adaptive fields; kueue-only quota/inadmissible absent for local/compute; last-N recent completions newest-first; unknown backend_id → graceful empty, never 500 | api | `uv run pytest tests/analyze/routers/test_lane_detail.py` | ❌ W0 | ⬜ pending |
| 88-03-XX | 88-03 | 2 | DRILL-02 | — | `GET /admin/agents/{agent_id}/_activity` returns per-agent 6-stage bucket COUNTS via bounded `GROUP BY` aggregate (not row materialization); liveness + queue depths + recent scan batches; agent-owns-0-files → empty state, never 500 | api + query-correctness | `uv run pytest tests/agents/routers/test_agent_activity.py` | ❌ W0 | ⬜ pending |
| 88-03-XX | 88-03 | 2 | DRILL-02 | — | The per-agent aggregate materializes the `stage_status_case` label in an inner subquery then `GROUP BY`s it (Postgres GroupingError guard); counts filtered by `agent_id == X` match hand-computed fixture corpus | integration (real PG) | `uv run pytest tests/integration/test_agent_stage_buckets.py` | ❌ W0 | ⬜ pending |
| 88-02/03-XX | 88-02, 88-03 | 2 | DRILL-01/02 (D-00b) | — | Both endpoints + their live-refresh ticks degrade to 0/None via `_safe_count`/SAVEPOINT under a forced DB error rather than 500-ing the 5s poll | api (fault injection) | `uv run pytest tests/analyze/routers/test_lane_detail.py tests/agents/routers/test_agent_activity.py -k degrade` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/analyze/routers/test_lane_detail.py` + `test_lane_drill_survival.py` — lane endpoint (DRILL-01) + poll-survival/a11y markup (DRILL-03)
- [ ] `tests/agents/routers/test_agent_activity.py` + `test_agent_drill_survival.py` — agent endpoint (DRILL-02) + poll-survival/a11y markup (DRILL-03)
- [ ] `tests/integration/test_agent_stage_buckets.py` — per-agent `GROUP BY` bucket-count aggregate (real Postgres 5433, GroupingError guard); mirrors `tests/integration/test_stage_progress_buckets.py`
- [ ] Reuse existing `conftest.py` fixtures (independent-session assertion pattern; agent + FileRecord + CloudJob factories)

*Test files placed in real `tests/buckets.json` directories (`analyze`, `agents`, `integration`) — NOT `tests/api`/`tests/unit` (nonexistent buckets). CI bucketing is directory-based; a file outside a bucket dir is skipped by every CI shard.*

*Framework already installed — no install task needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Visible `:focus-visible` ring + real focus movement on keyboard drill-in; Esc returns focus to the originating card/row after an intervening 5s poll swap | DRILL-03 | Focus-ring rendering and focus-restoration timing across a live poll swap are visual/interaction behaviors not fully assertable in a headless markup test | Keyboard-only: Tab to a lane card / agent row, Enter to open pane, wait >5s for a poll tick, Esc — confirm focus lands back on the same trigger and the ring is visible in both light + dark themes |
| Selected-card highlight persists across the 5s `outerHTML` poll (D-02) via the `?lane=`/`?agent=` param re-render | DRILL-03 | Requires observing the live poll re-render carrying the pushed URL param (OQ2 wiring) in a running browser | Open a drill-in, leave it open through ≥2 poll ticks, confirm the source card/row keeps its selected ring and a page reload re-opens the same detail |

*Remaining behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 90s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-11
