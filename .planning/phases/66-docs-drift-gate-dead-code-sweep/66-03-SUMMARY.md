---
phase: 66-docs-drift-gate-dead-code-sweep
plan: 03
subsystem: testing
tags: [vulture, dead-code, static-analysis, dev-tooling, uv, justfile]

# Dependency graph
requires:
  - phase: 66-01-docs-drift-gate
    provides: green wave-1 tree + shared justfile ownership (the D-12 "green suite before removal" precondition)
  - phase: 62-polish-cutover
    provides: the v7.0 CUT-02 dead-template cutover that already removed the 20 legacy templates this sweep would otherwise have targeted
provides:
  - vulture>=2.16 dev-only dependency (alphabetical, cooldown-clean, behind a passed legitimacy gate)
  - Hand-audited vulture_whitelist.py suppressing framework/dynamic false-positives so live code is never flagged
  - Non-blocking `just vulture` recipe documenting the repeatable sweep flags
  - Evidence that src/phaze carries NO confirmed-dead code (sweep was a deliberate no-op deletion)
affects: [dead-code-sweep, dev-tooling, ci-hygiene]

# Tech tracking
tech-stack:
  added: [vulture]
  patterns:
    - "Dead-code analysis as a non-blocking `just` recipe (NOT a CI/pre-commit gate) — framework false-positives require human reachability judgment per candidate (D-13)"
    - "Single greppable vulture_whitelist.py at repo root instead of scattered noqa comments through runtime source"
    - "Deletion guardrail (D-12): grep dynamic refs + green full suite before removing any candidate; nothing may alter runtime behavior"

key-files:
  created:
    - vulture_whitelist.py
  modified:
    - pyproject.toml
    - uv.lock
    - justfile

key-decisions:
  - "The confirmed-dead sweep was a deliberate NO-OP: zero confirmed-dead symbols in src/phaze, so nothing was deleted — the v7.0 cutover (Phase 62 CUT-02) + PR #191 already removed the vestigial dead code this sweep anticipated (RESEARCH Deep-Dive 3)"
  - "The whitelist is the durable CLEAN-02 artifact — it suppresses 20 grep-verified framework/dynamic false-positives so future `just vulture` runs stay signal-clean"
  - "vulture is non-blocking (a `just` recipe only), never wired into CI/pre-commit — framework false-positives would make it a false-failure DoS gate (T-66-09, accepted-avoided)"

patterns-established:
  - "Dead-code sweep = tool + hand-audited whitelist + non-blocking recipe; deletions only when grep + green suite prove a symbol dead"
  - "DO-NOT-DELETE view-adjacent helpers (build_dashboard_context / get_stage_progress / get_queue_activity) stay OUT of the whitelist — vulture never flags them (they have live callers)"

requirements-completed: [CLEAN-02]

# Metrics
duration: ~90min (incl. two blocking human checkpoints)
completed: 2026-07-03
---

# Phase 66 Plan 03: Vulture Dead-Code Sweep Summary

**Added `vulture>=2.16` as a dev tool with a hand-audited whitelist and a non-blocking `just vulture` recipe; the confirmed-dead sweep over `src/phaze` was a deliberate no-op — zero dead code remained after the v7.0 cutover, and the 20 grep-verified framework false-positives are now durably suppressed by `vulture_whitelist.py`.**

## Performance

- **Duration:** ~90 min (including two blocking human-verify checkpoints)
- **Started:** 2026-07-03T16:50Z
- **Completed:** 2026-07-03T17:40Z
- **Tasks:** 3 (Task 1 checkpoint approved · Task 2 shipped · Task 3 checkpoint approved as no-op)
- **Files modified:** 4 (3 modified + 1 created)

## Accomplishments
- **Task 2 shipped the tooling:** `vulture>=2.16` added to `[dependency-groups] dev` in alphabetical position (after `ruff>=0.15.18`), `uv.lock` regenerated cooldown-clean, a hand-audited `vulture_whitelist.py` created at repo root, and a non-blocking `just vulture` recipe (min-confidence 80 + whitelist + `--ignore-decorators`) added to the justfile.
- **Task 3 sweep was a deliberate NO-OP:** the sweep found NO confirmed-dead code. `just vulture` (min-confidence 80 + whitelist + `--ignore-decorators`) exits 0 with zero candidates. A deeper confidence-60 hunt surfaced 20 symbols, ALL grep-verified (D-12) as LIVE false-positives — nothing was deleted, and the working tree stayed clean.
- **Durable artifact:** `vulture_whitelist.py` now documents and suppresses those 20 framework/dynamic false-positives so future runs stay signal-clean.
- **Both blocking checkpoints human-approved:** Task 1 (package-legitimacy gate for vulture) and Task 3 (deletion-review gate) were each explicitly approved by the operator.

## Task Commits

Each task was committed atomically:

1. **Task 1: Package legitimacy gate for vulture** — checkpoint (human-approved; no commit — precedes install)
2. **Task 2: Add vulture dev dependency, whitelist, and `just vulture` recipe** — `1dc4a2a` (chore)
3. **Task 3: Confirmed-dead sweep and deletion** — checkpoint (human-approved as a NO-OP; no deletions, no commit)

**Plan metadata:** committed with this SUMMARY (`docs(66-03): complete vulture dead-code sweep plan (no-op deletion)`)

## Files Created/Modified
- `vulture_whitelist.py` (created) — hand-audited false-positive suppression list; header documents that the audit found ZERO genuinely-dead symbols and enumerates the false-positive categories (FastAPI handlers, Pydantic validators, ORM/schema fields, pydantic-settings config, transient dynamic attrs, watchdog callbacks, string-annotation-only imports, deferred-feature helpers, and the Phase-46 `heartbeat_tick` back-compat shim). Excluded from mypy via `pyproject.toml` `exclude`.
- `pyproject.toml` (modified) — `"vulture>=2.16",` appended to `[dependency-groups] dev` alphabetically; `vulture_whitelist.py` added to the mypy `exclude` pattern.
- `uv.lock` (modified) — regenerated with the vulture entry.
- `justfile` (modified) — non-blocking `vulture` recipe (test group, `[doc(...)]` idiom) running the documented sweep flags; comment notes the nonzero exit merely lists candidates to hand-verify.

## Decisions Made
- **The sweep is a NO-OP by design, not by omission.** RESEARCH Deep-Dive 3 anticipated this: the v7.0 cutover already removed the vestigial dead code. Phase 62 (CUT-02) deleted the 20 legacy tab-era templates + reduced `base.html`, and PR #191 removed the dead `_STAGE_PLACEHOLDER` constant (retained through Phase 60 to keep the dead-template guard reachable, then dropped at cutover). What remained were only framework false-positives — exactly the class the whitelist exists to absorb.
- **The whitelist is the CLEAN-02 deliverable, not a set of deletions.** With no dead code to remove, the durable value of this plan is the tool + the hand-audited whitelist that keeps `just vulture` honest going forward.
- **The 20 confidence-60 false-positives, all grep-verified LIVE:** FastAPI/watchdog framework callbacks, Pydantic schemas, string-annotation `cast(...)` imports, Jinja-template-referenced pagination helpers (`has_prev`/`has_next`), the src-called `enqueue_for_file`, deferred-feature scaffolding (`build_tree`, `list_inflight_jobs`, `ensure_bucket_lifecycle_ttl`, `get_analysis_failed_files`, `get_summary_counts`, `report_upload_failed`, `health_all`), and the Phase-46 `heartbeat_tick` back-compat shim.
- **DO-NOT-DELETE trio confirmed safe:** `build_dashboard_context` / `get_stage_progress` / `get_queue_activity` were never flagged (they have live callers feeding the Analyze workspace + `/pipeline/stats`) and are deliberately kept OUT of the whitelist.

## Deviations from Plan

None — plan executed exactly as written. The plan explicitly allowed for a sweep that removes only confirmed-dead code; that the confirmed-dead set turned out to be empty is the D-12/D-13 guardrail working as intended, not a deviation.

## Issues Encountered
- None. The `just vulture` recipe and the full test suite ran clean. DB-backed buckets require the ephemeral integration Postgres/Redis (`just test-db` + the 5433/6380 env), as expected for this repo.

## Verification
- `uv run vulture --version` → 2.16.
- `just vulture` → exit 0, zero candidates (min-confidence 80 + whitelist + `--ignore-decorators`).
- Deeper confidence-60 hunt → 20 symbols, all grep-verified LIVE (documented/suppressed by the whitelist); zero deletions.
- Full suite → 2613 passed.
- `uv run mypy .` → clean.
- `uv run ruff check .` → clean.
- DO-NOT-DELETE trio (`build_dashboard_context` / `get_stage_progress` / `get_queue_activity`) present and unflagged; working tree clean (no source changes under `src/phaze`).

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- CLEAN-02 delivered: vulture is installed dev-only behind a passed legitimacy gate, a hand-audited whitelist suppresses the framework false-positives, and `just vulture` gives the operator a repeatable dead-code check. No dead code remained to remove.
- This is Phase 66's last plan. The orchestrator owns marking the phase complete.

## Self-Check: PASSED

- `vulture_whitelist.py` exists at repo root — FOUND.
- `pyproject.toml`, `uv.lock`, `justfile` carry the vulture entries — FOUND (Task 2 commit `1dc4a2a`).
- Task 2 commit `1dc4a2a` present in git history — FOUND.
- No source changes under `src/phaze`; working tree clean before this SUMMARY commit — CONFIRMED.
- Green gates: full suite 2613 passed, `uv run mypy .` clean, `uv run ruff check .` clean, `just vulture` exit 0 — CONFIRMED.

---
*Phase: 66-docs-drift-gate-dead-code-sweep*
*Completed: 2026-07-03*
