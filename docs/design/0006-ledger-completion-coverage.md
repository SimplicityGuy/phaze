# ADR-0006 — Every file-keyed agent stage carries a completion predicate, and the reaper clears what recovery ignores

| | |
| --- | --- |
| **Status** | Accepted — implemented |
| **Date** | 2026-08-09 |
| **Decider** | Repository owner |
| **Investigation** | `phaze-k95r7` (found while investigating the amber "orphaned analyze file(s) awaiting recovery" badge) |
| **Supersedes** | — |
| **Related** | ADR-0004 (ledger replay safety), `phaze-2u8v.2` (the ledger reaper) |

______________________________________________________________________

## Context

On 2026-08-08 a single operator `POST /pipeline/recover` put ~1,247 jobs onto an analyze queue that
already had ~2,000 backed up, and 17 `s3_upload` ledger rows had been reporting `unreplayable` on
every recovery run for a month. Both symptoms were attributed to one cause — "recovery re-enqueues
already-completed work". They are two different findings, and conflating them is what sent the
investigation after an expiry problem that did not exist.

## Finding 1 — the analyze lane's predicate was already correct

`is_domain_completed` returns **`True`** for a `process_file` ledger row whose file carries a
completed analysis; such a row is neither counted by the amber badge nor re-enqueued. Verified
directly against Postgres (`tests/analyze/tasks/test_recovery.py::test_completed_analyze_row_is_neither_orphan_nor_reenqueued`).

What it reads is DERIV-03's discriminator:

```sql
done(analyze)  ⇔  EXISTS (SELECT 1 FROM analysis
                          WHERE file_id = files.id AND analysis_completed_at IS NOT NULL)
```

**Not** "an `analysis` row exists", and **not** "`failed_at IS NULL`". The gap between those three
spellings is a real population, not a technicality:

| shape | `analysis_completed_at` | domain-complete? | recovery re-drives? |
| --- | --- | --- | --- |
| completed (`PUT /analysis/{id}`) | set | yes | **no** |
| terminally failed | NULL (`failed_at` set) | yes | **no** |
| **partial** (`POST /analysis/{id}/progress` only) | **NULL** | **no** | **yes — correctly** |

A partial row carries real window counters and a real aggregate. It is written by the mid-flight
progress endpoint, which deliberately strips every completion side effect. A file in that state
started an analysis that never finished, so re-driving it is the correct behaviour — and the *only*
behaviour that ever completes it.

**Consequence for the live measurement.** A corpus query that counts "successfully analyzed" as row
existence (or as `failed_at IS NULL`) over-counts what recovery actually re-enqueues, by exactly the
partial-row population. The live figures behind this bead (1,212 of 3,734 `process_file` ledger rows,
and 774 of 1,545 queued analyze jobs) were **not** re-measured against `analysis_completed_at`, so
they are not yet evidence that completed work was re-enqueued. The re-measurement query is in
[Verification](#verification) below; run it before concluding anything further about the analyze lane.

## Finding 2 — `s3_upload` had no completion predicate at all

This is the actual defect, and the whole of the 17-row case.

`_DOMAIN_COMPLETED_STAGES` covered `process_file`, `extract_file_metadata` and `push_file`.
`s3_upload` was absent, so `is_domain_completed` returned `False` for **every** `s3_upload` row
unconditionally, no matter how finished its file was. Nothing else could have excluded them: the two
cloud exclusions (`_in_flight_cloud_job_ids` / `_awaiting_cloud_job_ids`) both key off a `cloud_job`
row, and these files no longer had one.

Nothing reaped them either. `ledger_reaper` was scoped to `Stage` members, and `s3_upload` is not a
`Stage` — so the rows were simultaneously **permanently unreplayable and permanently counted**, which
is the worst available combination.

The reported reason was also wrong in every particular. `_redrive_bucket` returned `None` for two
structurally different cases — "no `cloud_job` row at all" and "a row exists but no bucket resolves" —
and `redrive_upload` collapsed both into *"could not resolve a staging bucket"*, which the caller then
logged as *"its payload is time-limited and cannot be regenerated right now"*. For a completed file
nothing was time-limited, nothing was mis-bucketed, and the row was never a recovery candidate.

### The membership rule, stated

The covered set is exactly **the file-keyed agent tasks**: every keyed function that is both routed to
an agent (`enqueue_router.AGENT_TASKS`) and keyed on a `file_id` (`deterministic_key._KEY_BUILDERS`).

That is the population whose ledger clear is unreliable — the work runs off-controller, so the row is
cleared by a control-side callback that a crash, a restart or a swept `saq_jobs` row can lose. A
controller task's clear rides the broker's own `after_process`, so a surviving row there is genuinely
orphaned and needs no domain net; `write_file_tags` is agent-routed but keyed on a `log_id`, so no
per-file completion predicate can exist for it.

`s3_upload` satisfied both halves of that rule and had simply never been added. The totality test now
**derives** the set from those two registries rather than restating a hand-kept list, so the next
producer to qualify cannot be omitted silently the way this one was.

## Decision

1. **`s3_upload` joins `_DOMAIN_COMPLETED_STAGES`**, resolved against the same predicate `push_file`
   already used, now factored into one builder, `stage_status.cloud_lane_completed_clause()`:
   `cloud_job.status = 'succeeded' OR domain_completed_clause(ANALYZE)`. Both disjuncts say the same
   thing from opposite ends of the lane — SUCCEEDED covers the landed-but-not-yet-analyzed window;
   `domain_completed(analyze)` covers the onward advance, terminal failure included.
2. **The reaper gains a cloud-lane pass** (`resolved_cloud_ledger_clause`), the function-keyed twin of
   `resolved_ledger_clause`: `inflight ∧ ¬running ∧ completed`, with `running` counting **both**
   substrates (`saq_jobs` *and* a busy `cloud_job`, since the upload is invisible to the broker once
   its row is swept). It reaps exactly what recovery now ignores. Deriving the two independently is
   how a row ends up excluded-but-immortal, which is precisely how these 17 survived a month.
3. **The two `_redrive_bucket` failure modes are split.** `existing is None` raises
   `NoCloudJobToRedriveError` (a subclass of `S3StagingError`, so existing handlers are unaffected),
   which recovery reports as `StaleLedgerRow` with its own message. The run-level summary no longer
   asserts a cause at all — it states the count and the stages and defers to the per-row warnings,
   because "time-limited" was only ever one of at least two.

### Rejected: filtering at the badge

The badge and recovery share `is_domain_completed` by design; the parity is documented as
DEFINITIONAL. Every change here is inside that shared predicate, so both surfaces move together and
the contract is preserved by construction rather than by a second assertion.

## The 328 ambiguous rows — classified, and left alone

The bead identified 328 queued analyze jobs where the ledger entry is **newer than or equal to** the
analysis, and correctly refused to assume they were stale. They are **legitimate, and they were not
produced by recovery.** Two disjoint shapes, one verdict:

- **A re-analysis ("deepen") request.** The deepen path re-enqueues `process_file` for a file that is
  already analyzed and, by design, **keeps the old `analysis_completed_at`** until the fresh
  `put_analysis` lands (`routers/pipeline.py`, the timestamp-gated completion predicate). That is
  exactly the "ledger newer than the analysis" shape. Such a file **is** domain-complete, so
  `is_domain_completed` returns `True` and recovery has never re-driven it — before or after this
  change. The queued job came from the operator's own click. Deleting it cancels a deep analysis
  somebody asked for.
- **A partial analysis** (`analysis_completed_at IS NULL`). Genuinely unfinished work that recovery is
  right to re-drive, per Finding 1.

Either way the verdict is the same: **not stale, not safe to delete, and untouched by this change.**
The one behaviour worth flagging for a future bead is the *opposite* asymmetry the first shape
implies — an orphaned deepen request is indistinguishable from a completed analysis and is therefore
silently dropped by recovery. That is a gap in re-analysis durability, not an over-enqueue, and it is
out of scope here.

## Verification

Local (both lanes, real Postgres): `tests/analyze/tasks/test_recovery.py` (completed vs partial
analyze, completed vs pending `s3_upload`, the stale-row message), `tests/analyze/tasks/test_ledger_reaper.py`
(the cloud-lane pass and its two liveness guards), `tests/integration/test_orphan_count.py`
(badge/recovery parity). Full suite green.

**Not verified locally, and left for the operator** — the acceptance criterion "a fresh recovery run
on the live corpus re-enqueues none of the 446" needs the live corpus. Two steps, in order:

1. **Re-measure with the real discriminator.** The count that matters is not "has an analysis row":

   ```sql
   SELECT count(*) AS ledger_rows,
          count(*) FILTER (WHERE a.analysis_completed_at IS NOT NULL) AS truly_complete,
          count(*) FILTER (WHERE a.id IS NOT NULL
                             AND a.analysis_completed_at IS NULL
                             AND a.failed_at IS NULL)                AS partial_only
     FROM scheduling_ledger sl
     JOIN files f ON f.id = (sl.payload ->> 'file_id')::uuid
     LEFT JOIN analysis a ON a.file_id = f.id
    WHERE sl.function = 'process_file';
   ```

   `truly_complete` is the population recovery excludes (and the reaper clears). `partial_only` is the
   population it re-drives on purpose. If the original 1,212 lands mostly in `partial_only`, the
   analyze lane needs no further change and the badge is reporting real outstanding work.

2. **Then run Recover and compare.** The `s3_upload` `unreplayable` count should be 0 for any file
   whose analysis has landed, and the `*/5` reaper tick should report non-zero `s3_upload` /
   `push_file` counters once as it clears the historical backlog, then settle to zero.
