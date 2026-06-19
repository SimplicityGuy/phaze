---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 05
subsystem: agent-tasks
tags: [scan-live-set, terminal-ack, scheduling-ledger, orphan-recovery, CR-01]
requires:
  - "45-02 (scan_live_set terminal-ack via report_scan_terminal)"
provides:
  - "guarded report_scan_terminal on the scan_live_set no-match path (re-raise on retryable, swallow+log on terminal)"
affects:
  - "recover_orphaned_work (scan_live_set:<file_id> ledger row no longer leaked on a controller-down no-match)"
tech-stack:
  added: []
  patterns:
    - "not job.retryable terminal-attempt guard (mirrors functions.py:179-189 and the scan match-failure handler)"
    - "best-effort terminal-ack: swallow+log on the terminal attempt for a clean COMPLETE (no-match) outcome"
key-files:
  created: []
  modified:
    - "src/phaze/tasks/scan.py"
    - "tests/test_tasks/test_scan.py"
decisions:
  - "No-match terminal-ack failure on the TERMINAL attempt is swallowed (not re-raised) -- a clean COMPLETE must return so the ledger row is not leaked forever (T-45-16); this is the one deliberate divergence from the match-failure handler which re-raises."
  - "A None job in ctx is treated as NON-terminal -> re-raise (conservative; matches the match-path `job is not None and not job.retryable` guard exactly)."
metrics:
  duration: ~5 min
  completed: 2026-06-19
---

# Phase 45 Plan 05: Guard scan_live_set No-Match Terminal-Ack (CR-01) Summary

Wrapped the unguarded `report_scan_terminal` call on the `scan_live_set` no-match
early-return in a try/except that mirrors the match-failure handler's `not job.retryable`
discipline, closing CR-01 so a controller-down hiccup during a legitimate no-match scan no
longer perpetually leaks `scan_live_set:<file_id>` and re-enqueues the file on every recovery.

## What Was Built

### Task 1: Guard the scan_live_set no-match report_scan_terminal call (TDD)

The no-match branch (previously `scan.py:95-100`) called `await api.report_scan_terminal(...)`
with NO exception handling, then returned `{"status": "no_matches"}`. If the controller was
down/5xx after retries, the ack raised, the `no_matches` return never executed, SAQ recorded a
FAILED attempt, and on the retries-exhausted terminal attempt the ack STILL raised — so the
ledger row was never cleared and `recover_orphaned_work` re-enqueued the file forever.

The fix wraps the call in a try/except gated on `job is not None and not job.retryable`:

- **healthy no-match**: ack succeeds, returns `{"status": "no_matches"}`, called exactly once.
- **retryable attempt** (`job.retryable is True`): re-raise so SAQ retries; the row survives for the real retry.
- **terminal attempt** (`job is not None and not job.retryable`): swallow + `logger.warning(..., exc_info=True)`, STILL return `{"status": "no_matches"}` so the row is not leaked.
- **job absent** (`ctx.get("job") is None`): treated as NON-terminal → re-raise (conservative; matches the match-path guard exactly).

The one deliberate divergence from the match-failure handler: the no-match terminal-ack does
NOT re-raise after the swallow, because a no-match is a clean COMPLETE — re-raising would block
the return and leak the ledger row.

The match path and the existing match-failure handler are untouched. No `phaze.database` /
`phaze.models` / `sqlalchemy` import was added (L-05 boundary intact).

**TDD cycle:**
- RED (`dee05b4`): added 3 tests (terminal-attempt swallow+return, retryable re-raise, job-absent re-raise). The terminal-attempt test failed against the unguarded code; the other two passed coincidentally (unguarded code re-raises) but pin the contract.
- GREEN (`1553c25`): added the try/except guard; all 12 scan tests pass.
- REFACTOR: none needed — minimal change.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest tests/test_tasks/test_scan.py -q` | 12 passed |
| `uv run pytest tests/test_task_split.py -q` | 7 passed (agent worker Postgres-free) |
| `uv run mypy src/phaze/tasks/scan.py` | clean |
| `uv run ruff check src/phaze/tasks/scan.py tests/test_tasks/test_scan.py` | clean |
| `grep -c "not job.retryable" src/phaze/tasks/scan.py` | 2 (>= 2) |
| `grep -n "report_scan_terminal" src/phaze/tasks/scan.py` | 2 guarded call sites (:107 no-match guard, :151 match-failure handler) |
| pre-commit hooks (per-commit, no `--no-verify`) | all passed |

## Threat Model Outcomes

- **T-45-16 (DoS / recovery loop)** — MITIGATED. The no-match terminal-ack swallows + logs on the terminal attempt so the `no_matches` COMPLETE returns and the row is not perpetually re-enqueued. Proven by `test_scan_no_match_terminal_ack_raise_on_terminal_attempt_swallows_and_returns`.
- **T-45-06 (premature clear of a still-retryable scan)** — ACCEPTED/held. The ack only swallows on `job is not None and not job.retryable`; a retryable attempt (or job absent) re-raises, so the row survives until the genuine terminal attempt. Proven by the retryable / job-absent re-raise tests.
- **T-45-05 (spoofing the clear)** — inherited from Plan 02; unchanged (the ack posts the PATH file_id only).
- **T-45-SC (supply chain)** — no new packages this plan.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED

- FOUND: src/phaze/tasks/scan.py
- FOUND: tests/test_tasks/test_scan.py
- FOUND commit: dee05b4 (RED)
- FOUND commit: 1553c25 (GREEN)

## TDD Gate Compliance

- `test(...)` RED commit present: `dee05b4`
- `feat/fix(...)` GREEN commit present after it: `1553c25`
- REFACTOR: not required.
