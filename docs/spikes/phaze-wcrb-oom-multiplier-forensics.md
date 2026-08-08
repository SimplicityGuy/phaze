# phaze-wcrb — the "2–4× co-resident copies" signature is a censoring artifact

- **Bead:** `phaze-wcrb` (opened by [`phaze-7i0k`](phaze-7i0k-linux-memory-measurement.md) §6d)
- **Date:** 2026-08-06
- **Tree:** branch `wt/bead/issue/phaze-wcrb`, forked off `main` at `75020e8`
- **Status:** forensics + measurement only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**There are no 2–4× copies. There never were.** Every one of the 19 recoverable production OOM
kills carries a full kernel task table, and every table shows the *same* shape: **one** pod
running **one** `analysis_child`, plus two neighbouring analyze pods running perfectly ordinary
analyses at **6.39–7.81 GiB** — exactly the working set `phaze-7i0k` measured. The victim's
`anon-rss` is not a multiple of anything; it is the **residual**. Summed over every task on the
node, all 22 dumps report **30.76–30.82 GiB** — a 0.2% spread across 12 days, three boots and
four separate jobs. That sum is node capacity, and the OOM killer only fires when it is reached,
so the victim's size at kill time is mechanically `30.80 GiB − Σ(everything else)`. With 0, 1 or
2 healthy neighbours at ~7.7 GiB each, that residual lands at ~30.2, ~23.3 or ~15.5 GiB — the
"4× / 3× / 2× clusters". **The quantum is the neighbour's working set, subtracted, not the
victim's, multiplied**; identical arithmetic, opposite sign, which is precisely why it read as
multiplicative. The **hard floor at 15.27 GiB** falls out of the same identity: Kueue admits at
most 3 analyze pods, so at most two neighbours can be subtracted, so the smallest observable
runaway is `30.80 − 2 × 7.81 = 15.18 GiB` (observed floor: 15.27). Nothing lands between 8 and
15.27 GiB **because a node holding two healthy analyses and an 11 GiB runaway is not out of
memory — nobody dies, and no record is written.** The distribution is left-censored by node
capacity, not quantised by copies. Process duplication is refuted by direct observation: the task
tables list every process on the node and there is exactly **one** `analysis_child` per pod, never
two. The `attempts` relationship is established but is not the multiplier: the 19 kill records
come from **4 distinct analyze Jobs** — four files — retried 4, 4, 4 and **8** times, so `attempts`
explains the record *count*, not the record *magnitude*. What is left, and left honestly open, is a
much smaller and better-posed question than the one this bead inherited: **why does a single
`analysis_child` grow past ~8 GiB at all, on four files out of ~520**, monotonically, for 27–150
minutes, until the node it is on runs out. This spike narrows that with three new rule-outs and
hands it on.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Forensic source** | `vox`'s own kernel journal, all three relevant boots (`-2`, `-1`, `0`), 2026-07-24 → 2026-08-04. Read-only; nothing on vox was deleted or modified. |
| **What was mined** | not the `Out of memory: Killed process` one-liners `phaze-7i0k` used, but the **`Tasks state (memory values in pages)` table** the kernel dumps immediately before each kill — every process on the node, with `rss_anon`, `total_vm`, `pgtables_bytes`, `swapents` and `oom_score_adj`. 22 such tables survive. |
| **Pod identity** | the `task_memcg=/kubepods/burstable/pod<UID>/…` field on each `oom-kill:` line joined against `k0scontroller`'s kubelet/containerd log to recover the **Job name** (`phaze-analyze-<cloud_job_id>-<suffix>`) and the pod's first appearance. |
| **Live probe** | the deployed job image `ghcr.io/simplicityguy/phaze/job:2026.8.0` in a bare pod on vox, **`resources.limits.memory: 12Gi`**, no Kueue queue label (consumes no quota), the deployed `phaze-models` PVC read-only. Synthetic ffmpeg sine-pair audio only. |
| **RSS sampling** | host-side, from outside the process, per `phaze-7i0k` §9. |

**vox was not returned to the phaze backend registry, and no k0s / JuiceFS / gateway config was
touched.** The cluster was idle before and after; the probe pod's 12Gi limit means a runaway
inside it is cgroup-OOMKilled rather than allowed to reach the node.

**No operator media was read, copied, referenced or identified.** Pod names carry `cloud_job`
UUIDs, not filenames. The one aggregate query run against the control-plane database returned
duration/size/bitrate histograms with no path, title or artist column selected.

______________________________________________________________________

## 2. The arithmetic — every kill, victim against neighbours

Reconstructed from the 22 surviving task tables. `min` is kill time minus the pod's first
appearance in the journal. `SUM` is the anonymous RSS of **every** task on the node, not just
these three.

| kill time (PDT) | job | pod start | min | **victim** | neighbours | **SUM** |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 2026-07-24 23:41:38 | `713a368e` | 07-24 23:05 | 36.1 | **23.17** | 7.15 | **30.78** |
| 2026-07-25 02:15:04 | `713a368e` | 07-24 23:45 | 150.0 | **30.15** | — | **30.79** |
| 2026-07-25 03:39:02 | `713a368e` | 07-25 02:25 | 74.0 | **30.38** | — | **30.79** |
| 2026-07-25 05:09:21 | `713a368e` | 07-25 03:45 | 84.3 | **30.41** | — | **30.79** |
| 2026-07-29 03:00:41 | `713a368e` | 07-29 02:30 | 30.0 | **16.49** | 6.99 · 6.93 | **30.79** |
| 2026-07-29 04:06:25 | `713a368e` | 07-29 03:05 | 61.4 | **16.63** | 7.51 · 6.39 | **30.82** |
| 2026-07-29 05:12:03 | `713a368e` | 07-29 04:10 | 62.0 | **23.43** | 6.95 | **30.82** |
| 2026-07-29 06:49:16 | `713a368e` | 07-29 05:15 | 94.2 | **30.18** | — | **30.80** |
| 2026-08-03 03:03:17 | `2d2d618c` | 08-03 02:32 | 30.9 | **16.18** | 7.46 · 6.81 | **30.76** |
| 2026-08-03 03:35:59 | `2d2d618c` | 08-03 03:05 | 30.9 | **15.70** | 7.81 · 6.98 | **30.80** |
| 2026-08-03 04:12:46 | `2d2d618c` | 08-03 03:40 | 32.7 | **18.47** | 7.27 · 4.76 | **30.79** |
| 2026-08-03 04:52:38 | `2d2d618c` | 08-03 04:15 | 37.6 | **21.22** | 6.59 · 2.69 | **30.79** |
| 2026-08-03 12:15:44 | `334fa133` | 08-03 11:38 | 37.7 | **20.87** | 7.52 · 2.12 | **30.81** |
| 2026-08-03 13:03:54 | `334fa133` | 08-03 12:20 | 43.9 | **17.11** | 7.35 · 6.07 | **30.81** |
| 2026-08-03 13:45:30 | `334fa133` | 08-03 13:05 | 40.5 | **16.20** | 7.21 · 7.12 | **30.81** |
| 2026-08-03 14:34:17 | `334fa133` | 08-03 13:50 | 44.2 | **15.27** | 7.70 · 7.56 | **30.81** |
| 2026-08-04 08:49:31 | `3fa28d6d` | 08-04 08:22 | 26.7 | **16.71** | 7.80 · 5.99 | **30.82** |
| 2026-08-04 10:04:50 | `3fa28d6d` | 08-04 08:55 | 69.8 | **15.65** | 7.49 · 7.46 | **30.82** |
| 2026-08-04 11:45:37 | `3fa28d6d` | 08-04 11:10 | 35.6 | **23.36** | 6.93 | **30.78** |

(GiB. The 2026-08-04 10:04 row is one of four consecutive dumps two seconds apart — the kernel
also took `local-path-provisioner`, `metrics-server` and `coredns` in that cascade; all four
tables agree on the three python3 sizes to 0.03 GiB.)

**Read the last column first.** `SUM` is 30.76–30.82 GiB in 22 independent dumps spanning three
boots, twelve days and four jobs — a **0.06 GiB (0.2%) spread**. That is not a property of the
workload; it is the node. vox has 31.2 GiB allocatable, and a global OOM (`constraint=
CONSTRAINT_NONE`, on every record) is *by definition* the moment the sum reaches it.

Therefore the victim's `anon-rss` is not an independent quantity at all:

```
victim  =  30.80 GiB  −  Σ(everything else on the node)
```

and every apparent cluster is just an integer count of neighbours:

| neighbours | predicted victim | observed |
| --- | ---: | --- |
| 0 | 30.80 − 0.4 (system) ≈ **30.4** | 30.15 30.18 30.38 30.41 |
| 1 × ~7.0 | ≈ **23.4** | 23.17 23.36 23.43 |
| 2 × ~7.5 | ≈ **15.4** | 15.27 15.65 15.70 16.18 16.20 16.49 16.63 16.71 |

and the "handful of intermediates" that fitted nothing — 17.11, 18.47, 20.87, 21.22 — are exactly
the four events where a **third pod had only just started and had not yet reached its working
set** (2.12, 2.69, 4.76, 6.07 GiB). Every one of them closes to 30.8:

```
18.47 + 7.27 + 4.76 = 30.50      21.22 + 6.59 + 2.69 = 30.50
20.87 + 7.52 + 2.12 = 30.51      17.11 + 7.35 + 6.07 = 30.53
```

**A one-parameter model — "the sum is the node" — reproduces all nineteen values, including the
ones that previously had to be waved through as noise.** No multiplier survives contact with the
task tables.

______________________________________________________________________

## 3. The hard floor at 15.27 GiB — explained, and it is not about the victim

This was the load-bearing puzzle: a mechanism that produces "sometimes big" but not "never
between 8 and 15.27" was declared not to be the answer. The censoring model produces the floor
*exactly*, and for a reason that has nothing to do with how the runaway grows.

1. **Kueue admits at most 3 analyze pods** (24Gi quota ÷ 8Gi request — `phaze-7i0k` §7a). The
   task tables confirm it directly: **no dump anywhere contains four large `python3` tasks.**
   One of the three is the victim, so **at most two** neighbours can be subtracted.
1. The largest healthy neighbour observed across all 22 dumps is **7.814 GiB**, and the
   distribution of the 33 healthy-neighbour observations is **2.12–7.81 GiB, with 29 of 33 at
   ≥ 6.0 GiB** — an independent, *in-situ, production* confirmation of `phaze-7i0k`'s
   5.9–8.0 GiB envelope, measured at the moment of the kill rather than in a harness.
1. Therefore the minimum observable runaway is

   ```
   30.80  −  2 × 7.81  =  15.18 GiB          (observed floor: 15.27)
   ```

**And the gap is not empty because nothing lands there — it is empty because nothing there is
ever recorded.** A runaway at 11 GiB alongside two healthy 7.7 GiB analyses totals 26.4 GiB on a
31.2 GiB node. The node is fine. The OOM killer does not fire. No journal entry exists. The same
process keeps growing and is only *observed* once it has crossed the residual threshold — at
which point it is, necessarily, ≥ 15.2 GiB.

The distribution is **left-truncated at node capacity minus the maximum co-resident load**. Asking
why nothing lands between 8 and 15.27 GiB is asking why no OOM record exists for a node that was
not out of memory.

______________________________________________________________________

## 4. Process duplication — refuted by direct observation

The bead's strongest hypothesis was a `fork()` or an overlapping retry putting two copies of the
model set in one process tree. The task tables settle it: they enumerate **every task on the
node**, so a duplicate cannot hide. Here is the complete `python3`/`uv`/`pause` census at the
15.27 GiB floor event (2026-08-03 14:34:17), verbatim from the dump:

```
pid= 472613 anon= 0.000 GiB adj= -998  pause      ┐ pod A  (healthy)
pid= 472640 anon= 0.020 GiB adj=  873  uv         │
pid= 472687 anon= 0.042 GiB adj=  873  python3    │  ← job_runner (parent)
pid= 472690 anon= 7.700 GiB adj=  873  python3    ┘  ← analysis_child
pid= 481143 anon= 0.000 GiB adj= -998  pause      ┐ pod B  (healthy)
pid= 481168 anon= 0.014 GiB adj=  873  uv         │
pid= 481217 anon= 0.041 GiB adj=  873  python3    │  ← job_runner (parent)
pid= 481220 anon= 7.562 GiB adj=  873  python3    ┘  ← analysis_child
pid= 484409 anon= 0.014 GiB adj=  873  uv         ┐ pod C  (VICTIM)
pid= 484457 anon= 0.041 GiB adj=  873  python3    │  ← job_runner (parent)
pid= 484483 anon=15.266 GiB adj=  873  python3    ┘  ← analysis_child
```

- **Exactly one `analysis_child` per pod, in all 22 dumps.** Never two, never a fork, never a
  predecessor still resident. `analysis_exec.run_analysis_subprocess` reaps its child on timeout,
  cancellation and every other exceptional exit before propagating (`_kill_and_reap`), and the
  process tables show that contract holding in production.
- **The parent is 41–44 MB.** The `uv` shim is 14–17 MB. The pod's whole non-child footprint is
  under 60 MB. Nothing is co-resident with the child at model scale.
- **A retry cannot overlap its predecessor**, because a retry is a *new pod*: each victim sits in
  its own `pod<UID>` cgroup with its own `pause`, and consecutive attempts of the same Job appear
  in the journal strictly sequentially (§5). There is no window in which two attempts of one Job
  are resident.
- **`anon-rss` double-counting was checked and does not apply.** Had a `fork()` happened, parent
  and child would each report the shared anonymous pages; no pair of tasks in any dump has
  matching large `rss_anon`.

Additionally, and against the copy-on-write reading specifically: `pgtables_bytes ÷ rss` is
**8.08–8.16 bytes/page across all 19 victims and 8.38–9.36 across all 33 neighbour
observations** — full 4 KiB PTE coverage on both, i.e. one mapping of each page in one address
space, with no second page-table tree holding a duplicate. (Neighbours run slightly higher only
because the ratio's fixed upper-level cost is amortised over a smaller RSS: the four values above
8.6 are all pods still ramping at 2.1–4.8 GiB.)

**Process duplication is ruled out with direct evidence, as the acceptance criterion required.**

______________________________________________________________________

## 5. The `attempts` relationship — established, and it is orthogonal to the multiplier

Joining each victim pod's cgroup UID to its Job name collapses the population dramatically:

| Job (`cloud_job` id) | pods created | OOM kills | window |
| --- | ---: | ---: | --- |
| `713a368e-…` | **8** | 7 | 07-24 23:05 → 07-29 06:49 |
| `2d2d618c-…` | 4 | 4 | 08-03 02:32 → 08-03 04:52 |
| `334fa133-…` | 4 | 4 | 08-03 11:38 → 08-03 14:34 |
| `3fa28d6d-…` | 4 | 3 | 08-04 08:22 → 08-04 11:45 |

**Nineteen kill records are four files.** The kills are not 19 independent events over 12 days;
they are four retry chains. Within a chain the pods run strictly one after another — the next pod
is created 4–40 minutes after the previous one died — which is why the process tables never show
two attempts of the same Job at once.

So the `attempts` relationship is:

- **`attempts` explains the record count, not the record magnitude.** The multiplier is
  `f(neighbour count)` (§2); the number of records is `f(retry chain length)`. That the two
  numbers were both "3-ish" is a coincidence of two unrelated small integers.
- **The multiplier's dependence on `attempts` is therefore explicitly ruled out**: within the
  `334fa133` chain the four successive attempts died at 20.87, 17.11, 16.20 and 15.27 GiB —
  *decreasing* with attempt number, tracking neighbour load, not attempt count.
- **A genuine defect surfaces here, though.** `cloud_submit_max_attempts` is 3 and
  `cloud_job.attempts` is capped at 3 in the live database (`max(attempts) = 3` over 7297 rows),
  yet `713a368e` produced **8** pods. `kube_staging.build_job_manifest` sets `backoffLimit: 0`
  and `restartPolicy: Never`, so one Job ⇒ one pod; eight pods under one deterministic Job name
  means the Job was deleted and re-created eight times. `reconcile_cloud_jobs._handle_no_callback_terminal`
  has a documented path that holds *without charging an attempt* (the phaze-32wz
  pending-vs-vanished branch, which clears `kueue_workload` and re-enters the phantom-row branch).
  A pod whose node dies underneath it looks exactly like that. **A file that reliably takes the
  node down can therefore consume more than its retry budget** — and did, 8 times over 5 days,
  each time crashing the node. Scoped as a follow-up (§10).

______________________________________________________________________

## 6. What the runaway is — the residual question, restated correctly

The question this bead inherited was *"what puts 2–4 copies of the working set on the node?"*.
The task tables replace it with a sharper and much smaller one:

> **A single `analysis_child` process, on 4 files out of ~520 analyzed on this node, grows
> monotonically past the ~7.7 GiB every other file pays, and keeps growing for 27–150 minutes
> until the node runs out. What is it allocating?**

What the forensics do and do not fix about that process:

- **It is one process, not several.** (§4)
- **It reaches at least 30.41 GiB** — the largest kill with an empty node, i.e. it was still
  growing when it hit the ceiling. There is no evidence of an upper bound.
- **The 27–150 minute figure is elapsed time to node exhaustion, not a growth rate.** Only the
  kill-time size is observed, so the *shape* (linear ramp vs. a late step) is not determined by
  the journal. Naive division gives 0.20–0.63 GiB/min; that is an average, and its spread tracks
  CPU contention from the neighbours, which is what one would expect either way.
- **It is file-specific and deterministic.** Four files, every attempt, across three kernel
  builds (6.12.95 / .96 / .100) and two image tags. ~500 other analyze Jobs on the same node ran
  once and finished.

______________________________________________________________________

## 7. New rule-outs, with evidence

### 7a. Long-file decode is **not** it — decode is O(window) in memory, O(file) in wall clock

The most attractive remaining candidate was `es.EasyLoader(filename=…, startTime=…, endTime=…)`:
essentia's loaders are famously non-seeking, `phaze-7i0k` §8 attributes the pipeline's wall clock
to exactly that, and the cloud lane only ever sees long audio (≥90 min; a 2–6 h concert set is a
routine input — `config_backends.py`). If a per-window load materialised the whole file, a 12-hour
stereo source would cost 7.1 GiB mono at 44.1 kHz before any model ran.

Measured, in the deployed image, on the deployed node:

| file | rate | window | samples returned | RSS delta | wall |
| --- | ---: | ---: | ---: | ---: | ---: |
| `dur_600` (10 min) | 44 100 | 0–30 s | 1 323 000 | 0.018 GiB | 0.8 s |
| `dur_7200` (2 h) | 44 100 | 0–30 s | **1 323 000** | 0.029 GiB | **9.0 s** |
| `dur_600` (10 min) | 16 000 | 0–180 s | 2 880 000 | 0.012 GiB | 10.2 s |
| `dur_7200` (2 h) | 16 000 | 0–180 s | **2 880 000** | 0.043 GiB | **139.0 s** |

**A 12× increase in file duration changes the returned buffer by zero samples and the resident
set by tens of MB, while multiplying wall clock by 11–14×.** essentia's standard-mode `EasyLoader`
runs its internal streaming network and accumulates only the trimmed output; the whole file passes
through the decoder but never through the heap. Memory per window is a function of window
*length*, exactly as `phaze-7i0k` §2b assumed. **Duration-proportional decode is ruled out.**

(The wall-clock half is worth keeping: at 139 s per 180 s coarse window for a 2-hour file, a
12-hour file costs ~14 minutes *per coarse window*. That is a throughput finding, not a memory
one, and it explains why the victims died only 27–150 minutes in — they were still early in a run
that would have taken many hours.)

### 7b. A 6-hour file at **production caps** is normal — the gap `phaze-7i0k` left open

`phaze-7i0k` §2a ran a 12-hour file, but at `fine_cap=2 / coarse_cap=2` (to keep the wall clock
tractable); its only **production-caps** runs were 3.3 min, 10 min and 60 min. Since the cloud
lane sees nothing under 90 minutes, no long file had ever been run at the caps production
actually uses. That gap is now closed.

**Setup:** synthetic ffmpeg sine-pair stereo mp3, **21 600 s (6 h)**, 192 kbps; deployed image
`job:2026.8.0` (whose `analysis.py` is the pre-`phaze-15sw` **window-major** code — i.e. the exact
code that produced the kills); deployed models PVC read-only; **no cap flags**, so
`analyze_file`'s production defaults (`fine_cap=60`, `coarse_cap=30`) apply; pod
`limits.memory: 12Gi`; 939 host-side samples at 5 s.

| phase | window | RSS | VmHWM |
| --- | --- | --- | --- |
| **FINE, all 60 windows** | t = 0 → ~2050 s | **0.231–0.257 GiB, flat** | **0.256 GiB** |
| COARSE, graph residency builds | t ≈ 2160 s | 2.568 | 3.769 |
| COARSE | t = 2342 → 3604 s | 4.890 → 5.589 | 5.796 → 6.988 |
| COARSE | t = 4145 → **4696 s** | 5.853 → 6.074 | 7.275 → **7.470** |

Two things follow.

- **The FINE pass accumulates nothing.** Sixty full decode + `RhythmExtractor2013` +
  `KeyExtractor` cycles over a 6-hour source, 34 minutes of wall clock, and the resident set does
  not move off **0.25 GiB** — a range of 0.026 GiB across 410 samples. There is no per-window leak
  in the tier that runs first and therefore no candidate for a slow ramp during the first half
  hour, which is where most victims died.
- **The COARSE pass is textbook `phaze-7i0k`.** The high-water climbs asymptotically toward the
  known envelope (7.470 GiB at 78 minutes, still rising slowly) with the same sawtooth — maximum
  drawdown **1.464 GiB**, against 7i0k's 1.485 GiB and production's 1.20/1.44/1.47.

**At 78 minutes elapsed — longer than 14 of the 19 victims survived — a 6-hour file at production
caps sits inside the ordinary 5.9–8.0 GiB envelope.** Duration up to 6 h, at production caps, on
the production code, does not reproduce the fault. Added to the ruled-out list.

### 7c. Duration alone does not select the population

Aggregate query against the control plane (durations and outcomes only; no path, title or artist
column selected):

| duration bucket | files | analysis completed | analysis failed | no analysis row |
| --- | ---: | ---: | ---: | ---: |
| < 90 min | 9611 | 2673 | 108 | 6128 |
| 90–150 min | 1425 | 109 | 89 | 1031 |
| 150–240 min | 333 | 17 | 14 | 268 |
| 4–8 h | 39 | 4 | 2 | 24 |
| > 8 h | 4 | **1** | 0 | 3 |

There are only four files over 8 hours in the whole corpus — a striking numerical coincidence with
four runaway Jobs — **but one of them completed successfully**, and 39 files in the 4–8 h bucket
(4 completed) did not produce this failure. Duration is at most a *correlate*; it is not the
selector. The `cloud_job` rows for the four Jobs have since been reaped, so the four runaway files
cannot be tied to specific rows, and this spike deliberately did not go looking for them by any
other route.

### 7d. Confirmations of `phaze-7i0k`, from production rather than a harness

- **The ~8 GiB working set reproduces in situ.** 33 healthy `analysis_child` observations
  captured mid-run inside the OOM dumps span **2.12–7.81 GiB**, 29 of them at ≥ 6.0 GiB — the
  same envelope the harness measured (5.9–8.0), from a completely independent instrument.
- **`swapents` is 0 for every victim and every neighbour** (node total 0.73–1.32 GiB, all of it
  system daemons). Nothing was being pushed to swap; `anon-rss` is the whole story.
- **Kernel version is not a discriminator**, and the censoring identity survives it: the three
  boots run **6.12.95, 6.12.96 and 6.12.100** respectively, and `SUM` is 30.78–30.79, 30.76–30.82
  and 30.78 GiB on them. `phaze-7i0k`'s point stands — the 23.36 GiB kill of 2026-08-04 11:45 is
  on 6.12.100, the measurement kernel — and the two older builds behave identically.

______________________________________________________________________

## 8. What this changes operationally

### 8a. The `12Gi` limit is still correctly sized, but for a different reason

`phaze-7i0k` §7a chose 12Gi as "above anything measured, below the 15.27 GiB floor of the
pathological population". That reasoning is now **void** — the floor is a property of vox's RAM,
not of the fault, and on a bigger node the same runaway would first be *recorded* at a larger
number. The limit is nonetheless right, and for a stronger reason: the fault is a single process
in **unbounded** growth, so *any* limit above the working set converts it into a pod-scoped kill.
The correct sizing rule is "comfortably above the normal peak", not "below the observed floor".

Post-`phaze-15sw` the normal peak is **2.482 GiB**, so the number should be re-derived downward
(already tracked as `phaze-7qfd`). A limit near the working set is strictly better here than a
loose one: it kills the runaway in **minutes instead of 45**, and it does so without taking
coredns, metrics-server and local-path-provisioner with it — which is what actually happened on
2026-08-04 10:04.

### 8b. The population is pathological, not a legitimate mode

The acceptance criterion asked this explicitly. It is pathological: four files out of ~520, each
consuming its whole retry chain, each ending in a node-wide OOM, versus ~500 single-pod jobs that
completed. Sizing the limit to *catch* rather than *accommodate* it is correct.

### 8c. Enabling the limit makes the next occurrence self-diagnosing

Today the evidence is a kernel task table, because the pod dies with the node. With
`memory_limit` set, the same file produces a `Reason: OOMKilled` container status on a pod that is
still identifiable, with its `cloud_job` row still live and its logs still on disk in
`/var/log/pods` — i.e. the exact per-file identification this spike could not recover. That is the
cheapest path to closing §6.

______________________________________________________________________

## 9. What this measurement does and does not support

- **Supported:** the composition of every OOM event (victim + neighbours + node total), the
  invariance of the node total, the derivation of the floor, the absence of any duplicate
  `analysis_child`, the mapping of 19 kills to 4 Jobs, the O(window)/O(file) split in
  `EasyLoader`, and the flat FINE / ordinary COARSE profile of a 6-hour file at production caps.
- **Not supported:** any claim about *what* the runaway allocates. §6 states the question and
  stops there. In particular the growth **shape** is undetermined — the journal records only the
  size at kill time, so a linear leak and a late step are equally consistent with the data.
- **Not supported:** any per-file causal claim. The correlation with very long duration (§7c) is
  suggestive and contradicted by at least one completed >8 h file.
- **Preserved judgement:** `phaze-7i0k` §6d refused to fit a story to the data. This spike
  removes the framing that made a story feel necessary, and declines to supply a new one for the
  part that is still unknown.

______________________________________________________________________

## 10. Follow-ups

Proposed for filing by the planner (this seat files no beads). None of them is the duplication fix
the bead anticipated, because there is no duplication.

| | action | why |
| --- | --- | --- |
| 1 | **Identify what the runaway allocates**, using an enabled `memory_limit` to convert the next occurrence into a pod-scoped OOMKill with recoverable pod logs, `cloud_job` row and file identity | §6, §8c. This is the whole remaining question, and it is now a per-pod problem rather than a node-forensics one. |
| 2 | **Bound re-drives of a Job whose pod dies with its node** | §5. `cloud_submit_max_attempts` is 3, `cloud_job.attempts` caps at 3, and `713a368e` still produced 8 pods over 5 days, crashing the node each time. The pending-vs-vanished branch does not charge an attempt, and a node-OOM death is indistinguishable from it. |
| 3 | **Re-derive `memory_limit` from the post-`phaze-15sw` 2.482 GiB peak** | §8a; already tracked as `phaze-7qfd`. Note that the "below 15.27 GiB" constraint that shaped the interim number is void. |
| 4 | **Delete the "2–4× co-resident copies" framing** from `phaze-7i0k` §6d and from any downstream planning | §2. It was a correct reading of an incomplete instrument, and it sent four spikes looking for a duplication that does not exist. |

______________________________________________________________________

## Appendix — reproducing this

**Forensics.** Everything in §2–§5 comes out of `journalctl -k` on vox and needs no code:

```sh
# the task tables (NOT just the one-line kill records)
sudo journalctl -k -b <N> --no-pager -o short-iso | grep -v 'UFW BLOCK'
#   -> blocks starting 'Tasks state (memory values in pages):'
#      row format: [ pid ] uid tgid total_vm rss rss_anon rss_file rss_shmem
#                  pgtables_bytes swapents oom_score_adj name
#      -- every memory column is a PAGE COUNT except pgtables_bytes, which is bytes
#   -> sum rss_anon over every row: it is the node, on every event.

# victim pod -> Job name
#   'oom-kill:...task_memcg=/kubepods/burstable/pod<UID>/...' , then
sudo journalctl -g '<UID>' --no-pager -o cat | grep -o 'phaze-analyze-[0-9a-f-]\{36\}-[a-z0-9]\{5\}'
```

**The loader probe (§7a).** A bare pod on the burst node, deployed image, models PVC read-only,
`limits.memory: 12Gi`, no Kueue queue label, plus the `phaze-7i0k` sine-pair generator at 600 and
7200 s. Then, per file and rate, one `es.EasyLoader(filename=…, sampleRate=R, startTime=0,
endTime=W)()` with `/proc/self/statm` read either side and `len(buf)` recorded. The discriminating
observation is that `len(buf)` is **identical** across a 12× duration change while wall clock is
not.

**Two things to get right when re-running.** The kernel task-table columns are **pages**, not kB —
`rss_anon × 4096` — and `pgtables_bytes` is already bytes, which makes the mixed-unit row easy to
misread. And sample instantaneous RSS from *outside* the process (`phaze-7i0k` §9).
