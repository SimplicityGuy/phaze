---
phase: 50-push-pipeline
verified: 2026-06-26T16:10:00Z
status: passed
score: 5/5 must-haves verified
human_verification_resolved: "Cloud-window dashboard cards driven live against the real app + templates (2 PUSHING + 1 PUSHED seeded): both cards render with distinct headings, /pipeline/stats re-emits them OOB with correct counts, degrade-safety confirmed (200 on sibling-query failure). User approved 2026-06-26. See 50-HUMAN-UAT.md (status: resolved)."
overrides_applied: 0
re_verification: null
deferred:
  - truth: "A cloud-routed long file physically reaches the compute agent's local disk over rsync/SSH-over-Tailscale"
    addressed_in: "Phase 51"
    evidence: "Phase 51 goal: 'The compute agent is deployable and fully operator-controlled — a Tailscale-connected compose stack'; no live SSH target exists in this environment. The code paths are fully verified; live transfer confirmation requires Phase 51 deployment (per test_env_note)."
human_verification:
  - test: "Render the pipeline dashboard and confirm the two new cloud-window count cards appear correctly"
    expected: "Two separate cards visible: one labeled 'Cloud · Staged' (rendering PUSHING count) and one labeled 'Analyzing (cloud)' / 'Cloud · Analyzing' (rendering PUSHED count), each refreshing on the 5-second stats poll without 500-ing the page"
    why_human: "Card visual appearance, aria labeling, and OOB swap correctness cannot be verified by grep; requires a browser load against a running app instance"
---

# Phase 50: Push Pipeline Verification Report

**Phase Goal:** A cloud-routed long file physically reaches the compute agent's local disk, is integrity-verified, analyzed, and cleaned up — the control plane keeping the pipeline "one ahead" with no orphaned scratch files and no double-enqueues.
**Verified:** 2026-06-26T16:10:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | File-server agent pushes to compute scratch dir via rsync/SSH; compute only receives | VERIFIED | `push_file` in `src/phaze/tasks/push.py`: shell-free argv via `asyncio.create_subprocess_exec`, pinned known_hosts (`StrictHostKeyChecking=yes`, `UserKnownHostsFile`, `BatchMode=yes`), registered on agent_worker (fileserver role); staging cron enqueues only on fileserver queue |
| 2 | Compute agent verifies sha256 before analysis; mismatch triggers clean re-push | VERIFIED | `functions.py:185-199`: `asyncio.to_thread(compute_sha256, Path(scratch_path))`, mismatch → explicit unlink + `report_push_mismatch`; `FileNotFoundError` defense-in-depth also routes to mismatch; 15 tests pass in `test_process_file_scratch.py` |
| 3 | CR-01 fix coherent: scratch kept on retryable failure, deleted on terminal outcomes | VERIFIED | `functions.py:178`: `cleanup_scratch = True`; `except Exception` block sets `cleanup_scratch = False` when `job.retryable is True`; `finally:` checks `if payload.scratch_path and cleanup_scratch:`; `TimeoutError`/`ProcessExpired` return before `finally` with `cleanup_scratch=True`; `test_scratch_survives_retryable_failure` + `test_scratch_cleaned_up_on_terminal_non_retryable_failure` pass |
| 4 | Control plane keeps ≤N in-flight; WR-04 advisory lock prevents window overshoot under concurrent cron ticks | VERIFIED | `release_awaiting_cloud.py:128`: `pg_advisory_xact_lock(:lock_key)` held across COUNT+SELECT+PUSHING in one transaction; `cloud_max_in_flight` default 2; routing seam confirms `cloud` is always 0 (no direct-to-compute path remains); 12 tests pass in `test_staging_cron.py` |
| 5 | WR-02 fix: `report_pushed` guarded on state==PUSHING; idempotent; no orphaned scratch or double-enqueue | VERIFIED | `agent_push.py:103-117`: UPDATE WHERE `FileRecord.state == FileState.PUSHING`; `if res.rowcount == 0: return PushedResponse(...)` no-op; deterministic `push_file:<file_id>` key deduplicates double-tick enqueues; `test_pushed_duplicate_callback_is_idempotent_noop` passes |

**Score:** 5/5 truths verified

### Deferred Items

Items not yet met but explicitly addressed in later milestone phases.

| # | Item | Addressed In | Evidence |
|---|------|-------------|----------|
| 1 | Live rsync-over-Tailscale transfer to a real compute agent's scratch dir | Phase 51 | Phase 51 goal: "Tailscale-connected compose stack … with Tailscale connectivity, no media mount, a scratch volume"; no SSH target in this env (per test_env_note); code paths fully verified by unit tests |

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/tasks/push.py` | rsync-over-SSH push task + startup janitor | VERIFIED | `push_file` task with shell-free argv, temp-file secret materialization (shredded in finally), CancelledError reaping (WR-03), startup janitor `_maybe_sweep_scratch` |
| `src/phaze/routers/agent_push.py` | `/pushed` and `/mismatch` control callbacks | VERIFIED | Both endpoints present; WR-02 guard on PUSHING state; mismatch re-drive with attempt cap via `push_max_attempts` |
| `src/phaze/tasks/release_awaiting_cloud.py` | `stage_cloud_window` bounded cron | VERIFIED | Advisory lock, COUNT+SELECT+PUSHING in one transaction, fileserver/compute gates as clean no-ops, deterministic key dedup |
| `src/phaze/tasks/functions.py` | CR-01 fix + sha256 verify + scratch finally-cleanup | VERIFIED | `cleanup_scratch` flag, `asyncio.to_thread(compute_sha256, ...)`, `finally` unlinks only on terminal outcomes |
| `src/phaze/models/file.py` | `FileState.PUSHING` / `FileState.PUSHED` | VERIFIED | Both members present as code-only StrEnum additions over existing String(30) column; no migration |
| `src/phaze/config.py` | `cloud_max_in_flight`, `push_max_attempts`, `compute_scratch_dir`, push/SSH knobs, `SECRET_FILE_PRESERVE_WHITESPACE` (WR-01) | VERIFIED | All present on `ControlSettings`/`AgentSettings`; `SECRET_FILE_PRESERVE_WHITESPACE = frozenset({"push_ssh_key", "push_known_hosts"})` preserves trailing newline |
| `src/phaze/schemas/agent_push.py` | `PushedResponse`, `PushMismatchRequest`, `PushMismatchResponse` | VERIFIED | ORM-free schemas; mypy clean |
| `src/phaze/services/pipeline.py` | `get_cloud_window_count`, `get_cloud_staging_candidates`, `get_pushing_count`, `get_pushed_count` | VERIFIED | COUNT queries over PUSHING/PUSHED states; degrade-safe per-card helpers via `_safe_count` |
| `src/phaze/templates/pipeline/partials/staged_pushing_card.html` | "Staged (pushing)" count card (IN-02 fix) | VERIFIED | Distinct H2: "Cloud · Staged" with `aria-labelledby="staged-pushing-heading"` |
| `src/phaze/templates/pipeline/partials/analyzing_cloud_card.html` | "Analyzing (cloud)" count card (IN-02 fix) | VERIFIED | Distinct H2: "Cloud · Analyzing" with `aria-labelledby="analyzing-cloud-heading"` |
| `src/phaze/tasks/_shared/deterministic_key.py` | `push_file:<file_id>` key builder | VERIFIED | `"push_file": lambda k: str(k["file_id"])` present |
| `src/phaze/tasks/reenqueue.py` | PUSHING → orphaned re-drive; PUSHED/ANALYZED/ANALYSIS_FAILED → push-done | VERIFIED | `_PUSH_DONE` set, `_select_done_push_ids`, fileserver-kind re-drive partition |
| `src/phaze/services/enqueue_router.py` | `push_file` in `AGENT_TASKS` frozenset | VERIFIED | `"push_file"` at line 68 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `stage_cloud_window` | `controller.py` CronJob | `CronJob(stage_cloud_window, "*/5 * * * *")` | WIRED | Confirmed at controller.py:234 |
| `agent_push.router` | `main.py` | `app.include_router(agent_push.router)` | WIRED | Confirmed at main.py:209 |
| `push_file` | `agent_worker.settings["functions"]` | `from phaze.tasks.push import push_file` | WIRED | Confirmed at agent_worker.py:70, 277 |
| `report_pushed` / `report_push_mismatch` | `PhazeAgentClient` | methods in `agent_client.py` | WIRED | Confirmed via 50-03-SUMMARY + `test_agent_client.py` 15 passing |
| `enqueue_process_file` | `agent_push.py` | called with `expected_sha256=file.sha256_hash, scratch_path=...` | WIRED | Confirmed at agent_push.py:122-129 |
| `stage_cloud_window` → `_enqueue_push_file` | fileserver queue | `PUSH_FILE_SAQ_TIMEOUT_SEC` explicit timeout | WIRED | Confirmed at release_awaiting_cloud.py:101-106 (WR-03 fix) |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `staged_pushing_card.html` | `pushing_count` | `get_pushing_count` → `COUNT(state==PUSHING)` DB query | Yes | FLOWING |
| `analyzing_cloud_card.html` | `analyzing_cloud_count` | `get_pushed_count` → `COUNT(state==PUSHED)` DB query | Yes | FLOWING |
| `stage_cloud_window` | `window` | `get_cloud_window_count` → `COUNT(state IN {PUSHING,PUSHED})` in same transaction | Yes | FLOWING |
| `process_file` sha256 gate | `actual_sha256` | `asyncio.to_thread(compute_sha256, Path(scratch_path))` reads the actual pushed file | Yes | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `FileState.PUSHING/PUSHED` resolve correctly | `uv run python -c "from phaze.models.file import FileState; assert FileState.PUSHING=='pushing' and FileState.PUSHED=='pushed'"` | exit 0 | PASS |
| `ControlSettings` defaults match design | `uv run python -c "from phaze.config import ControlSettings; c=ControlSettings(); assert c.cloud_max_in_flight==2 and c.push_max_attempts==3"` | exit 0 | PASS |
| `test_push_pipeline.py` (argv/exit_code/janitor) | `uv run pytest tests/test_push_pipeline.py -q` | 9 passed | PASS |
| `test_process_file_scratch.py` (sha256/cleanup/CR-01) | `uv run pytest tests/test_process_file_scratch.py -q` | 15 passed | PASS |
| `test_staging_cron.py` (≤N window math + advisory lock) | `uv run pytest tests/test_staging_cron.py -q` | 12 passed | PASS |
| `test_routing_seam.py` (no direct-to-compute path) | `uv run pytest tests/test_routing_seam.py -q` | 3 passed | PASS |
| Full non-integration test suite | `uv run pytest tests/ -q --ignore=tests/integration` | 1316 passed, 1 known pre-existing failure (`test_agent_task_router.py`), 0 regressions | PASS |

### Probe Execution

Step 7c: SKIPPED — no conventional `scripts/*/tests/probe-*.sh` found; phase-50 plans do not declare probe-based verification; live transfer is Phase 51 manual verification per test_env_note.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| CLOUDPIPE-01 | 50-01, 50-02, 50-06, 50-07 | Control plane keeps ≤N files staged-or-in-flight (default 2) | SATISFIED | `stage_cloud_window` with `cloud_max_in_flight`; advisory lock (WR-04); COUNT from committed FileState; dashboard count cards |
| CLOUDPIPE-02 | 50-01, 50-03 | File-server pushes to compute scratch over rsync/SSH-over-Tailscale | SATISFIED | `push_file` task with shell-free argv, pinned host keys, file-server initiates; staging cron enqueues on fileserver queue only |
| CLOUDPIPE-03 | 50-01, 50-04 | Compute agent verifies sha256 after transfer before analyzing | SATISFIED | `asyncio.to_thread(compute_sha256, ...)` in `process_file`; mismatch → `report_push_mismatch`; `FileNotFoundError` guard |
| CLOUDPIPE-04 | 50-01, 50-04 | Compute agent deletes scratch copy after analysis (success or terminal failure) | SATISFIED | `finally: if payload.scratch_path and cleanup_scratch: Path(...).unlink(missing_ok=True)`; CR-01 fix: kept on retryable |
| CLOUDPIPE-05 | 50-01, 50-02, 50-05 | Failed/interrupted push is re-driven without orphaned scratch files or double-enqueues | SATISFIED | Deterministic `push_file:<file_id>` key; `report_pushed` WR-02 guard; attempt-capped mismatch re-drive; recovery re-drives PUSHING → fileserver |

All five CLOUDPIPE requirements are SATISFIED.

### Code Review Findings — Resolution Status

| Finding | Severity | Status | Evidence |
|---------|----------|--------|----------|
| CR-01: `finally` deleted scratch on retryable failure, stranding file in PUSHED | CRITICAL | FIXED | `cleanup_scratch` flag; `except Exception` sets `False` when `job.retryable`; commit `73f191b` |
| WR-01: `_resolve_secret_files` stripped trailing newline from SSH key | WARNING | FIXED | `SECRET_FILE_PRESERVE_WHITESPACE = frozenset({"push_ssh_key", "push_known_hosts"})` in `config.py:444`; commit `a909e12` |
| WR-02: `report_pushed` had no state guard; duplicate callback clobbered ANALYZED file | WARNING | FIXED | UPDATE `WHERE state == FileState.PUSHING`; `rowcount == 0` early return; commit `4c33711` |
| WR-03: `push_file` SAQ job timeout equaled `push_timeout_sec`; no `CancelledError` reaping | WARNING | FIXED | `PUSH_FILE_SAQ_TIMEOUT_SEC = 660`; `except (TimeoutError, asyncio.CancelledError): proc.kill(); await proc.wait()`; commit `d17204b` |
| WR-04: `stage_cloud_window` window count not concurrency-safe; overlapping cron ticks could overshoot | WARNING | FIXED | `pg_advisory_xact_lock` at transaction start in `stage_cloud_window`; commit `a494229` |
| IN-01: sha256 verification silently skipped when `scratch_path` set but `expected_sha256` is None | INFO | ACKNOWLEDGED | `logger.warning(...)` at `functions.py:200-207`; not a hard error (control plane always pins both); commit included in `73f191b` |
| IN-02: both cloud-window cards rendered identical "Cloud Window" H2 heading | INFO | FIXED | "Cloud · Staged" and "Cloud · Analyzing" distinct headings; commit `f1b6463` |

### Anti-Patterns Found

No TBD, FIXME, or XXX markers in any phase-50-modified files. `Open-Q1` references in `agent_push.py` are cross-references to an explicit design decision (PUSHING slot retained on mismatch re-drive, per the CONTEXT/RESEARCH docs), not unresolved debt.

No stub patterns: no empty handlers, no `return []` / `return {}` stubs, no placeholder returns in any production module.

### Human Verification Required

#### 1. Dashboard cloud-window count cards visual render

**Test:** Start the application and navigate to the pipeline dashboard. Confirm two new count cards appear: one labeled "Cloud · Staged" (PUSHING count) and one labeled "Cloud · Analyzing" (PUSHED count), positioned beside the existing "Awaiting cloud" card. Trigger the 5-second stats poll and confirm both cards update via HTMX OOB swap without a full page reload.
**Expected:** Both cards render with correct labels and live counts; no console errors; page does not 500 on the stats poll when DB is reachable.
**Why human:** Visual appearance, label text, HTMX OOB swap behavior, and poll correctness cannot be confirmed by grep or unit tests.

### Gaps Summary

No gaps — all five CLOUDPIPE success criteria are verified in the codebase. All seven code-review findings were addressed before phase submission. The one live-transfer item is explicitly deferred to Phase 51 (operator provisioning, Tailscale connectivity, compute agent compose) and is not a Phase 50 gap.

---

_Verified: 2026-06-26T16:10:00Z_
_Verifier: Claude (gsd-verifier)_
