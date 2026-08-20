# phaze-vu88k.1 — per-file health/performance baseline, before the molecule's refactors

- **Bead:** `phaze-vu88k.1` (root bead of epic `phaze-vu88k`, "Clear phaze's actionable structural
  and performance findings")
- **Date:** 2026-08-20
- **Analyzed commit:** `ef1ef7d262cc0731329c41056948ecb578da2da0` (`main`, PR #496 merge —
  `repositories.head_commit` in the Repowise index used for this run)
- **Status:** measurement only. No `src/` behaviour changed by this bead.

## What this is

Every downstream bead in this molecule (`phaze-vu88k.2` through `.9`) reports a before/after
number against a snapshot of this repo's Repowise health data. `repowise health` only prints the
worst 20 files, which is not enough to prove a specific bead's fix moved a specific file. This
artifact is that snapshot, plus the script (`scripts/health_baseline.py`) that produced it and can
reproduce it later.

The data source is the Repowise sqlite index (`.repowise/wiki.db`), tables `health_file_metrics`
and `health_findings`. `.repowise/` is gitignored — a per-checkout, regenerable index, not tracked
source — so this script and its committed output are what make the numbers reproducible across
worktrees and across time.

## The historical/structural/performance split (criterion 4)

The epic's filing claimed a specific split of the 362 defect-dimension findings in the 47
below-6.0 files: 234 historical (git-history-derived, not reachable by refactor) vs 128 structural
(the reachable subset). Recomputing precisely from the index at this commit confirms **128
structural** exactly, but only **201** of the 234 come from the seven biomarker types the epic
named as historical (`prior_defect`, `function_hotspot`, `change_entropy`, `co_change_scatter`,
`knowledge_loss`, `churn_risk`, `hidden_coupling`). The remaining **33** findings
(`coverage_gradient`: 27, `complex_conditional`: 6) are defect-dimension but were not named in
either bucket by the epic.

This baseline does **not** silently fold those 33 into "historical" to make the round number match.
`scripts/health_baseline.py` reports them separately as `other_defect`, per file and in the
summary, so a downstream bead can decide their disposition on the evidence rather than inherit an
assumption. Plausibly:

- `complex_conditional` (6) reads like a fifth structural biomarker (a complexity finding,
  addressable by refactor) — candidate for `.5`/`.6` (the structural beads) to fold in explicitly,
  not silently.
- `coverage_gradient` (27) is a coverage-trend biomarker. The epic already established "adding
  tests is NOT a route to a higher score" for these files (they average 98.5% line coverage), which
  suggests this one behaves like the historical bucket (decays with stability, not with a
  refactor) — but that is an inference, not something this baseline measured directly.

Neither is this bead's call to make; it is flagged here so `.5`/`.6`/`.9` see it before they scope
their own reachable ceiling.

The three buckets, exactly as coded in `scripts/health_baseline.py`:

| Bucket | Biomarker types | Dimension | Reachable by refactor? |
| --- | --- | --- | --- |
| **structural** | `complex_method`, `nested_complexity`, `large_method`, `bumpy_road` | defect | yes — this is what beads `.5`/`.6` target |
| **historical** | `prior_defect`, `function_hotspot`, `change_entropy`, `co_change_scatter`, `knowledge_loss`, `churn_risk`, `hidden_coupling` | defect | no — decays only as the file stays stable |
| **other_defect** | everything else with `dimension = 'defect'` (currently `coverage_gradient`, `complex_conditional`) | defect | undetermined — see above |
| **performance** | `io_in_loop`, `serial_await_in_loop`, `nested_loop_with_io`, `hot_path_sync_io`, `membership_test_against_list_in_loop` | performance | yes — this is what bead `.2` targets. Excluded from the defect dimension entirely (`scoring.py:217-229`) |

## Headline numbers

| Metric | Value |
| --- | --- |
| `src/` files in the index | 372 |
| Files scoring below 6.0 | 47 |
| Total defect-dimension findings (all 372 files) | 669 |
| — structural / historical / other_defect | 202 / 393 / 74 |
| Defect-dimension findings in the 47 below-6.0 files | 362 |
| — structural / historical / other_defect | 128 / 201 / 33 |
| Performance findings (all 372 files) | 118 |
| — `io_in_loop` / `serial_await_in_loop` / `nested_loop_with_io` / `hot_path_sync_io` / `membership_test_against_list_in_loop` | 80 / 33 / 3 / 1 / 1 |

All of the above reproduce exactly the counts established when the epic was filed. The 33
`other_defect` findings are the one number the epic's own text did not carry — see above.

## The 47 files below the 6.0 floor

Sorted by score ascending. `struct` / `hist` / `other` are the defect-dimension split for that
file; `perf` is that file's count of performance-dimension findings (independent of the defect
split, not summed into it).

| File | Score | Maintainability | Performance | struct | hist | other | perf findings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `src/phaze/services/analysis.py` | 1.93 | 2.10 | 9.85 | 8 | 11 | 2 | 1 |
| `src/phaze/routers/agent_s3.py` | 2.09 | 5.10 | 10.00 | 7 | 4 | 1 | 0 |
| `src/phaze/services/metadata.py` | 2.25 | 6.20 | 10.00 | 10 | 3 | 1 | 0 |
| `src/phaze/tasks/reenqueue.py` | 2.62 | 7.50 | 8.48 | 5 | 7 | 2 | 3 |
| `src/phaze/services/proposal_queries.py` | 3.17 | 8.80 | 10.00 | 2 | 5 | 2 | 0 |
| `src/phaze/config.py` | 3.46 | 8.10 | 9.30 | 3 | 8 | 0 | 1 |
| `src/phaze/services/review.py` | 3.46 | 6.70 | 8.00 | 3 | 5 | 1 | 4 |
| `src/phaze/routers/execution.py` | 3.55 | 6.20 | 10.00 | 3 | 6 | 1 | 0 |
| `src/phaze/routers/pipeline_scans.py` | 3.61 | 5.90 | 10.00 | 3 | 5 | 0 | 0 |
| `src/phaze/tasks/release_awaiting_cloud.py` | 3.75 | 5.70 | 8.60 | 3 | 1 | 1 | 2 |
| `src/phaze/tasks/controller.py` | 3.92 | 7.70 | 10.00 | 2 | 6 | 0 | 0 |
| `src/phaze/routers/shell.py` | 4.17 | 8.50 | 10.00 | 4 | 7 | 2 | 0 |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | 4.24 | 7.70 | 10.00 | 5 | 7 | 1 | 0 |
| `src/phaze/routers/cue.py` | 4.26 | 8.50 | 10.00 | 3 | 3 | 2 | 0 |
| `src/phaze/job_runner.py` | 4.30 | 6.50 | 10.00 | 4 | 4 | 1 | 0 |
| `src/phaze/services/kube_staging.py` | 4.30 | 9.00 | 10.00 | 3 | 8 | 1 | 0 |
| `src/phaze/routers/agent_push.py` | 4.40 | 8.00 | 10.00 | 3 | 5 | 1 | 0 |
| `src/phaze/tasks/metadata_extraction.py` | 4.44 | 7.90 | 10.00 | 1 | 4 | 0 | 0 |
| `src/phaze/tasks/execution.py` | 4.47 | 6.70 | 9.30 | 2 | 5 | 1 | 1 |
| `src/phaze/routers/agent_analysis.py` | 4.55 | 8.50 | 9.18 | 2 | 4 | 0 | 2 |
| `src/phaze/services/tracklist_drain.py` | 4.55 | 7.10 | 8.00 | 3 | 4 | 0 | 7 |
| `src/phaze/routers/proposals.py` | 4.76 | 8.80 | 9.79 | 2 | 6 | 1 | 1 |
| `src/phaze/routers/duplicates.py` | 4.82 | 10.00 | 10.00 | 1 | 4 | 1 | 0 |
| `src/phaze/services/search_queries.py` | 4.85 | 7.80 | 10.00 | 2 | 3 | 0 | 0 |
| `src/phaze/tasks/functions.py` | 4.85 | 8.20 | 10.00 | 1 | 6 | 0 | 0 |
| `src/phaze/agent_watcher/__main__.py` | 4.97 | 6.90 | 10.00 | 4 | 2 | 1 | 0 |
| `src/phaze/services/dedup.py` | 5.00 | 10.00 | 9.18 | 3 | 7 | 0 | 2 |
| `src/phaze/routers/tags.py` | 5.06 | 9.40 | 8.60 | 1 | 6 | 1 | 2 |
| `src/phaze/routers/record.py` | 5.13 | 9.40 | 10.00 | 2 | 4 | 0 | 0 |
| `src/phaze/tasks/_shared/deterministic_key.py` | 5.15 | 8.80 | 10.00 | 1 | 4 | 0 | 0 |
| `src/phaze/services/cloud_staging.py` | 5.18 | 7.50 | 10.00 | 3 | 2 | 1 | 0 |
| `src/phaze/routers/admin_agents.py` | 5.20 | 9.10 | 10.00 | 1 | 3 | 0 | 0 |
| `src/phaze/services/agent_task_router.py` | 5.39 | 7.30 | 10.00 | 1 | 2 | 1 | 0 |
| `src/phaze/services/proposal.py` | 5.39 | 9.00 | 8.00 | 1 | 4 | 1 | 5 |
| `src/phaze/scripts/download_models.py` | 5.41 | 9.00 | 10.00 | 1 | 3 | 0 | 0 |
| `src/phaze/tasks/scan.py` | 5.44 | 8.60 | 10.00 | 3 | 2 | 0 | 0 |
| `src/phaze/services/tracklist_scraper.py` | 5.50 | 9.70 | 10.00 | 1 | 5 | 0 | 0 |
| `src/phaze/config_backends.py` | 5.54 | 10.00 | 9.30 | 1 | 4 | 1 | 1 |
| `src/phaze/tasks/agent_worker.py` | 5.58 | 8.40 | 9.30 | 0 | 4 | 1 | 1 |
| `src/phaze/services/tracklist_priority.py` | 5.61 | 10.00 | 10.00 | 1 | 3 | 1 | 0 |
| `src/phaze/services/agent_liveness.py` | 5.67 | 8.40 | 10.00 | 4 | 2 | 1 | 0 |
| `src/phaze/cert_bootstrap.py` | 5.77 | 9.70 | 10.00 | 3 | 2 | 1 | 0 |
| `src/phaze/services/backends/lane_snapshot.py` | 5.84 | 5.20 | 8.00 | 5 | 0 | 1 | 4 |
| `src/phaze/services/analysis_sizing.py` | 5.85 | 7.10 | 9.30 | 5 | 0 | 0 | 1 |
| `src/phaze/services/tag_writer.py` | 5.85 | 8.70 | 10.00 | 0 | 4 | 0 | 0 |
| `src/phaze/models/__init__.py` | 5.90 | 8.80 | 10.00 | 0 | 5 | 0 | 0 |
| `src/phaze/tasks/push.py` | 5.94 | 9.00 | 10.00 | 2 | 2 | 0 | 0 |

`src/phaze/services/analysis.py` (score 1.93, the worst in the repo) is **off limits** to routine
refactoring per the epic — it carries D-07/D-08/D-09 and is excluded from beads `.5`/`.6`. Its row
is included here only for completeness of the ledger: `phaze-vu88k.9` has an acceptance criterion
that consumes its split, so it must be measured even though it is never fixed.

This file's split was independently cross-checked (dispatcher `disp/health`, querying the same
index) before this bead was submitted: structural 8 findings / 3.059 impact, historical 11 /
3.498, other_defect 2 / 0.220, `max_ccn` 9, `max_nesting` 6, `nloc` 1239. This baseline's own
query (`scripts/health_baseline.py`'s classification, re-run with `health_impact` summed per
bucket for this one file) reproduces the same counts and the same impact sums exactly:

```console
$ sqlite3 .repowise/wiki.db "
SELECT
  CASE
    WHEN biomarker_type IN ('complex_method','nested_complexity','large_method','bumpy_road') THEN 'structural'
    WHEN biomarker_type IN ('prior_defect','function_hotspot','change_entropy','co_change_scatter','knowledge_loss','churn_risk','hidden_coupling') THEN 'historical'
    WHEN dimension='defect' THEN 'other_defect'
  END AS bucket,
  COUNT(*), ROUND(SUM(health_impact),3)
FROM health_findings
WHERE file_path='src/phaze/services/analysis.py' AND status='open' AND dimension='defect'
GROUP BY bucket;"
historical|11|3.498
other_defect|2|0.22
structural|8|3.059
```

No disagreement, no adjustment made.

## Reproducing and diffing this baseline

```bash
uv run scripts/health_baseline.py --out <new-baseline>.json
diff docs/spikes/phaze-vu88k.1-health-baseline-2026-08-20.json <new-baseline>.json
```

`--db` defaults to `.repowise/wiki.db` resolved relative to the script's own location (not the
caller's cwd), so the same invocation reproduces from any checkout that has run `repowise init`.
Output is deterministic — file order and every nested key are sorted — so a `diff` between two
runs reflects only real index changes, never key or row reordering.

**Demonstrated now — actual run, not an assertion.** A "later index" does not exist yet at the time
this bead runs (this file IS the first baseline); `phaze-vu88k.9` is the bead tasked with re-running
this script at the end of the molecule against the post-refactor index and producing the real,
non-empty diff against this file. What this bead can demonstrate now, and did, is that the script
itself re-runs cleanly and produces a deterministic, diffable result — the mechanism the
acceptance criterion asks for, exercised for real:

```console
$ uv run scripts/health_baseline.py --out /tmp/rerun-2026-08-20.json
$ echo "exit code: $?"
exit code: 0
$ diff docs/spikes/phaze-vu88k.1-health-baseline-2026-08-20.json /tmp/rerun-2026-08-20.json
$ echo "diff exit code: $?"
diff exit code: 0
$ wc -l /tmp/rerun-2026-08-20.json
    7696 /tmp/rerun-2026-08-20.json
```

Both invocations exited 0, both emitted the same 7696-line JSON document, and `diff` between the
committed baseline and the fresh re-run produced no output — an empty diff, run and captured, not
claimed.

## Machine-readable companion

The full per-file ledger (all 372 files, path/score/four sub-scores/coverage/findings-by-biomarker/
findings-by-dimension/defect-split) is committed alongside this document as
[`phaze-vu88k.1-health-baseline-2026-08-20.json`](./phaze-vu88k.1-health-baseline-2026-08-20.json),
the raw output of `scripts/health_baseline.py` at the commit above.
