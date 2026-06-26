---
status: resolved
trigger: |
  Analyze (process_file) jobs on the phaze pipeline time out at the 4h limit en masse.
  Over ~57h on nox: 60 completions vs 72 timeouts. essentia windowed analysis of long
  DJ/concert sets routinely takes 2-4+ hours per file; the longest exceed the 14400s
  (4h) timeout, are killed, and retried. User wants a diagnosis + plan (NOT fixes applied
  yet) covering four questions (see Scope).
created: 2026-06-17
updated: 2026-06-26
mode: diagnose_and_plan_only
resolution: "Root cause (long-set analysis exceeds local capacity/timeout) is addressed by the v5.0 Cloud Burst milestone — long files (≥ threshold) are offloaded to a free OCI A1 arm64 compute agent over Tailscale instead of timing out locally (phases 47-51). Diagnosis spawned the throughput/UI/cloud phases (43-45) and the v5.0 cloud-burst design. Session closed during the v5.0 milestone audit; live verification of the offload is deployment-gated (homelab OCI A1 rollout)."
---

# Debug Session: analyze-4h-timeouts

## Symptoms (already established — do NOT re-gather from user)

- **Expected:** ANALYZE stage progresses steadily toward 11428; the dashboard "completed"
  counter (`COUNT(DISTINCT analysis.file_id)`) climbs.
- **Actual:** Counter crawls (60 → 78 over days). Most analyze jobs hit the 4h wall and
  produce nothing.
- **Timeline:** Surfaced 2026-06-16/17 while watching the pipeline dashboard. Tied to a
  batch queued 2026-06-13 19:25 of the staging archive (heavy on full DJ/techno livesets).
- **Reproduction:** Enqueue analysis over the long-set-heavy archive; watch SAQ UI + the
  ANALYZE card.

## Evidence already gathered (live, read-only, on nox)

SSH: `datum@nox`. Worker container: `phaze-agent-worker` (Up ~2 days). Control plane
(API + Postgres broker + Redis cache) is on **lux** (not yet queried — read-only access
to lux NOT yet authorized; ask before touching).

1. **Counter is correct, not buggy.** ANALYZE "done" = `COUNT(DISTINCT AnalysisResult.file_id)`
   computed fresh every 5s in `get_stage_progress` (`src/phaze/services/pipeline.py`). It
   moved 60→78, so it faithfully tracks committed analysis rows. A timed-out job writes no
   row (the PUT never runs), so the counter correctly does not advance.

2. **Worker is healthy, not hung.** `docker stats`: `phaze-agent-worker` at **483% CPU**
   (~5 cores), **28.6 GiB / 62 GiB** RAM. essentia is actively computing.

3. **Timeout ratio (since container start, ~57h, from `docker logs phaze-agent-worker`):**
   - Finished process_file: **60**
   - Timed out at 4h (CancelledError → TimeoutError): **72**
   - Retrying: **72**
   - Aborted / retries-exhausted: **0** (none have hit their 2nd timeout yet)
   - Distinct files timed out: **72** (each once so far — no compounding yet)
   - **More files time out (72) than complete (60).**

4. **Successful completion durations (started→completed):** 57, 84, 92, 109, 137, 138, 140,
   170, 186, 189, 191, **238** min. Successful jobs take **2–4h**; 238 min squeaked under
   the 240-min (4h) timeout. Anything needing >4h is killed.

5. **A confirmed timeout in logs:** job `process_file:72a2d398-...`, file
   `/data/staging/techno livesets 2004/Eran Saiag - Live at BPM FM (99esc) 08-30-DAB-2004-DF/01-....mp3`
   ran the full 14400s → `TimeoutError` → "Retrying". Traceback:
   `saq/worker.py:368 asyncio.wait_for(asyncio.shield(task), job.timeout)` → `TimeoutError`.

## Relevant code (already located)

- **Timeout/retries set at enqueue:** `src/phaze/services/analysis_enqueue.py:78` `timeout=14400`,
  `:83` `retries=2`. Deterministic key `process_file:<file_id>` (`process_file_job_key`).
- **Worker settings:** `src/phaze/tasks/agent_worker.py` — `worker_max_jobs=8` (SAQ concurrency),
  `after_process=increment_completed`. Pool: `src/phaze/tasks/pool.py` `ProcessPoolExecutor`,
  `worker_process_pool_size=4` (`src/phaze/config.py:225`). So 8 SAQ slots but only **4** can
  compute at once; 4 wait at `run_in_executor` while their 4h clock runs.
- **Task body:** `src/phaze/tasks/functions.py:114 process_file` → `run_in_process_pool` →
  `api.put_analysis(...)` (HTTP PUT to control plane writes the AnalysisResult row).
- **Completion counter hook:** `src/phaze/tasks/_shared/deterministic_key.py:118 increment_completed`
  (only bumps on `Status.COMPLETE`).
- **SAQ version:** `saq[postgres]>=0.26.4` (Postgres broker; Redis is cache-only since Phase 36).
- **Reaper:** `src/phaze/tasks/scan_reaper.py` exists (stall reaper, 24h per memory) — verify
  whether it touches analyze jobs.
- **File state enum:** `src/phaze/models/file.py FileState` has `ANALYZED`/`FAILED` but
  process_file does NOT set state on timeout (state set on API side after successful PUT).

## Scope — four questions to answer (deliver a written plan, apply NOTHING)

**Q1 — Extend timeout 14400s→86400s (4h→24h, 6x).** Evaluate. Pros/cons given durations top
out near 4h today. Where exactly to change (`analysis_enqueue.py:78`; check `pipeline_scans.py`
and `queue_defaults.py` for parity per the inline comments). Interaction with the 4-worker
process pool (a 24h job pins a pool slot for a full day). Risk: a genuinely-infinite/hung file
now wedges a slot for 24h instead of 4h. Recommend a value + any guardrail.

**Q2 — Distribute analysis across machines + cloud free tiers (ROUGH OUTLINE ONLY).** phaze
already has a distributed agent model (Phase 26+: control plane on lux, agent workers pull
per-agent SAQ queues over HTTP via `/api/internal/agent/*`, `agents-add` CLN, `PhazeAgentClient`).
So horizontal scale = "stand up more agents." Outline:
  - How a new agent registers + pulls work (reference the existing agent bootstrap/queue routing).
  - The catch: agents need the audio files. nox reads `/data/staging/...` locally; a cloud agent
    would need the file shipped to it (the payload carries `original_path`, not bytes — D-23
    "no read-back"). Flag this as the key design problem for remote agents.
  - **Cloud always-free tiers to research (verify current 2026 limits via WebSearch):**
    - **OCI Always Free:** up to 4x Ampere A1 (Arm) cores + 24 GB RAM — the most generous; BUT
      essentia-tensorflow ships **no linux/arm64 wheel** (CLAUDE.md: cp314 linux x86_64 only).
      So OCI A1 Arm is likely BLOCKED for essentia — call this out explicitly. OCI also has 2x
      small AMD x86 micro VMs (1/8 OCPU, 1 GB) — too weak for essentia.
    - **GCP Free:** 1x e2-micro/month (us regions), 0.25-1 vCPU — too weak; egress/credits caveats.
    - **AWS Free:** 12-month (not always-free) t2/t3.micro 1 vCPU — weak + expires.
    - **Azure Free:** 12-month B1S 1 vCPU + credits — weak + expires.
  - Honest verdict to include: free tiers are 1-vCPU micro instances that are **far slower than
    nox's 5 active cores** and (for the most generous, OCI Arm) blocked by the x86-only essentia
    wheel. Likely conclusion: free tiers won't meaningfully expedite this; the real levers are
    (a) bound the per-file analysis cost, (b) add a second real x86 agent (e.g. lux or a spare
    box). Still give the outline + a small comparison table; let the user decide.
  - Also research whether linux/arm64 essentia wheels now exist (would unlock OCI Arm) — verify,
    don't assume.

**Q3 — Identify the actual bugs + fix plan.** Confirm (with code, not assumption) which are
real:
  - (a) **ProcessPoolExecutor child not killed on timeout.** When `wait_for` cancels the awaiting
    coroutine, the `run_in_executor` future is abandoned but the child process keeps computing
    (concurrent.futures can't cancel a running child). Confirm via code + (if possible) evidence
    of CPU staying high after a timeout. Consequence: leaked compute, pool-slot starvation.
    Fix direction: run analysis in a killable subprocess with a hard kill on timeout, or recycle
    the pool worker (`max_tasks_per_child` / pool restart) — research options.
  - (b) **Timed-out/abandoned files are invisible** — no `FAILED` state, no UI surface, so the
    denominator can never reach 11428 and there's no straggler list. Fix direction: mark file
    FAILED (or a dedicated state) after retries exhausted; surface count on dashboard.
  - (c) **Retrying a deterministically-too-long file wastes another up-to-4h.** Same input →
    same timeout. Fix direction: don't blind-retry on TimeoutError, or route timed-out files to
    a bounded/sampled analysis path.
  - For each: confirm real / not, severity, and a fix outline (no code).

**Q4 — On worker restart, will the 72 timed-out files reprocess? If not, how to trigger it?**
Trace the actual SAQ-Postgres state machine + phaze's re-enqueue paths:
  - What status are the 72 jobs in now (active/queued/retrying/aborted)? They showed "Retrying"
    → likely re-queued in `saq_jobs`. Determine whether a worker restart re-picks queued/retrying
    jobs, and whether SAQ requeues jobs that were `active` when the worker died (sweep/`abort`/ttl).
    Note `ttl: 3600` seen on the job — explain its effect.
  - Check phaze's reboot re-enqueue resilience (Phase 32, `tasks/reenqueue.py`) and the stall
    reaper (`scan_reaper.py`) — do they re-enqueue analyze work on startup?
  - The deterministic key means a re-enqueue of the same file DEDUPS against any still-incomplete
    job. Explain whether that helps or blocks manual re-trigger.
  - Concrete answer: after restart, do the 72 resume automatically? If not, the exact operator
    action to requeue them (e.g. the dashboard "Run analysis" enqueue path / a CLI / which files
    qualify = music/video FileRecords with no AnalysisResult row). Read-only verification on nox
    is fine; do NOT mutate anything.

## Constraints
- Read-only on nox (`datum@nox`). NO destructive actions. lux not yet authorized — ask first.
- Apply NO code changes this session (diagnose + plan only).
- Repo rules: Python 3.14, `uv run` for everything, fixes (later) go via worktree + PR.

## Current Focus
- hypothesis: CONFIRMED. Root cause is genuine — long-set analysis runtime exceeds the 4h
  timeout; the three "bugs" are downstream amplifiers. Q1–Q4 answered with code evidence.
- next_action: deliver the written Root Cause Report + four-part plan at a checkpoint
  (goal=find_root_cause_only, plan_only — apply nothing).

## Evidence (appended this session — code + external verification)

- checked: analysis_enqueue.py:70-85 enqueue_process_file. found: timeout=14400 (:78),
  retries=2 (:83), key=process_file:<file_id> (:74); ttl NOT set here → falls to the
  before_enqueue default hook. implication: the ONLY process_file timeout site. Both
  producers (dashboard + recovery) funnel through this one helper.
- checked: routers/pipeline.py:221-280 (_enqueue_analysis_jobs + trigger_analysis) and
  reenqueue.py:162-184 (_reconcile_agent_stages). found: BOTH call enqueue_process_file;
  dashboard enqueues get_files_by_state(DISCOVERED). implication: a single edit to
  analysis_enqueue.py:78 changes the timeout for every process_file producer — no parity
  problem exists for process_file.
- checked: pipeline_scans.py:419-425. found: scan_directory uses timeout=0/retries=0 (a
  DIFFERENT job, unbounded bulk scan guarded by the stall reaper). implication: the
  "parity" the inline comment warns about is only a pattern echo; scan_directory's 0 must
  NOT be confused with process_file's 14400. No shared constant to keep in sync.
- checked: queue_defaults.py:62-86 apply_project_job_defaults. found: only overrides
  timeout/retries/ttl when still at the SAQ default (10/1/600). 14400≠10 and 2≠1 → both
  honored untouched; ttl default 600→worker_keep_result. implication: the ttl:3600 seen on
  the job = worker_keep_result (config.py:227), the keep-result retention, NOT a queue-life.
- checked: config.py:222-227. found: worker_max_jobs=8, worker_process_pool_size=4,
  worker_keep_result=3600. implication: 8 SAQ coroutine slots, 4 real compute slots; 4 jobs
  can sit awaiting an executor seat while their 4h wall-clock already ticks.
- checked: pool.py:16-23 run_in_process_pool. found: loop.run_in_executor(process_pool,...).
  implication: on wait_for timeout SAQ cancels the awaiting coroutine, but concurrent.futures
  cannot cancel an already-started child → child keeps computing (Q3a CONFIRMED). Live
  corroboration: worker at 483% CPU / 28.6 GiB.
- checked: functions.py:114-154 process_file. found: sets NO FileState; only PUTs on success.
  implication: a timeout/abort leaves the file in DISCOVERED forever, never FAILED (Q3b
  CONFIRMED). Memory: 28.6 GiB / 4 slots ≈ 7 GiB per essentia analysis.
- checked: reenqueue.py:11-38 + 187-242 (durability reframe + recover_orphaned_work) and
  pipeline.py:695-717 count_inflight_jobs. found: Phase-36 Postgres broker makes queued/active
  saq_jobs DURABLE across restart; SAQ re-dequeues them; recovery is GATED on
  count_inflight_jobs==0 (no-op unless the broker is wiped). implication (Q4): the 72 retrying
  jobs auto-resume on restart; recovery does nothing because they are still in saq_jobs.
- checked: scan_reaper.py:38-78. found: touches ONLY ScanBatch status==RUNNING. implication:
  does NOT re-enqueue or reap analyze jobs (Q4).
- checked: WebSearch essentia-tensorflow aarch64. found: NO linux/arm64 wheel (x86-64 manylinux
  + macOS arm64 only). implication (Q2): OCI Ampere A1 Arm is BLOCKED for essentia.
- checked: WebSearch OCI Always Free 2026. found: as of 2026-06-15 the Ampere A1 free allowance
  was HALVED to 2 OCPU / 12 GB (was 4/24); AMD micro is 1/8 OCPU / 1 GB. GCP e2-micro=2 shared
  vCPU(1/8 core)/1 GB; AWS t2.micro=1 vCPU/1 GB (12-mo); Azure B1s=1 vCPU/1 GB (12-mo).
  implication (Q2): every free tier has 1 GB RAM — disqualified outright (essentia needs ~7 GB
  per file here), independent of CPU and the arm64 wheel block.

## Resolution

root_cause: Long full-set (DJ/concert) essentia windowed analysis legitimately runs longer
  than the hardcoded 14400s (4h) wall-clock timeout at analysis_enqueue.py:78. Successful
  completions already cluster at 2–4h (max observed 238 min, 2 min under the wall); files
  needing >4h are killed by SAQ's asyncio.wait_for and retried. The timeout is the root cause;
  three code-confirmed amplifiers make it worse: (a) the ProcessPoolExecutor child is not killed
  on timeout (pool.py) → leaked compute + slot starvation; (b) timed-out files never leave
  DISCOVERED (functions.py sets no FAILED state) → invisible stragglers, denominator never
  reaches 11428; (c) retries=2 blind-retries deterministically-too-long files, burning up to 12h
  of slot time per file for a guaranteed re-timeout.
fix: (deferred — diagnose/plan only this session)
verification: (n/a — plan only)
files_changed: []

## Live state snapshot (lux, read-only, 2026-06-17)

Authorized read-only query of the control-plane Postgres (`postgres` container, db `phaze`):
- `saq_jobs` process_file: **11356 queued · 8 active · 2 complete** (all on queue `phaze-agent-nox`;
  the only registered agent). The 72 timed-out files ARE among the 11356 durable queued rows →
  **Q4 CONFIRMED: they auto-resume on restart, no operator action needed** (but re-time-out until
  cost is bounded).
- `files.state`: **11428 / 11428 = DISCOVERED** — including the 79 with analysis rows. So
  `put_analysis` (`routers/agent_analysis.py:69`) upserts AnalysisResult but **never sets
  FileState.ANALYZED on success either** — not just on timeout. Q3b is broader than thought:
  the analyze stage never advances the file state at all.
- `analysis` rows: **79** (= dashboard "done"). music/video files = 11318.
- Latent bug exposed: `trigger_analysis`/`recover_orphaned_work` select `get_files_by_state(DISCOVERED)`;
  with every file DISCOVERED, any re-trigger re-enqueues all 11428 (incl. the 79 done).

## Cost model (analysis.py:472 analyze_file) — root of the slowness

Two passes, both **O(duration)**:
- FINE: 30s window every 30s → RhythmExtractor2013 + KeyExtractor each. 3h set = ~360 windows.
- COARSE: 180s window every 180s → 12 model inferences each. 3h set = ~60 windows (720 inferences).
Only consumers of the per-window series: BPM sparklines (`proposals.py:89`) + proposal detail
(`proposals.py:269`) — both visualization/aggregate. NO algorithm needs contiguous windows →
**sampling/striding windows is safe.**

## DECISIONS (user, 2026-06-17) — for implementation (separate worktree+PR session)

1. **Bound cost = cap + even stride.** When a file exceeds the cap, stride evenly across the WHOLE
   file (not first-N). Constant cost regardless of duration; sparklines/aggregate unaffected.
2. **Caps = 60 fine / 30 coarse** per file (config-exposed). PLUS NEW REQUIREMENT: when a file was
   strided (sampled, not exhaustive), **mark it "incompletely analyzed / more data available"** so it
   can be re-run later with a higher/unbounded budget to gather the rest. Three outcomes now:
   full-analyzed · sampled(partial, re-runnable) · failed.
3. **State machine full fix:** set `ANALYZED` on successful PUT; add `ANALYSIS_FAILED` on
   retries-exhausted; record sampled/partial coverage (so the latent re-enqueue-all bug is fixed and
   stragglers are visible). Worker is Postgres-free → failure/coverage marking goes via control API.
4. **Retries = 1, NOT on timeout:** retry once for transient errors; treat TimeoutError as terminal
   → mark ANALYSIS_FAILED, no wasteful re-run.
5. **Kill-on-timeout (Q3a):** `pebble.ProcessPool` (or equiv) with a hard per-task timeout that
   SIGKILLs+recycles the child; add an INNER analysis timeout below the SAQ job timeout.
6. **SAQ timeout (Q1):** with bounded cost, lower to ~2h as a safety net (not 8/24h); inner timeout
   does the real killing.
7. **Q2 capacity:** skip cloud free tiers (1 GB RAM disqualifies all; OCI Arm also lacks the wheel).
   Real lever = a second on-prem x86 agent via `agents-add` (the single nox queue is 11356 deep).

## Implementation sequencing (gating order)

1. Q3a kill-on-timeout (pebble) + inner analysis timeout — gating; stops compute leaks.
2. Q3c bound cost (cap+stride 60/30) + emit coverage (windows_analyzed/total, sampled flag).
3. State + coverage persistence: analysis row coverage columns (Alembic migration) +
   FileState.ANALYZED on success + ANALYSIS_FAILED on terminal failure + "sampled" marker; control
   API endpoint for worker to report terminal failure/coverage.
4. retries=1 + timeout-aware terminal handling; lower SAQ timeout to ~2h.
5. UI: dashboard straggler/failed count + "sampled — more available" badge; a "deepen analysis"
   re-trigger for sampled files (enqueue with higher caps via payload flag).
6. (later/optional) second on-prem x86 agent.
