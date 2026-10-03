# Coverage baseline refresh (phaze-zimj8)

## Initial full-suite measurement, 2026-10-03

Provenance: `e1925742a5b1281183b9d26e8a34663df6be1c69`. Previous baseline provenance:
`65e5af46e5812969445b1d6c4aaf09a8b1a4b5d7`. The original false regression was caused by `4796ab47`,
which removed covered review-service branches without refreshing their denominator.

The isolated serial full default non-browser suite reported **9964 passed, 6 skipped,
195 deselected, 314 warnings in 1580.92 s**. Coverage was **99.12% lines**, **96.51%
branches**, and **98.65% combined**; every tracked module passed the 90% line floor.
The database header named the isolated seat on localhost:5433 with an exclusive lock.

The old baseline contains **258 modules**; this full report contains **341 modules**.
**216 module entries moved**, including new and removed paths. The blast radius is every
future bead whose branch gate consumes this baseline; no runtime population changes.

## Unsafe decreases found and repaired before regeneration

The initial report must not be accepted unchanged: four modules lost branch percentage
while their uncovered count increased. Commit `5f539975` adds missing boundary cases
without changing production code:

| Module | Old covered/total | Initial covered/total | Repair |
| --- | ---: | ---: | --- |
| `routers/agent_analysis.py` | 22/22 | 53/54 | Typed style list derives an omitted dominant label |
| `schemas/agent_analysis.py` | 4/4 | 13/14 | Legacy style label rejects storage-column overflow |
| `services/analysis_wire.py` | 34/36 | 38/42 | Empty/invalid mood scores and invalid style geometry/entries |
| `tasks/functions.py` | 36/36 | 45/46 | Coarse-work heartbeat arrives before the first progress count |

These focused cases passed **6 tests in 0.39 s**, on the isolated exclusive database.
The repaired full measurement below verifies all four module branches at 100%.

Two percentage improvements also have increased uncovered counts: `job_runner.py`
moves from 41/46 to 52/58 (5 to 6 uncovered), and `services/video_audio.py` moves from
42/48 to 53/60 (6 to 7 uncovered). These are explicitly reported; the old branch gate
already accepts their improved ratios. No claim is made that uncovered counts never rose.

The historical review-service 42/44 acceptance number is obsolete: the current
`services/review.py` facade has **41/41 lines and no branches**. Its old 44/46 baseline
cannot be compared as a current denominator. The moved implementation modules appear
in the new-path rows below.

## Repaired full measurement and baseline provenance, 2026-10-03

The second isolated serial full default non-browser suite measured commit
`59bef0afbe3bde51664f8fc3a2c93e034684f83e`, with greenlet tracing and per-test coverage
contexts. It completed **9968 passed, 1 failed, 6 skipped, 195 deselected, 314 warnings
in 1686.75 s**. The sole failure was the new audit document being placed in the frozen
historical `docs/spikes` population. This was a failed validation run, not a green gate.
Moving this current audit to maintained `docs/coverage-baseline-refresh.md` restored the
unchanged historical corpus: its two checks passed in **23.61 s**.

The complete measurement is still authentic coverage evidence: **99.13% lines**,
**96.62% branches**, and **98.69% combined**. The standalone floor check passed 95%
repo-wide lines and 90% for every tracked module. Its binary coverage artifact retains
**17986 test contexts**. The regenerated baseline records the actual measured
`59bef0af` provenance, **341 modules**, and **250 branch-bearing modules**; it does not
claim a later main commit produced this measurement. All **216** entries that changed
against the old baseline remain accounted for in the initial table below, with exactly
these five subsequent measurement differences:

| Module | Initial full covered/total branches | Repaired full covered/total branches | Repaired lines covered/total | Uncovered branch change against old baseline |
| --- | ---: | ---: | ---: | ---: |
| `routers/agent_analysis.py` | 53/54 | 54/54 | 155/155 | 0 |
| `schemas/agent_analysis.py` | 13/14 | 14/14 | 90/90 | 0 |
| `services/analysis_wire.py` | 38/42 | 42/42 | 87/87 | -2 |
| `tasks/functions.py` | 45/46 | 46/46 | 198/198 | 0 |
| `services/agent_client.py` | 16/16 | 15/16 | 151/152 | 0 |

The context-traced measurement of `agent_client.py` misses the defensive
`AsyncRetrying` loop-exhaustion tripwire `[273,303]`, documented in the source as
unreachable when tenacity returns or reraises normally. The old baseline was 9/10
branches with one uncovered branch; the repaired full report is 15/16, also with one
uncovered branch. Its ratio improves from 90% to 93.75%. This measurement difference
is reported explicitly rather than recording the earlier 16/16 figure.

The repaired report has **zero** modules whose branch percentage decreased while their
uncovered branch count increased. The two improved-ratio modules with increased
uncovered counts described above remain visible; no existing gap was silently relabeled
as covered. No production code changed. The first final clean-tree submit at `d3999882` completed **9967 passed, 2 failed,
6 skipped, 195 deselected, 314 warnings in 1101.39 s**. Both failures required the new
maintained document to be indexed in `docs/README.md`; its missing index entry is now
added. Both complete affected documentation test modules then passed **20 tests in
2.26 s**. That attempt was also a failed validation gate. Its authentic full report confirmed
all four repaired modules at 100% branches. Explicit branch-check against that report
passed for these modules and the zero-branch review facade, without another test run.
The corrected final tree still requires its own green clean-tree submit.

## Every mover in the initial audit

Counts remain exact. 'No measurable branches' means at least one side has no branches;
it is not a 0% or 100% branch score. New/removed paths have no same-module comparison.

| Module | Branch direction | Old branches | Initial branches | Old lines | Initial lines | Uncovered branch delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `agent_watcher/__main__.py` | ratio held | 25/26 | 25/26 | 96/97 | 105/106 | 0 |
| `agent_watcher/observer.py` | ratio held | 12/12 | 12/12 | 41/41 | 42/42 | 0 |
| `analysis_child.py` | ratio held | 8/8 | 8/8 | 61/61 | 69/69 | 0 |
| `cli/__init__.py` | up | 21/22 | 43/44 | 140/143 | 228/231 | 0 |
| `config.py` | up | 67/68 | 6/6 | 284/284 | 22/22 | -1 |
| `config_agent.py` | new | absent | 16/16 | absent | 56/56 | n/a |
| `config_backends.py` | up | 25/28 | 27/30 | 103/106 | 110/113 | 0 |
| `config_base.py` | new | absent | 6/6 | absent | 68/68 | n/a |
| `config_control.py` | new | absent | 11/12 | absent | 104/106 | n/a |
| `config_redis.py` | new | absent | 5/6 | absent | 20/20 | n/a |
| `config_secrets.py` | new | absent | 24/24 | absent | 60/60 | n/a |
| `constants.py` | no measurable branches | 0/0 | 0/0 | 19/19 | 21/21 | 0 |
| `database.py` | ratio held | 2/2 | 2/2 | 29/29 | 31/31 | 0 |
| `enums/stage.py` | ratio held | 38/38 | 46/46 | 86/86 | 103/103 | 0 |
| `enums/tracklist_candidate.py` | no measurable branches | 0/0 | 8/8 | 64/64 | 102/102 | 0 |
| `job_runner.py` | up | 41/46 | 52/58 | 229/232 | 291/294 | 1 |
| `logging_config.py` | ratio held | 18/18 | 20/20 | 65/65 | 73/73 | 0 |
| `main.py` | up | 5/6 | 7/8 | 96/96 | 98/98 | 0 |
| `models/__init__.py` | no measurable branches | 0/0 | 0/0 | 24/24 | 29/29 | 0 |
| `models/analysis.py` | no measurable branches | 0/0 | 0/0 | 37/37 | 45/45 | 0 |
| `models/backend_breaker.py` | new | absent | 0/0 | absent | 11/11 | n/a |
| `models/cloud_job.py` | no measurable branches | 0/0 | 0/0 | 35/35 | 42/42 | 0 |
| `models/deployment.py` | new | absent | 0/0 | absent | 15/15 | n/a |
| `models/file.py` | no measurable branches | 0/0 | 0/0 | 21/21 | 22/22 | 0 |
| `models/orphan_companion_diagnostic.py` | new | absent | 0/0 | absent | 12/12 | n/a |
| `models/runtime_config_override.py` | new | absent | 0/0 | absent | 10/10 | n/a |
| `models/scan_batch.py` | no measurable branches | 0/0 | 0/0 | 24/24 | 28/28 | 0 |
| `models/set_profile.py` | new | absent | 0/0 | absent | 15/15 | n/a |
| `models/tracklist_lookup_cache.py` | no measurable branches | 0/0 | 0/0 | 30/30 | 41/41 | 0 |
| `routers/admin_agents.py` | ratio held | 34/34 | 44/44 | 177/177 | 208/208 | 0 |
| `routers/admin_runtime_config.py` | new | absent | 8/8 | absent | 87/87 | n/a |
| `routers/agent_analysis.py` | down | 22/22 | 53/54 | 114/114 | 154/155 | 1 |
| `routers/agent_config.py` | new | absent | 0/0 | absent | 16/16 | n/a |
| `routers/agent_exec_batches.py` | up | 25/28 | 27/28 | 72/75 | 75/75 | -2 |
| `routers/agent_files.py` | ratio held | 16/16 | 22/22 | 68/68 | 87/87 | 0 |
| `routers/agent_heartbeat.py` | ratio held | 2/2 | 2/2 | 23/23 | 24/24 | 0 |
| `routers/agent_metadata.py` | ratio held | 2/2 | 6/6 | 51/51 | 44/44 | 0 |
| `routers/agent_orphan_companions.py` | new | absent | 12/12 | absent | 44/46 | n/a |
| `routers/agent_scan_batches.py` | up | 21/22 | 23/24 | 46/47 | 51/52 | 0 |
| `routers/agent_tag_writes.py` | ratio held | 12/12 | 14/14 | 44/44 | 48/48 | 0 |
| `routers/cue.py` | ratio held | 28/32 | 28/32 | 120/124 | 122/126 | 0 |
| `routers/deployments.py` | new | absent | 7/8 | absent | 27/28 | n/a |
| `routers/duplicates.py` | ratio held | 18/24 | 18/24 | 114/125 | 116/127 | 0 |
| `routers/execution.py` | ratio held | 37/38 | 37/38 | 201/205 | 203/207 | 0 |
| `routers/pipeline/_common.py` | no measurable branches | 0/0 | 0/0 | 26/26 | 30/30 | 0 |
| `routers/pipeline/analysis.py` | ratio held | 35/36 | 35/36 | 173/176 | 178/181 | 0 |
| `routers/pipeline/backfill.py` | up | 11/14 | 13/16 | 41/41 | 48/48 | 0 |
| `routers/pipeline/dashboard_stats.py` | ratio held | 8/8 | 10/10 | 107/107 | 122/122 | 0 |
| `routers/pipeline/lanes.py` | ratio held | 2/2 | 2/2 | 17/17 | 25/25 | 0 |
| `routers/pipeline/proposals.py` | ratio held | 12/12 | 12/12 | 49/49 | 51/51 | 0 |
| `routers/pipeline/skip.py` | up | 37/42 | 41/42 | 133/137 | 143/144 | -4 |
| `routers/pipeline/tracklists.py` | up | 17/18 | 21/22 | 108/108 | 127/127 | 0 |
| `routers/pipeline_scans.py` | ratio held | 42/42 | 46/46 | 169/169 | 204/204 | 0 |
| `routers/proposals.py` | up | 48/54 | 53/54 | 174/186 | 198/198 | -5 |
| `routers/record.py` | ratio held | 10/10 | 14/14 | 81/81 | 112/112 | 0 |
| `routers/request_guards.py` | ratio held | 4/4 | 4/4 | 22/22 | 34/34 | 0 |
| `routers/routing.py` | ratio held | 2/2 | 2/2 | 22/22 | 24/24 | 0 |
| `routers/scan.py` | new | absent | 18/18 | absent | 90/90 | n/a |
| `routers/search.py` | ratio held | 6/6 | 6/6 | 29/29 | 31/31 | 0 |
| `routers/shell.py` | removed | 66/72 | absent | 324/336 | absent | n/a |
| `routers/shell/__init__.py` | new | absent | 12/12 | absent | 38/38 | n/a |
| `routers/shell/stage_context.py` | new | absent | 6/8 | absent | 130/136 | n/a |
| `routers/shell/stage_maps.py` | new | absent | 0/0 | absent | 19/19 | n/a |
| `routers/shell/summary.py` | new | absent | 49/52 | absent | 159/164 | n/a |
| `routers/tags.py` | up | 57/66 | 65/66 | 285/307 | 308/309 | -8 |
| `routers/view_state.py` | ratio held | 6/6 | 4/4 | 54/54 | 55/55 | 0 |
| `runtime_config.py` | new | absent | 71/72 | absent | 289/290 | n/a |
| `runtime_config_backends.py` | new | absent | 6/6 | absent | 52/55 | n/a |
| `runtime_config_catalog.py` | new | absent | 12/14 | absent | 23/25 | n/a |
| `runtime_config_notify.py` | new | absent | 8/10 | absent | 56/61 | n/a |
| `runtime_config_triggers.py` | new | absent | 17/20 | absent | 107/111 | n/a |
| `schemas/agent_analysis.py` | down | 4/4 | 13/14 | 66/66 | 89/90 | 1 |
| `schemas/agent_config.py` | new | absent | 0/0 | absent | 9/9 | n/a |
| `schemas/agent_execution.py` | ratio held | 4/4 | 4/4 | 35/35 | 26/26 | 0 |
| `schemas/agent_heartbeat.py` | no measurable branches | 0/0 | 0/0 | 10/10 | 30/30 | 0 |
| `schemas/agent_orphan_companions.py` | new | absent | 4/4 | absent | 29/29 | n/a |
| `schemas/agent_proposals.py` | ratio held | 2/2 | 2/2 | 16/16 | 21/21 | 0 |
| `schemas/agent_s3.py` | ratio held | 6/6 | 6/6 | 37/37 | 38/38 | 0 |
| `schemas/agent_scan_batches.py` | ratio held | 2/2 | 2/2 | 24/24 | 19/19 | 0 |
| `schemas/agent_tag_writes.py` | ratio held | 2/2 | 2/2 | 33/33 | 34/34 | 0 |
| `schemas/agent_tasks.py` | ratio held | 12/16 | 12/16 | 70/73 | 71/74 | 0 |
| `schemas/deployment.py` | new | absent | 3/4 | absent | 25/25 | n/a |
| `schemas/wire_mixins.py` | new | absent | 0/0 | absent | 9/9 | n/a |
| `schemas/wire_payload.py` | new | absent | 2/2 | absent | 10/10 | n/a |
| `services/agent_bootstrap.py` | ratio held | 8/8 | 8/8 | 44/44 | 42/42 | 0 |
| `services/agent_client.py` | up | 9/10 | 16/16 | 130/131 | 152/152 | -1 |
| `services/agent_liveness.py` | ratio held | 16/16 | 16/16 | 98/98 | 100/100 | 0 |
| `services/agent_s3_reports.py` | up | 19/22 | 28/28 | 140/141 | 165/165 | -3 |
| `services/agent_task_router.py` | up | 9/10 | 10/10 | 57/59 | 60/62 | -1 |
| `services/agent_upsert.py` | new | absent | 0/0 | absent | 4/4 | n/a |
| `services/analysis.py` | up | 109/116 | 67/68 | 411/411 | 324/325 | -6 |
| `services/analysis_decoder.py` | ratio held | 34/34 | 36/36 | 157/159 | 190/190 | 0 |
| `services/analysis_derive.py` | new | absent | 26/26 | absent | 43/43 | n/a |
| `services/analysis_exec.py` | up | 41/42 | 45/46 | 149/149 | 163/163 | 0 |
| `services/analysis_models.py` | new | absent | 10/10 | absent | 47/47 | n/a |
| `services/analysis_probe.py` | new | absent | 11/12 | absent | 46/46 | n/a |
| `services/analysis_sizing.py` | ratio held | 50/50 | 50/50 | 167/167 | 172/172 | 0 |
| `services/analysis_timeline.py` | up | 34/38 | 99/102 | 117/122 | 274/278 | -1 |
| `services/analysis_windows.py` | new | absent | 14/16 | absent | 65/65 | n/a |
| `services/analysis_wire.py` | down | 34/36 | 38/42 | 57/57 | 84/87 | 2 |
| `services/backend_breaker.py` | new | absent | 16/16 | absent | 78/78 | n/a |
| `services/backends/kueue.py` | ratio held | 26/30 | 26/30 | 121/124 | 125/128 | 0 |
| `services/backends/lane_detail.py` | up | 15/16 | 54/54 | 79/87 | 273/283 | -1 |
| `services/backends/lane_metrics.py` | ratio held | 10/10 | 10/10 | 77/80 | 68/71 | 0 |
| `services/backends/lane_snapshot.py` | up | 29/30 | 33/34 | 129/140 | 141/149 | 0 |
| `services/backends/local.py` | no measurable branches | 0/0 | 2/2 | 32/32 | 41/41 | 0 |
| `services/burst_telemetry_slots.py` | new | absent | 5/6 | absent | 26/28 | n/a |
| `services/cloud_attempts_reset.py` | new | absent | 34/34 | absent | 137/137 | n/a |
| `services/cloud_staging.py` | up | 21/24 | 24/26 | 102/103 | 116/117 | -1 |
| `services/companion.py` | ratio held | 22/22 | 20/20 | 58/58 | 68/68 | 0 |
| `services/companion_read.py` | no measurable branches | 0/0 | 2/2 | 6/6 | 16/16 | 0 |
| `services/cue_generator.py` | ratio held | 24/24 | 26/26 | 84/84 | 95/95 | 0 |
| `services/date_convention.py` | ratio held | 24/24 | 32/32 | 89/89 | 112/112 | 0 |
| `services/dedup.py` | ratio held | 44/44 | 46/46 | 165/165 | 192/192 | 0 |
| `services/execution_dispatch_protocol.py` | ratio held | 23/24 | 23/24 | 135/135 | 160/160 | 0 |
| `services/harmonic_journey.py` | new | absent | 14/14 | absent | 147/147 | n/a |
| `services/k8s_quantity.py` | new | absent | 4/4 | absent | 11/11 | n/a |
| `services/kube_staging.py` | up | 57/60 | 60/60 | 184/186 | 196/198 | -3 |
| `services/live_sentinel.py` | new | absent | 0/0 | absent | 13/13 | n/a |
| `services/media_path_resolve.py` | new | absent | 16/18 | absent | 39/42 | n/a |
| `services/metadata_parsing.py` | up | 40/44 | 40/40 | 119/123 | 130/132 | -4 |
| `services/pipeline/analyze.py` | ratio held | 14/14 | 14/14 | 90/96 | 91/97 | 0 |
| `services/pipeline/cloud.py` | ratio held | 2/2 | 2/2 | 56/56 | 58/58 | 0 |
| `services/pipeline/files.py` | up | 13/14 | 19/20 | 70/74 | 87/91 | 0 |
| `services/pipeline/proposals.py` | ratio held | 2/2 | 14/14 | 23/23 | 40/40 | 0 |
| `services/pipeline/reconciliation.py` | ratio held | 6/6 | 6/6 | 43/43 | 54/54 | 0 |
| `services/pipeline/stages.py` | ratio held | 11/12 | 11/12 | 89/89 | 88/88 | 0 |
| `services/pipeline/tracklists.py` | ratio held | 2/2 | 2/2 | 39/39 | 41/41 | 0 |
| `services/pipeline_counters.py` | ratio held | 4/4 | 4/4 | 25/25 | 26/26 | 0 |
| `services/poster.py` | new | absent | 4/4 | absent | 53/53 | n/a |
| `services/proposal.py` | no measurable branches | 45/46 | 0/0 | 167/168 | 27/27 | -1 |
| `services/proposal_context.py` | new | absent | 25/26 | absent | 89/90 | n/a |
| `services/proposal_parsing.py` | new | absent | 30/30 | absent | 114/114 | n/a |
| `services/proposal_persistence.py` | new | absent | 16/16 | absent | 42/42 | n/a |
| `services/proposal_provider.py` | new | absent | 2/2 | absent | 24/24 | n/a |
| `services/queue_introspection.py` | ratio held | 10/10 | 8/8 | 53/53 | 49/49 | 0 |
| `services/reanalysis_backfill.py` | ratio held | 30/30 | 30/30 | 113/113 | 123/123 | 0 |
| `services/record_facts.py` | new | absent | 10/10 | absent | 64/64 | n/a |
| `services/record_metadata.py` | new | absent | 4/4 | absent | 28/28 | n/a |
| `services/resizable_limiter.py` | new | absent | 19/20 | absent | 74/74 | n/a |
| `services/review.py` | no measurable branches | 44/46 | 0/0 | 236/242 | 41/41 | -2 |
| `services/review_changes.py` | new | absent | 10/10 | absent | 101/101 | n/a |
| `services/review_cue.py` | new | absent | 1/2 | absent | 53/53 | n/a |
| `services/review_dedupe.py` | new | absent | 12/12 | absent | 46/46 | n/a |
| `services/review_tagwrite.py` | new | absent | 19/20 | absent | 87/87 | n/a |
| `services/runtime_config_overrides.py` | new | absent | 4/4 | absent | 27/27 | n/a |
| `services/scan_deletion.py` | ratio held | 2/2 | 6/6 | 35/35 | 44/44 | 0 |
| `services/scheduling_ledger.py` | up | 7/8 | 16/16 | 47/48 | 63/63 | -1 |
| `services/search_queries.py` | ratio held | 20/20 | 20/20 | 79/79 | 95/95 | 0 |
| `services/set_glyph_colors.py` | new | absent | 9/10 | absent | 28/29 | n/a |
| `services/set_projection.py` | new | absent | 85/90 | absent | 204/207 | n/a |
| `services/set_projection_backfill.py` | new | absent | 7/8 | absent | 50/51 | n/a |
| `services/set_projection_writer.py` | new | absent | 18/18 | absent | 77/77 | n/a |
| `services/set_similarity.py` | new | absent | 23/26 | absent | 97/98 | n/a |
| `services/stage_status.py` | up | 36/40 | 42/46 | 133/136 | 147/150 | 0 |
| `services/stranded_analysis_recovery.py` | new | absent | 0/0 | absent | 6/6 | n/a |
| `services/tag_formats.py` | new | absent | 8/8 | absent | 24/24 | n/a |
| `services/tag_proposal.py` | up | 31/32 | 14/14 | 53/53 | 46/46 | -1 |
| `services/tag_write_disk.py` | up | 58/62 | 70/70 | 115/119 | 139/139 | -4 |
| `services/track_segments.py` | new | absent | 23/24 | absent | 103/104 | n/a |
| `services/tracklist_candidate_queue.py` | up | 29/30 | 31/32 | 146/146 | 152/152 | 0 |
| `services/tracklist_candidates.py` | up | 136/138 | 138/140 | 374/374 | 376/376 | 0 |
| `services/tracklist_drain.py` | up | 59/60 | 77/78 | 285/285 | 326/326 | 0 |
| `services/tracklist_drain_arm.py` | ratio held | 8/8 | 10/10 | 57/57 | 68/68 | 0 |
| `services/tracklist_lookup_cache.py` | ratio held | 34/34 | 46/46 | 110/110 | 155/155 | 0 |
| `services/tracklist_matcher.py` | ratio held | 20/20 | 20/20 | 58/58 | 59/59 | 0 |
| `services/tracklist_priority.py` | ratio held | 25/26 | 25/26 | 111/112 | 112/113 | 0 |
| `services/tracklist_query.py` | ratio held | 40/40 | 46/46 | 155/155 | 182/182 | 0 |
| `services/tracklist_render.py` | ratio held | 52/54 | 52/54 | 287/287 | 281/281 | 0 |
| `services/tracklist_result_scorer.py` | ratio held | 32/32 | 32/32 | 113/113 | 116/116 | 0 |
| `services/tracklist_scraper.py` | up | 35/36 | 39/40 | 182/182 | 191/191 | 0 |
| `services/video_audio.py` | up | 42/48 | 53/60 | 134/138 | 180/188 | 1 |
| `tasks/_shared/deterministic_key.py` | ratio held | 19/20 | 19/20 | 63/63 | 67/67 | 0 |
| `tasks/_shared/live_worker.py` | new | absent | 38/38 | absent | 119/119 | n/a |
| `tasks/_shared/model_bootstrap.py` | ratio held | 4/4 | 8/8 | 34/34 | 39/39 | 0 |
| `tasks/agent_worker.py` | up | 39/42 | 43/46 | 161/166 | 198/203 | 0 |
| `tasks/cloud_reconcile_observation.py` | new | absent | 47/48 | absent | 144/147 | n/a |
| `tasks/companion_read.py` | ratio held | 2/2 | 2/2 | 31/31 | 33/33 | 0 |
| `tasks/controller.py` | up | 21/22 | 29/30 | 114/114 | 154/154 | 0 |
| `tasks/cue_write.py` | no measurable branches | 0/0 | 0/0 | 21/21 | 23/23 | 0 |
| `tasks/discogs.py` | ratio held | 7/8 | 7/8 | 43/43 | 44/44 | 0 |
| `tasks/execution.py` | up | 56/58 | 24/24 | 279/281 | 226/227 | -2 |
| `tasks/execution_filesystem.py` | new | absent | 34/36 | absent | 183/185 | n/a |
| `tasks/functions.py` | down | 36/36 | 45/46 | 177/177 | 198/198 | 1 |
| `tasks/heartbeat.py` | ratio held | 10/10 | 26/26 | 68/69 | 146/148 | 0 |
| `tasks/ledger_reaper.py` | ratio held | 6/6 | 8/8 | 36/36 | 40/40 | 0 |
| `tasks/metadata_extraction.py` | ratio held | 4/4 | 4/4 | 35/35 | 40/40 | 0 |
| `tasks/proposal.py` | ratio held | 8/8 | 14/14 | 44/44 | 57/57 | 0 |
| `tasks/push.py` | ratio held | 12/12 | 12/12 | 87/87 | 91/91 | 0 |
| `tasks/reap_orphaned_backend_cloud_jobs.py` | new | absent | 22/22 | absent | 85/85 | n/a |
| `tasks/reconcile_cloud_jobs.py` | up | 73/80 | 77/82 | 234/243 | 290/292 | -2 |
| `tasks/recovery_backfill.py` | new | absent | 8/8 | absent | 50/50 | n/a |
| `tasks/recovery_policy.py` | new | absent | 28/28 | absent | 108/108 | n/a |
| `tasks/recovery_queries.py` | new | absent | 2/2 | absent | 30/30 | n/a |
| `tasks/recovery_replay.py` | new | absent | 25/26 | absent | 107/108 | n/a |
| `tasks/reenqueue.py` | up | 73/74 | 8/8 | 312/313 | 42/42 | -1 |
| `tasks/release_awaiting_cloud.py` | up | 25/28 | 35/38 | 145/148 | 184/187 | 0 |
| `tasks/s3_upload.py` | ratio held | 5/6 | 5/6 | 63/64 | 64/65 | 0 |
| `tasks/scan.py` | ratio held | 32/32 | 28/28 | 134/134 | 145/145 | 0 |
| `tasks/submit_cloud_job.py` | ratio held | 8/10 | 8/10 | 40/40 | 43/43 | 0 |
| `tasks/tag_write.py` | no measurable branches | 0/0 | 0/0 | 36/36 | 47/47 | 0 |
| `tasks/tracklist_drain_control.py` | ratio held | 16/16 | 16/16 | 59/59 | 61/61 | 0 |
| `telemetry/__init__.py` | new | absent | 0/0 | absent | 6/6 | n/a |
| `telemetry/_env.py` | new | absent | 6/6 | absent | 34/34 | n/a |
| `telemetry/bootstrap.py` | new | absent | 26/26 | absent | 133/133 | n/a |
| `telemetry/catalogue.py` | new | absent | 2/2 | absent | 66/66 | n/a |
| `telemetry/context.py` | new | absent | 9/10 | absent | 37/37 | n/a |
| `telemetry/db.py` | new | absent | 9/10 | absent | 42/42 | n/a |
| `telemetry/http.py` | new | absent | 24/30 | absent | 65/70 | n/a |
| `telemetry/instruments.py` | new | absent | 20/20 | absent | 67/67 | n/a |
| `telemetry/pipeline.py` | new | absent | 8/8 | absent | 17/17 | n/a |
| `telemetry/saq.py` | new | absent | 14/14 | absent | 59/61 | n/a |
| `telemetry/slots.py` | new | absent | 32/32 | absent | 127/130 | n/a |
| `telemetry/tracing.py` | new | absent | 4/4 | absent | 56/56 | n/a |
| `version.py` | new | absent | 0/0 | absent | 2/2 | n/a |
| `web/template_globals.py` | new | absent | 0/0 | absent | 9/9 | n/a |
