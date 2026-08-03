# ADR-0004 — Regenerate expiring ledger payloads at replay time; make replay-safety an enforced invariant

| | |
| --- | --- |
| **Status** | Accepted — implemented |
| **Date** | 2026-07-31 |
| **Decider** | Repository owner |
| **Investigation** | `phaze-71nz` (found while clearing the `phaze-o0n6` stranded-`active` backlog) |
| **Supersedes** | — |

______________________________________________________________________

## Context

`recover_orphaned_work` (`src/phaze/tasks/reenqueue.py`) — the controller startup hook **and** the
operator-facing "Recover" button, `POST /pipeline/recover` — replays each orphaned
`scheduling_ledger` row's **stored payload verbatim** through its keyed producer:

```python
await queue.enqueue(row.function, key=row.key, **policy, **row.payload)
```

That is correct only when a ledger payload is **time-invariant**. `s3_upload` payloads are not:
they embed `part_urls`, presigned multipart PUT URLs bounded by `s3_presign_put_ttl_sec` and signed
at the **original** enqueue (`services/cloud_staging._stage_file_to_s3`). Replaying a row that has
been orphaned for days therefore replays dead credentials.

**Measured 2026-07-31 on the live deployment.** A single `POST /pipeline/recover` replayed 430
orphaned `s3_upload` rows onto the io lane. 428 ran their retries out to terminal `failed`; the io
worker logged **122× HTTP 403 and 257× HTTP 400** across the window — the signature of
expired/invalid presigned URLs. **Zero succeeded.** The same run's 2,512 `process_file` rows
replayed onto the analyze lane and processed normally, because that payload (`file_id`, paths, caps,
`agent_id`, `models_path`) carries nothing that expires.

The blast radius was bounded and non-corrupting (the files kept their `cloud_job` rows, the staging
reaper spilled them back, and SAQ expired the terminal `failed` rows on its own), so the cost was
not data loss. The cost was **trust in an incident tool**. An operator reaching for Recover is by
definition already having a bad day, and the button burned an entire stage into `failed` while
returning `200` and *"Recovery started — re-enqueuing any orphaned work across all stages."* Nothing
in the response, the logs, or the ledger distinguished "replayed and will succeed" from "replayed
and is guaranteed to fail". Blast radius scales with how long rows have been orphaned — which is
exactly when recovery is most likely to be used.

## Decision

Three parts, taken together.

### 1. Option (a): REGENERATE at replay time — the chosen approach

The bead offered three:

| | Approach | Verdict |
| --- | --- | --- |
| **(a)** | Regenerate the presigned URLs at replay time from durable inputs | **CHOSEN** |
| (b) | Exclude `s3_upload` from ledger replay; let the staging path re-drive it | Rejected |
| (c) | Stop storing derived URLs in the ledger payload at all | Rejected |

**(a) is chosen because the codebase already contains the regenerator**, and reusing it means
recovery duplicates zero staging logic. `services/cloud_staging.redrive_upload` — the live
`POST /agents/s3/{file_id}/failed` under-cap re-drive path — already does exactly the required work:
best-effort abort the prior multipart, initiate a fresh one, presign fresh part URLs, refresh the
`cloud_job` row, and park a fresh `s3_upload` enqueue. Recovery now calls it through a small
registry (`reenqueue._REPLAY_REGENERATORS`). The only inputs consumed are durable: the row's
`payload["file_id"]` and the recorded `cloud_job.staging_bucket` / `backend_id` (MKUE-02). Nothing
time-limited is read from the ledger payload at all.

This keeps the stage genuinely **recoverable**, which is the property the operator is pressing the
button for. It also cannot drift from the live staging path, because it *is* the live staging path.

**(b) was rejected** because it converts the incident tool into a partial one. Recovery would stop
producing guaranteed-failing jobs but would also stop recovering the stage; the operator's
`s3_upload` backlog would depend entirely on the `stage_cloud_window` drain noticing it, and the
"Recover" button would silently mean less than its name. The bead offers (b) only as a fallback "if
(a) cannot be done without duplicating staging logic" — and it can, so the fallback is not needed.

**(c) was rejected** — for now — because the ledger payload is *also* the live re-drive loop's
state. `POST /agents/s3/{file_id}/failed` reads the row it is keyed on, and `redrive_upload` commits
a **fresh** payload (fresh `part_urls`) back to that same row, which `tests/agents/routers/
test_agent_s3.py::test_failed_under_cap_redrive_keeps_fresh_part_urls` pins deliberately (WR-02).
Removing `part_urls` from the payload means the enqueue kwargs and the ledger payload stop being the
same object, which breaks the single-`before_enqueue`-chokepoint contract that makes the ledger
trustworthy in the first place (Phase 45 L-01). (c) is the cleaner end state and remains open as a
later refactor; it is a larger change than this bug warrants, and (a) makes it unnecessary for
correctness.

### 2. The invariant, stated and enforced

> **A `scheduling_ledger` payload must be replayable at an arbitrary future time.**

This — not `s3_upload` specifically — is the root cause. `s3_upload` did not break replay; it was
added as a keyed producer whose payload happened to embed presigned URLs, and no layer ever asked
whether that was replayable. The invariant now lives in `src/phaze/tasks/_shared/replay_safety.py`
and is enforced in three places, none of which name `s3_upload` as the condition:

1. **Classification (anti-omission).** Every keyed producer is declared either
   `LEDGER_REPLAY_TIME_INVARIANT` or `LEDGER_REPLAY_REGENERATED`. The split is total and disjoint
   over `deterministic_key._KEY_BUILDERS`, asserted by test. A new keyed producer fails the build
   until its author answers the question. Every member of `LEDGER_REPLAY_REGENERATED` must have a
   registered regenerator — `reenqueue` raises at import on a mismatch — so "never replay verbatim"
   can never quietly become "never recover this stage".
2. **Content detection at the write chokepoint.** `apply_deterministic_key` screens each payload it
   is about to make durable with `find_time_limited_paths` and logs an ERROR (payload *paths* only —
   the values are credentials) when a declared-replay-safe producer writes presign / token / expiry
   material. It **detects, never blocks**: the ledger write is best-effort by contract (T-45-03) and
   an enqueue must never fail on a bookkeeping opinion.
3. **Refusal at replay.** `_replay_row` runs the same detector before every verbatim replay and
   refuses on a hit — tallying `unreplayable` rather than enqueueing a job that cannot succeed. This
   is the general net: a future producer that stores expiring material and is *mis*-classified as
   time-invariant is caught by the substrate rather than by an incident.

The detector is a value-shape rule (presigned-URL query parameters in both SigV4 and the SigV2
`AWSAccessKeyId`/`Expires`/`Signature` form the live store emitted, plus token / signature /
credential / explicit-expiry key names), not a schema rule, because the keyed producers share no
payload base class and expiry is a property of a *value*, not of a type — `part_urls: list[str]` is
an ordinary annotation whose contents happen to be credentials.

### 3. The operator surface

`recover_orphaned_work`'s per-stage tally gains a fourth counter, `unreplayable`, distinct from the
three that existed:

| Counter | Meaning |
| --- | --- |
| `reenqueued` | a fresh job landed; the work **will** run |
| `skipped` | the deterministic key deduped against a live job; the work is **already** running |
| `errored` | the replay itself failed transiently; the ledger row survives and the next pass retries |
| `unreplayable` | the row was **deliberately not** replayed; the work is **not covered** by this run |

The run-wide total is hoisted to the top level of the return value. Because
`POST /pipeline/recover` is fire-and-forget, its response genuinely cannot know the outcome — so the
returned fragment now polls `GET /pipeline/recover/status`, which renders the finished tally and, on
`unreplayable > 0`, a distinct warning naming the skipped stages instead of the success copy.
"Recovery started" is no longer the last word an operator sees.

## Consequences

- An orphaned `s3_upload` row now costs real S3 work at recovery time (an abort + a create + a
  presign per row, serially) instead of a bare enqueue. Recovery is a background task and the row
  count is bounded by the orphan set, so this is accepted; correctness over throughput.
- A row whose regeneration inputs are unavailable — no `FileRecord`, no resolvable staging bucket,
  no online fileserver — is skipped and **visibly** tallied. That is a deliberate reduction in what
  recovery silently claims to have done.
- The regenerated enqueue follows `_stage_file_to_s3`'s own destination choice
  (`select_active_agent(kind="fileserver")`), not the row's `payload["agent_id"]`. On a
  multi-fileserver deployment that is not phaze-fjii's per-owner rule. This is **inherited, not
  introduced**: it is what the live staging path does today, and recovery must land where the live
  producer would. Fixing the choice belongs with the staging path.
- `_recovery_state` (the polled last-run cell in `routers/pipeline.py`) is process-local. The API
  runs as a single uvicorn process (`Dockerfile` CMD sets no `--workers`); were that to change, the
  poll would degrade to "no recent recovery" — never to a wrong answer — and the cell would need to
  move to Redis.

## Verification

- `tests/analyze/tasks/test_recovery_replay_safety.py` drives an orphaned `s3_upload` row through
  `recover_orphaned_work` against a **real** wire-compatible object store (`ThreadedMotoServer`),
  not a mocked S3 client: the stored payload is proven dead **by the store** before recovery runs,
  the enqueued payload is proven to differ, and the regenerated URL is proven to **work** (a real
  part PUT returning an ETag). Reverting to verbatim replay makes it red on both halves.
- `tests/analyze/core/test_replay_safety.py` verifies the detector against real presigned URLs in
  both signature versions, verifies it is quiet on all nine time-invariant producers' payload
  shapes, and holds the classification totality/disjointness/regenerator-coverage assertions.
- The general (non-`s3_upload`) case is covered by driving a `process_file` row carrying a presigned
  URL through recovery and asserting it is refused — the guard is about payloads, not names.

## Notes

moto does not enforce presigned-URL expiry, so the presign-TTL half of the failure cannot be
reproduced against it directly; that half is covered by testing the detector against genuinely
signed URLs. What the harness does reproduce faithfully is the other half of the same production
reality — an orphaned staging upload's multipart is swept (by `redrive_upload`'s abort, the terminal
`/failed` handler, or the `AbortIncompleteMultipartUpload` bucket-lifecycle rule this repo
configures, phaze-sqpv), after which its part URLs are rejected by the store no matter how well
signed they are.
