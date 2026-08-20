# phaze-vu88k.9 — post-molecule health/performance ledger

- **Bead:** `phaze-vu88k.9` (epic `phaze-vu88k`, "Clear phaze's actionable structural and
  performance findings"). This is the only bead in the molecule permitted to claim the molecule
  improved anything.
- **Baseline measured against:** `phaze-vu88k.1`, commit `ef1ef7d262cc0731329c41056948ecb578da2da0`,
  artifact `docs/spikes/phaze-vu88k.1-health-baseline-2026-08-20.md` /
  `.json`.
- **This measurement's pinned commit:** `98446be3fce64144f17aa00aec784677739908ab`
  (`main`, "chore(merge): molecule phaze-vu88k" — the commit `main` was at when this bead was
  claimed, and the commit the whole molecule's own work landed on). All numbers below are read
  from a Repowise index synced to exactly this commit.
- **Status:** measurement and reporting only. No `src/` behaviour changed by this bead.

## 1. How the index was refreshed (criterion 1)

Ran `repowise update` (**not** `repowise init` / `init --force`) from the primary checkout. This
avoided the tooling trap recorded on phaze-z124l.3: `repowise doctor` before and after confirms
`Claude Code MCP entry: OK — not registered (repowise init registers it)` and
`Agent: claude-code: OK — wired up`, i.e. the single-registration state from phaze-z124l.3 was
undisturbed. **No re-init was needed and none was run, so there is nothing to re-apply.**

`repowise status` after the update: `Last sync commit: 98446be3fce64144f17aa00aec784677739908ab`
— exactly the pinned commit above. The update ran for 4m15s, updated 125 pages, decayed 685.

`scripts/health_baseline.py` (unmodified, the same script `phaze-vu88k.1` committed) was then
re-run against this refreshed index:

```console
$ uv run scripts/health_baseline.py --out docs/spikes/phaze-vu88k.9-health-followup-2026-08-20.json
$ echo $?
0
```

Its output is committed alongside this document as
[`phaze-vu88k.9-health-followup-2026-08-20.json`](./phaze-vu88k.9-health-followup-2026-08-20.json)
— the "after" side of the diff the `phaze-vu88k.1` artifact promised.

## 2. A confound this bead found and will not paper over

**A second, wholly independent epic — `phaze-1i0h6`, "Reduce defect-prone production hotspots
with evidence-driven boundary refactors" — landed on `main` in the same window as `phaze-vu88k`
and touched 7 of the same files, plus `src/phaze/services/analysis.py` (which `phaze-vu88k` kept
off limits throughout).** `phaze-1i0h6` is not a child of this molecule and none of its beads are
dependencies of `phaze-vu88k.9`; it happened to be dispatched concurrently and its merge commits
(`393b15d8`, and the per-bead `chore(merge): bead phaze-1i0h6.*` commits) are ancestors of this
bead's pinned commit `98446be3` because `phaze-vu88k`'s epic branch periodically merged `main`
back in.

Files touched by **both** epics, computed from the actual diffs of every `chore(merge): bead
phaze-vu88k.*` and `chore(merge): bead phaze-1i0h6.*` commit between the baseline and the pinned
commit:

| File | Touched by `phaze-vu88k` bead | Touched by `phaze-1i0h6` |
| --- | --- | --- |
| `src/phaze/config.py` | `.5` | yes |
| `src/phaze/routers/agent_s3.py` | `.5` | yes |
| `src/phaze/routers/pipeline_scans.py` | `.6` | yes |
| `src/phaze/services/metadata.py` | `.5` | yes |
| `src/phaze/services/proposal_queries.py` | `.5` | yes |
| `src/phaze/tasks/reenqueue.py` | `.5` | yes |
| `src/phaze/tasks/release_awaiting_cloud.py` | `.7` | yes |
| `src/phaze/services/analysis.py` | *(off limits — never touched by this molecule)* | yes |

**What this means for the numbers below.** Every developer-reported before/after delta quoted in
§4 for these 7 files (e.g. "config.py structural findings 3 → 0, max CCN 24 → 8") is real and is
specifically `phaze-vu88k`'s own contribution, measured by that bead's developer against the
frozen `phaze-vu88k.1` baseline on their own branch before `phaze-1i0h6`'s changes were layered
on top. But the **live, current-state numbers this ledger reads from the pinned-commit index**
(e.g. "config.py max_ccn is now 9") reflect **both** epics' work combined, and cannot be
attributed to `phaze-vu88k` alone. Where the two disagree by more than rounding, it is called out
per-file. `analysis.py` is the sharpest case: its live numbers (§6) moved entirely because of
`phaze-1i0h6.1`, not because of any change this molecule made — the molecule's own disposition of
`analysis.py` remains "off limits, untouched," exactly as scoped.

## 3. Headline counts (criterion 6, no editorialising)

| | Before (`ef1ef7d2`) | After (`98446be3`) |
| --- | ---: | ---: |
| `src/` files in the index | 372 | 382 |
| Files below 6.0 | **47** | **44** |

The file count grew by 10 — all ten are files extracted by refactors during this window (from
both epics); see §7. Of the original 47: 6 crossed above 6.0
(`routers/admin_agents.py`, `routers/agent_s3.py`, `routers/duplicates.py`, `routers/record.py`,
`routers/tags.py`, `services/metadata.py`), 3 files not previously below 6.0 are now below it
(`services/companion.py`, and two template files — see §8 for why those two are a measurement
artifact, not a regression), and the remaining 41 of the original 47 are still below 6.0 (§6).
47 − 6 + 3 = 44.

## 4. STRUCTURE dimension (criterion 2)

**Scope: the 47 files the `phaze-vu88k.1` baseline flagged below 6.0.** Baseline: 128 structural
findings (`complex_method`, `nested_complexity`, `large_method`, `bumpy_road`) across those 47
files. Measured now, same 47 files, same biomarker set: **69 remain — 59 cleared.**
`src/phaze/services/analysis.py` contributes 8 of those 69 and was never in scope (§6); excluding
it, 61 of the 120 addressable structural findings remain.

Per-file max CCN / max nesting / structural-findings-count movement, for every file this
molecule's structural beads (`.5`, `.6`, `.7`) actually touched. These numbers are the
**developers' own before/after**, each measured against the frozen `phaze-vu88k.1` baseline on
that bead's own branch (commit messages: `10e2e62a`, `87100398`, `f430baf4`, `faabf5d5`,
`1ffe1957` for `.5`; `0185f87c`, `b09664d8`, `9432d10b` for `.6`; `2c9f0f19` for `.7`) — the most
precise available source, since the frozen baseline index itself never recorded max_ccn/max_nesting
per file (only `score`/`defect_score`/finding counts; see `scripts/health_baseline.py`'s
`METRIC_COLUMNS`). Where the file is one of the 7 confounded files from §2, the "live now" column
is shown separately because it is not this molecule's number alone.

| File | Bead | CCN before → after | Nesting before → after | Structural findings before → after | Live now (CCN / nesting / struct.) — confounded? |
| --- | --- | --- | --- | --- | --- |
| `config.py` | `.5` | 24 → 8 | 4 → 3 | 3 → 0 | 9 / 4 / 2 — **yes (§2)** |
| `services/metadata.py` | `.5` | 24 → 8 | 5 → 3 | 10 → 0 | 8 / 3 / 0 — **yes (§2)** |
| `routers/agent_s3.py` | `.5` | 17 → 8 | 4 → 2 | 7 → 0 | 3 / 2 / 0 — **yes (§2)** |
| `tasks/reenqueue.py` | `.5` | 19 → 9 | 4 → 3 | 5 → 1 (justified, see below) | 12 / 3 / 2 — **yes (§2)** |
| `services/proposal_queries.py` | `.5` | 11 → 7 | 2 (unchanged) | 2 → 0 | 11 / 2 / 1 — **yes (§2)** |
| `routers/pipeline_scans.py` | `.6` | 13 → 7 | 4 → 2 | 3 → 0 | 6 / 4 / 1 — **yes (§2)** |
| `routers/cue.py` | `.6` | 14 → 7 | 2 (unchanged) | 3 → 0 | 7 / 2 / 0 — no |
| `routers/duplicates.py` | `.6` | 15 → 6 | 3 (unchanged) | 1 → 0 | 6 / 3 / 0 — no |
| `routers/shell.py` | `.6` | 12 → 9 | 3 (unchanged) | 4 → 1 | 9 / 3 / 1 — no |
| `routers/record.py` | `.6` | 11 → 7 | 1 (unchanged) | 2 → 0 | 5 / 1 / 0 — no |
| `routers/agent_analysis.py` | `.6` | 12 → 6 | 3 → 2 | 2 → 0 | 6 / 2 / 0 — no |
| `routers/tags.py` | `.6` | 14 → 8 | 3 (unchanged) | 1 → 0 | 8 / 3 / 0 — no |
| `routers/admin_agents.py` | `.6` | 10 → 8 | 2 (unchanged) | 1 → 0 | 8 / 2 / 0 — no |
| `routers/agent_push.py` | `.6` | 13 → 7 | 2 (unchanged) | 3 → 2 (1 relocated, justified below) | 7 / 2 / 2 — no |
| `routers/proposals.py` | `.6` | 9 (unchanged) | 3 (unchanged) | 2 → 1 (no written justification — flagged §5) | 9 / 3 / 1 — no |
| `tasks/reconcile_cloud_jobs.py` | `.7` | 38 → 8 | not stated | 5 → 0 | 8 / 3 / 0 — no |
| `tasks/release_awaiting_cloud.py` | `.7` | 23 → 8 | 5 → 3 | 3 → 0 | 7 / 3 / 0 — **yes (§2)** |
| `tasks/scan.py` | `.7` | 20 → 7 | 4 → 3 | 3 → 0 | 7 / 3 / 0 — no |
| `tasks/controller.py` | `.7` | 9 → 6 | 4 → 3 | 2 → 0 | 6 / 3 / 0 — no |
| `tasks/functions.py` | `.7` | 12 → 7 | not stated | 1 → 0 | 7 / 3 / 0 — no |

All 19 "live now" columns were independently re-measured for this ledger by
`scripts/structural_findings.py` (added by bead `.6` specifically for this purpose — it drives
Repowise's own complexity walker over the working tree rather than the gitignored index) and by
direct query of `health_file_metrics` in the refreshed index; the two agree on every row above.
`scripts/structural_findings.py`'s repo-wide finding total (298) is **not** used as this ledger's
headline structural number — its own docstring states it was validated only against the 12
below-6.0 routers (32/32 exact reproduction), and it disagrees with the index-derived repo-wide
total (150) by roughly 2×, most plausibly because the live index applies suppression/dedup rules
across overlapping biomarkers on the same function that the standalone script does not replicate.
Within its validated scope (the rows above) the two sources agree exactly, which is why both are
usable there.

**`tasks/execution.py` (the file, in `src/phaze/tasks/`) was deliberately not touched** — bead
`.7`'s commit `2c9f0f19` states it explicitly: "contested by two other in-flight molecules; its
two findings are dispositioned as won't-fix." Confirmed still at 2 structural findings in the live
index.

## 5. Consolidated left-unfixed list (criterion 5)

Every structural or performance finding this molecule's beads left open, bucketed by *why*:

### 5a. Left with an explicit, written justification

- **`tasks/reenqueue.py`, `is_domain_completed` (CCN 9).** Bead `.5`, commit `faabf5d5`: "of the
  eleven in-scope functions it has the lowest cognitive/CCN ratio (8 against 9, nesting 1) — a
  flat four-arm decision table, not tangled logic. The detector gates on CCN alone with no
  cognitive floor... Extracting one arm to clear a threshold was tried, measured, and reverted."
  Its docstring's four-way cross-stage comparison only means something with all four arms visible
  together.
- **`routers/agent_push.py`, `report_push_mismatch`'s `large_method` finding.** Bead `.6`, commit
  `9432d10b`: "RELOCATED onto `_redrive_under_cap_push` (nloc 65) rather than removed... splitting
  either further to satisfy a line count would be refactoring for the metric rather than for the
  reader." Honest accounting: 3 → 2, not 3 → 0.
- **`tasks/execution.py`, 2 findings.** Bead `.7`, commit `2c9f0f19`: "deliberately NOT touched
  (contested by two other in-flight molecules)."
- **`src/phaze/services/analysis.py`, all 8 structural findings.** §6 below — this molecule's
  largest deliberate exclusion, disposed per criterion 7.

### 5b. Left out of scope by molecule design (never named in any child bead's target list)

The remaining structural findings in files that no `phaze-vu88k` child bead targeted at all:
`job_runner.py` (4), `services/kube_staging.py` (3), `services/tracklist_drain.py` (4),
`services/search_queries.py` (2), `agent_watcher/__main__.py` (4), `services/dedup.py` (3),
`tasks/_shared/deterministic_key.py` (1), `services/cloud_staging.py` (3),
`services/agent_task_router.py` (1), `scripts/download_models.py` (1),
`services/tracklist_scraper.py` (1), `config_backends.py` (1), `services/tracklist_priority.py`
(1), `services/agent_liveness.py` (4), `cert_bootstrap.py` (3),
`services/backends/lane_snapshot.py` (5), `services/analysis_sizing.py` (5), `tasks/push.py` (2),
`services/review.py` (3), `tasks/metadata_extraction.py` (1). The epic's own filing text scoped
`.5`/`.6`/`.7` to specific named files and directories, not every below-6.0 file — this is that
scoping decision showing up in the ledger, not an oversight.

### 5c. Left unfixed with no justification on record — flagged, not fabricated

- **`services/proposal.py`, 5 performance findings (unchanged, 5 → 5).** Bead `.4`'s commit
  `a6c3435a` states "the tenth [finding], `load_companion_targets`, is left for phaze-vu88k.3,
  which owns `services/proposal.py`." But bead `.3`'s actual diff (commit `c71ed0c3`) touches
  only `services/companion.py` — `services/proposal.py` was never modified by any commit in this
  molecule. This ledger will not invent a justification that was never written down: these 5
  findings are unaddressed, the stated hand-off did not happen, and this is called out rather than
  silently absorbed into "cleared."
- **`routers/proposals.py`, 1 structural finding (`_diff_row_context`'s path/filename mirror).**
  Bead `.6`'s commit `9432d10b` describes the extraction that produced `_diff_facet_fields` but
  does not state why one finding remains open afterward. Flagged rather than guessed at.

### 5d. Performance findings left unfixed with a stated reason (from `.2`/`.3`/`.4`)

- **`services/backends/kueue.py`, 10 remaining (14 → 10 measured; the bead's own commit claims 2
  cleared, the measured drop is 4 — see the note in §7).** No further findings in this file were
  in scope for `.2`.
- **`services/backends/compute_agent.py`, 4 remaining (8 → 4).** Same bead, same scope note.
- **`services/companion.py`, 11 remaining (13 → 11, `nested_loop_with_io` cleared ×2).** Bead
  `.3`'s commit is scoped to the one query it batches; the rest of the file's `io_in_loop` (6) and
  `serial_await_in_loop` (4) findings were not claimed as in scope.
- **`services/tracklist_drain.py`, 7 remaining, unchanged.** Bead `.3`'s own acceptance criteria
  explicitly protect this file's drain ordering and arm/disarm state machine ("a reordering here
  is a behaviour change, not a refactor") — the file was named in scope but its findings were
  correctly left as a considered non-fix, not an omission.
- **`services/review.py`, 4 remaining, unchanged.** Never touched by any bead in this molecule.

## 6. `src/phaze/services/analysis.py` — explicit disposition (criterion 7)

**No refactor was performed on this file by this bead or by any other bead in the
`phaze-vu88k` molecule. It remains off limits, exactly as the epic scoped it.**

The measurements below are the ones already established in this bead's own acceptance criteria,
frozen at the `phaze-vu88k.1` baseline index (`ef1ef7d2`) — they are quoted verbatim, not
re-derived:

- **Defect-dimension split at `ef1ef7d2`:** historical 11 findings / 3.498 impact; **structural 8
  findings / 3.059 impact**; other-defect 2 / 0.220. Structural is ~45% of the defect deduction —
  a *higher* reachable fraction than the repo-wide 35% (128 of 362 in the original 47-file
  cohort). **This file was skipped on risk, not on hopelessness.**
- **`max_ccn` 9, `max_nesting` 6, `nloc` 1239** at `ef1ef7d2`. Five files this molecule *did*
  refactor were structurally worse going in: `reconcile_cloud_jobs.py` (CCN 38), `config.py` (CCN
  24), `metadata.py` (CCN 24), `scan.py` (CCN 20), `reenqueue.py` (CCN 19). **`analysis.py`'s 1.93
  score is a history score, not a complexity score** — no later reader should re-open this file on
  the strength of the 1.93 alone.
- **Per-finding justification, all 8 structural findings:**
  - `_decode_windows` (1271–1376): `complex_method` 0.707 + `nested_complexity` 0.587 +
    `bumpy_road` 0.055 = 1.349 impact, 44% of the structural total. **This function IS the D-09
    surface** (the chunk teardown invariant that a leaked network reinstates the measured 0.3108
    GiB/chunk OOM slope).
  - `_analyze_fine_windows` (1509–1604) and `_analyze_coarse_windows` (1626–1721), 0.342 impact
    each: **the D-07 chunk surface** (exhaustive analysis bounded by chunk, not by a cap).
  - `_peak_rss_gib` (469–507), 0.342 impact: nests to dispatch on platform, and the branches are
    documented as verified-not-assumed — Darwin `ru_maxrss` is bytes where Linux `VmHWM` is KiB,
    so collapsing the branches wrong is a 1024× under-report.
  - `_sweep_one_model` (1379–1423), 0.342 impact: nests around two load-bearing lines its own
    docstring names — `on_failure` runs *inside* the `except` block because retaining the
    exception pins the classifier graph via the traceback's `_predict_single` frame (which
    recreates the co-residency the restructure removed), and the `finally` bounds residency to one
    graph. In these last two, **the nesting is the correctness.**
- **Guard tests exist and are unmocked:** `tests/analyze/services/pipeline/test_analysis_streaming_decode.py:562`
  (`test_repeated_gated_chunk_decodes_do_not_grow_peak_rss`) and `:606`
  (`test_the_chunk_decode_leaves_no_connected_network_behind`). Both confirmed present in the
  worktree at the pinned commit.
- **Counter-evidence, stated honestly:** `7fad72f9` (2026-08-18, on `main`, `phaze-mp0op`)
  refactored this file successfully — five observability callbacks collapsed into one
  `AnalysisSignals` seam, 116 lines changed, +279 lines of new tests. **The file is not
  untouchable; it is changeable when a change has a seam and a test.** What was declined by this
  molecule is unseamed structural churn on the invariant surface — a narrower claim than "this
  file cannot be refactored," and that narrower claim is the one being made here.
- This is an application of the epic's Rule 2 ("'All findings cleared' is a red flag, not a
  goal"), not an exception to it.

**What the live index at the pinned commit shows now, for transparency (not this molecule's
result):** `score` 2.55, `max_ccn` 7, `max_nesting` 4, `nloc` 1161, structural findings 4 (down
from 8). Per §2, this movement is entirely attributable to the concurrent, independent
`phaze-1i0h6.1` bead ("Extract the chunk decoder subsystem from services/analysis.py without
weakening exhaustive analysis"), which extracted `services/analysis_decoder.py` from this file. It
is not a `phaze-vu88k` result and this ledger does not claim it as one. Notably, `phaze-1i0h6.1`'s
extraction operated on the same D-09 surface this molecule declined to touch for risk reasons —
that a *different* team, under a *different* bead with its own review gate, chose to take that risk
does not change the disposition this molecule made; it is recorded here only so the numbers are
not left unexplained.

**One sentence of live evidence for this file's own counter-evidence claim above, verified by the
dispatcher:** `phaze-1i0h6.1`'s two commits on this file land in sequence as `ae75db1f`
("refactor(analysis): extract chunk decoder protocol") immediately followed, within the same bead,
by `25b4912c` ("fix(analysis): tear down a partially built chunk decode network") — the extraction
opened a partial-teardown gap on exactly the D-09 surface, and the unmocked guard tests
(`test_repeated_gated_chunk_decodes_do_not_grow_peak_rss` /
`test_the_chunk_decode_leaves_no_connected_network_behind`, both still present at lines 562/606)
caught it before it could ship as another OOMKill. Read that sequence as the guards doing their
job, not as a near-miss: it is live evidence for this ledger's own claim that the file is
changeable when a change has a seam and a test, and it is why those two tests must stay unmocked.

## 7. New files (extraction targets from both epics, informational)

10 files appear in the after-index that were not in the before-index — all are files extracted
during this window by refactors (from either epic; not separated here since none are below 6.0
and none affect the 47-file cohort's accounting): `config_registry_policies.py` (9.15),
`services/agent_s3_reports.py` (8.05), `services/analysis_decoder.py` (6.76),
`services/analysis_timeline.py` (8.1), `services/execution_dispatch_protocol.py` (8.45),
`services/metadata_parsing.py` (9.7), `static/js/analysis_timeline.js` (8.35), and three template
partials scoring 9.7–10.0. None dropped below the 6.0 floor on arrival.

**A measurement-methodology note, not a defect:** `services/backends/kueue.py` and
`services/backends/compute_agent.py` were touched by exactly one commit each in this window
(`8c9bf715`, bead `.2`), whose own message claims "clears 2 io_in_loop + 2 serial_await_in_loop
findings (one pair per file)." The live index instead measures kueue.py 14 → 10 (4 cleared) and
compute_agent.py 8 → 4 (4 cleared) — double the commit's own claim, in the favourable direction.
Plausible mechanism: batching one query out of a loop can remove more than the single detector hit
it was aimed at (e.g. a paired `nested_loop_with_io` finding riding on the same call site). Stated
here so the discrepancy between the developer's narrative and the measured number is visible
rather than silently resolved in either direction.

## 8. Data-quality caveat: coverage re-ingestion did not run with this update

`repowise update` refreshes code/doc/health analysis; it does **not** re-ingest `coverage.py`
reports (the epic's baseline text notes coverage was "already ingested: 251 files, 29,778 rows" as
a one-time, separate step — not part of the update path exercised here). Consequently:

- `line_coverage_pct` is `null` for 165 of 382 files after this update, versus 121 of 372 before
  (a pre-existing partial-data condition that widened, not a regression this bead measured
  correctly).
- 5 files newly show an `untested_hotspot` finding (0 before, 5 after) exactly where
  `line_coverage_pct` went from a real percentage to `null`: `routers/shell.py`,
  `tasks/controller.py`, `tasks/scan.py`, `templates/record/record_body.html`,
  `templates/shell/shell.html`. This is why `routers/shell.py`'s score reads as a *drop* (4.17 →
  2.90) even though its structural findings fell 4 → 1 and its historical findings fell 7 → 4 —
  the drop is a stale-coverage artifact, not a regression from `.6`'s refactor. Same mechanism for
  `tasks/controller.py` (3.92 → 3.70) and the two "newly below 6.0" template files in §3.
- Separately and for real (not an artifact): touching a file adds a fresh `function_hotspot` /
  `change_entropy`-style historical signal in the same commit window, because those biomarkers are
  churn-recency signals and a just-landed fix is, by definition, recent churn. `services/companion.py`
  is the clean example: structural findings unchanged (2 → 2), one `nested_loop_with_io` cleared,
  but `historical` findings went 1 → 2 (a new `function_hotspot`) and the file's score dropped
  6.21 → 4.82 net of a coverage-null flip on the same file. This is the epic's own stated dynamic
  working exactly as described ("historical decays only as the file stays stable") — a file that
  was *just* stabilized temporarily reads as *less* stable by this specific signal, and that will
  correct itself with time, not with more editing.

Fixing coverage re-ingestion is out of scope for a measurement bead; it is recorded here so a
reader does not mistake a stale-data score movement for a code regression.

## 9. PERFORMANCE dimension (criterion 2 and 3)

**Repo-wide, all `src/` files** (this dimension is not scoped to the 47-file cohort in the
baseline's own wording):

| Biomarker | Before | After | Cleared |
| --- | ---: | ---: | ---: |
| `io_in_loop` | 80 | 59 | 21 |
| `serial_await_in_loop` | 33 | 25 | 8 |
| `nested_loop_with_io` | 3 | 1 | 2 |
| `hot_path_sync_io` | 1 | 1 | 0 |
| `membership_test_against_list_in_loop` | 1 | 0 | 1 |
| **Total** | **118** | **86** | **32** |

Per-file `performance_score` movement, every file that had a performance finding before or after
(37 files):

| File | Perf findings before → after | `performance_score` before → after |
| --- | --- | --- |
| `config.py` | 1 → 0 | 9.30 → 10.00 |
| `config_backends.py` | 1 → 1 | 9.30 → 9.30 |
| `main.py` | 1 → 1 | 9.30 → 9.30 |
| `routers/agent_analysis.py` | 2 → 2 | 9.18 → 9.18 |
| `routers/pipeline/analysis.py` | 2 → 2 | 8.60 → 8.60 |
| `routers/pipeline/skip.py` | 1 → 1 | 9.30 → 9.30 |
| `routers/proposals.py` | 1 → 0 | 9.79 → 10.00 |
| `routers/tags.py` | 2 → 1 | 8.60 → 9.30 |
| `services/analysis.py` | 1 → 1 | 9.85 → 9.85 |
| `services/analysis_exec.py` | 2 → 2 | 8.60 → 8.60 |
| `services/analysis_sizing.py` | 1 → 1 | 9.30 → 9.30 |
| `services/backends/compute_agent.py` | 8 → 4 | 8.00 → 8.36 |
| `services/backends/kueue.py` | 14 → 10 | 8.00 → 8.00 |
| `services/backends/lane_snapshot.py` | 4 → 4 | 8.00 → 8.00 |
| `services/companion.py` | 13 → 11 | 8.00 → 8.00 |
| `services/cue_review.py` | 4 → 4 | 8.36 → 8.36 |
| `services/dedup.py` | 2 → 2 | 9.18 → 9.18 |
| `services/dedup_review.py` | 1 → 1 | 9.30 → 9.30 |
| `services/enqueue_router.py` | 1 → 1 | 9.30 → 9.30 |
| `services/filename_convention_learner.py` | 2 → 2 | 9.18 → 9.18 |
| `services/pipeline/files.py` | 2 → 2 | 9.18 → 9.18 |
| `services/proposal.py` | 5 → 5 | 8.00 → 8.00 |
| `services/reanalysis_backfill.py` | 1 → 1 | 9.30 → 9.30 |
| `services/review.py` | 4 → 4 | 8.00 → 8.00 |
| `services/scan_deletion.py` | 2 → 2 | 9.18 → 9.18 |
| `services/scheduling_ledger.py` | 2 → 2 | 9.18 → 9.18 |
| `services/text_repair_backfill.py` | 4 → 4 | 8.36 → 8.36 |
| `services/tracklist_drain.py` | 7 → 7 | 8.00 → 8.00 |
| `services/tracklist_lookup_cache.py` | 3 → 3 | 8.48 → 8.48 |
| `tasks/agent_worker.py` | 1 → 1 | 9.30 → 9.30 |
| `tasks/discogs.py` | 2 → 0 | 9.18 → 10.00 |
| `tasks/execution.py` | 1 → 1 | 9.30 → 9.30 |
| `tasks/proposal.py` | 10 → 0 | 8.00 → 10.00 |
| `tasks/reenqueue.py` | 3 → 0 | 8.48 → 10.00 |
| `tasks/release_awaiting_cloud.py` | 2 → 2 | 8.60 → 8.60 |
| `tasks/tracklist.py` | 4 → 0 | 8.00 → 10.00 |
| `web/static.py` | 1 → 1 | 9.30 → 9.30 |

Beads `.2`/`.3`/`.4` per-finding narratives for the files that moved: batched `FileRecord` lookups
in the reap loops (`kueue.py`/`compute_agent.py`, `.2`); batched per-directory media lookup
(`services/companion.py`, `.3`); batched per-file reads in `generate_proposals`
(`tasks/proposal.py`, `.4`, 9 of 10 findings claimed cleared, 10 measured — §7); one batched
`DELETE` plus a set for a hot membership test (`tasks/discogs.py` / `routers/proposals.py`, `.4`);
batched tracklist re-arm into three statements (`tasks/tracklist.py` /
`services/tracklist_priority.py`, `.4`); the orphan-recovery renaming that incidentally moved two
`tasks/reenqueue.py` findings off this file without changing DB call count or order (`.5`,
explicitly **not** counted as a performance fix by that bead's own commit message).

## 10. Health-score movement — an observation, not the measure (criterion 3)

`performance_score` is excluded from `defect_score` entirely (`scoring.py:217-229`), and the
overall `score` tracks `defect_score` (established in the epic's filing, reconfirmed here: still
true for every file above). **None of the performance work in §9 moved any file's headline score.**
Where a headline score did move for a file that had *only* performance findings cleared and no
structural change (`tasks/discogs.py`, `tasks/tracklist.py`), that movement came from that file's
own historical/structural mix, not from the performance work — consistent with §9's finding that
`performance_score` itself was often unchanged even where perf findings cleared (e.g. `kueue.py`:
14 → 10 findings, `performance_score` unchanged at 8.00).

Repo-wide average health: 8.9 (unchanged from before per `repowise status`, rounded to the same
first decimal). This is reported once, here, as an observation — it is explicitly **not** offered
as evidence the molecule "worked," per criterion 3.

## 11. Reproducing this ledger

```bash
uv run scripts/health_baseline.py --out <new>.json
diff docs/spikes/phaze-vu88k.9-health-followup-2026-08-20.json <new>.json
~/.local/share/uv/tools/repowise/bin/python scripts/structural_findings.py --summary $(find src -name '*.py')
```

The first two reproduce this bead's own snapshot against a later index (as `phaze-vu88k.1`'s
artifact anticipated). The third reproduces the source-derived structural numbers in §4 with no
index at all, on any commit, in any worktree — see `scripts/structural_findings.py`'s own
docstring for validated scope and known limitations.
