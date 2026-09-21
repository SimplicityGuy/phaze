# Stranded analysis diagnosis (`phaze-hia9z`)

Read-only production measurements on 2026-09-21. The source population and throughput baseline
come from [the earlier timing spike](spikes/phaze-zaf2l-where-phaze-spends-time.md), §§2, 3, and 6.
No archive paths, file IDs, or digests were copied into this record.

## Population and durable state

The discriminating predicate is an incomplete `analysis` row with real fine-tier progress and no
`cloud_job` sidecar:

```sql
SELECT count(*)
FROM analysis AS a
WHERE a.analysis_completed_at IS NULL
  AND a.failed_at IS NULL
  AND a.fine_windows_analyzed > 0
  AND NOT EXISTS (SELECT 1 FROM cloud_job AS c WHERE c.file_id = a.file_id);
```

It still returns **661 files**. The earlier spike measured these against **11,412** files with a
duration (**5.79%**) and **223.99** of **11,492.22** audio-hours (**1.95%**). Every one has
`fine_windows_analyzed = fine_windows_total`. None has a coarse-window count. All **661** have one
`scheduling_ledger` entry for `process_file`, and none has a matching `saq_jobs` key. All ledger
entries are agent-routed, carry `original_path` and `models_path` fields, carry no `scratch_path`,
and have NULL captured timeout and retry bounds. Their last enqueue stamp is 2026-08-08; their
analysis progress was last updated between 2026-07-31 and 2026-08-13. The current broker has
**8 queued** and **2 complete** rows, for other work. None of the 661 has an executed proposal,
so the normal approved-move path has not relocated their recorded source. Thirteen ledger payloads
still have the removed `fine_cap`/`coarse_cap` fields; the compatibility validator in
`src/phaze/schemas/agent_tasks.py::ProcessFilePayload` accepts and discards them on replay.

The cost of a full re-analysis is **223.99 audio-hours / 2.7183 audio-hours per wall-hour = 82.4
wall-hours**, or **3.43 days** of the measured burst lane. The fine-window counters are progress
only: `src/phaze/routers/agent_analysis.py::post_analysis_progress` writes those counters and
deliberately does not stamp completion or save `analysis_window` rows. The stored fine pass cannot
be resumed as a completed output by this pipeline; replay starts analysis from the source file.
Seventeen files do have **752** older `analysis_window` rows (**639 fine**, **113 coarse**) dated
2026-06-14 through 2026-06-18. Those predate the July–August partial progress, and the current
worker has no path that resumes from those rows. They do not reduce the **82.4 wall-hour** replay
estimate.

## What the evidence establishes

The progress POST persists a partial row without changing the completion or failure marker
(`src/phaze/routers/agent_analysis.py::post_analysis_progress`). Only the final result PUT stamps
`analysis_completed_at` and clears the ledger entry (`put_analysis` and
`_finalize_analysis_outcome` in the same module). The terminal failure callback stamps `failed_at`
and clears the ledger. A job that stops after progress but before either callback therefore leaves
exactly the observed partial row and ledger entry. The derived file state remains `discovered`
because neither terminal marker exists; the old `files.state` write was removed (the D-09 comments
beside those handlers).

The missing broker key makes each ledger row a recovery candidate. However,
`src/phaze/tasks/reenqueue.py::recover_orphaned_work` performs its normal startup pass only when
`count_inflight_jobs` is zero. With unrelated queued work present, it returns before reconciling
these rows. The manual `force=True` path bypasses that gate but runs over the *whole* ledger. A
read-only count found **3,035** `process_file` and **237** `s3_upload` ledger keys without a live
queued/active job. Cloud ownership and domain completion exclude many from actual replay. Among
`process_file` rows with neither a live key nor a cloud sidecar, there are **665**: the **661**
partial rows above and **4** with no fine progress. This is why a global Recover click is not a
precise 661-file action.

The exact event that ended the original jobs is **not recorded** in the surviving rows. A process
death, cancellation during a deploy, lost callback, or broker cleanup can all leave the same
signature. The earlier OOMKill idea is **unproven**. The absence of `scratch_path` and of a
`cloud_job` sidecar points to the local agent route rather than a staged cloud job; it does not
prove what happened to that agent. This verdict identifies the durable liveness gap without
claiming a cause the available evidence cannot distinguish.

## Operator visibility already added since the timing spike

The earlier spike correctly reported invisibility at its measurement date. The current code has
since added an amber `analyzeOrphan` badge on the global rail
(`src/phaze/templates/shell/partials/rail.html`). Its count comes from
`src/phaze/services/pipeline/orphans.py::get_stage_orphan_counts`, which deliberately reuses the
same live-job, completion, and cloud-ownership exclusions as recovery. The badge counts recovery
candidates, so it includes the **661** and the **4** other no-cloud orphans in this snapshot. It
does not claim that all 665 hold fine-tier work. The cache is refreshed off the five-second poll
path (`src/phaze/main.py::_orphan_refresh_loop`). This resolves the bead's visibility criterion;
adding a second stalled counter would double-present the same work under a different definition.
Read-only inspection of the running `phaze-api` image confirmed that its rail template contains
the same `analyzeOrphan` span, so this is deployed rather than only present in the source tree.

## Recovery decision

No production requeue was performed during the read-only investigation. A targeted requeue
must avoid the global Recover action's broader ledger population and account for the measured
**82.4 wall-hours** of lane work. `phaze backfill recover-stranded-analyses` is the targeted
operator command added by this bead. Its default mode counts only; `--enqueue` passes only the
selected keys to the existing owner-affine recovery machinery. The exact selector, including
fine-tier equality, missing cloud and live-job owners, and the executed-move exclusion, returned
**661** against production in a read-only dry run. Focused tests pin those exclusions, the default
read-only CLI behavior, and that scoped recovery never sweeps unrelated ledger rows.

**Decision, 2026-09-21 (`phaze-hia9z`): do not queue from an unreviewed worktree or use the global
Recover action.** Keep the 661 pending until the scoped command has passed review and reached the
deployed image. At that point its count-only run must still select the intended population before
the operator schedules the **82.4 wall-hour** replay. This preserves the durable ledger entries
and avoids accidentally re-driving the other orphaned stages. No production rows or queue jobs
were changed by this investigation.
