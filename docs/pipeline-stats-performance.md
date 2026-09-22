# Pipeline stats skip-probe correction (phaze-ajnaa)

The historical [pipeline timing spike](spikes/phaze-zaf2l-where-phaze-spends-time.md) measured
`/pipeline/stats` at 534.0 ms mean on an 11,428-file corpus and found about 7,000–8,100
`stage_skip` index scans per request. Its suggested source was the shared correlated
`skipped_clause` predicate. This follow-up identifies the polled caller that triggers those
probes. The historical spike is unchanged because its evidence audit locks that file.

## Production baseline, 2026-09-21

The current corpus held 25,866 files, 6,847 awaiting cloud rows, and zero `stage_skip` rows.
Two warmups followed by seven host-local HTTPS requests to `/pipeline/stats`, measured with
`curl time_starttransfer`, gave 608.5, 576.1, 597.4, 568.5, 555.1, 570.3, and 571.9 ms:
**578.3 ms mean**. Two 15-request rounds, each corrected by a duration-matched idle window
using `pg_stat_user_tables`, measured **6,394.3** and **5,938.1** `stage_skip` scans per request.

Read-only `EXPLAIN (ANALYZE, BUFFERS)` showed that the corpus-wide status-bucket aggregate
hashes its skip subquery once. The polled `get_awaiting_cloud_count` instead performed **6,847**
`stage_skip` index searches in one invocation. A one-to-one left join still produced **5,137**
index searches. A positive membership predicate over the non-null `(file_id, stage)` marker set
produced a hashed subplan with **one** index search. The original and optimized counts both
returned **2,767** in one read-only production transaction. Their single-run execution times
were 23.7 and 21.3 ms, respectively; that small difference is timing noise, not a measured
endpoint improvement.

## Decision and verification

The five-second poll remains global while the tab is visible; background tabs shed requests.
It drives DAG rail counters and controls on every admin page, so changing its cadence would
change operator feedback. The sequential reads in `_build_dag_context` are a separate lever
for a separately measured change.

After review and deployment, remeasure endpoint mean with two warmups and seven timed
`time_starttransfer` samples. Recheck per-request scan fan-out using two 15-request rounds,
each with a duration-matched idle control. Compare the post-deploy results with both the
historical 534.0 ms baseline and the current 578.3 ms baseline on its larger corpus. The
implementation is reviewable now; the endpoint and whole-request scan acceptance measurements
remain pending deployment.
