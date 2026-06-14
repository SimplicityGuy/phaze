# Phase 42: Recovery-Only Pipeline Automation - Research

**Researched:** 2026-06-14
**Domain:** SAQ task-queue automation / startup recovery / Postgres broker durability (internal runtime behavior)
**Confidence:** HIGH (all claims grounded in current codebase file:line; no external packages added)

## Summary

Phase 42 removes the only steady-state automatic enqueue in Phaze — the `*/5 * * * *`
`reenqueue_discovered` cron (`controller.py:185`) — and reframes automatic enqueue as a
**restart / queue-loss RECOVERY pass that covers all stages**, not a periodic auto-advance of
the Analyze stage. The headline change is one line: delete that cron job. The interesting work
is making the *startup* recovery (a) cover all eight pipeline stages instead of only
DISCOVERED→analyze, and (b) fire only on genuine queue-loss rather than re-running the full
eligible pipeline on every reboot.

**The reframing (CRITICAL):** Phase 36 migrated the SAQ broker from Redis to Postgres
(`saq_jobs` table, `build_pipeline_queue` → `PostgresQueue`, `queue_factory.py:66`). Queued and
active jobs are now **durable across a controller restart** — they live in a Postgres table, not
a volatile Redis instance. The original `reenqueue_discovered` premise ("Redis is empty after a
reboot, so every DISCOVERED file re-enqueues"; `controller.py:108-109`, `reenqueue.py:11-14`) is
**obsolete**. On a normal restart nothing is lost; SAQ re-dequeues the surviving `saq_jobs` rows
itself. The only thing a "queue-loss" can now mean is someone truncating/restoring `saq_jobs`
out from under the app — a rare, detectable event.

**Primary recommendation:** **Option D (Hybrid).** (1) Delete the `*/5` cron. (2) Replace
`reenqueue_discovered` with a generalized `recover_orphaned_work(ctx)` that reconciles ALL
stages by re-enqueuing each stage's existing *pending-set* query. (3) Gate the whole pass behind
a cheap **queue-loss detector** — "`saq_jobs` has zero `queued/active/scheduled` rows AND the DB
shows pending work" — so a durable Phase-36 restart is a near-total no-op and the all-stages
re-run only happens after a real wipe. Every stage already has a deterministic key
(`deterministic_key.py:74-83`), so even when it fires it dedups survivors to no-ops and converges.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Steady-state auto-advance (to remove) | API/Backend (controller worker cron) | — | `controller.py` cron_jobs is the only periodic enqueuer |
| Startup recovery reconcile | API/Backend (controller `startup` hook) | Database (`saq_jobs`, FileRecord state) | Recovery reads DB-truth pending sets + live queue depth |
| Job durability across restart | Database (Postgres `saq_jobs` broker) | — | Phase 36 moved the broker into Postgres |
| Per-stage "needs work" definition | Database (state + output-table queries) | — | Each stage's pending set is a DB query already used by manual triggers |
| Queue-loss detection | Database (`saq_jobs` row count) | — | Only the broker table can witness its own loss |
| Idempotent re-enqueue | API/Backend (`before_enqueue` deterministic key) | — | `apply_deterministic_key` collapses replays to no-ops |

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| (goal) Remove steady-state auto-advance | Delete the every-5-min `reenqueue_discovered` cron | Q1 inventory: it is the ONLY steady-state enqueuer (`controller.py:185`) |
| (goal) Recovery covers ALL stages | metadata, analyze, fingerprint, proposals, tracklist (search/scan/scrape/match) | Q3 per-stage pending-set table — each set is an existing query |
| (goal) Automatic enqueue only in recovery | Gate the reconcile on genuine queue-loss, not every reboot | Q2 durability reframing + Q5 detection |
| (implicit) No double-enqueue | Idempotent reconcile | Q6: all 8 routable functions are deterministically keyed |

## User Constraints (from operator-approved theme `project_dag_manual_control_recovery`)

No `CONTEXT.md` exists for this phase yet (only `.gitkeep`). The binding constraints come from
the operator-approved theme memory (2026-06-14):

### Locked Decisions
- **"The DAG is the single manual control surface; automation only in recovery."**
- Phase 42: "replace 5-min `reenqueue_discovered` with restart/queue-loss detection reconciling
  ALL in-flight stages; **zero steady-state auto-enqueue**."
- Recovery must restore **ALL stages**, not just DISCOVERED→analyze.
- Phase 42 is LAST in the 39→40→41→42 chain; depends on Phase 32 (the re-enqueue plumbing).

### Claude's Discretion
- Detection mechanism for "restart / queue-loss" (the original cron's `*/5` cadence vs DB-load
  balance was explicitly Claude's discretion in Phase 32; same latitude here).
- Whether recovery is a startup hook, an operator "Recover" button, or both.

### Deferred Ideas (OUT OF SCOPE)
- The unwired "tracklist search starts automatically" empty-state copy (a Tracklists-tab display
  bug, not pipeline automation).
- The per-tracklist `routers/tracklists.py::trigger_scan` file_id-only dead-letter bug (its own
  PR; flagged in Phase 40 follow-up).

## Q1 — Current Automation Inventory

Three control-side automatic enqueuers/crons exist. Source: `controller.py:161-189`.

| Mechanism | Trigger | What it enqueues | Class | Phase-42 action |
|-----------|---------|------------------|-------|-----------------|
| `reenqueue_discovered` (startup) | Boot, once (`controller.py:113-117`) | `process_file` for every `FileState.DISCOVERED` file → active agent queue | **Recovery** (but Analyze-only) | **Generalize to all stages + gate on loss** |
| `reenqueue_discovered` (cron) | `*/5 * * * *` (`controller.py:185`) | same as above, every 5 min | **Steady-state auto-advance** ❌ | **DELETE** |
| `reap_stalled_scans` (cron) | `* * * * *` (`controller.py:180`) | Nothing — marks stalled RUNNING `ScanBatch` rows FAILED (`scan_reaper.py:38-78`) | Maintenance (not an enqueuer) | **KEEP unchanged** |
| `refresh_tracklists` (cron) | `0 3 1 * *` monthly (`controller.py:177`) | `scrape_and_store_tracklist` for stale/unresolved tracklists (`tracklist.py:231-260`) | Steady-state auto-advance of the Scrape stage | **DECISION NEEDED — see note** |

**Must change for "automation only in recovery":**
1. The `*/5` `reenqueue_discovered` cron — delete (the headline).
2. The startup `reenqueue_discovered` call — replace with the all-stages, loss-gated reconcile.

**`refresh_tracklists` note `[ASSUMED]`:** This monthly cron is a *steady-state* auto-enqueue of
the Scrape stage (re-scrapes stale tracklists with no operator action). Strictly read, "zero
steady-state auto-enqueue" implies it too should go (its function stays registered for the manual
Scrape DAG trigger, Phase 41). The operator's theme focused on `reenqueue_discovered`; whether the
monthly refresh also moves to manual-only is a **decision for discuss-phase**. Recommendation:
remove the cron, keep the function (matches the DAG-is-the-control-surface principle); refresh
becomes the existing manual Scrape trigger over the pending set.

`reap_stalled_scans` is NOT an enqueuer and is orthogonal to this phase — leave it alone.

## Q2 — Queue Durability After Phase 36 (the reframing)

**Confirmed from code:** The controller queue is a `PostgresQueue`
(`queue_factory.py:66`, `build_pipeline_queue` → `PostgresQueue.from_url`), constructed at module
level (`controller.py:158`). Per-agent queues are likewise `PostgresQueue` via `AgentTaskRouter`
(routing in `enqueue_router.py`; busy-counts read `saq_jobs` directly in `pipeline.py:320`).
`get_stage_busy_counts` proves the broker is a Postgres table: it runs
`SELECT ... FROM saq_jobs WHERE status IN ('queued','active')` (`pipeline.py:320`).

**What survives a controller restart now `[VERIFIED: codebase grep]`:**
- All `saq_jobs` rows: `queued`, `active`, `scheduled`, parked-paused (`scheduled = SENTINEL`,
  `stage_control.py:65`). They are rows in Postgres; a worker process dying does not delete them.
- SAQ's `PostgresQueue` reclaims `active` jobs whose owning worker vanished via the job `timeout`
  (process_file uses `timeout=14400`, `analysis_enqueue.py:76`) — the same mechanism that already
  handles a crashed agent. So in-flight work is *resumed*, not lost.

**Therefore the original premise is obsolete.** "Redis is empty after a reboot, so every
DISCOVERED file re-enqueues" (`controller.py:108-109`) was true under the Redis broker. Under the
Postgres broker a normal restart loses **nothing**, and the deterministic-key dedup makes the
existing startup re-enqueue a **near-total no-op** (every still-queued file dedups; only files
that were eligible-but-never-queued would actually enqueue — which is the auto-advance leak this
phase must close).

**What a genuine "queue-loss" looks like now:**
- `saq_jobs` is truncated / dropped (manual cleanup, the 2026-06-11 purge-and-rebuild incident
  class, `project_phase32_queue_doubling_incident`).
- A Postgres restore-from-backup that predates in-flight jobs.
- A migration that recreates `saq_jobs` empty.
In all three, `saq_jobs` ends up with **zero rows** while the domain DB (`files`, `tracklists`,
output tables) still shows work that *should* be in flight. That asymmetry is the detection signal
(Q5).

## Q3 — Per-Stage State Model & the "Needs-Recovery" Query

Each stage already has a canonical "pending set" query — the one its **manual DAG trigger** uses.
Recovery = re-run those same queries (so recovery and manual triggers cannot drift). DB-truth
"done" per stage is `get_stage_progress` (`pipeline.py:163-272`); the pending set is its complement.

| Stage | Function | "Done" marker (DB truth, `pipeline.py`) | Pending-set query (manual trigger, `routers/pipeline.py`) | Deterministic key |
|-------|----------|------------------------------------------|------------------------------------------------------------|-------------------|
| metadata | `extract_file_metadata` | DISTINCT `file_id` in `metadata` (`:233`) | ALL music/video files (`:525`, `:554`) — *not* state-gated | `extract_file_metadata:<file_id>` ✓ |
| analyze | `process_file` | DISTINCT `file_id` in `analysis` (`:245`) | `get_files_by_state(DISCOVERED)` (`:260`, `:424`) | `process_file:<file_id>` ✓ |
| fingerprint | `fingerprint_file` | DISTINCT `file_id` in `fingerprint_results` status='completed' (`:237-241`) | `METADATA_EXTRACTED` state + failed-retry (`:609-617`) | `fingerprint_file:<file_id>` ✓ |
| proposals | `generate_proposals` | DISTINCT `file_id` in `proposals` (`:261`) | files with BOTH metadata AND analysis, state in {ANALYZED, METADATA_EXTRACTED}, **batched** (`:293-303`) | `generate_proposals:<sha256(sorted file_ids)>` ✓ |
| scan_search | `search_tracklist` | DISTINCT `Tracklist.file_id` (`:249`) | music/video with NO `Tracklist` (`:714-716`) | `search_tracklist:<file_id>` ✓ |
| scan (fingerprint-scan) | `scan_live_set` | (no own done-table; shares Tracklist) | music/video with NO `Tracklist` (`:775-777`) | `scan_live_set:<file_id>` ✓ |
| scrape | `scrape_and_store_tracklist` | DISTINCT `tracklist_id` in `tracklist_versions` (`:253`) | `get_scrape_pending_tracklists` (no version row, `pipeline.py:506`) | `scrape_and_store_tracklist:<tracklist_id>` ✓ |
| match | `match_tracklist_to_discogs` | DISTINCT `tracklist_id` via `discogs_links` (`:257`) | `get_match_pending_tracklists` (no discogs link, `pipeline.py:520`) | `match_tracklist_to_discogs:<tracklist_id>` ✓ |

**Queryable definition of "this item should have a live job but doesn't":**
`item ∈ stage.pending_set` (DB complement of done) **AND** no `saq_jobs` row exists for that item's
deterministic key. The second clause is `saq_jobs` keyed by `<function>:<natural_id>` — the exact
prefix scan `get_stage_busy_counts` already runs (`pipeline.py:320`, `split_part(key,':',1)`).

**The hard nuance:** for metadata/fingerprint/scan_search/scan, the pending set is *all eligible
files*, NOT "files the operator chose to run." So re-enqueuing the full pending set after a loss
DOES re-run the eligible pipeline — which the operator explicitly accepted ("restoring ALL
stages"). The protection that matters is firing this **only on detected loss** (Q5), so a normal
restart never auto-advances. analyze is the one stage whose pending set (`DISCOVERED`) already
equals "discovered intent" — which is why the legacy task picked it.

## Q4 — The Core Design Tension (options + recommendation)

State is a proxy for "a job should exist," but the operator's new model is that stages are
*manually* triggered. After Phase 36, real queue-loss is rare, so the question is: re-enqueue
every eligible item across all stages on restart, or only restore genuinely-lost in-flight jobs?

The fundamental constraint: **after a total `saq_jobs` wipe there is no surviving evidence of what
was queued** — you can only reconstruct intent from DB state, and for most stages "eligible" ≠
"operator chose to run it." So you cannot perfectly distinguish "was queued and lost" from
"eligible but never triggered." The lever you DO control is *when* the reconcile fires.

| # | Option | Behavior | Trade-offs | Rank |
|---|--------|----------|------------|------|
| A | Startup-only state reconcile, ALL stages, **unconditional** | On every boot, re-enqueue every stage's pending set; dedup no-ops survivors | Simple. But on a durable Phase-36 restart it STILL enqueues eligible-but-never-triggered items (metadata=all files) → auto-advance leak. Contradicts "manual only". | ❌ Reject |
| B | Loss-detected reconcile, ALL stages | Same reconcile, but gated: only runs when a queue-loss is detected | Eliminates the every-restart leak. Needs a detection signal (Q5). After a *true* loss it does re-run eligible work — operator-accepted. | ✅ Core of recommendation |
| C | Drop auto-recovery; operator "Recover" button on the DAG | No startup enqueue at all; a manual DAG action reconciles all stages on demand | Purest "DAG is the only control surface." Zero auto-enqueue by construction. But under-delivers on operator's "automatic … in recovery mode" (recovery becomes manual). | 🥈 Good fallback |
| D | **Hybrid (RECOMMENDED)** | Delete cron; startup runs the loss-gated all-stages reconcile (B); ALSO expose the same reconcile as a manual DAG "Recover" button (C) | Auto-heals a true wipe on next boot AND gives the operator an explicit recover control. Both call ONE idempotent `recover_orphaned_work(ctx)`. | 🥇 **Recommend** |

**Recommendation: Option D.** It satisfies every locked decision: zero steady-state auto-enqueue
(cron deleted), automatic recovery only on detected loss (B), covers all stages (Q3 table), and
adds a manual Recover affordance consistent with the DAG-is-the-control-surface theme (C). The
single `recover_orphaned_work` implementation is shared by the startup hook and the button, so
they cannot drift (mirrors the `analysis_enqueue` single-producer discipline).

**Idempotency / double-enqueue risk:** Low. Every routable function is deterministically keyed
(Q6), so a reconcile that overlaps a survivor dedups to a no-op — exactly the property that made
the Phase-32 startup re-enqueue safe. The 2026-06-11 queue-doubling incident
(`project_phase32_queue_doubling_incident`) was caused by *legacy random-key* jobs that predated
the deterministic-key cutover; that cohort is long drained, so the historical doubling vector is
closed. Keep the reconcile's enqueue strictly through the keyed producers (never a raw random-key
`queue.enqueue`).

## Q5 — Restart / Queue-Loss Detection

| Approach | Mechanism | Verdict |
|----------|-----------|---------|
| Unconditional startup reconcile | Always run; rely on dedup | Cheap but leaks auto-advance (Option A) — rejected |
| **`saq_jobs` emptiness signal** | On boot: `total = COUNT(*) FROM saq_jobs WHERE status IN ('queued','active','scheduled')`. If `total == 0` AND any stage pending-set is non-empty → **loss**, run reconcile. Else → no-op. | **Recommended — simplest + safe** |
| Persistent heartbeat / queue-epoch row | A durable `pipeline_recovery_state` row stamped each boot/tick; mismatch ⇒ loss | More machinery; only needed if "partial loss" must be detected. Defer. |

**Recommended signal: `saq_jobs` emptiness.** Under the Postgres broker a normal restart leaves
`saq_jobs` **populated** (durable), so "zero `saq_jobs` rows but the domain DB still shows pending
work" is an unambiguous wipe/truncate/fresh-restore signal. It is one cheap `COUNT(*)` (reuse the
`get_stage_busy_counts` `saq_jobs` access pattern, `pipeline.py:320`, with the same
`begin_nested()` degrade-safe wrapper). What SAQ gives us: `queue.count("queued"|"active")` per
queue (`pipeline.py:95-106`) and the raw `saq_jobs` table — both already used in the codebase.
No new SAQ feature required.

**Edge case to document:** a brand-new/empty deployment also has `saq_jobs == 0`; if the DB has
pending work (e.g., files freshly discovered) the detector would fire and enqueue — which is the
*correct* desired behavior there too (it is the first legitimate run). So the detector needs no
special "first boot" case; it self-heals identically.

## Q6 — Idempotency / Safety

**Every one of the 8 routable pipeline functions has a deterministic key** in
`_KEY_BUILDERS` (`deterministic_key.py:74-83`), applied unconditionally at the single
`before_enqueue` chokepoint (`apply_deterministic_key`, `:86-103`, registered for every queue in
`build_pipeline_queue`, `queue_factory.py:67-68`):

```
process_file:<file_id>                  extract_file_metadata:<file_id>
fingerprint_file:<file_id>              scan_live_set:<file_id>
search_tracklist:<file_id>              scrape_and_store_tracklist:<tracklist_id>
match_tracklist_to_discogs:<tracklist_id>   generate_proposals:<sha256(sorted file_ids)>
```

So a reconcile re-enqueue of any item still in `saq_jobs` dedups against the existing key (SAQ's
per-queue incomplete-set) and returns `None` — counted as `skipped`, mirroring
`enqueue_process_file` returning `None` on dedup (`analysis_enqueue.py:56-85`).

**No routable stage is un-keyed** — there is no doubling vector among the eight. One care point:
`generate_proposals` keys on the **set hash** of `file_ids` (`_hash_ids`, `:57-67`), so a recovery
batch must reconstruct the **same** batch membership the manual trigger would produce
(`pipeline.py:293-303`) or it will key differently and not dedup against an in-flight batch. Build
the proposals recovery batch from the identical convergence query to keep keys aligned.

**Routing safety (carry forward Phase-32 pitfalls, `reenqueue.py:22-27`):**
- Agent stages (metadata, analyze, fingerprint, scan_live_set) MUST route to the active agent's
  per-agent queue via `select_active_agent` + `task_router.queue_for(agent.id)` — NEVER the
  consumer-less controller queue.
- Controller stages (search_tracklist, scrape, match, generate_proposals) route to the controller
  queue (`CONTROLLER_TASKS`, `enqueue_router.py:44-52`).
- Cold-boot with **no live agent** ⇒ `NoActiveAgentError` ⇒ agent-stage recovery is skipped with a
  WARNING (already handled, `reenqueue.py:72-76`). See Open Questions — startup-only recovery will
  *miss* agent stages if no agent has connected yet at boot.
- Reuse the cached `ctx["task_router"]` (`controller.py:105`); never construct a fresh
  `AgentTaskRouter` per call (pool leak, Phase-32 Pitfall 4).

## Q7 — Test Strategy

**Today's coverage (`tests/test_tasks/test_reenqueue.py`, 7 tests):** seeds DISCOVERED files +
an active agent via `seed_active_agent` / `_seed_file`, runs `reenqueue_discovered` against a
`DedupFakeTaskRouter` (`tests/_queue_fakes`) that models SAQ deterministic-key dedup. Asserts:
all-discovered re-enqueue, straggler/in-flight skip counts, no-active-agent WARNING+zeros,
empty→zeros, complete-payload + policy, plus one `@pytest.mark.integration` real-broker dedup
test (`:227-287`). `ctx` shape: `{"async_session": sessionmaker, "task_router": router}`.

**New tests for Phase 42:**
1. **No steady-state auto-enqueue** — assert `controller.settings["cron_jobs"]` no longer contains
   `reenqueue_discovered` (a structural test on the settings dict; cheap, deterministic). Keep the
   `reap_stalled_scans` + (decision-pending) `refresh_tracklists` crons asserted present so the
   delete is scoped.
2. **Loss detector** — unit-test the `saq_jobs`-emptiness signal: (a) populated `saq_jobs` +
   pending DB work ⇒ detector False ⇒ reconcile no-op; (b) empty `saq_jobs` + pending work ⇒
   True ⇒ reconcile enqueues. Use the existing integration-DB pattern (`tests/integration`,
   real Postgres `saq_jobs`) for the truthful read; the unit layer can fake the count.
3. **Recovery restores ALL stages** — seed one item per stage in its pending set (a DISCOVERED
   file, a METADATA_EXTRACTED file, a file with metadata+analysis but no proposal, an untracked
   music file, a tracklist with no version, a tracklist with no discogs link), run
   `recover_orphaned_work` against the `DedupFakeTaskRouter` + a fake controller queue, assert one
   keyed enqueue per stage on the correct queue (agent vs controller) with the correct key prefix.
4. **Idempotent reconcile** — pre-enqueue half the keys "live", assert they dedup to `skipped` and
   only the rest enqueue (generalizes `test_cron_reenqueues_stragglers`).
5. **Cold-boot no-agent** — agent stages skip with WARNING; controller stages still recover
   (extends `test_no_active_agent_skips`).

Determinism: keep using the `DedupFakeTaskRouter` for per-stage routing/keys and reserve a single
`@pytest.mark.integration` test for the real `saq_jobs` emptiness read (mirrors
`test_real_broker_dedup_returns_none`, `:227`).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`); markers `integration` in use |
| Quick run command | `uv run pytest tests/test_tasks/test_reenqueue.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min, CLAUDE.md) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | File Exists? |
|-----|----------|-----------|-------------------|-------------|
| Remove cron | `reenqueue_discovered` absent from cron_jobs | unit | `uv run pytest tests/test_tasks/test_controller_reenqueue.py -x` | ⚠️ extend existing |
| Loss detection | empty `saq_jobs`+pending ⇒ fire | unit+integration | `uv run pytest tests/test_tasks/test_recovery.py -x` | ❌ Wave 0 |
| All-stages recovery | one keyed enqueue per stage | unit | `uv run pytest tests/test_tasks/test_recovery.py -x` | ❌ Wave 0 |
| Idempotency | survivors dedup to skipped | unit | `uv run pytest tests/test_tasks/test_recovery.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tasks -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green + 85% coverage before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_tasks/test_recovery.py` — all-stages reconcile + loss-detector + idempotency
- [ ] Reuse `tests/_queue_fakes.DedupFakeTaskRouter` + `seed_active_agent`; add per-stage seed
      helpers (metadata/fingerprint/proposals/tracklist fixtures) — extend, don't fork
- [ ] Extend `tests/test_tasks/test_controller_reenqueue.py` with the "cron removed" structural assert

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-stage "pending" query | New recovery-specific SQL | The existing manual-trigger queries (`routers/pipeline.py`) + `get_stage_progress` complements | Recovery and manual triggers must not drift; reuse keeps keys/sets identical |
| Job key / dedup | A recovery-local key scheme | `apply_deterministic_key` chokepoint (`deterministic_key.py`) | Anti-drift; one key authority closes the doubling vector |
| Enqueue producers | Raw `queue.enqueue(...)` in the recovery task | `enqueue_process_file` + the routed producers | Single-producer discipline (full payload + policy + key) |
| "Is anything in flight" | A bespoke counter | `saq_jobs` `COUNT`/`queue.count()` (`pipeline.py:95,320`) | Postgres broker IS the durable truth now |
| Active-agent selection | New liveness rule | `select_active_agent` (`enqueue_router.py:93`) | One liveness definition across the app (CONTEXT decision) |

## Common Pitfalls

### Pitfall 1: Reconcile that auto-advances on every restart
**What goes wrong:** Re-enqueuing each stage's full pending set unconditionally re-runs eligible
work (metadata pending = ALL files) on a durable restart where nothing was lost. **Avoid:** gate
on the `saq_jobs`-emptiness loss signal (Q5). **Warning sign:** metadata/fingerprint jobs appear
after a clean reboot with no operator action.

### Pitfall 2: `generate_proposals` batch-key mismatch
**What goes wrong:** Recovery batches `file_ids` differently than the manual trigger, so the set-hash
key differs and fails to dedup an in-flight batch (`_hash_ids`, `deterministic_key.py:57`).
**Avoid:** build the recovery batch from the identical convergence query (`pipeline.py:293-303`).

### Pitfall 3: Recovering agent stages with no agent online
**What goes wrong:** Cold reboot → no agent yet → startup recovery skips metadata/analyze/
fingerprint/scan; a startup-only pass never retries, leaving agent stages unrecovered until an
operator acts. **Avoid:** see Open Questions — pair startup recovery with the manual Recover button
(Option D) and/or a one-shot recovery on first agent check-in.

### Pitfall 4: Deleting the wrong cron
**What goes wrong:** Removing `reap_stalled_scans` (maintenance) or `refresh_tracklists` without a
decision. **Avoid:** delete ONLY the `*/5 reenqueue_discovered` line (`controller.py:185`); decide
`refresh_tracklists` explicitly in discuss-phase.

## Runtime State Inventory

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `saq_jobs` (Postgres broker) holds queued/active/scheduled/parked jobs; `files.state`, output tables (`metadata`, `analysis`, `fingerprint_results`, `proposals`, `tracklists`, `tracklist_versions`, `discogs_links`) define per-stage done | Read-only for recovery; no migration |
| Live service config | Controller `cron_jobs` list (`controller.py:176-186`) — in code, in git | Code edit (delete one cron) |
| OS-registered state | None — SAQ crons live in the worker process, re-registered from `settings` each boot | None (verified: cron_jobs is a Python list in `settings`) |
| Secrets/env vars | None affected (`queue_url`, `redis_url` unchanged) | None |
| Build artifacts | None — pure behavior change, no package/version change | None |

**The canonical question:** after the code change, the only runtime systems carrying old behavior
are running worker processes; a homelab redeploy (new image) re-registers `settings` without the
deleted cron. No stored data carries the old automation.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (`saq_jobs` broker) | recovery detection + enqueue | ✓ (project constraint) | 16+ | — |
| Redis (cache_redis counters) | best-effort counter hooks only | ✓ | 7+ | counters degrade silently |
| SAQ | queue API | ✓ | >=0.26.3 (Postgres backend, Phase 36) | — |

No new external dependency. **Package Legitimacy Audit: SKIPPED — this phase installs no packages.**

## Security Domain

This phase changes internal automation behavior only; no new endpoints, auth, or external input.
The recovery task constructs job keys from server-generated UUIDs / tracklist ids (no untrusted
free-text enters the key — threat T-32-01 already mitigated, `analysis_enqueue.py:37`). The one
relevant control is **V5 input validation**: the loss-detector and `saq_jobs` reads must use the
existing static-SQL / bound-param discipline (no f-string interpolation, mirroring
`_STAGE_BUSY_SQL`, `pipeline.py:320`, and `_read_stage_control`'s `%(name)s` paramstyle,
`stage_control.py:96`). No ASVS category beyond V5 applies.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `refresh_tracklists` monthly cron also counts as "steady-state auto-enqueue" to remove | Q1 | If operator wants it kept, removing it stops automatic stale-tracklist refresh |
| A2 | Operator accepts that a *true* queue-loss re-runs the full eligible pipeline (all stages) | Q4 | If they want loss recovery to restore ONLY analyze, Option B's scope shrinks |
| A3 | "Recover" button (Option D's manual half) is desired, not just startup auto-recovery | Q4 | If button is unwanted, fall back to pure Option B |
| A4 | SAQ `PostgresQueue` reclaims timed-out `active` jobs on its own (no app code needed) | Q2 | If not, in-flight `active` jobs at restart need explicit requeue |

## Open Questions

1. **Cold-boot agent-stage recovery.** Startup recovery runs before any agent has checked in, so
   agent stages (metadata/analyze/fingerprint/scan) get skipped (`NoActiveAgentError`). A
   startup-only pass never retries.
   - Recommendation: Option D's manual Recover button covers this (operator clicks once an agent is
     online); optionally trigger a one-shot recovery on first agent heartbeat. Decide in plan.
2. **`refresh_tracklists` disposition** (A1) — keep monthly cron, or move to manual Scrape trigger?
3. **`generate_proposals` in recovery** — proposals is controller-side and batch-keyed; confirm the
   recovery batch uses the exact convergence query so keys align (Pitfall 2).
4. **`saq_jobs` `scheduled` rows in the detector** — paused/parked jobs use `scheduled = SENTINEL`
   (`stage_control.py:65`) and are still `queued`; ensure the emptiness `COUNT` includes them so a
   paused-but-present queue is not misread as "lost." Recommendation: count
   `status IN ('queued','active')` (parked rows are still `queued`).

## Sources

### Primary (HIGH confidence) — codebase, verified file:line this session
- `src/phaze/tasks/controller.py` — cron_jobs, startup re-enqueue, queue construction
- `src/phaze/tasks/reenqueue.py` — current Analyze-only recovery + routing pitfalls
- `src/phaze/tasks/scan_reaper.py`, `src/phaze/tasks/tracklist.py` — other crons
- `src/phaze/tasks/_shared/deterministic_key.py` — all 8 key builders
- `src/phaze/tasks/_shared/queue_factory.py` — PostgresQueue + hook chain (Phase 36 durability)
- `src/phaze/tasks/_shared/stage_control.py` — STAGE_TO_FUNCTION, SENTINEL, key prefixes
- `src/phaze/services/pipeline.py` — get_stage_progress, saq_jobs busy reads, pending complements
- `src/phaze/services/analysis_enqueue.py`, `enqueue_router.py` — single-producer + routing
- `src/phaze/routers/pipeline.py` — per-stage manual pending-set queries
- `src/phaze/models/file.py` — FileState enum
- `tests/test_tasks/test_reenqueue.py` — existing test patterns + fakes
- `MEMORY.md` / `project_dag_manual_control_recovery.md` — operator-approved theme + locked decisions

## Metadata

**Confidence breakdown:**
- Automation inventory (Q1): HIGH — read every cron/startup site directly
- Postgres durability reframing (Q2): HIGH — PostgresQueue + `saq_jobs` reads verified in code
- Per-stage pending-set map (Q3): HIGH — each set is an existing query, cited file:line
- Design options + recommendation (Q4): MEDIUM-HIGH — reasoning is sound; A2/A3 need operator confirm
- Detection signal (Q5): MEDIUM — emptiness signal is simple+safe but unvalidated against partial loss
- Idempotency (Q6): HIGH — all 8 functions keyed at the chokepoint, verified

**Research date:** 2026-06-14
**Valid until:** 2026-07-14 (stable internal domain; revisit if the broker backend changes again)
