# phaze-zaf2l — where phaze actually spends time, measured on the live system

- **Bead:** `phaze-zaf2l` (spike — replace the exhausted static performance detector with
  measurement, per the operator decision recorded below)
- **Date:** 2026-08-26
- **Tree:** branch `wt/bead/issue/phaze-zaf2l`, forked off `main` at `f9de77e6`
- **Code under test:** the **deployed** production release — `ghcr.io/simplicityguy/phaze:2026.8.5`
  (API + worker on `host-prod`) and `ghcr.io/simplicityguy/phaze/job:2026.8.5` (the analyze job
  image on `vox`). Nothing was overlaid, patched or rebuilt: every figure below comes from the
  binaries the operator is running right now
- **Status:** measurement only. **No product code changed.** All database access was read-only
  (`SET default_transaction_read_only = on` on every session; `SELECT` only)

______________________________________________________________________

## Why this bead exists

Epic `phaze-o8sie` ran nine static Repowise performance leads to verdict. **Six came back REFUTED
and zero code changed.** Bead `phaze-o8sie.9` established the mechanism: `io_in_loop` cannot
distinguish N queries per ROW from N queries per BATCH OF 500, so a keyset pager — an await in a
loop by definition — fires the detector identically to the N+1 it fixes. A codebase that has
already done its batching work lights up maximally.

> **Operator decision 2026-08-26.** Question as put: *"Given the static performance detector is
> exhausted as a source, how should the rescoped molecule get its leads?"* Answer as given
> (selected option label, verbatim): **"Measure first, then file"**. Durable record: bead
> `phaze-zaf2l`'s description and a comment on that bead.

This document is the measurement. Every follow-on bead it files carries a number taken here.

______________________________________________________________________

## Verdict in one paragraph

**The analysis pipeline is where the wall clock goes, the coarse tier is where the analysis goes,
and the operator can see none of it.** At the production operating point — `cap = 4` concurrent
analyze pods on `vox` — one file costs **1.4951× its own duration** end to end, measured on a
completed production run of a 4,761.835 s file that took **7,119.473 s**; a second, independent
derivation from seven days of completions (**2.7183 audio-hours per wall-hour** across **410
files**) predicts **1.4715×**, agreeing to **1.58%**. That does **not** contradict
`phaze-b2qs9`'s **0.56–0.79×**: that figure was measured **solo on an idle node** and this one is
at `W=4`, and `phaze-8r6t4` §10 already priced W=4 at **+83.6%** per-file wall against W=2. Inside
that run the **fine tier is 5.31%** (378.266 s) and **coarse + the model sweep is 94.69%**
(6,741.207 s) — a split that is **duration-dependent and inverts on a multi-hour file**, see §3b's
2026-08-28 forward note before citing it — and `AnalysisSignals.progress` is documented **"fine tier only"**, so the pod log
*and* the web progress bar (one throttle, one counter, OBS-02) reach 100% at 5.31% of the job and
then sit there in silence for **1 h 52 m**, which is exactly what all four in-flight pods were
observed doing. On the admin UI the heaviest surface is not a page but a **poll**:
`/pipeline/stats` costs **534.0 ms**, reads **~350,000 rows** and initiates **~47,000–55,000**
index scans per request against tables holding **~38,000 rows total**, and `shell.html` fires it
**every 5 s on every page** — a **10.68%** continuous duty cycle, **384.5 s of handler time per
wall hour**, for a browser tab left open. `/pipeline/tracklist-drain-status` costs **1,378.6 ms**
for a **6,095-byte** response, and its database fan-out is **below the measurement noise floor** —
so unlike the two above, its cost is *not* database time, and `build_drain_queue` building the
whole queue to report a status is a lead rather than an attribution.
**SAQ is not a bottleneck and gets no bead:** production queue depth held at **9** across
**28 samples**, dequeue latency measured **11–24 ms**, and a local burst drained **5,000 jobs at
318.3/s** — within **3.0%** of the 500-job rate, i.e. flat in depth — against a production
controller load of **0.0939 jobs/s**, which is **0.03%** of that capacity. The real backlog is not
in the queue at all: it is **8,079 `cloud_job` rows in `awaiting`**, a table, which at the measured
**2.4480 files/hour** is **137.5 days** of drain. And one finding was invisible to every surface
above: **661 files (223.99 audio-hours) hold analysis rows with 100.0% of their fine windows
complete, sit in state `discovered`, and have no `cloud_job` row at all** — stranded, queued
nowhere, **644 of them created in three weeks between 2026-07-27 and 2026-08-16 and none since**.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Analysis host** | `vox` — Debian 13 (trixie), kernel 6.12.100+deb13-amd64, k0s v1.36.2, **8 logical CPU**, 32,829,576 KiB (31.31 GiB) capacity. The k0s burst node, **in the production registry and serving real traffic throughout** — it was *not* taken out for this measurement |
| **Control-plane host** | `host-prod` — Debian 13 (trixie), kernel 6.12.96+deb13-amd64, **14 cores, 125 GB RAM**. Runs `phaze-api`, `phaze-worker`, `phaze-redis` (redis:8-alpine) and `postgres` (**PostgreSQL 18.6**) as Docker containers |
| **Fileserver host** | `host-store` — 8 cores, 62 GB. Runs the agent workers (`-meta` / `-io` / `-analyze`) and the watcher; it backs the registry's local catch entry — `kind = "local"`, `rank 99`, `cap 1` |
| **Local host** | MacBookPro18,1, macOS 26.6.2, 10 cores, 34,359,738,368 B (32 GiB), Python 3.14.5 — used **only** for the SAQ burst of §5, never for any production figure |
| **Concurrency** | `backends.toml` sets `vox` to `rank 10`, **`cap = 4`**, `cpu_request 1500m`, `memory_request 3Gi`, `memory_limit 4Gi`. The second Kueue backend, `xenolab`, is **commented out** (disabled 2026-07-14, power incident), so `cap 4` on `vox` plus `cap 1` local is the whole analysis capacity. Four analyze pods were running for the entire measurement window |
| **Per-file analysis wall clock** | the job's **own** `job_runner_step_ok` line for `step=analyze`, whose `elapsed_ms` the runner takes from its own clock — not an inferred pod age, and not the `kubectl` `AGE` column |
| **Tier split** | the timestamp of the last `job_runner_progress` line (`fine_windows_analyzed == fine_windows_total`) against `job_runner_analyze_begin` and the `analyze` step's own `elapsed_ms`. Both are shipped log lines; no instrumentation was added |
| **Throughput** | `analysis.analysis_completed_at` joined to `metadata.duration`, over a 7-day window. This is the completion ledger the application itself writes |
| **Admin-UI render time** | `curl` **`%{time_starttransfer}`** (time to first byte ≈ server-side render), run **on `host-prod` itself** against the API's own listener, so tailnet RTT is excluded. **2 warm-up requests then 7 timed**, per URL; min / median / max / mean all reported |
| **Per-request DB work** | deltas of `pg_stat_user_tables` across N requests, against a **duration-matched idle control** measured immediately beforehand so the production worker's own traffic is subtracted rather than attributed. Two metrics, deliberately separated: **`seq_scan + idx_scan`** counts scan *initiations* (this is what exposes nested-loop fan-out) and **`seq_tup_read + idx_tup_fetch`** counts *rows*. Every query is scoped to `schemaname='public'` in `current_database()` |
| **Queue depth** | sampled every 120 s for the duration of the spike, straight from `saq_jobs` and `cloud_job` |
| **Machine headroom** | `sysctl vm.swapusage` read **`used = 0.00M`** before every timed run, and `memory_pressure` reported 56% free. No production figure was taken while the local machine was under load, and the local SAQ burst was the only thing this machine ran |

**No operator media is named anywhere in this document.** Files are referred to as `<set-01>` …
`<set-05>` and characterized only by container, duration and size. No filename, path, content
digest, file UUID, bucket key or per-file metadata value appears here. **Nothing was written to
production**: no job was enqueued, no pod was created or deleted, no row was updated, and the only
requests issued against `phaze-api` were `GET`s that the admin UI itself issues on a timer.

### 1a. What this measurement CANNOT see, stated up front

- **The solo (`W=1`) analysis ratio was not re-measured.** Taking `vox` out of the registry is a
  production mutation and is out of scope for a read-only spike. `phaze-b2qs9`'s 0.56–0.79× is
  therefore neither confirmed nor refuted here; §3c reconciles the two operating points instead.
- **Peak RSS was not measured.** `_log_job_peak_rss` writes to the analysis child's stdout, which
  is the protocol channel, so it never reaches the pod log; only child *stderr* surfaces (as
  `analysis_child_output`, one line per run). What §3d reports is `kubectl top`, a **sampled
  gauge**, which is *not* a high-water mark and must not be read as one.
- **The review surfaces were measured EMPTY.** `proposals` holds **0 rows** right now, so the
  12.5–23.1 ms figures for `/s/propose`, `/s/rename`, `/s/tagwrite` and `/s/move` measure an empty
  surface and **discharge no claim about a loaded one**. This is recorded as a gap, not a result.

______________________________________________________________________

## 2. The population, measured

One query over the live production database settles the corpus (CLAUDE.md rule 4):

```sql
SELECT count(*), count(duration), min(duration), max(duration), avg(duration),
       percentile_cont(0.50) WITHIN GROUP (ORDER BY duration), sum(duration)/3600.0
FROM metadata;
```

| | |
| --- | --- |
| rows in `files` / `metadata` | **11,428** / **11,428** |
| rows carrying a duration | **11,412** (16 NULL) |
| shortest / longest | **8.470 s** / **43,466.880 s** (12 h 04 m 27 s) |
| mean / **median** | 3,625.306 s / **3,531.967 s** (58 m 52 s) |
| p90 / p99 | 7,046.040 s (1 h 57 m) / 10,789.232 s (2 h 59 m) |
| **total audio in the archive** | **11,492.22 hours** |

| duration band | files | audio hours |
| --- | ---: | ---: |
| < 10 m | 1,604 | 128.06 |
| 10–30 m | 551 | 219.93 |
| **30–60 m** | **4,748** | **4,200.18** |
| **1–2 h** | **3,760** | **4,954.17** |
| 2–4 h | 706 | 1,753.52 |
| 4–8 h | 39 | 196.73 |
| ≥ 8 h | 4 | 39.64 |

**The corpus is continuous with `phaze-b2qs9`'s.** That spike's `<set-07>`, described as "the
longest file in the corpus", measured **43,466.893061 s** by `ffprobe`; the longest duration in
`metadata` today is **43,466.880 s** — the same file, differing only by the truncation `phaze-b2qs9`
§1a already documents between `ffprobe`'s fractional duration and `_probe_duration_sec`'s. Both
spikes are therefore measuring the same archive, and the count in CLAUDE.md ("all 11,428 files in
the corpus") is still exact.

**8,508 of 11,412 files (74.55%) fall in the 30 m – 2 h range and carry 9,154.35 of the 11,492.22
audio-hours (79.66%).** This matters for §3: the four files measured there have durations
3,514.648 / 3,906.363 / 4,200.281 / 4,761.835 s, all inside that dominant band and clustered on the
**3,531.967 s median**. The analysis measurement is therefore taken on the modal file of the
archive, not on a convenient one.

Pipeline state at the time of measurement:

| | |
| --- | --- |
| `analysis` rows / distinct files | 5,846 / **5,846** (exactly one row per file — no superseded duplicates) |
| of those, completed | **4,383** |
| of those, hard-failed | **4** (3 `AnalysisDecodeError`, 1 `AnalysisProbeError`) |
| of those, **neither** completed nor failed | **1,459**, of which **1,455 are older than 7 days**, oldest **2026-06-14** |
| files with no `analysis` row at all | **5,582** |
| `analysis_window` rows | 316,159 |
| `cloud_job` by status | **awaiting 8,079–8,080**, succeeded 799, uploaded 32, uploading 3, running 4 |

______________________________________________________________________

## 3. Surface 1 — the analysis pipeline

### 3a. One complete production run, end to end

`<set-01>` ran to completion during the measurement window and emitted its own timings:

| | |
| --- | --- |
| file | `<set-01>` — mp3, 4,761.835 s (1 h 19 m 22 s), 100.2 MB |
| fine windows | 159 / 159 analyzed |
| presign / download / verify / extract | 152 ms / 992 ms / 360 ms / 156 ms |
| **`analyze` step (`elapsed_ms`)** | **7,119,473 ms = 7,119.473 s** |
| callback | 175 ms |
| **end to end** (banner → `job_runner_complete`) | **7,121.167 s** |
| **everything that is not `analyze`** | **1.694 s — 0.02% of the job** |
| **ratio to the file's own duration** | **1.4951×** |

The staging path — presign, download, verify, extract, callback — costs **1.694 s against
7,121.167 s**. It is not where the time goes, and it gets no bead.

### 3b. The tier split — and the observability gap it exposes

| phase | seconds | share of `analyze` |
| --- | ---: | ---: |
| fine tier (159 windows, 30 s each) | **378.266** | **5.31%** |
| **coarse tier + model sweep** | **6,741.207** | **94.69%** |

> **FORWARD NOTE, added 2026-08-28 by `phaze-bg115`. The measurement above is correct and
> unchanged. What follows is a limit on how far it generalises.**
>
> **This split is a property of the file's DURATION, not of phaze's analysis.** It was taken on
> a **4,761.835 s** file, and it holds for files of that length — it has since been
> independently reproduced. It does not hold as duration grows.
>
> Measured 2026-08-27/28 on the burst node, deployed job image, real 34-graph model set, node
> drained and uncontended, two real corpus files, telemetry captured through a real collector
> (348 spans, `receiver_refused` 0, `exporter_send_failed` 0). Same node, same image, same code,
> same run — **duration is the only variable**:
>
> | file duration | fine | coarse + model sweep | decode, whole analysis |
> | ---: | ---: | ---: | ---: |
> | **3,578.964 s** (59 m 39 s) | 7.9% | 92.1% | 7.7% |
> | **36,182.359 s** (10 h 03 m) | **49.3%** | **50.7%** | **57.5%** |
>
> The short file reproduces this section almost exactly, which **confirms** it. The long file
> inverts the split.
>
> **Why the tiers diverge — the partial final chunk of each tier is a natural experiment on
> whether cost scales with work.** On the 10 h 03 m file:
>
> | tier | full chunks | partial final chunk | reading |
> | --- | --- | --- | --- |
> | fine | 60 windows at ~12 s/window | **6 windows at 84.75 s/window** | a **7x** per-window jump on identical work — most of a fine chunk's cost is FIXED, not per-window (fitting these two points puts the fixed part at ~67% of a full chunk) |
> | coarse | 30 windows at ~106 s/window | 22 windows at **106.94 s/window** | unchanged — the coarse tier is **linear in windows** (fixed part ~2%) |
>
> The fine tier is **97.7% decode**; the coarse tier **18.4%**. A longer file is more chunks, and
> chunks are where the fine tier's fixed cost is paid — so the fine share climbs with duration
> while the coarse share falls.
>
> **The consequence for anyone sizing work against the 94.69%:** on a multi-hour file it is
> roughly **2x wrong**. On a 10-hour set, halving the model sweep cuts total analysis by about
> **25%**, not about **47%**.
>
> **The general form**, per ADR-0012 rule 5: **a tier split measured on one file is a property of
> that DURATION**, because the tiers scale differently — one is linear in windows and the other
> carries a large fixed per-chunk cost. Any split, ratio or share taken from a single file
> inherits that file's duration as a condition, and must be cited with it.
>
> Raw capture and the per-chunk figures: beads `phaze-uvln0` and `phaze-8ifq8`. **No analysis
> parameter changed** — coarse windows remain 180 s and coverage remains exhaustive (operator
> constraint, `phaze-m1drf`, 2026-08-27).

**Every** pod observed showed the same shape — fine tier complete within minutes, then silence.
Fine elapsed is measured from each job's own `job_runner_analyze_begin` to its own final
`job_runner_progress` line, not from pod start:

| file | duration (s) | fine windows | **fine elapsed (s)** | s / window | fine as % of duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` | 4,761.835 | 159 | **378.266** | 2.379 | 7.94% |
| `<set-02>` | 3,906.363 | 130 | **759.831** | 5.845 | 19.45% |
| `<set-03>` | 3,514.648 | 117 | **286.066** | 2.445 | 8.14% |
| `<set-04>` | 4,200.281 | 140 | **360.220** | 2.573 | 8.58% |
| `<set-05>` | 5,789.803 | 193 | **519.875** | 2.694 | 8.98% |

Four of the five sit in a tight **2.379–2.694 s per fine window**; `<set-02>` at **5.845 s** is a
2.4× outlier this spike does not explain and does not guess at — `phaze-b2qs9` §1a records that a
48 kHz source pays a 48,000 → 44,100 Hz resample in the fine tier that a 44.1 kHz source does not,
which is a candidate. It could not be settled from the database: `metadata` stores `bitrate` and
**no sample-rate column at all**, so confirming or refuting it means reading the audio itself.
Named as an open question rather than answered.

`<set-01>` — the one file that ran to completion inside the window — then emitted **not one
further log line for 1 h 52 m 21 s** while holding **1.6–2.2 CPU cores**, until its completion
block at 04:18:47.

**This is by construction, not a fault in the run.** `AnalysisSignals.progress` in
`src/phaze/services/analysis.py` carries the docstring *"Fire the UI progress channel (fine tier
only). A no-op when unset."*; `analyze_file` calls `_analyze_fine_windows` and then
`_analyze_coarse_windows`, and the coarse pass — which is where `_run_model_sets_over_windows`
sweeps the 34 graphs — never calls it. `job_runner.py:312`'s `_make_progress_cb` is documented as
*"the sync `progress_cb` the analysis-child driver invokes per FINE window"*, and OBS-02
(`phaze-sfbx.3`) deliberately binds the console line and the web progress POST to **one throttle
and one counter** so they can never diverge. They do not diverge: **both reach 100% at 5.31% of the
job and then report nothing for the remaining 94.69%.**

Mechanism verified in the source *and* instance observed in four live production pods — ADR-0016's
two halves, both discharged.

### 3c. Does `phaze-b2qs9`'s 0.56–0.79× still hold?

**It is not contradicted, and it does not describe production.** Two independent methods agree on
the production number:

| method | figure |
| --- | ---: |
| direct — `<set-01>`'s own `analyze` `elapsed_ms` ÷ its own duration | **1.4951×** |
| derived — `W ÷ throughput`, from 410 completions over 167.487 h (below) | **1.4715×** |
| **agreement** | **1.58%** |

A **third**, fully independent corroboration falls out of an existing spike. `phaze-8r6t4`'s
throughput sweep measured **29.8 files/hour at W=4** (§ its "files/hour vs concurrency — NEW"
figure) on **synthesized sine files of 180 / 300 / 420 s — mean exactly 300 s**. That is
**2.4833 audio-hours per wall-hour**, against the **2.7183** measured here from seven days of real
production completions: **agreement to 9.46%**, with production the faster of the two, which is the
expected direction because the real corpus's mean file is 3,625.306 s and amortises per-file fixed
cost over twelve times more audio. Two caveats stated rather than buried: that sweep ran
`release/2026.8.1-prep` where production now runs `2026.8.5`, and its inputs were synthetic sine
pairs rather than real mp3. It is a cross-check, not a substitute for the two rows above.

The reconciliation is the operating point, and it was already priced: `phaze-b2qs9` §1b states its
runs were solo on an idle node, and `phaze-8r6t4` §10 measured **+83.6% per-file wall at W=4
against W=2**. A solo 0.56–0.79× and a W=4 1.4951× are consistent with each other and with that
correction. `vox` sat at **98–99% CPU** for the entire window (7,903–7,955 m of 8,000 m), which is
the deliberate oversubscription `backends.toml` describes: 4 pods × 1 process × 4 TF threads = 16
threads on 4 physical cores.

**Steady-state throughput, 7 days, from the application's own completion ledger:**

| | |
| --- | --- |
| files completed | **410** |
| span | **167.487 h** |
| audio processed | **455.272 h** |
| **files per wall-hour** | **2.4480** |
| **audio-hours per wall-hour** | **2.7183** |

**Window sensitivity**, because a single window is not a rate. Re-run at 04:41 UTC over four
window lengths (the 7-day row differs from the table above only because the window had slid by
~35 minutes):

| window | files | span (h) | files/wall-h | **audio-h/wall-h** | implied ratio at W=4 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3 days | 152 | 70.622 | 2.1523 | 2.4771 | 1.615× |
| 7 days | 408 | 167.425 | 2.4369 | **2.7163** | 1.473× |
| 14 days | 1,001 | 335.650 | 2.9823 | 2.9175 | 1.371× |
| 30 days | 2,577 | 719.294 | 3.5827 | 2.2348 | 1.790× |

Audio-hours per wall-hour holds in a **2.2348–2.9175** band across every window, and the directly
measured **1.4951×** sits inside the **1.371–1.790×** ratio band that band implies. Files per
wall-hour is the *less* stable of the two (2.1523–3.5827) precisely because it is blind to the mix
of durations — which is why every drain estimate below is given as a band rather than a point.

What that buys, at the measured rate — point estimate from the 7-day window, band from the four
windows above:

| question | point | band |
| --- | ---: | ---: |
| drain the **5,582** files with no analysis row | **2,280.2 h = 95.0 days** | 1,558–2,594 h = **65–108 days** |
| drain the **8,079** `cloud_job` rows in `awaiting` | **3,300.2 h = 137.5 days** | 2,255–3,754 h = **94–156 days** |
| re-analyze the whole **11,492.22**-hour archive | **4,227.7 h = 176.2 days** | 3,939–5,142 h = **164–214 days** |

### 3d. Memory

`kubectl top` sampled the four pods across the window at **823–1,364 MiB** against a
`memory_limit` of **4Gi**, with the node at **19–21%** of 31.31 GiB. This is a **sampled gauge and
not a peak**, so it cannot confirm a high-water figure — but nothing in the window approached the
limit, and it is consistent with the **1.7383 GiB** joint peak `backends.toml` records from
`phaze-5lop` after `phaze-15sw`'s model-major restructure. The duration-linear growth
`phaze-b2qs9` measured (+0.31 GiB per fine chunk, 10.28 GiB at 12 h) is **not** in evidence here.
**No bead is filed against analysis memory.**

______________________________________________________________________

## 4. Surface 2 — the admin UI read paths

Measured on `host-prod`, against the live 11,428-file corpus. 2 warm-up + 7 timed requests each;
`time_starttransfer` in ms.

| route | http | min | **p50** | max | mean | bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `/health` | 200 | 4.0 | 4.3 | 4.5 | 4.3 | 15 |
| **`/s/analyze`** | 200 | 574.3 | **633.6** | 658.6 | **627.0** | 100,711 |
| `/` and `/s/summary` | 200 | 262.0 | 282.1 | 305.0 | 283.0 | 90,239 |
| `/s/tracklist` | 200 | 218.2 | 219.4 | 234.5 | 221.8 | 81,614 |
| `/s/metadata` | 200 | 83.0 | 97.8 | 242.7 | 114.9 | 88,834 |
| `/s/files` | 200 | 42.3 | 51.6 | 53.3 | 50.5 | **681,013** |
| `/s/dedupe` | 200 | 18.1 | 18.8 | 20.5 | 19.1 | 111,775 |
| `/s/discover` | 200 | 14.7 | 17.0 | 17.6 | 16.3 | 91,568 |
| `/s/apply` | 200 | 20.7 | 23.6 | 149.2 | 40.7 | 80,043 |
| `/s/propose` * | 200 | 12.7 | 15.2 | 57.4 | 23.1 | 83,909 |
| `/s/rename` * | 200 | 11.8 | 13.9 | 16.0 | 14.1 | 84,438 |
| `/s/tagwrite` * | 200 | 11.3 | 12.8 | 13.8 | 12.7 | 84,440 |
| `/s/move` * | 200 | 10.7 | 12.6 | 13.7 | 12.5 | 84,436 |
| `/s/audit` | 200 | 11.6 | 12.6 | 13.9 | 12.6 | 79,049 |
| `/s/agents` | 200 | 9.2 | 9.9 | 11.7 | 10.1 | 87,359 |
| `/s/cue` | 200 | 9.1 | 9.3 | 11.2 | 9.7 | 76,676 |
| `/s/operations` | 200 | 5.8 | 6.2 | 6.9 | 6.3 | 76,573 |

\* **measured against `proposals` = 0 rows.** These four numbers characterize an *empty* surface
and are not evidence about a loaded one (§1a).

| partial / fragment | http | min | **p50** | max | mean | bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **`/pipeline/tracklist-drain-status`** | 200 | 1,236.8 | **1,419.0** | 1,473.9 | **1,378.6** | 6,095 |
| **`/pipeline/stats`** | 200 | 510.1 | **536.1** | 569.3 | **534.0** | 20,001 |
| `/pipeline/pending-files` | 200 | 34.9 | 35.8 | 41.2 | 37.0 | 625 |
| `/pipeline/analyze-files` | 200 | 29.8 | 30.5 | 31.6 | 30.6 | 64,177 |
| `/api/v1/duplicates` | 200 | 19.2 | 20.2 | 21.0 | 20.2 | 4,058 |
| **`/record/<uuid-1>`** (the record slide-in) | 200 | 17.1 | 19.7 | 24.7 | **20.6** | 29,419 |
| `/admin/agents/_table` | 200 | 8.6 | 9.3 | 9.6 | 9.2 | 12,142 |
| `/pipeline/tracklist-sets` | 200 | 7.2 | 8.3 | 8.5 | 7.9 | 15,742 |
| `/pipeline/recover/status` | 200 | 3.0 | 3.3 | 3.4 | 3.2 | 211 |

**The record slide-in is fast — 20.6 ms against the real corpus — and gets no bead.**

### 4a. `/pipeline/stats` — the cost is a poll, not a page

`src/phaze/templates/shell/shell.html:290-291`:

```html
<div id="pipeline-stats"
     hx-get="/pipeline/stats"
     hx-trigger="every 5s [document.visibilityState === 'visible'], refresh"
```

That div is in **`shell.html`**, which `src/phaze/routers/shell/__init__.py:215` returns for every
stage. Confirmed against the **served HTML** rather than the template — `/s/cue`, `/s/agents`,
`/s/dedupe` and `/s/files` each return **exactly one** `hx-get="/pipeline/stats"` — so this is the
page the browser actually gets, not an inference from reading Jinja. The 534.0 ms is therefore paid
**every 5 seconds for as long as any admin tab is visible**:

| | |
| --- | --- |
| cost per request | **534.0 ms** |
| interval | **5 s**, every page, while visible |
| **duty cycle** | **10.68%** of one request-handling slot, continuously |
| **handler time per wall hour** | **384.5 s** (720 requests × 0.534 s) |

Per-request database work, background-corrected against a duration-matched idle control, two
independent rounds at N=15:

| table | rows in table | scans/request (r1) | scans/request (r2) | **rows read/request** |
| --- | ---: | ---: | ---: | ---: |
| `scheduling_ledger` | 3,245 | 19,177.7 | 15,467.9 | 8,505.4 |
| `analysis` | 5,846 | 17,007.0 | 15,079.8 | 18,325.3 |
| `files` | 11,428 | 9,022.7 | 7,943.8 | **115,132.5** |
| `stage_skip` | **0** | 8,083.9 | 7,006.4 | — |
| `cloud_job` | 9,281 | 1,007.8 | 1,006.3 | **174,289.5** |
| `metadata` | 11,428 | 871.7 | 871.0 | 33,575.2 |
| `saq_jobs` | 13 | 32.9 | 31.0 | 41.2 |
| `agents` | 3 | 5.3 | 5.4 | 13.1 |
| **total** | **~38,000** | **~55,200** | **~47,400** | **~350,000** |

One request initiates **~47,000–55,000 index scans** and reads **~350,000 rows** from tables holding
**~38,000 rows in total** — it re-reads `cloud_job` **18.8×** over and `files` **10.1×** over. The
scan counts scale with the *file* table, not with the answer: `stage_skip` holds **zero rows** and
is probed **~7,000–8,100 times per request**.

The shape is a correlated subquery evaluated per file row, not a Python-level loop —
`src/phaze/services/stage_status.py:266`:

```python
return exists(select(StageSkip.id).where(StageSkip.file_id == FileRecord.id, StageSkip.stage == stage.value))
```

`_build_dag_context` (`src/phaze/routers/pipeline/dashboard_stats.py:182`) then runs roughly nine
further awaits **sequentially after** the `asyncio.gather` at line 615 — the code says so at line
591 (*"…stays a sequential await AFTER this gather"*).

**This is precisely the class of cost the static detector cannot see** — none of it is an await
inside a `for`, so `io_in_loop` never fired on it, and it is the single most expensive repeating
operation in the admin UI.

### 4b. `/pipeline/tracklist-drain-status` — 1.4 s that is not database time

| | |
| --- | --- |
| cost | **1,378.6 ms** mean, 1,473.9 ms max |
| response | **6,095 bytes** |
| **scans per request** | **below the noise floor** — see below |
| trigger | `hx-trigger="load, drain-refresh from:body"` — **on load, NOT polled** |

Its database fan-out could **not** be measured: over a 16.418 s window at N=12 the idle control
recorded *more* scans than the test window on every table (`analysis` 25,968 idle vs 19,476 test,
`cloud_job` 12,988 vs 9,741, `metadata` 12,984 vs 9,750), so the background-corrected per-request
figure comes out **negative**. The honest reading is that this endpoint's own scan fan-out is
**indistinguishable from zero** against the production worker's own background traffic — which is
itself ~26,000 scans per 16 s. It is emphatically **not** the `/pipeline/stats` shape.

So the 1.4 s is not database time. It is in `build_drain_queue`
(`src/phaze/tasks/tracklist_drain.py:148`), which the status endpoint calls to build the **entire**
drain queue and then uses for `entries[:10]` and a few aggregate counts. It is a one-shot cost when
the Tracklists workspace opens, **not** a duty cycle — stated explicitly because overstating it as
a poll would be wrong.

### 4c. `/s/analyze` — the same shape, paid on first paint

The heaviest *page* carries the same correlated-subquery fan-out as the poll, because it renders
the same DAG/stage context. Corrected per-request scans, N=12:

| table | scans/request |
| --- | ---: |
| `scheduling_ledger` | 15,469.2 |
| `analysis` | 13,216.1 |
| `files` | 6,926.0 |
| `stage_skip` | 6,063.5 |
| `cloud_job` | 939.1 |
| `metadata` | 816.6 |
| `saq_jobs` | 32.2 |
| `agents` | 4.7 |
| **total** | **~43,500** |

At **627.0 ms** mean / **633.6 ms** p50 this is the slowest first paint in the admin UI, and it is
also the stage an operator watching an 8,079-file backlog drain will keep open — where it then also
pays §4a's 534.0 ms every 5 s.

______________________________________________________________________

## 5. Surface 3 — SAQ latency and queue depth

### 5a. Production, observed

| | |
| --- | --- |
| queue-depth samples taken | **28**, one per 120 s |
| `saq_jobs` queued, every sample | **9** |
| `saq_jobs` active, every sample | **0** |
| what the 9 are | periodic maintenance only — `reap_stuck_aborting_jobs`, `reap_stalled_scans`, `continue_armed_tracklist_drain`, `reap_stranded_active_jobs`, `reconcile_stale_stage_parks`, `stage_cloud_window`, `reap_resolved_ledger_rows`, `reconcile_cloud_jobs`, `refresh_tracklists` |
| controller lifetime throughput | **51,626 complete**, 4 failed, 12 retried, over **549,747.192 s (6.3628 days)** = **0.0939 jobs/s** |

Per-job latency, from the jobs' own `queued` / `started` / `completed` fields (n=4, all that the
table retains):

| queue | function | **queue latency** | run time |
| --- | --- | ---: | ---: |
| `controller` | `submit_cloud_job` | **16 ms** | 63 ms |
| `controller` | `submit_cloud_job` | **11 ms** | 67 ms |
| `phaze-agent-<host-store>-io` | `s3_upload` | **16 ms** | 1,763 ms |
| `phaze-agent-<host-store>-io` | `s3_upload` | **24 ms** | 2,171 ms |

### 5b. Burst, measured

Production cannot be burst without mutating it, so the burst was run **locally** against this
seat's isolated Postgres + Redis, using the project's own `build_pipeline_queue`
(`src/phaze/tasks/_shared/queue_factory.py`) — the real `ResilientPostgresQueue`, the real broker,
the real `before_enqueue` hooks — with a trivial task so the figure is broker cost, not task cost.
ADR-0016: this is the queue's behaviour **in this environment**, not a belief imported from
elsewhere.

| burst | enqueue | enqueue rate | drain | **drain rate** | concurrency |
| ---: | ---: | ---: | ---: | ---: | ---: |
| **500 jobs** | 1.6478 s | 303.4/s | 1.5240 s | **328.1/s** | 10 |
| **5,000 jobs** | 11.2633 s | 443.9/s | 15.7063 s | **318.3/s** | 10 |

**Drain rate is flat in depth: a 10× larger burst cost 3.0% in throughput**, and the depth curve
was linear from 5,000 to 0 with no inflection.

### 5c. Verdict — no bead

**SAQ is not a bottleneck and no bead is warranted.** Production runs at **0.0939 jobs/s** against a
measured drain capacity of **318.3 jobs/s** — a **0.03%** utilisation — with observed depth pinned
at **9** and dequeue latency of **11–24 ms**. Per the bead's acceptance criterion 4, that is
recorded as a result with its figures rather than as an absence.

**The backlog is real, but it is not in the queue.** It sits in **`cloud_job.status = 'awaiting'`,
8,079 rows** — a table, drained by `stage_cloud_window` against the `cap = 4` admission limit. Queue
depth is *structurally* bounded by that cap, so a burst cannot form in SAQ no matter how deep the
backlog gets. Making SAQ faster would not move a single file.

______________________________________________________________________

## 6. An unbudgeted finding: 661 files stranded with completed fine-tier work

This was not on the list of surfaces to measure; it fell out of §2's state query and is the
cheapest real work this document found.

**1,459** `analysis` rows carry neither `analysis_completed_at` nor `failed_at`; **1,455 are more
than 7 days old**. Narrowing to rows that did real work and are queued nowhere:

```sql
SELECT count(*), sum(m.duration)/3600.0
FROM analysis a JOIN metadata m ON m.file_id = a.file_id
WHERE a.analysis_completed_at IS NULL
  AND a.failed_at IS NULL
  AND a.fine_windows_analyzed > 0
  AND NOT EXISTS (SELECT 1 FROM cloud_job cj WHERE cj.file_id = a.file_id);
```

The `NOT EXISTS` is what makes this bucket the interesting one. Of the wider 864-file set that has
fine-window work and no completion, **4 were genuinely running** (the live pods — the `analysis` row
is written when analysis *begins*, so "no completion" alone does not mean stranded) and **199 were
`cloud_job` `awaiting`** and will be redone. Only the remaining **661** are queued nowhere.

| | |
| --- | --- |
| files | **661** |
| audio | **223.99 hours** |
| **fine windows complete** | **100.0%** |
| file state | **`discovered`** (862 of the wider 864-file set; 2 in `analysis_failed`) |
| `cloud_job` row | **none** |
| created between | **2026-06-14** and **2026-08-12** |

Weekly creation, which is the interesting part:

| week of | files stranded |
| --- | ---: |
| 2026-06-08 | 2 |
| 2026-06-15 | 15 |
| 2026-07-27 | 130 |
| 2026-08-03 | 141 |
| **2026-08-10** | **373** |
| 2026-08-17 onward | **0** |

**644 of 661 fall in the three week-buckets beginning 2026-07-27, 2026-08-03 and 2026-08-10 — the
latest row of all is 2026-08-12 — and nothing has entered this bucket since.** That window
brackets the analysis-memory work (`phaze-b2qs9`'s measurement of duration-linear peak RSS is dated
2026-08-11/12, and `phaze-15sw`/D-09 followed), and `backends.toml` explicitly warns to *"EXPECT
pod-scoped OOMKills"*. A pod OOMKilled during the coarse tier — 94.69% of the run, per §3b — would
leave exactly this signature: fine tier 100%, coarse absent, no completion, no failure row.

**That is a correlation, not a demonstrated cause**, and the bead filed from it is scoped to
establish the mechanism before proposing a fix. What is *measured* is the cost: re-analysing
223.99 audio-hours at 2.7183 audio-h/wall-h is **82.4 wall-hours = 3.43 days** of the entire burst
lane. Across the wider 864-file set (562.14 audio-hours) it is **206.8 wall-hours = 8.62 days**.

These files are invisible to the dashboard: `get_analysis_stalled_count`
(`src/phaze/services/pipeline/failures.py:81`) counts only files in `ANALYSIS_FAILED` carrying a
`"timeout:"` error — deliberately a **subset of failed** — and these rows have `failed_at IS NULL`.
They are also excluded from the 5,582 "no analysis row" count, because they *have* one.

______________________________________________________________________

## 7. What got filed, and what did not

| surface | measured | bead |
| --- | --- | --- |
| Analysis — coarse tier invisible | fine tier **5.31%** of a 7,119.473 s run; **1 h 52 m** of silence | **`phaze-bp9kz`** |
| Admin UI — `/pipeline/stats` poll | **534.0 ms** every 5 s, **10.68%** duty cycle, **~350,000 rows/request** | **`phaze-ajnaa`** |
| Admin UI — `/pipeline/tracklist-drain-status` | **1,378.6 ms** for 6,095 bytes, DB fan-out below the noise floor | **`phaze-ih3zd`** |
| Admin UI — `/s/analyze` | **627.0 ms**, **~43,500** scans/request (same shape as stats) | **`phaze-y0upq`** |
| Stranded analysis rows | **661 files / 223.99 audio-h / 82.4 wall-h** to redo | **`phaze-hia9z`** |
| **SAQ latency + depth** | **11–24 ms**, depth **9**, **318.3 jobs/s** drain, **0.03%** utilised | **no bead — result recorded (§5c)** |
| **Analysis staging path** | **1.694 s of 7,121.167 s = 0.02%** | **no bead — result recorded (§3a)** |
| **Analysis memory** | sampled **823–1,364 MiB** against a 4Gi limit | **no bead — result recorded (§3d)** |
| **The record slide-in** | **20.6 ms** against the real corpus | **no bead — result recorded (§4)** |
| `/s/files` payload size | **681,013 bytes**, 6.7× the next largest page, but **50.5 ms** to render | **no bead — no measured client-side cost** |
| Review surfaces under load | **not measured** — `proposals` = 0 rows | **gap recorded (§1a), no bead** |
| Solo (`W=1`) analysis ratio | **not measured** — would require a production mutation | **gap recorded (§1a), no bead** |

______________________________________________________________________

## 8. The general form (CLAUDE.md rule 5)

The lesson `phaze-o8sie.9` recorded about `io_in_loop` generalises, and this document is the
evidence for the generalisation:

> **A static detector keyed on a syntactic shape ranks by how often the shape appears, which is
> uncorrelated with cost.** `/pipeline/stats` is the most expensive repeating operation in the
> admin UI — 384.5 s of handler time per wall hour — and it contains **no await in any loop**, so
> it never appeared in the 210 ranked opportunities at all. Meanwhile the detector's top-ranked
> finding was a `begin_nested()` savepoint written deliberately for per-file failure isolation.
> The detector was not merely exhausted; on this codebase it was **anti-correlated** with cost.

The replacement is not a better detector. It is that **a performance claim names the surface, the
population it was measured on, and the number** — which is what the five acceptance rules in
CLAUDE.md already require of every other kind of claim in this repository.
