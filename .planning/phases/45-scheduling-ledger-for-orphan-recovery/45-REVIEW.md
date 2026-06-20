---
phase: 45-scheduling-ledger-for-orphan-recovery
reviewed: 2026-06-19T22:57:19Z
depth: standard
files_reviewed: 8
files_reviewed_list:
  - src/phaze/tasks/scan.py
  - src/phaze/tasks/metadata_extraction.py
  - src/phaze/tasks/fingerprint.py
  - src/phaze/routers/agent_metadata.py
  - src/phaze/routers/agent_fingerprint.py
  - src/phaze/schemas/agent_metadata.py
  - src/phaze/schemas/agent_fingerprint.py
  - src/phaze/services/agent_client.py
findings:
  critical: 0
  warning: 0
  info: 1
  total: 1
status: resolved
---

# Phase 45: Gap-Closure Code Review Report (45-05 + 45-06)

**Reviewed:** 2026-06-19T22:57:19Z
**Depth:** standard
**Files Reviewed:** 8
**Status:** resolved (both warnings closed 2026-06-20 by quick task 260620-jvu)

> This report covers the gap-closure pass only (plans 45-05 and 45-06).
> The original phase-45 review findings for all other files are in the
> git history at commit `727338d`.

## Summary

Plan 45-05 guarded the unguarded `report_scan_terminal` call on the `scan_live_set` no-match path
(CR-01). Plan 45-06 added control-side `/failed` terminal-ack endpoints for `extract_file_metadata`
and `fingerprint_file`, agent-client methods, and agent-worker terminal-failure guards (CR-02).

**Key invariants verified:**

1. **Agent worker Postgres-free boundary** — PASS. No `phaze.database`, `phaze.models`, or
   `sqlalchemy` imports at runtime in `scan.py`, `metadata_extraction.py`, `fingerprint.py`, or
   `agent_client.py`. New client methods (`report_metadata_failed`, `report_fingerprint_failed`)
   are httpx-only with schema imports confined to `TYPE_CHECKING` and function-local guards.

2. **Terminal-ack endpoints use PATH `file_id` only** — PASS. Both `/metadata/{file_id}/failed`
   and `/fingerprints/{file_id}/failed` reconstruct the clear key from the PATH `file_id` and
   the fixed function name. `agent` is bound from the auth token via `get_authenticated_agent`
   (never a body field). No body field can redirect the clear to another file's key (T-45-05).

3. **Terminal-failure guards ack only on retries-exhausted attempt** — PASS. All three tasks
   gate the ack on `job is not None and not job.retryable`, re-raising without acking on
   retryable and job-absent attempts (row survives for the real retry; T-45-06).

4. **CR-01 no-match path correctly swallows ack failure on terminal attempt** — PASS. The
   `try/except` wrapping `report_scan_terminal` on the no-match path (scan.py:106-113) swallows
   the ack exception on the terminal attempt, logs a warning, and returns the `no_matches`
   COMPLETE. Retryable/job-absent attempts re-raise.

5. **Ledger clear keys match WRITE hook keys exactly** — PASS.
   - `put_metadata` clears `extract_file_metadata:{file_id}` (agent_metadata.py:73).
   - `report_metadata_failed` clears `extract_file_metadata:{file_id}` (agent_metadata.py:101).
   - `put_fingerprint` clears `fingerprint_file:{file_id}` (agent_fingerprint.py:54).
   - `report_fingerprint_failed` clears `fingerprint_file:{file_id}` (agent_fingerprint.py:85).
   All match `_KEY_BUILDERS` deterministic key format.

Two warnings are raised. The primary one is structural: the match-failure exception handler in
`scan.py` and the new `metadata_extraction.py`/`fingerprint.py` handlers leave the terminal ack
call itself unguarded inside the `except` block. If the ack call also raises (double-failure),
the ack exception replaces the original exception and the ledger row is never cleared. The
no-match path in `scan.py` correctly avoids this by nesting the ack in its own `try/except`;
the other three handlers do not. This pattern is inherited from `functions.py:183-189` but is
worth fixing in the new code rather than perpetuating.

> **Both warnings CLOSED 2026-06-20** by quick task `260620-jvu` (harden-ledger-ack-warnings).
> WR-01: the three terminal-ack `except` handlers now nest the ack in a swallow-and-log
> `try/except`, so the original task error always re-raises (commit d9123af). WR-02: both
> failure schemas now use `cleared: Literal[True]`, machine-enforcing the always-True invariant
> (commit d992f84). IN-01 (info) is out of scope and remains open.

---

## Warnings

### WR-01: Terminal ack call is unguarded inside `except` block — double-failure leaves ledger row un-cleared

**Resolved:** 2026-06-20 — fixed in quick task 260620-jvu (commit d9123af). All three terminal-ack
`except` handlers now wrap the ack in a nested `try/except` that swallows + logs on the terminal
attempt; the trailing `raise` always re-raises the original task error (E1). New tests in
`test_scan.py`, `test_metadata_extraction.py`, and `test_fingerprint.py` prove no exception masking.

**File:** `src/phaze/tasks/scan.py:149-152`
Also: `src/phaze/tasks/metadata_extraction.py:73-75`, `src/phaze/tasks/fingerprint.py:64-66`

**Issue:** In the match-failure handler in `scan.py` and both new CR-02 handlers, the terminal
ack call is invoked bare inside an `except` block, with `raise` placed after it:

```python
# scan.py:149-152 (match-failure path)
except Exception:
    job = ctx.get("job")
    if job is not None and not job.retryable:
        await api.report_scan_terminal(payload.file_id)   # UNGUARDED
    raise  # never reached if the ack call raises
```

```python
# metadata_extraction.py:72-75
except Exception:
    job = ctx.get("job")
    if job is not None and not job.retryable:
        await api.report_metadata_failed(payload.file_id)  # UNGUARDED
    raise  # never reached if the ack call raises
```

```python
# fingerprint.py:63-66
except Exception:
    job = ctx.get("job")
    if job is not None and not job.retryable:
        await api.report_fingerprint_failed(payload.file_id)  # UNGUARDED
    raise  # never reached if the ack call raises
```

In Python, when a new exception is raised inside an `except` block, the new exception replaces
the pending one for propagation purposes — the subsequent bare `raise` is never executed. So if
the ack call raises (E2) while handling the original task failure (E1):

- E2 propagates to SAQ (not E1).
- The `raise` that would re-raise E1 is never reached.
- The ledger row is NOT cleared (the ack never completed).
- SAQ records E2 as the terminal failure, does not retry (retries exhausted), but the
  ledger row remains.
- On the next recovery pass, `recover_orphaned_work` finds the row still present and
  re-enqueues the job — the unbounded loop these handlers were introduced to prevent.

This scenario requires the controller to be unreachable for BOTH the primary call AND the ack
call in the same SAQ attempt, which is a double-failure. When the controller recovers, the
next recovery pass re-enqueues and the subsequent run can succeed, so the leak is temporary and
self-healing. However, it violates the "cleared on completion AND terminal failure" invariant
stated in L-02.

Note that the no-match path in the same function (`scan.py:106-113`) **correctly** handles
this by nesting the ack in its own `try/except` that swallows exceptions on the terminal
attempt. That pattern is the right one to apply here too.

The same unguarded pattern exists in `functions.py:183-189` (`process_file`), so this is a
pre-existing codebase convention being replicated in the new code rather than a novel defect
introduced here.

**Fix:** Wrap the ack call in a nested `try/except` that swallows on the terminal attempt,
mirroring the no-match path. The `raise` after the nested block always re-raises the original
exception E1 regardless of whether the ack succeeded:

```python
# scan.py match-failure path (lines 149-152) — apply same pattern to metadata and fingerprint
except Exception:
    job = ctx.get("job")
    if job is not None and not job.retryable:
        try:
            await api.report_scan_terminal(payload.file_id)
        except Exception:
            logger.warning(
                "scan_live_set match-failure terminal-ack failed",
                file_id=str(payload.file_id),
                exc_info=True,
            )
    raise  # always re-raise original exception so SAQ records the primary failure
```

Apply the same pattern in `metadata_extraction.py` around `report_metadata_failed` (line 74)
and in `fingerprint.py` around `report_fingerprint_failed` (line 65).

---

### WR-02: Failure response schemas use `cleared: bool` instead of `Literal[True]`

**Resolved:** 2026-06-20 — fixed in quick task 260620-jvu (commit d992f84). Both
`MetadataFailureResponse` and `FingerprintFailureResponse` now declare `cleared: Literal[True]`,
so Pydantic raises `ValidationError` on `cleared=False`. New schema-construction tests prove it.

**File:** `src/phaze/schemas/agent_metadata.py:48`
Also: `src/phaze/schemas/agent_fingerprint.py:38`

**Issue:** Both new failure response schemas declare `cleared: bool`:

```python
class MetadataFailureResponse(BaseModel):
    agent_id: str
    file_id: uuid.UUID
    cleared: bool          # can be True or False at the type level
```

The schema docstrings state "`cleared` is always `True`" — the endpoint never clears-and-returns-
`False`. This invariant is enforced only by the call-site literal `cleared=True` in the router,
not by the schema type. A future refactor that omits `cleared=True` from the constructor would
silently produce `cleared=False` responses: Pydantic would not raise (the field accepts both),
and callers testing `response.cleared` would get a wrong answer.

**Fix:** Change `cleared: bool` to `cleared: Literal[True]` in both schemas. Pydantic raises
`ValidationError` on any construction with `cleared=False` or a missing value that defaults to
`False`, making the invariant machine-enforced:

```python
# schemas/agent_metadata.py
from typing import Literal
import uuid
from pydantic import BaseModel

class MetadataFailureResponse(BaseModel):
    agent_id: str
    file_id: uuid.UUID
    cleared: Literal[True]
```

```python
# schemas/agent_fingerprint.py
from typing import Literal
import uuid
from pydantic import BaseModel

class FingerprintFailureResponse(BaseModel):
    agent_id: str
    file_id: uuid.UUID
    cleared: Literal[True]
```

---

## Info

### IN-01: `extract_tags` synchronous disk call is not dispatched via `asyncio.to_thread`

**File:** `src/phaze/tasks/metadata_extraction.py:49`

**Issue:** The synchronous `extract_tags(payload.original_path)` call blocks the SAQ event loop
for the duration of the mutagen header read. The adjacent `scan_directory` task correctly uses
`await asyncio.to_thread(compute_sha256, full_path)` for its disk I/O (`scan.py:261`). The
docstring acknowledges this ("Sync mutagen call -- I/O bound header read") without explaining
why `to_thread` is omitted. This is a pre-existing issue not introduced by this phase.

**Fix:** Wrap with `asyncio.to_thread` to match the `scan_directory` pattern:

```python
import asyncio
...
tags = await asyncio.to_thread(extract_tags, payload.original_path)
```

---

_Reviewed: 2026-06-19T22:57:19Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
