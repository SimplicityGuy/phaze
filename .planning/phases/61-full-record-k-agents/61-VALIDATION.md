---
phase: 61
slug: full-record-k-agents
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-01
---

# Phase 61 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution. Derived from `61-RESEARCH.md` § Validation Architecture (Nyquist-consistent with Phases 57–60).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio + httpx AsyncClient |
| **Config file** | `pyproject.toml` (project-standard); async `client` fixture in `tests/conftest.py` |
| **Quick run command** | `uv run pytest tests/test_record_palette_agents.py tests/test_base_html_sri.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` (85% floor) |
| **Estimated runtime** | ~30–60 seconds (targeted file); full suite longer |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_record_palette_agents.py tests/test_base_html_sri.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (85% floor; pre-commit hooks + mypy strict must pass — never `--no-verify`)
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~60 seconds

---

## Per-Task Verification Map

Task IDs are assigned by the planner; this map is keyed by requirement + behavior so the planner can attach each `<automated>` verify to the owning task.

| Req | Wave | Behavior | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|-----|------|----------|------------|-----------------|-----------|-------------------|-------------|--------|
| RECORD-01 | — | `GET /record/{file_id}` returns a BARE fragment (no `<html>`/`<head>`) with header/facts/timeline/diff/identity/pending-approvals/history; scoped strictly by `file_id` | T-61 access-control (scope reads by file_id, mirror `proposals.py:257`) | Reads scoped by typed UUID `file_id`; no cross-file leakage | unit (route+template) | `uv run pytest tests/test_record_palette_agents.py::test_record_fragment_bare_and_scoped -x` | ❌ W0 | ⬜ pending |
| RECORD-01 | — | Missing/de-duplicated file → 404 friendly fragment (not 500); close/focus contract intact | T-61 (typed UUID + 404 on miss) | 404 fragment, no stack trace | unit | `...::test_record_missing_file_404_fragment -x` | ❌ W0 | ⬜ pending |
| RECORD-01 | — | Record body carries `_diff_row.html` approval rows wired to existing proposals/tags routes (approve/edit/undo URLs present); Alpine islands re-init after swap | T-61 XSS (`|tojson` not `|e` in Alpine JS contexts) | No apostrophe-filename breakout (Phase 60 class) | unit | `...::test_record_pending_approvals_wired -x` | ❌ W0 | ⬜ pending |
| RECORD-02 | — | ⌘K grouped endpoint returns Files/Tracklists/Artists/Commands over `search()` + `distinct_artists()`; rows `role="option"`, headers `role="presentation"` | T-61 (parameterized ILIKE) | Bound query param; no interpolation | unit | `...::test_cmdk_grouped_results -x` | ❌ W0 | ⬜ pending |
| RECORD-02 | — | `distinct_artists()` returns DISTINCT `FileMetadata.artist`/`Tracklist.artist` matching query, LIMIT-bounded, no None | — | Read-only; debounce + LIMIT (unindexed cols) | unit | `...::test_distinct_artists_query -x` | ❌ W0 | ⬜ pending |
| RECORD-02 | — | Artist `Enter` → file list with `artist=` param; Scan command posts `/pipeline/scan-live-sets` | — | Reuses `enqueue_router` guards | unit | `...::test_cmdk_commands_and_artist_nav -x` | ❌ W0 | ⬜ pending |
| RECORD-03 | — | Agents page renders Section 1 (heartbeating, `classify`/`sort_key`) + Section 2 (compute lanes) with Active/Waiting/Idle — **never a DEAD/rose state** | — | KDEPLOY-04: DEAD forbidden | unit | `...::test_agents_two_sections_never_dead -x` | ❌ W0 | ⬜ pending |
| RECORD-03 | — | `classify_compute_lanes` → ACTIVE(running), WAITING(submitted+inadmissible), IDLE(none); degrades to IDLE on DB error | — | Degrade-safe (mirror `services/pipeline.py:1117/1162`) | unit | `...::test_compute_lane_liveness_states -x` | ❌ W0 | ⬜ pending |
| RECORD-04 | — | file_count==0 renders empty-state guide listing each agent + `scan_roots`; "Scan {agent}" posts `POST /pipeline/scans` (agent_id + scan_root), NOT `scan-live-sets`; no free-text path input | T-61 info-disclosure (D-08: no directory-browse; `scan_roots` prefix + `..` validation) | Reuses `pipeline_scans.py:319` traversal guard | unit | `...::test_empty_state_agent_roots_scan -x` | ❌ W0 | ⬜ pending |
| RECORD-04 | — | file_count>0 does NOT render the empty state (branch correctness) | — | N/A | unit | `...::test_empty_state_suppressed_when_files_exist -x` | ❌ W0 | ⬜ pending |
| Dep/SRI (load-bearing) | — | `@alpinejs/focus@3.15.12` present in BOTH `shell.html` AND `base.html`, `<script defer>` before Alpine core, full-semver pinned, SRI matches | T-61 supply-chain (SRI SHA-384 + first-party + full-semver) | SRI guarded where the shell loads it | unit (extended SRI guard) | `uv run pytest tests/test_base_html_sri.py -x` (extended to scan `shell.html`) | ⚠ EXTEND existing | ⬜ pending |
| Fragment/poll (cross-cutting) | — | Record + palette + empty-state fragments are bare; no `hx-trigger="every"`/`setInterval`/`hx-swap-oob` on approval-row subtrees (single-poll, counts-only OOB, D-02) | — | No in-progress-subtree re-render | unit (fragment guard) | `...::test_new_fragments_single_poll_clean -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_record_palette_agents.py` — route+template assertions for the record fragment, ⌘K grouped results, Agents two sections, and the empty-state branch (covers RECORD-01..04 + the fragment guard).
- [ ] `tests/test_base_html_sri.py` — **EXTEND** `_extract_cdn_scripts` to also scan `shell.html` (parametrize over both templates), so the focus-plugin hash is guarded where the shell actually loads it (RESEARCH Pitfall 1).
- [ ] Fixtures (`tests/conftest.py` factories): a file with `AnalysisResult` + `AnalysisWindow` rows (fine+coarse); a pending `RenameProposal` + tag comparison for the record's approvals; `FileMetadata`/`Tracklist` rows with distinct artists; `CloudJob` rows in running / submitted+inadmissible / none states; an empty-DB case (file_count==0).
- [ ] Framework install: none — existing pytest infra covers all of this.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Focus-trap containment (⌘K + slide-in): Tab cycles within, Esc returns focus to `#cmdk-trigger` / the opener | RECORD-01, RECORD-02 | `x-trap` focus behavior + keyboard cycling is a live-DOM interaction not fully assertable in httpx | Open ⌘K (⌘K / `?palette=1`), Tab through — focus stays inside; Esc → focus returns to trigger. Open a record, Tab — focus stays in panel; Esc/✕ → focus returns to the opening row. |
| Live scan progress on empty state rides the existing poll (no new loop) | RECORD-04 | Requires a live scan + observing OOB count updates over 5s ticks | With 0 files, click "Scan {agent}", observe progress advancing via the existing `/pipeline/stats` fanout (no second request loop in Network tab). |

*Automated coverage handles fragment shape, wiring, queries, liveness states, branch correctness, and SRI; the above two are the live-DOM/keyboard behaviors.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (new test file + extended SRI test + fixtures)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** ready 2026-07-01 (plan-checker verified — plans satisfy every Nyquist criterion; Wave 0 physically lands during 61-01 execution)
