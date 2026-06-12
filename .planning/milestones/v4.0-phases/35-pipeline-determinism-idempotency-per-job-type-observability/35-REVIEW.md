---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
reviewed: 2026-06-12T03:30:50Z
depth: standard
files_reviewed: 18
files_reviewed_list:
  - alembic/versions/019_add_proposals_pending_unique_index.py
  - src/phaze/main.py
  - src/phaze/models/proposal.py
  - src/phaze/routers/agent_files.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/agent_task_router.py
  - src/phaze/services/ingestion.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/pipeline_counters.py
  - src/phaze/services/proposal.py
  - src/phaze/tasks/_shared/deterministic_key.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/controller.py
  - src/phaze/templates/base.html
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/dag_canvas.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - src/phaze/schemas/agent_tasks.py
findings:
  critical: 2
  warning: 4
  info: 2
  total: 8
status: resolved
resolution:
  resolved_at: 2026-06-12
  commits:
    - "bddd3a1 — CR-01/CR-02: build complete metadata + fingerprint enqueue payloads"
    - "01e1245 — WR-01/WR-03/WR-04: harden proposal upsert + counter fallback"
  fixed: [CR-01, CR-02, WR-01, WR-03, WR-04]
  deferred:
    - "WR-02 — proposed_path traversal: pre-existing storage-side gap; exploit depends on the out-of-scope execution/move stage's own path guard. Tracked as follow-up."
  notes: "IN-01/IN-02 (info) left as-is — latent-consistency / micro-optimization, no behavior impact."
---

> **Resolution (2026-06-12):** Both BLOCKERs and three of four WARNINGs fixed with regression
> tests (commits `bddd3a1`, `01e1245`). WR-02 deferred (see frontmatter). The four phase pillars
> — determinism (D-05), idempotency (D-04), counters (D-02/D-03), DAG canvas — verified correct.

# Phase 35: Code Review Report

**Reviewed:** 2026-06-12T03:30:50Z
**Depth:** standard
**Files Reviewed:** 18
**Status:** issues_found

## Summary

Phase 35 lands four pillars: a central `before_enqueue` deterministic-key hook (D-05),
a partial-index proposals upsert + migration 019 (D-04), maintained Redis counters with
DB-truth reconcile (D-02/D-03), MANUAL-META removal of metadata auto-enqueue (D-06), and a
new DAG canvas (35-05).

Three of the four pillars are solid. The deterministic-key hook is registered at all four
enqueue seams (`main.py`, `agent_task_router.py`, `controller.py`, `agent_worker.py`), the
batch-hash is order-independent and collision-safe, the drift-guard test enforces full
routable coverage, the proposals upsert correctly targets the partial `WHERE status='pending'`
index (human approvals are structurally protected), migration 019 orders dedupe-before-index
correctly, and the counter reconcile isolates failures so the 5s poll can never 500. The DAG
canvas triggers only hit existing endpoints, edges are honest, and all template interpolation
is server-computed ints (no XSS).

However, the D-06 work introduced a **shipping blocker**: removing the auto-enqueue path
deleted the *only* code that built a complete `ExtractMetadataPayload`. The surviving manual
trigger — now the sole metadata path, surfaced by the new "Extract Metadata" canvas button —
enqueues `file_id` only, which fails strict payload validation. Metadata extraction is broken
end-to-end. The fingerprint trigger has the identical (pre-existing, now in-scope) defect.

## Critical Issues

### CR-01: MANUAL-META metadata trigger enqueues an incomplete payload — every job dead-letters

**File:** `src/phaze/routers/pipeline.py:422-425` (helper) — consumed by `:428-488`
**Issue:**
D-06 removed the auto-enqueue from `routers/agent_files.py`, which was the **only** site that
built the full payload:

```python
# REMOVED in this phase (agent_files.py) — was correct:
payload=ExtractMetadataPayload(file_id=row.id, original_path=row.original_path,
                               file_type=row.file_type, agent_id=agent.id)
```

The surviving (and now sole) enqueue path sends only `file_id`:

```python
async def _enqueue_extraction_jobs(queue: Any, file_ids: list[str]) -> None:
    for fid in file_ids:
        await queue.enqueue("extract_file_metadata", file_id=fid)
```

The worker task validates strictly (`schemas/agent_tasks.py:40-48`, `ExtractMetadataPayload`
has `model_config = ConfigDict(extra="forbid")` and four required fields with no defaults):

```python
# tasks/metadata_extraction.py:34
payload = ExtractMetadataPayload.model_validate(kwargs)  # kwargs == {"file_id": fid}
```

`model_validate({"file_id": fid})` raises `ValidationError` (missing `original_path`,
`file_type`, `agent_id`) on **every** job, so SAQ retries then dead-letters all of them. This
is the same class as the v4.0.8 `process_file` payload incident — and because D-06 makes this
the only metadata path (and the DAG canvas "Extract Metadata" button POSTs straight to
`/pipeline/extract-metadata`), operator-triggered metadata extraction is fully broken. The
trigger endpoints already have `original_path` / `file_type` (on the loaded `FileRecord`) and
`routed.agent_id` in scope — they are simply discarded. No test drives the endpoint and
asserts a complete payload, so the gap is uncovered.

**Fix:** Build the full payload per file, mirroring `analysis_enqueue.enqueue_process_file`:

```python
from phaze.schemas.agent_tasks import ExtractMetadataPayload

async def _enqueue_extraction_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    for f in files:
        payload = ExtractMetadataPayload(
            file_id=f.id, original_path=f.original_path,
            file_type=f.file_type, agent_id=agent_id,
        )
        await queue.enqueue("extract_file_metadata", **payload.model_dump(mode="json"))
```

Thread `files` (not `file_ids`) and `routed.agent_id` through both
`trigger_metadata_extraction` and `trigger_extraction_ui`.

### CR-02: Fingerprint trigger enqueues an incomplete payload (same defect class)

**File:** `src/phaze/routers/pipeline.py:494-497`
**Issue:**
`_enqueue_fingerprint_jobs` enqueues `fingerprint_file` with only `file_id`:

```python
async def _enqueue_fingerprint_jobs(queue: Any, file_ids: list[str]) -> None:
    for fid in file_ids:
        await queue.enqueue("fingerprint_file", file_id=fid)
```

But `FingerprintFilePayload` (`schemas/agent_tasks.py:51-58`) requires `file_id`,
`original_path`, **and** `agent_id` with `extra="forbid"`, and the worker validates strictly
(`tasks/fingerprint.py:33` `FingerprintFilePayload.model_validate(kwargs)`). Every fingerprint
job therefore fails validation and dead-letters. This is pre-existing (the manual fingerprint
trigger has been the only path since Phase 16, while the strict payload arrived in Phase 26),
but it lives in an in-scope file and is the same correctness failure as CR-01, so it must be
fixed in the same pass.

**Fix:** Build the full `FingerprintFilePayload(file_id, original_path, agent_id)` per file,
exactly as in the CR-01 fix; thread `files` + `routed.agent_id` through
`trigger_fingerprint` and `trigger_fingerprint_ui`.

## Warnings

### WR-01: `store_proposals` indexes `file_ids` / `files_context` with an untrusted LLM `file_index`

**File:** `src/phaze/services/proposal.py:299, 310`
**Issue:**
`fid = file_ids[proposal.file_index]` and `files_context[proposal.file_index]` use
`proposal.file_index` directly, where `FileProposalResponse.file_index` is a plain `int` the
LLM emits with no bounds. A hallucinated index ≥ `len(file_ids)` raises `IndexError` and
crashes the whole `generate_proposals` batch. Worse, a **negative** index silently wraps
(Python negative indexing) and writes the proposal against the *wrong* file — a data-integrity
bug with no error.

**Fix:** Validate the index before use:

```python
idx = proposal.file_index
if not (0 <= idx < len(file_ids)):
    logger.warning("proposal file_index out of range", file_index=idx, batch_size=len(file_ids))
    continue
fid = file_ids[idx]
```

### WR-02: LLM-proposed `proposed_path` is stored with traversal segments intact

**File:** `src/phaze/services/proposal.py:312-316`
**Issue:**
The only sanitization on the LLM-chosen destination path is leading/trailing-slash strip and
`//` collapse:

```python
path_raw = path_raw.strip("/")
while "//" in path_raw:
    path_raw = path_raw.replace("//", "/")
```

`..` segments survive (`"/../../etc/".strip("/")` → `"../../etc"`). If the execution stage
joins `proposed_path` under a destination root without its own traversal guard, this is a path
traversal that lets a prompt-injected/hallucinated proposal escape the managed tree. Storage is
in-scope here even though the file move is not.

**Fix:** Reject or strip path components equal to `..` (and any absolute-drive/UNC prefix)
before persisting, e.g. drop segments where `seg in ("", ".", "..")`. Confirm the execution
stage also validates the final resolved path stays within the destination root (defense in
depth).

### WR-03: Counter degrade-fallback mixes job/batch counts into a per-file `done`

**File:** `src/phaze/routers/pipeline.py:52-60, 82-92`
**Issue:**
`_NODE_COMPLETED_FNS` maps `proposals → ("generate_proposals",)` and
`scan_search → ("scan_live_set", "search_tracklist")`, but those `completed` counters count
**job/batch** completions, whereas the node's `done` is a **distinct-file/tracklist** count
(`get_stage_progress`). When the DB `done` reads 0 and the counter is > 0, `_reconciled_done`
renders the batch/job count as the file `done` — e.g. one completed `generate_proposals` batch
of 10 files renders `proposalsDone = 1`, understating, while repeated re-runs of the same file
inflate other nodes. This only fires in the degraded DB-down branch and is documented as a
backstop, but it presents a number with the wrong unit.

**Fix:** Either drop the batch-shaped functions (`generate_proposals`) from the fallback map,
or cap the fallback at the node `total` and label it as an estimate. At minimum, document the
unit mismatch at the call site so a future reader does not treat the fallback as authoritative.

### WR-04: `store_proposals` unconditionally regresses file state to `PROPOSAL_GENERATED`

**File:** `src/phaze/services/proposal.py:348-351`
**Issue:**
After the upsert, the file's state is set to `PROPOSAL_GENERATED` for every row in the batch,
with no check on the current state. The convergence-gate query normally excludes already-proposed
files, but a stale or duplicated batch (the deterministic key dedups in-flight, not historical)
can still carry a file that has since reached `APPROVED` / `EXECUTED`. Re-running then yanks that
file's lifecycle state backward to `PROPOSAL_GENERATED` even though its approved proposal row is
(correctly) untouched — leaving file state and proposal state inconsistent.

**Fix:** Only advance state forward, e.g. skip the assignment when
`file_record.state in {FileState.APPROVED, FileState.DUPLICATE_RESOLVED, FileState.EXECUTED}`,
or guard the update with an explicit forward-only state check.

## Info

### IN-01: Proposals triggers omit the `NoActiveAgentError` guard the sibling triggers use

**File:** `src/phaze/routers/pipeline.py:246, 410`
**Issue:**
`trigger_proposals` / `trigger_proposals_ui` call
`enqueue_router.resolve_queue_for_task("generate_proposals", ...)` without the
`try/except NoActiveAgentError` wrapper that `trigger_analysis`, `trigger_fingerprint`, and
`trigger_metadata_extraction` all use. `generate_proposals` is a controller task so it should
not raise today, but the asymmetry is a latent 500 if routing semantics ever change.
**Fix:** Wrap in the same guard for consistency, returning the friendly no-agent message.

### IN-02: `store_proposals` issues a per-proposal `SELECT` for the file-state update

**File:** `src/phaze/services/proposal.py:348-351`
**Issue:**
Each proposal triggers a separate `SELECT FileRecord WHERE id = fid` to flip state — an N+1
across the batch. Performance is out of scope for v1, noted only as a maintainability flag.
**Fix:** Batch the state transition into a single
`update(FileRecord).where(FileRecord.id.in_(file_ids))` after the upsert loop.

---

_Reviewed: 2026-06-12T03:30:50Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
