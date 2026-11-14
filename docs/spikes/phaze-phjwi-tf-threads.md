# phaze-phjwi: TensorFlow threads and worker concurrency per lane

Part of the spike molecule `phaze-0toy3`. Baseline: `docs/spikes/phaze-fzubr-analysis-cpu-profile.md`
(the 11 `musicnn_msd` mood graphs are 84.5% of analysis CPU, so thread behaviour of that sweep is the
behaviour of the analysis). This document carries **aggregates only**: the three archive files appear
as `set-02`, `set-05` and `set-06`; hosts are named by role.

## Question

Within each lane's real CPU budget, which TensorFlow intra-op / inter-op / OpenMP thread counts and
which worker concurrency give the most windows per wall-hour, and at what cost in windows per
cpu-hour? Do threads change the results (against the 1-thread control)? And which model-variant set
does every deployed lane run?

## Method

- **Workload.** The production model-major sweep (`_run_model_sets_over_windows`: 11 `musicnn_msd`
  graphs plus the genre EffNet, the deployed `msd` set) on **four full-length 180 s coarse windows**
  from three files: two from `set-02` (indices 3 and 7), one from `set-05` (16), one from `set-06` (30).
  The windows were decoded once with the production decode in three throwaway containers (archive
  `:ro`, `--network none`), saved as float32 arrays in a scratch directory and fed to every
  configuration, so no configuration touches the archive and every one sees identical samples.
- **Containers.** One at a time on the analysis host, the deployed image `2026.11.1`:
  `docker run --rm --network none --cpus <budget> --memory 8g --read-only`, models `:ro`, `nice -n 10`
  inside. The host's one-minute load before each run was **5.0 to 6.7** on 8 cores (production
  analysis runs there), so wall times carry that contention; the runs were sequential and the
  configurations interleave no order effect I could see, but no configuration was repeated.
- **Budget.** The cap is `--cpus 2` for the matrix and `--cpus 1` for two oversubscription arms. I did
  not run a 4-core arm: the host already carries 4 analysis workers and a load of about 5, and a 4-CPU
  container would have taken CPU from production. Larger budgets are therefore **projected** from the
  per-CPU efficiency measured here, and from the 4-physical-core arms already recorded in
  `src/phaze/services/analysis_sizing.py` (`phaze-rvcn`).
- **Concurrency.** For `conc = n`, `n` processes inside the one container each take every n-th of the
  four windows and all start on a shared timestamp after TF has loaded. Group wall time is the last
  process's end minus the first's start; cpu is the sum of every process's `process_time`.
- **Equivalence.** Every score of all 12 graphs for all 4 windows (about 30 values per window: every label of the 11 mood graphs and the genre top 10)
  against the `1/1/1`, concurrency-1 control run in the same session.
- **Lane configuration, read-only.** `docker inspect` of the four deployed local agent containers,
  filtered to analysis settings; the repo's compose files and `docs/k8s-burst.md` for the compute agent
  and the burst pods. The burst cluster's API is not reachable from the machine this ran on, so the
  burst side was **read live, read-only, by the dispatcher on 2026-11-13 and relayed to me** (the
  ConfigMap keys, the latest analyze Jobs' resources and the node's capacity); I did not read it
  myself, and the relayed facts are cited as such below.

## Evidence

### Which variant set every lane runs

| lane | evidence | variant set |
|---|---|---|
| local analyze (`phaze-agent-worker-analyze`, 8-core host) | environment: no `PHAZE_ANALYSIS_MODEL_VARIANTS`; `TF_NUM_INTRAOP_THREADS=1`, `TF_NUM_INTEROP_THREADS=1`, `OMP_NUM_THREADS=1`, `PHAZE_LANE_ANALYZE_CONCURRENCY=4`; no container CPU limit or cpuset | `msd`: **11 mood graphs + genre** (code default, `analysis_models.py`) |
| local meta / io / watcher | environment carries only `PHAZE_LANE_{META,IO}_CONCURRENCY` (2 and 4); no analysis settings; they do not run the analyze task | not applicable (no analysis) |
| cloud compute agent | `docker-compose.cloud-agent.yml`: `PHAZE_LANE_ANALYZE_CONCURRENCY=1`, no variants variable, no thread variables | `msd` by default |
| burst pods (the burst cluster) | read live, read-only (relayed by the dispatcher): ConfigMap `phaze-agent-env` has 8 keys (OTLP endpoint, agent API URL, agent kind, deployment environment, models dir, role, telemetry instance, CA file) and **no** thread, OMP, `TF_`, concurrency or `PHAZE_ANALYSIS_MODEL_VARIANTS` key; the latest analyze Jobs (image `2026.11.1`) request cpu 1500m and memory 3Gi, limit memory 4Gi only (**no CPU limit**), and carry no thread env; the node has 8 CPUs, all allocatable; 4 Jobs present, so up to 4 pods at once | `msd`, **verified** |

No file in the repo (and, on the evidence above, no deployed lane) sets `PHAZE_ANALYSIS_MODEL_VARIANTS` outside the code default, so a lane runs the
11-graph set unless its own environment says otherwise. The *stored* data is mixed (older windows hold
the 33-graph ensemble, per `phaze-8y6l8` and `phaze-fzubr`); that is history, not a lane setting.

### Settings matrix (4 windows, 2-CPU budget unless noted)

| configuration | wall s | cpu s | windows per wall-hour | windows per cpu-hour | cpu/wall | max abs diff vs control |
|---|---|---|---|---|---|---|
| conc 1, intra 1 / inter 1 / omp 1 (**deployed per worker; control**) | 419 | 419 | 34.3 | **34.4** | 1.00 | 0 |
| conc 1, intra 2 / inter 1 / omp 2 | 232 | 443 | 62.1 | 32.5 | 1.91 | 1.2e-7 |
| conc 1, intra 2 / inter 2 / omp 2 | 228 | 444 | 63.2 | 32.4 | 1.95 | 1.2e-7 |
| conc 1, intra 2 / inter 1 / omp 1 | 235 | 449 | 61.3 | 32.1 | 1.91 | 1.2e-7 |
| conc 1, intra 4 / inter 1 / omp 4 (2x oversubscribed) | 254 | 505 | 56.7 | 28.5 | 1.99 | 1.2e-7 |
| **conc 2**, intra 1 / inter 1 / omp 1 | 226 | 449 | **63.7** | 32.1 | 1.99 | 0 |
| conc 2, intra 2 / inter 1 / omp 2 (2x oversubscribed) | 245 | 490 | 58.8 | 29.4 | 2.00 | 1.2e-7 |
| conc 4, intra 1 / inter 1 / omp 1 (2x oversubscribed) | 248 | 496 | 58.0 | 29.0 | 2.00 | 0 |
| 1-CPU budget, conc 1, intra 1 (2 windows) | 209 | 209 | 34.4 | 34.4 | 1.00 | 0 |
| 1-CPU budget, conc 1, intra 2 / omp 2 (2x oversubscribed, 2 windows) | 222 | 222 | 32.4 | 32.4 | 1.00 | 1.2e-7 |

The control's cost per window is 104.8 cpu-s for the 12-graph sweep (419 s / 4), consistent with
`phaze-fzubr`'s 100.5 cpu-s (96.8 + 3.67) on that sample.

The two per-hour columns are derived: windows ÷ seconds × 3600. The conc-4 row's windows per cpu-hour
read 29.1 when first published, which the row's own cpu seconds do not support (4 / 496 × 3600 = 29.03;
even 496.5 s gives 29.00), so it is corrected to 29.0; the measured cpu seconds are unchanged. Every
other derived cell agrees with its row to rounding.

**Two ways to state CPU cost, not to be mixed.** "More cpu-s per window" is the ratio of cpu seconds,
`cpu_s / 419`; "fewer windows per cpu-hour" is `1 − (windows per cpu-hour) / 34.4`. They describe the
same runs but are different numbers: 449 s is **7.2% more cpu-s per window** (449 / 419 = 1.072) and
**6.7% fewer windows per cpu-hour** (1 − 32.1 / 34.4 = 0.067). The items below quote cpu-s per window
first, from the cpu s column, and give the per-cpu-hour fall alongside.

What the matrix says:

1. **Throughput per wall-hour is set by how many of the budget's CPUs are kept busy.** One thread in a
   2-CPU container leaves one CPU idle (34.3 windows per wall-hour); two threads in one worker (61.3 to
   63.2) and two single-thread workers (63.7) both fill it, at **1.79x to 1.84x** for the threads
   (61.3 / 34.3 to 63.2 / 34.3) and **1.86x** for the workers (63.7 / 34.3). That reproduces the 2-thread
   gain `phaze-8y6l8` saw.
2. **Per cpu-hour, one thread per worker is the most efficient setting**, always. Two intra-op threads
   cost **5.7% to 7.2% more cpu-s per window** (443 to 449 against 419 cpu s; equivalently 5.5% to 6.7%
   fewer windows per cpu-hour, 32.5 to 32.1 against 34.4); two workers cost **7.2%** more cpu-s per window
   (449 / 419; 6.7% fewer windows per cpu-hour), about the same; oversubscribing the budget costs
   **17% to 21% more cpu-s per window** (490 / 419 = 1.169 to 505 / 419 = 1.205; 14.5% to 17.2% fewer
   windows per cpu-hour, 29.4 to 28.5 against 34.4) whether by threads or by workers. Under a 1-CPU
   budget a second thread is a net loss on both axes.
3. **Threads and workers are interchangeable at a fixed budget.** `conc 2 x 1 thread` (63.7) and
   `conc 1 x 2 threads` (61.3 to 63.2) differ by 1% to 4% (63.7 / 63.2 = 1.008; 63.7 / 61.3 = 1.039),
   about the spread of the three 2-thread variants below (228 to 235 s, 235 / 228 = 1.031, about 3%). I
   did not repeat any configuration, so 3% is the only noise estimate available. Workers also need about 1 GiB each, threads share one process.
4. **Inter-op 2 and OMP 1 against 2 change nothing** at intra 2 (228, 232 and 235 s). `analysis_sizing.py`
   pins inter-op at 1; this agrees.
5. **Equivalence.** Maximum absolute difference against the control over all 12 graphs and 4 windows:
   **0 at 1 intra-op thread for any concurrency, 1.2e-7 for any run with 2 or more intra-op threads**
   (float summation order). No window failed in any configuration.

### What the matrix does not show

- No arm above 2 CPUs. At 4 physical cores `analysis_sizing.py` records a single process going from
  475.8 s at 1 thread to 174.4 s at 4 (2.7x), which fits item 1 (a lone worker uses idle cores well).
- No repeated runs; host load was 5.0 to 6.7 during every arm.
- The burst recommendation is carried from the 2-CPU arms to an 8-CPU node with CFS shares, not a hard
  quota; the 2x thread-to-CPU ratio is the same but the scheduler behaviour is not identical.
- Three files, four windows, one decoder format (mp3).

## Verdict

- **Threads buy wall-clock only where the budget has idle CPUs.** They never improve CPU efficiency
  (always 5.7% to 7.2% more cpu-s per window, 17% to 21% more when oversubscribed), and results are
  unchanged to 1.2e-7.
- **Where a lane's CPU budget is already filled by concurrent workers, more threads are a pure loss.**
  The deployed local analyze lane runs 4 workers at 1 thread each, which is the efficient setting;
  what it leaves on the table is idle cores, not thread tuning.
- A lone worker on a multi-core node should use threads equal to its cores (the existing derivation),
  and several workers sharing a node should use one thread each.

## Recommendation (per lane, with rollback)

All of these are environment changes. A thread value set in a container's environment is a **ceiling**
that the hot-reloaded runtime config can lower but not raise (`runtime_config.py`), so every raise below
needs an env change and an agent restart; a restart ends the in-flight children, so do it when the lane
is drained or accept the loss of the running analyses (nothing resumes mid-file).

1. **Local analyze lane (8-core host, 4 workers x 1 thread, no CPU cap).** Keep intra/inter/OMP at
   1/1/1. If the operator will give analysis more of the host, raise
   `PHAZE_LANE_ANALYZE_CONCURRENCY` (4 to 5 or 6), not the thread count: each added worker adds a full
   ~34 windows per cpu-hour at the efficient setting. Expected gain: **+25% to +50% windows per
   wall-hour** if the cores are free (**projected** from this matrix, not measured at 8 cores; the host
   showed a load of about 5 with 4 workers, so 6 workers would leave under 1 core for everything else,
   which is the operator's call). Memory is not the limit (62 GiB host, about 1 GiB per worker).
   Rollback: set the variable back to 4 and recreate the container.
2. **Cloud compute agent (concurrency 1).** No change: with one worker, the derived threads
   (`min(4, physical cores)`) fill the cores, which is the case this matrix shows paying about 1.8x (1.79x to 1.84x).
   Rollback: not applicable.
3. **Burst pods (the burst cluster: 8-CPU node, up to 4 pods, `cpu_request` 1500m, no CPU limit, no thread
   env).** Verified oversubscription. No thread variable reaches the pod, so each analysis child derives
   its own at import (`ANALYSIS_SIZING = apply_thread_env()` in `services/analysis.py`): intra-op
   `min(4, physical cores)` = **4** (OpenMP 4, inter-op 1), not TensorFlow's raw default of 8, because the
   derivation stamps the variables inside the process and so does not show in the Job's env. The
   derivation sees no CPU quota (there is no limit), so it ignores that four pods share the node. Four
   busy pods then run 16 threads on 8 CPUs (about 2x oversubscribed; 4x if the derivation were somehow
   bypassed and TF used 8). Fair share is 2 CPUs per pod, which is exactly this matrix's 2-CPU budget:
   there, 4 intra-op threads gave 56.7 windows per wall-hour and 28.5 per cpu-hour, against 61.3 to 63.2 and
   32.1 to 32.5 for 2 threads (62.1 and 32.5 for the intra 2 / OMP 2 row recommended here). **Recommendation: pin 2 intra-op threads (and OMP 2) per pod, by adding
   `TF_NUM_INTRAOP_THREADS=2` and `OMP_NUM_THREADS=2` to the `phaze-agent-env` ConfigMap.** It is
   an operator change with no code, it reaches new pods only, and it is the setting the matrix supports
   (1 thread would leave half of each pod's fair share idle, about 45% less throughput per pod). A CPU
   limit of 2 would reach the same thread count automatically (the derivation reads the cgroup quota)
   but needs a new `cpu_limit` backend key (none exists today) and forbids a pod using idle CPUs when
   fewer than 4 run, so it is the second choice. **Expected gain at 8 CPUs, extrapolated, not measured on
   this node:** **+10% to +14% windows per wall-hour** across the lane while all 4 pods are busy (the
   wall figure from the 2-CPU arm, the cpu-efficiency figure for a saturated node), plus less memory
   pressure from fewer threads. It pays only while the queue keeps 4 pods running (70k files wait now); with
   one pod alone, 4 threads would use idle CPUs better, so revisit if the cap is ever lowered. This
   contradicts the advice in `docs/k8s-burst.md` not to set thread variables in the ConfigMap; that advice
   was written for a different node and cap, and should be amended with the decision. To confirm before
   changing: a running pod logs `analyze thread sizing: ... intra_op=<n>` at start (it should say 4).
   Rollback: remove the two keys; the next pod falls back to the derivation (4 / 1 / 4).
4. **Nothing in these results argues for changing inter-op (keep 1) or OMP (keep equal to intra-op).**

## Cleanup

Containers ran with `--rm`; `docker ps -a` on the analysis host showed no `phjwi-*` containers after
the last run. The remote workdir (job files with the archive-derived windows, harness, outputs) and the
local scratch copies (selection, results, scripts) were removed after the figures above were taken
(commands and output are in the bead report).
