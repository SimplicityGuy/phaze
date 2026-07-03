---
phase: 66-docs-drift-gate-dead-code-sweep
verified: 2026-07-03T18:20:00Z
status: passed
score: 16/16 must-haves verified
overrides_applied: 0
---

# Phase 66: Docs-Drift Gate & Dead-Code Sweep Verification Report

**Phase Goal:** A CI gate cross-checking REQUIREMENTS.md traceability against passed phases (DOCS-01) + re-link the `/saq` monitor in the shell (CLEAN-01) + delete vestigial dead code (CLEAN-02).
**Verified:** 2026-07-03T18:20:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A CI gate cross-checks REQUIREMENTS.md traceability against passed phases and **fails** when the table is stale (DOCS-01, roadmap SC1) | VERIFIED | `tests/shared/core/test_requirements_traceability.py` (5 assertion functions) exists, is hermetic (0 `phaze.*` imports), and `uv run pytest tests/shared/core/test_requirements_traceability.py -q` → 5 passed. Wired into CI via `just docs-drift` (justfile:95-98) → `.github/workflows/code-quality.yml:54-55` step with no `if:` gate, so it runs on every PR including doc-only ones. `ci.yml` history shows zero Phase-66 commits (untouched, confirmed via `git log --oneline -- .github/workflows/ci.yml`). |
| 2 | A discreet in-UI link to the still-mounted `/saq` SAQ monitor is reachable from the shell (CLEAN-01, roadmap SC2) | VERIFIED | `src/phaze/templates/admin/agents.html:25-28` renders `<a href="/saq" target="_blank" rel="noopener">SAQ monitor ↗</a>` inside `{% if enable_saq_ui %}`, styled `text-xs text-gray-400 dark:text-gray-500` (discreet/muted, matches D-10). Context key wired in `src/phaze/routers/admin_agents.py:111` — `"enable_saq_ui": get_settings().enable_saq_ui`. |
| 3 | Vestigial dead code is identified and removed (CLEAN-02, roadmap SC3) | VERIFIED | `vulture>=2.16` installed dev-only (`pyproject.toml:228`, `uv.lock` regenerated with a `vulture` package entry). `just vulture` → exit 0, zero remaining candidates against `--min-confidence 80` + hand-audited `vulture_whitelist.py` (228 lines, 20 grep-verified false-positive categories documented). A deliberate, evidenced NO-OP: the v7.0 cutover (Phase 62 CUT-02 + PR #191) already removed the dead code this sweep targeted; nothing further was confirmed-dead. DO-NOT-DELETE trio (`build_dashboard_context` in `src/phaze/routers/pipeline.py:454`, `get_queue_activity`/`get_stage_progress` in `src/phaze/services/pipeline.py:197,295`) all still present. |
| 4 | The dead-template guard's own entry-root-literal blind spot is closed (CLEAN-02, roadmap SC4) | VERIFIED | `tests/shared/core/test_dead_template_guard.py:98` adds `test_entry_literals_resolve_to_templates`; existing `test_no_orphan_templates` (line 78) is byte-for-byte unchanged (confirmed by reading both). `uv run pytest tests/shared/core/test_dead_template_guard.py -q` passes (2 tests). |
| 5 | A passed phase whose mapped requirement is left unmarked fails the guard by name (D-01/D-02/D-08) | VERIFIED | `_passed_phase_completeness_offenders()` (test_requirements_traceability.py:136-150) builds a precise `"Phase {N} passed but {rid} checkbox [ ] unmarked"` message; exercised live on first run against the real repo (caught the Phase 65 stale checkbox, commit `cf11724`). |
| 6 | Checkbox state disagreeing with Traceability Status fails the guard (D-03) | VERIFIED | `_checkbox_table_offenders()` (lines 171-188), asserted by `test_active_checkbox_and_table_status_agree`. |
| 7 | A requirement marked Complete without a passed phase fails the guard (D-02) | VERIFIED | `_marked_requirement_offenders()` (lines 153-168), asserted by `test_active_marked_requirements_have_passed_phases`. |
| 8 | The in-flight Phase 66 state itself (unmarked reqs, Pending) PASSES the guard (D-05) | VERIFIED | `test_inflight_phase_with_unmarked_requirements_passes` (lines 230-243) passes against the real repo today — confirmed via direct pytest run. |
| 9 | Archived milestones are validated for internal consistency only, never VERIFICATION-gated (D-04) | VERIFIED | `test_archived_milestones_internally_consistent` (lines 219-227) iterates `milestones/*-REQUIREMENTS.md` and calls only `_checkbox_table_offenders`, never `_verification_passed`. Passes. |
| 10 | The `/saq` link is absent when `enable_saq_ui` is false — never a dead 404 (D-09) | VERIFIED | `tests/agents/routers/test_admin_agents.py::test_saq_link_absent_when_enable_saq_ui_false` — ran live against ephemeral test DB (`just test-db` + `TEST_DATABASE_URL`/`PHAZE_REDIS_URL`): **passed**. |
| 11 | The `/saq` link opens in a new tab with `rel="noopener"` (D-11) | VERIFIED | `test_saq_link_present_when_enable_saq_ui_true` asserts `target="_blank"` and `rel="noopener"` present — ran live, **passed**. |
| 12 | The polled `/_table` partial is unchanged / never carries the link | VERIFIED | `table_partial()` context dict (admin_agents.py:129-140) has no `enable_saq_ui` key; `test_saq_link_absent_from_poll_partial` — ran live, **passed**. |
| 13 | vulture is a dev-only dependency, alphabetically placed, cooldown-clean (D-13) | VERIFIED | `pyproject.toml:228` — `"vulture>=2.16",` after `ruff>=0.15.18`; `uv run vulture --version` → `2.16`; `uv.lock` entry present with `upload-time 2026-03-25` (clears the 7-day exclude-newer cooldown). |
| 14 | A hand-audited whitelist suppresses framework false positives (D-13) | VERIFIED | `vulture_whitelist.py` (228 lines) exists at repo root, documents 20 grep-verified false-positive categories; `just vulture` (min-confidence 80 + whitelist + `--ignore-decorators`) exits 0. |
| 15 | Only confirmed-dead code was removed; DO-NOT-DELETE trio preserved (D-12) | VERIFIED | `git log`/`git status` show zero `src/phaze` deletions from this phase's commits; the trio confirmed present (see truth 3). |
| 16 | No runtime/backend behavior change; full suite green after the sweep | VERIFIED | Per 66-03-SUMMARY: full suite 2613 passed, `uv run mypy .` clean, `uv run ruff check .` clean. Spot-verified this session: `uv run ruff check` + `uv run mypy` on all Phase-66-touched files clean; `uv run pytest tests/agents/routers/test_admin_agents.py -q` → 17 passed against a live ephemeral test DB. |

**Score:** 16/16 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/shared/core/test_requirements_traceability.py` | DOCS-01 hermetic traceability drift guard (5 assertions) | VERIFIED | Exists, contains exactly 5 `def test_` functions, zero `phaze.*` imports, `uv run pytest -q` → 5 passed. |
| `tests/shared/core/test_dead_template_guard.py` | D-14 entry-literal-resolves assertion added, existing test untouched | VERIFIED | `test_entry_literals_resolve_to_templates` added at line 98; `test_no_orphan_templates` (line 78) unmodified. `grep -c 'def test_'` → 2. |
| `justfile` | `docs-drift` + `vulture` recipes | VERIFIED | Both recipes present (`docs-drift` lines 95-98, `vulture` lines 105-108). |
| `.github/workflows/code-quality.yml` | always-run step invoking `just docs-drift` | VERIFIED | Step `🧭 Docs-drift traceability gate` present after pre-commit, no `if:` gate. |
| `src/phaze/routers/admin_agents.py` | `enable_saq_ui` injected into `page()` context | VERIFIED | Line 111, via `get_settings()` call-site idiom; absent from `table_partial()`. |
| `src/phaze/templates/admin/agents.html` | flag-gated discreet `/saq` footer link | VERIFIED | Lines 21-28, muted styling, `target="_blank" rel="noopener"`. |
| `tests/agents/routers/test_admin_agents.py` | render tests for link present/absent + attrs | VERIFIED | 3 new tests (lines 317-365); all pass live (17/17 total in file). |
| `pyproject.toml` | `vulture>=2.16` dev dependency | VERIFIED | Line 228, alphabetically after `ruff`; `vulture_whitelist.py` added to mypy `exclude`. |
| `vulture_whitelist.py` | hand-audited false-positive suppression list | VERIFIED | 228 lines, references `phaze` symbols, documents 20 categories. |
| `uv.lock` | vulture entry regenerated | VERIFIED | `name = "vulture"`, `version = "2.16"` present. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `.github/workflows/code-quality.yml` | `tests/shared/core/test_requirements_traceability.py` | `just docs-drift` recipe | WIRED | Automated `verify.key-links` confirmed pattern found in source. |
| `tests/shared/core/test_requirements_traceability.py` | `.planning/REQUIREMENTS.md` + `.planning/ROADMAP.md` + `.planning/phases/*/*VERIFICATION*.md` | `read_text` parse-then-assert | WIRED | Automated `verify.key-links` confirmed pattern found in source. |
| `src/phaze/templates/admin/agents.html` | `/saq` | flag-gated anchor | WIRED | Automated `verify.key-links` confirmed target referenced in source. |
| `src/phaze/routers/admin_agents.py::page` | `settings.enable_saq_ui` | `get_settings()` call-site | WIRED | Automated `verify.key-links` reported "Source file not found" because it treats `admin_agents.py::page` as a literal path — **false negative**. Manually confirmed via `grep -n 'enable_saq_ui\|get_settings' src/phaze/routers/admin_agents.py`: line 33 imports `get_settings`, line 111 calls `get_settings().enable_saq_ui` inside `page()`. |
| `justfile vulture recipe` | `src/phaze` + `vulture_whitelist.py` | `uv run vulture --ignore-decorators --min-confidence` | WIRED | Automated `verify.key-links` reported "Source file not found" because `"justfile vulture recipe"` isn't a real path — **false negative**. Manually confirmed via `grep -n -A3 'vulture' justfile`: the `vulture` recipe body is exactly `uv run vulture src/phaze vulture_whitelist.py --min-confidence 80 --ignore-decorators ...`, and running it live exits 0. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|---------------------|--------|
| `admin/agents.html` (`enable_saq_ui`) | `enable_saq_ui` context key | `get_settings().enable_saq_ui` — a live `pydantic-settings` `BaseSettings` field (`config.py:292`, alias `PHAZE_ENABLE_SAQ_UI`, default `True`), read at call-site (not snapshotted) | Yes | FLOWING — confirmed via live test toggling `PHAZE_ENABLE_SAQ_UI` env var (`monkeypatch.setenv` + `get_settings.cache_clear()`) and observing the rendered HTML change between true/false. |
| `test_requirements_traceability.py` guard | offender lists | Live `read_text()` of `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/phases/*/*VERIFICATION*.md` on disk — no mocked/static fixtures | Yes | FLOWING — the guard caught a real drift (Phase 65 stale checkbox) on its first run, proving it reads live repo state rather than a fixture. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Traceability + dead-template guards pass hermetically | `uv run pytest tests/shared/core/test_requirements_traceability.py tests/shared/core/test_dead_template_guard.py -q` | `7 passed` | PASS |
| `just docs-drift` recipe runs | `just docs-drift` (via direct pytest invocation) | exit 0 | PASS |
| `/saq` link render tests (live DB) | `uv run pytest tests/agents/routers/test_admin_agents.py -q` (ephemeral `phaze_test`/Redis via `just test-db`) | `17 passed` (13 pre-existing + 3 new + 1 registration) | PASS |
| vulture sweep is clean / non-blocking | `just vulture` | exit 0, zero candidates | PASS |
| Lint/type clean on touched files | `uv run ruff check ...` / `uv run mypy src/phaze/routers/admin_agents.py` | `All checks passed!` / `Success: no issues found` | PASS |
| `ci.yml` untouched by this phase | `git log --oneline -- .github/workflows/ci.yml` | last touching commit is Phase 65 (`2c7f5fa`), no Phase-66 commits | PASS |

### Probe Execution

Step 7c: SKIPPED — no `scripts/*/tests/probe-*.sh` files exist in this repo and neither the PLAN nor SUMMARY files for Phase 66 declare any probe scripts. This phase is verified via direct pytest/CLI execution instead (see Behavioral Spot-Checks above).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| DOCS-01 | 66-01 | CI gate cross-checks REQUIREMENTS.md traceability against passed phases | SATISFIED | `test_requirements_traceability.py` + `just docs-drift` wired into `code-quality.yml`, all green. |
| CLEAN-01 | 66-02 | Discreet in-UI `/saq` link restored, presentation-only | SATISFIED | `agents.html` flag-gated anchor + `admin_agents.py` context key + 3 passing render tests. |
| CLEAN-02 | 66-01 (D-14 portion), 66-03 (sweep portion) | Dead-template guard blind spot closed + vestigial dead code removed | SATISFIED | D-14 assertion added and green; vulture sweep tooling installed, whitelist hand-audited, confirmed no dead code remains (deliberate no-op, evidenced). |

No orphaned requirements: REQUIREMENTS.md Traceability table maps all three phase-66 IDs (DOCS-01, CLEAN-01, CLEAN-02) to Phase 66, and all three appear in at least one plan's `requirements:` frontmatter (66-01: DOCS-01, CLEAN-02; 66-02: CLEAN-01; 66-03: CLEAN-02).

Note: REQUIREMENTS.md still shows DOCS-01/CLEAN-01/CLEAN-02 as `[ ]` / Pending as of this verification. This is the **intended, tested in-flight state** (D-05 regression `test_inflight_phase_with_unmarked_requirements_passes` asserts this exact state must pass the guard) — the orchestrator is expected to flip these checkboxes and the Traceability Status to Complete, and add a `66-VERIFICATION.md` with `status: passed` (this file), once the phase is marked complete. This is standard end-of-phase bookkeeping, not a gap.

### Anti-Patterns Found

None. Scanned all 9 phase-touched files for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER` and stub-language patterns — the only match was a docstring reference to the historical `_STAGE_PLACEHOLDER` constant name in `test_dead_template_guard.py` (documentary, not a debt marker in this phase's own code).

### Human Verification Required

None. All must-haves are structurally verifiable and were confirmed either by direct grep/read or by executing the actual test suite against a live ephemeral Postgres/Redis instance (`just test-db`). The one cosmetic/visual item (discreet muted footer link styling, D-10) was confirmed structurally — `text-xs text-gray-400 dark:text-gray-500` matches the codebase's existing muted-text idiom (`agents.html:9` referenced in the plan) — and is low-risk, presentation-only, non-interactive markup that does not require browser-rendered visual confirmation to trust.

### Gaps Summary

No gaps. All three requirements (DOCS-01, CLEAN-01, CLEAN-02) are functionally delivered and verified against the live codebase:

- DOCS-01's guard is hermetic, passes today, already proved its value by catching a real Phase 65 drift on first run, and is wired into the always-run `code-quality` CI job.
- CLEAN-01's `/saq` link is flag-gated symmetrically with the mount condition, opens safely in a new tab, and is covered by 3 passing render tests executed live in this session.
- CLEAN-02 shipped both halves: the dead-template guard's D-14 blind spot is closed (test passes), and the vulture-based sweep is installed, hand-audited, and non-blocking — its "no dead code found" conclusion is evidenced (v7.0 cutover already removed it) rather than merely asserted.

The code review (`66-REVIEW.md`) found 0 blockers and 3 advisory warnings (WR-01/02/03 — parser-robustness gaps in the traceability/dead-template guards, e.g. whole-file vs section-scoped parsing, and a bidirectional-drift blind spot). These do not affect the guard's correctness against the current repo state and are appropriately deferred as follow-up hardening rather than phase-blocking issues.

---

*Verified: 2026-07-03T18:20:00Z*
*Verifier: Claude (gsd-verifier)*
