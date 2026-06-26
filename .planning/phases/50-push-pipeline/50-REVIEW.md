---
phase: 50-push-pipeline
reviewed: 2026-06-26T04:54:40Z
depth: standard
files_reviewed: 23
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/models/file.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/pipeline.py
  - src/phaze/schemas/agent_push.py
  - src/phaze/schemas/agent_tasks.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/analysis_enqueue.py
  - src/phaze/services/enqueue_router.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/pipeline_counters.py
  - src/phaze/tasks/_shared/deterministic_key.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/functions.py
  - src/phaze/tasks/push.py
  - src/phaze/tasks/reenqueue.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/templates/pipeline/partials/analyzing_cloud_card.html
  - src/phaze/templates/pipeline/partials/staged_pushing_card.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - src/phaze/templates/pipeline/dashboard.html
findings:
  critical: 1
  warning: 4
  info: 2
  total: 7
status: issues_found
---

# Phase 50: Code Review Report

**Reviewed:** 2026-06-26T04:54:40Z
**Depth:** standard
**Files Reviewed:** 23
**Status:** issues_found

## Summary

Phase 50 wires the cloud push pipeline: a bounded `stage_cloud_window` cron stages `push_file` (rsync-over-SSH) jobs for held `AWAITING_CLOUD` files, the compute agent integrity-verifies (sha256) the scratch copy and analyzes it, and two internal-API callbacks (`/pushed`, `/mismatch`) drive the state machine and the attempt-capped re-drive loop.

The argv construction in `tasks/push.py` is sound: `create_subprocess_exec` with a list argv (no shell), a `--` operand terminator, a UUID-based remote destination, and `PushFilePayload` validators (absolute-path + alphanumeric `file_type`) that defend the rsync operands. Pinned host keys (`StrictHostKeyChecking=yes` + fixed `UserKnownHostsFile`) and `BatchMode=yes` are present. Secret materialization to 0600 temp files with `finally` shredding is correct in the happy path.

However, one BLOCKER and several WARNINGs cluster around the **failure / retry / cancellation paths**, which is exactly where this pipeline's correctness lives. The most serious: the scratch-cleanup `finally` in `process_file` destroys the pushed copy on a *retryable* error, so the SAQ retry can never re-verify or analyze it — the file is stranded in `PUSHED` and permanently consumes a bounded cloud-window slot (default `cloud_max_in_flight=2`, so two such failures jam the whole cloud pipeline). Secondary issues: the file-secret resolver strips the trailing newline off the SSH private key, the push callback has no state guard against duplicate/late callbacks, and `push_file` inherits a 600s SAQ job timeout equal to its own I/O timeout (breaking the documented inner<outer kill ordering and risking an orphaned rsync child).

No structural-findings substrate was supplied with this review, so all findings below are narrative.

## Narrative Findings (AI reviewer)

## Critical Issues

### CR-01: `process_file` `finally` deletes the scratch copy on a *retryable* failure, stranding the file in `PUSHED`

**File:** `src/phaze/tasks/functions.py:203-213, 245-250`

**Issue:** The outer `try` deletes `scratch_path` in `finally` on **every** exit path, including a retryable re-raise. Trace a transient (non-sha256) failure:

1. First attempt: `run_in_process_pool` (or the `put_analysis` call at line 225, which is outside the inner `try`) raises a transient error. The generic `except Exception` (line 203) sees `job.retryable is True`, so it re-raises *without* reporting (correct intent: let SAQ's `retries=2` run).
2. The `finally` (line 249) unlinks `scratch_path`. The pushed copy is now **gone**.
3. SAQ retries `process_file` **in place** (it does not re-run `push_file`). On the retry, `read_path = scratch_path`, and the sha256 gate at line 175 calls `compute_sha256(Path(scratch_path))`. `compute_sha256` opens the file (`services/hashing.py:22 with file_path.open("rb")`), so it raises `FileNotFoundError`.
4. That `FileNotFoundError` is raised at line 175, which is inside the outer `try` but **before** the inner `try` (line 183) — so none of the `TimeoutError`/`ProcessExpired`/`Exception` handlers catch it, and **no** `report_analysis_failed` / `report_push_mismatch` callback fires. It propagates out, SAQ retries again until exhausted, then the job is terminally `FAILED` with no callback.

Result: the file is left in `FileState.PUSHED` forever. `get_cloud_window_count` counts `PUSHING + PUSHED`, so the stranded file permanently occupies a window slot. With the default `cloud_max_in_flight=2`, two transient analysis errors permanently jam the entire cloud pipeline (no further files are ever staged). The `retries=2` budget is rendered useless for every cloud file, and a `put_analysis` 5xx (very plausible) triggers the same trap.

**Fix:** Only delete the scratch copy on a *terminal* outcome; on a retryable re-raise either keep the copy or signal control to re-push before the retry. A success path still deletes (SAQ never retries a `COMPLETE` job), so gate the cleanup on terminal disposition rather than blanket-deleting:

```python
async def process_file(ctx, **kwargs):
    payload = ProcessFilePayload.model_validate(kwargs)
    ...
    cleanup_scratch = True  # default: delete (success / terminal failure / mismatch)
    try:
        ...
        except Exception as exc:
            job = ctx.get("job")
            if job is not None and not job.retryable:
                await api.report_analysis_failed(...)   # terminal -> delete
            else:
                cleanup_scratch = False                 # retryable -> KEEP the scratch
            raise
        ...
    finally:
        if payload.scratch_path and cleanup_scratch:
            Path(payload.scratch_path).unlink(missing_ok=True)
```

Also wrap the sha256 gate (line 174-181) so a missing/early-deleted scratch file routes to `report_push_mismatch` (re-push) instead of escaping uncaught. Either approach must guarantee a cloud file can never end terminal-`PUSHED` with no callback.

## Warnings

### WR-01: `_resolve_secret_files` strips the trailing newline off file-mounted SSH private keys

**File:** `src/phaze/config.py:134` (consumed by `src/phaze/tasks/push.py:132`)

**Issue:** `_resolve_secret_files` calls `contents.strip()` on every `<VAR>_FILE` secret, and `push_ssh_key` is a member of `AgentSettings.SECRET_FILE_FIELDS` (config.py:427). The `.strip()` is correct for tokens (the docstring justifies it for `PHAZE_AGENT_TOKEN` hashing), but it removes the trailing newline that OpenSSH private keys require. `push.py:132` then writes the stripped value verbatim (`key_path.write_text(cfg.push_ssh_key.get_secret_value())`). OpenSSH's PEM/OpenSSH key parser rejects a key without a final newline (`Load key "...": invalid format` / `error in libcrypto`), so **every** push that loads its key via `PHAZE_PUSH_SSH_KEY_FILE` (the documented Docker/SOPS path, D-05) fails at the ssh layer — while a key supplied directly via the env var (un-stripped) works, making the bug provisioning-dependent and easy to miss.

**Fix:** Normalize the key to end with exactly one newline when materializing it (do not rely on the operator), and/or exempt key material from the strip. Minimal, in `push.py`:

```python
key_text = cfg.push_ssh_key.get_secret_value()
if not key_text.endswith("\n"):
    key_text += "\n"
key_path.write_text(key_text)
```

### WR-02: `report_pushed` has no state guard — a duplicate/late callback clobbers an already-`ANALYZED` file back to `PUSHED` and re-enqueues analysis

**File:** `src/phaze/routers/agent_push.py:95-108`

**Issue:** The handler unconditionally executes `UPDATE files SET state='pushed' WHERE id=:id` (no `state=PUSHING` precondition) and always enqueues a fresh `process_file`. `report_pushed` can legitimately be called twice: if the agent's `push_file` task posts the callback, the response fails transiently (the client exhausts its 5xx retries → `AgentApiServerError`), `push_file` raises, and SAQ retries the whole `push_file` job → re-rsync → second `report_pushed`. If the **first** callback actually committed server-side and `process_file` has since completed (file now `ANALYZED`), the second callback resets the row to `PUSHED` and re-enqueues `process_file` against a scratch copy the first run already deleted (line 250) — re-analyzing a finished file and re-triggering the CR-01 stranding.

**Fix:** Make the transition idempotent: scope the UPDATE to the expected state and only enqueue when a row actually transitioned.

```python
res = await session.execute(
    update(FileRecord)
    .where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)
    .values(state=FileState.PUSHED)
)
if res.rowcount == 0:
    await session.commit()  # already advanced; do NOT re-enqueue
    return PushedResponse(file_id=file_id)
...  # only now clear ledger + enqueue process_file
```

### WR-03: `push_file` inherits a 600s SAQ job timeout equal to `push_timeout_sec`, breaking the inner<outer kill ordering and risking an orphaned rsync child

**File:** `src/phaze/tasks/release_awaiting_cloud.py:87`, `src/phaze/routers/agent_push.py:192`, `src/phaze/tasks/push.py:155-161`

**Issue:** Both `push_file` producers (`_enqueue_push_file` and the mismatch re-drive) call `queue.enqueue("push_file", ...)` with **no explicit `timeout`**, so `apply_project_job_defaults` stamps it to the agent role default `worker_job_timeout=600` (queue_defaults.py). But `push_timeout_sec` also defaults to 600, and the asyncio outer guard is `push_timeout_sec + 30 = 630` (push.py:155). So the kill layers are: rsync `--timeout`=600, **SAQ job net=600**, asyncio outer=630. The config docstring (config.py:582) explicitly requires `push_timeout_sec` to stay *below* the SAQ `push_file` job timeout "so the kill is deterministic" — but nothing raises the SAQ timeout above 600, so that margin does not exist, and the asyncio outer guard (630) sits *above* the SAQ net and can never fire. Worse, when SAQ cancels the coroutine at 600s it raises `CancelledError`, which is **not** the `TimeoutError` handled at push.py:156 — so `proc.kill()` is never called, the rsync child is orphaned, and the `finally` shreds `id_key`/`known_hosts` while a live `ssh -i` may still be reading them.

**Fix:** Set an explicit `push_file` job timeout strictly above the outer asyncio bound at both enqueue sites (mirroring `enqueue_process_file`'s `timeout=7200`), e.g. `timeout=cfg.push_timeout_sec + 60`, and additionally kill the child in a `finally`/`except (TimeoutError, asyncio.CancelledError)` so a SAQ cancellation cannot leak the rsync subprocess.

### WR-04: `stage_cloud_window` window count is not concurrency-safe against overlapping cron ticks — the load-bearing ≤N window can be exceeded

**File:** `src/phaze/tasks/release_awaiting_cloud.py:112-142`, `src/phaze/services/pipeline.py:860-871`

**Issue:** The window is computed by `get_cloud_window_count` (a plain `COUNT(state IN {PUSHING,PUSHED})` over committed truth) and then candidates are selected `FOR UPDATE SKIP LOCKED`. The docstring claims "a 144-file backlog can never stage more than `slots` at a time" because it is "one transaction." That holds for a single tick, but `stage_cloud_window` has no deterministic key and SAQ does not guarantee non-overlapping cron execution. Two overlapping ticks both read `window=0` (each other's `PUSHING` flips are uncommitted), and `SKIP LOCKED` makes tick B skip A's locked rows and lock the *next* `slots` `AWAITING_CLOUD` rows — so the committed result is `2 * cloud_max_in_flight` files in flight, exceeding the cap the window is supposed to enforce. Likelihood is low (the staging tick is fast), but this is the single load-bearing backpressure invariant, so a stale `COUNT` is a real correctness gap.

**Fix:** Count and select within the same locked scope, or serialize the cron. E.g. give the window count an advisory lock (`pg_advisory_xact_lock`) at the top of the transaction so only one tick can compute slots + stage at a time, or compute the window from the row-locked candidate set rather than an unlocked `COUNT`.

## Info

### IN-01: sha256 verification is silently skipped when `scratch_path` is set but `expected_sha256` is `None`

**File:** `src/phaze/tasks/functions.py:174`

**Issue:** The integrity gate is `if payload.scratch_path and payload.expected_sha256:`. `read_path` is already `scratch_path` (line 167), so a payload with a scratch path but no expected hash analyzes an **unverified** pushed copy. The control plane always pins both (`report_pushed` reads the non-null `FileRecord.sha256_hash`), so this is only reachable via a malformed payload — but it is a defense-in-depth gap in a security-relevant verification path. Consider treating `scratch_path` set + `expected_sha256` missing as a hard error (report mismatch / fail) rather than a silent skip.

### IN-02: both cloud-window cards render an identical `<h2>Cloud Window</h2>` heading

**File:** `src/phaze/templates/pipeline/partials/staged_pushing_card.html:20`, `src/phaze/templates/pipeline/partials/analyzing_cloud_card.html:20`

**Issue:** The "Staged (pushing)" and "Analyzing (cloud)" cards both use the heading "Cloud Window" with distinct `aria-labelledby` targets, producing two stacked sections with the same visible H2 on the dashboard. Minor UX/clarity nit — consider distinct headings (e.g. "Cloud · Staged" / "Cloud · Analyzing") or a single combined "Cloud Window" section wrapping both counts.

---

_Reviewed: 2026-06-26T04:54:40Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
