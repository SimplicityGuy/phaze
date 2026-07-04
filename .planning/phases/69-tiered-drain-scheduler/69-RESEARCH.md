# Phase 69: Tiered Drain Scheduler - Research

**Researched:** 2026-07-04
**Domain:** Multi-backend async task scheduling over Postgres (SQLAlchemy 2.0 async / asyncpg), advisory-lock concurrency control, per-file rank-first dispatch
**Confidence:** HIGH (all three novel mechanisms grounded in the live codebase; two decision points flagged as ASSUMED for discuss/plan confirmation)

## Summary

Phase 69 generalizes the single-backend drain (`stage_cloud_window`) into a per-file, rank-first tiered scheduler over `resolve_backends()`. Every building block already exists after Phase 68: the `Backend` protocol with per-backend `is_available()` / `in_flight_count()` / `dispatch()` / `reconcile()`, the `cloud_job.backend_id` column (migration 029), and the advisory-lock count-and-claim pattern in the drain. The phase is a **policy layer over existing substrate**, not new infrastructure — consistent with the milestone's zero-new-dependency constraint.

The three flagged novel mechanisms resolve as follows. **(1) Lock scope:** keep the single existing advisory-lock key `5_000_504`; the correct fix is to make the drain *snapshot each backend's `in_flight_count()` once per tick under the lock* and decrement locally as it claims — this alone guarantees no cap overshoot because reconcile only ever *decrements* in-flight (it never claims a slot), making concurrent reconcile releases provably cap-safe. Reconcile shares the lock by acquiring the same key at the top of each per-row transaction. No per-backend lock keys are needed. **(2) Staleness "waited-since":** use `FileRecord.updated_at` with **zero migration** — verified that no writer touches a parked `AWAITING_CLOUD` row until the drain flips it to `PUSHING`, so `updated_at` equals the entry-to-AWAITING_CLOUD timestamp for a waiting file, and re-stamps to a fresh clock on each fail-back (desirable). **(3) Black-hole:** reuse the persistent `cloud_job.attempts` counter as the anti-thrash bound — a file whose attempts reach the cap becomes *cloud/kueue-ineligible* (filtered out of the eligible set), which deterministically routes it to local; this breaks the A↔B thrash while preserving D-06 statelessness (the exclusion derives from a counter, not from remembered backend IDs).

**Primary recommendation:** Build a pure in-memory selection function (`select_backend(candidate, snapshot)`) that the drain calls per candidate over a once-per-tick snapshot, keep the single advisory lock, use `updated_at` for staleness (no migration), and reuse `cloud_job.attempts` as a total-cloud attempt bound (flag the "total vs literally-per-backend" reading for discuss confirmation).

## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Long files do NOT spill to slow local immediately when higher-ranked backends are full (diverges from design §4.3 "no staleness logic"). A **staleness guard** applies: a file must sit in `AWAITING_CLOUD` beyond a wait threshold before local becomes an eligible dispatch target.
- **D-02:** The staleness threshold is **operator-configurable with a shipped sensible default** — a NEW config knob (suggested `cloud_spill_to_local_after_seconds`, suggested default ~15 min; exact name/default at plan-time). NOT a fixed constant, NOT duration-derived.
- **D-03:** The staleness wait applies **only when higher-ranked backends are FULL (busy)**. If every cloud/Kueue backend is **OFFLINE**, the file spills to local **immediately** (still subject to local's `cap`). The guard gates the *full→local* path, not the *offline→local* path.
- **D-04:** **Local is the guaranteed safety net.** Bounded **per-backend** dispatch attempts (reuse `cloud_submit_max_attempts`) stop cloud/Kueue thrash; once exhausted the file falls to local. A file goes `ANALYSIS_FAILED` **only** when local itself fails **or** a global total-attempt ceiling is hit.
- **D-05:** **Purely per-backend caps.** The sum of per-backend `cap`s is the only total ceiling. The old global `cloud_max_in_flight` setting is **retired**.
- **D-06:** **Stateless re-rank.** A job that fails mid-flight on backend X is re-picked normally next tick (may re-pick X, bounded by the attempt cap). **No per-file failure memory** (no "last-failed backend_id").

### Already locked by ROADMAP/REQUIREMENTS (do NOT re-open)

- **SCHED-04 tie-break:** equal-`rank` backends tie-broken by **lowest current utilization `in_flight/cap`, then stable `id`**. No weighted/proportional fair-share.
- **SCHED-05 single recovery owner:** `reconcile_cloud_jobs` + the recovery ledger become `backend_id`-aware; the AST over-enqueue guard is extended so compute-backed cloud files gain no second recovery path.
- **cap source:** per-backend `cap` from the Phase-67 `backends` registry entries.
- **offline detection:** the Phase-68 `Backend.is_available()` probe.
- **in-flight counting:** the Phase-68 uniform `Backend.in_flight_count()` over `{UPLOADING,UPLOADED,SUBMITTED,RUNNING}`.

### Claude's Discretion

- Exact new config field name(s) + default for the staleness threshold (D-02).
- Whether the "waited-since" signal reads an existing timestamp or needs a new one → **researcher recommends existing `FileRecord.updated_at`, no migration** (see Q2 below).

### Deferred Ideas (OUT OF SCOPE)

- Duration-scaled staleness threshold (over-engineering; revisit only if flat threshold proves too blunt).
- Per-file "avoid last-failed backend" memory (stateless re-rank chosen instead).
- Keep-a-global-master-ceiling (rejected in favor of purely per-backend caps).
- N concurrent Kueue clusters, per-cluster S3 buckets, N-lane UI, provisioning, dollar-cost model, new provider SDKs (Phase 70/71 or milestone non-goals).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SCHED-01 | Per tick, each `AWAITING_CLOUD` file → available lowest-`rank` backend with `in_flight_count() < cap`, evaluated per candidate so a full top rank spills to next | Q4 selection algorithm + once-per-tick snapshot; `resolve_backends()` + `in_flight_count()`/`is_available()` already exist |
| SCHED-02 | Global `cloud_max_in_flight` → per-backend `cap`, count-and-claim in one txn under existing `pg_advisory_xact_lock`, overlapping ticks never overshoot | Q1 lock scope — single key `5_000_504`, snapshot-and-claim atomicity, reconcile-only-decrements proof |
| SCHED-03 | Offline/failed backend returns file to `AWAITING_CLOUD`; next tick re-dispatches against current availability; black-hole guard | Q3 `cloud_job.attempts` reuse; reconcile terminal behavior change (at-cap → AWAITING_CLOUD not ANALYSIS_FAILED); compute failure-return wiring |
| SCHED-04 | Equal-`rank` tie-break: lowest `in_flight/cap` utilization, then stable `id` | Q4 — computed from the once-per-tick snapshot; pure in-memory sort |
| SCHED-05 | Exactly one recovery owner per backend kind; reconcile + ledger + AST guard `backend_id`-aware | Q5 — reconcile loops `resolve_backends()` scoped by `backend_id`; ledger recovery excludes files with an in-flight `cloud_job` row |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Per-file backend selection (rank/cap/staleness/attempts) | Control plane (`stage_cloud_window` cron + a new pure selection helper) | — | Routing decisions live on the control plane (application server); agents stay decision-free (DIST-01). Stays inside `stage_cloud_window` (design §5). |
| Per-backend cap enforcement (count-and-claim) | Postgres (advisory lock + `cloud_job` count) | Control plane | Cap is a DB-serialized invariant; the advisory lock is the concurrency primitive. |
| In-flight counting | Postgres (`cloud_job` by `backend_id`) | `Backend.in_flight_count()` | Phase 68 substrate; uniform across kinds. |
| Offline detection | `Backend.is_available()` (compute→agent heartbeat; kueue→cluster probe; local→always) | — | Phase 68; probe cost matters (kueue probe is network) → snapshot once per tick. |
| Failure → re-dispatch | Control plane reconcile (kueue) + `/pushed` callback (compute) | Recovery ledger | SCHED-05: exactly one owner per kind; no second recovery path. |
| Terminal `ANALYSIS_FAILED` | Control plane (local failure or global ceiling) | — | D-04: local is the safety net; cloud failures spill, never hard-fail directly. |

## Standard Stack

**No new packages.** This phase is a pure application-code change on the already-pinned stack. Zero-new-dependency is an explicit milestone constraint (REQUIREMENTS.md framing).

| Component | Already present | Role in Phase 69 |
|-----------|----------------|-------------------|
| SQLAlchemy 2.0 async + asyncpg | ✓ | `pg_advisory_xact_lock`, `FOR UPDATE SKIP LOCKED`, `on_conflict_do_update` |
| Postgres 16 | ✓ | Advisory locks, `cloud_job` sidecar, `updated_at` timestamps |
| SAQ (Postgres broker, Phase 36) | ✓ | Deterministic-key dispatch dedup |
| pydantic-settings | ✓ | The one new config knob (D-02 staleness threshold) |
| structlog | ✓ | Cron no-op / hold logging |

**No installation step.** No `## Package Legitimacy Audit` is required — the phase installs nothing. `[VERIFIED: codebase grep — pyproject.toml unchanged this phase; REQUIREMENTS.md milestone framing states "Zero new dependencies"]`

## Architecture Patterns

### System Architecture Diagram — the Phase-69 tiered drain tick

```
                    stage_cloud_window(ctx)  [*/5 controller cron]
                              │
                              ▼
                    cloud_enabled gate ── False ──► clean no-op {staged:0, skipped:0}
                              │ True
                              ▼
              ┌───── one DB transaction ──────────────────────────────┐
              │  pg_advisory_xact_lock(5_000_504)   [serializes ticks] │
              │            │                                            │
              │            ▼                                            │
              │   SNAPSHOT (once per tick):                            │
              │   for b in resolve_backends(cfg):                      │
              │       available[b] = await b.is_available(session)     │  ← M probes, not N×M
              │       remaining[b] = b.cap - await b.in_flight_count() │
              │            │                                            │
              │            ▼                                            │
              │   candidates = get_cloud_staging_candidates(           │
              │        session, sum(remaining over non-local))         │  ← FIFO, FOR UPDATE SKIP LOCKED
              │            │                                            │
              │            ▼                                            │
              │   for file in candidates:            [pure in-memory]  │
              │       b = select_backend(file, snapshot, now)          │  ← rank-first eligible
              │       if b is None: continue          (hold this file) │
              │       await b.dispatch(file, session, task_router)     │  ← writes cloud_job + flips state
              │       remaining[b] -= 1               (local decrement) │
              │            │                                            │
              │            ▼                                            │
              │   await session.commit()   [single commit; releases    │
              │                             advisory lock + row locks]  │
              └────────────────────────────────────────────────────────┘
                              │
                              ▼
        reconcile (separate */5 cron, shares lock key 5_000_504 per-row):
          for b in resolve_backends(cfg): await b.reconcile(session, ctx)
             KueueBackend.reconcile  → terminal SUCCEEDED (decrement)
                                     → FAILED under global ceiling → file back to AWAITING_CLOUD (decrement, +attempts)
                                     → FAILED at global ceiling     → ANALYSIS_FAILED
             ComputeAgentBackend.reconcile → no-op (terminalizes via /pushed callback)
             LocalBackend.reconcile        → no-op
```

### Component Responsibilities

| File | Change in Phase 69 |
|------|--------------------|
| `src/phaze/tasks/release_awaiting_cloud.py` | Generalize `stage_cloud_window`: replace the single-`resolve_backends().next(non-local)` pick with the snapshot + per-candidate `select_backend` loop. Keep the single advisory lock + single post-loop commit. |
| **NEW** `src/phaze/services/backend_selection.py` (suggested) | Pure, synchronous `select_backend(file, snapshot, now, cfg)` → `Backend | None`. Rank-first eligible, staleness gate, attempt-exclusion, tie-break. Fully unit-testable with no DB. |
| `src/phaze/services/pipeline.py` | Retire `get_cloud_window_count()` (global PUSHING/PUSHED count) in favor of per-backend `in_flight_count()`. `get_cloud_staging_candidates` unchanged (still FIFO claim), but `limit` now = sum of non-local `remaining`. |
| `src/phaze/services/backends.py` | `LocalBackend.dispatch` becomes reachable from the drain (it already exists, Phase 68 unit-tested but unwired). Reconcile terminal semantics change (see Q3). |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | Replace the monolithic global loop with per-backend `backend.reconcile()` (backend_id-scoped). At-cap terminal changes from ANALYSIS_FAILED → return-to-AWAITING_CLOUD unless the global ceiling is hit (Q3). Share the advisory lock (Q1). |
| `src/phaze/tasks/reenqueue.py` | Extend the orphan-exclusion predicate: a `process_file`/`push_file` ledger row for a file with an in-flight `cloud_job` row is owned by the backend reconcile/callback — exclude from ledger recovery (Q5). |
| `src/phaze/config.py` | Add the D-02 staleness knob. `cloud_max_in_flight` already retired (Phase 67, REG-04 — the field is gone; `cap` lives on each backend entry). |

### Pattern 1: Once-per-tick snapshot + local decrement (the load-bearing efficiency + correctness pattern)

**What:** Probe `is_available()` and `in_flight_count()` exactly once per backend per tick, then run the entire per-candidate selection in memory, decrementing a local `remaining[backend_id]` counter on each claim.

**When to use:** Always, in the drain tick. Never re-probe inside the candidate loop.

**Why:** `KueueBackend.is_available()` performs a **network cluster probe** (`kube_staging.get_local_queue()`); calling it per-candidate-per-backend would be N×M network round-trips inside a locked transaction (holds the advisory lock — blocks all other ticks — for the whole probe storm). The snapshot makes it O(M) probes and O(N×M) pure in-memory comparisons. It also makes selection **internally consistent**: a concurrent reconcile committing a decrement mid-loop cannot shift counts the drain already snapshotted, so the tie-break and cap math stay deterministic for the tick. `[VERIFIED: codebase — backends.py:294 KueueBackend.is_available calls kube_staging.get_local_queue()]`

### Pattern 2: Rank-first eligible selection (SCHED-01/04, D-01/D-03/D-04)

```python
# Source: proposed src/phaze/services/backend_selection.py — pure, no I/O, unit-testable.
# snapshot: dict[backend_id -> {"backend": Backend, "available": bool, "remaining": int, "cap": int}]
def select_backend(file, snapshot, now, cfg):
    # 1. Eligible = available AND has a free slot.
    eligible = [s for s in snapshot.values() if s["available"] and s["remaining"] > 0]

    # 2. Attempt-exclusion (D-04): a file that exhausted its cloud budget is cloud/kueue-INELIGIBLE.
    #    Local is never excluded (the guaranteed safety net).
    if file.cloud_attempts >= cfg.cloud_submit_max_attempts:
        eligible = [s for s in eligible if s["backend"].rank == LOCAL_RANK]  # local only

    # 3. Staleness gate on local (D-01/D-03): local is eligible ONLY when
    #      (a) every non-local backend is OFFLINE (spill immediately), OR
    #      (b) the file has waited past the threshold (spill after blip), OR
    #      (c) the file already exhausted its cloud budget (step 2 forced local).
    non_local = [s for s in snapshot.values() if s["backend"].rank != LOCAL_RANK]
    any_non_local_online = any(s["available"] for s in non_local)
    waited = (now - file.updated_at).total_seconds() >= cfg.cloud_spill_to_local_after_seconds
    stale_ok = (not any_non_local_online) or waited or file.cloud_attempts >= cfg.cloud_submit_max_attempts
    if not stale_ok:
        eligible = [s for s in eligible if s["backend"].rank != LOCAL_RANK]

    if not eligible:
        return None  # hold this file this tick (clean no-op)

    # 4. Rank-first, tie-break by utilization then stable id (SCHED-04).
    eligible.sort(key=lambda s: (s["backend"].rank,
                                 (s["cap"] - s["remaining"]) / s["cap"],  # in_flight/cap
                                 s["backend"].id))
    return eligible[0]["backend"]
```

**Key subtlety (D-03 full-vs-offline):** `any_non_local_online` distinguishes "every cloud backend is OFFLINE" (→ local eligible immediately) from "cloud backends are online but FULL" (→ local gated behind the staleness threshold). This is exactly D-03's *the guard gates the full→local path, not the offline→local path.*

### Anti-Patterns to Avoid

- **Re-probing `is_available()`/`in_flight_count()` inside the candidate loop.** Network probe storm + count instability. Use the snapshot.
- **Per-backend advisory lock keys.** Unnecessary — the whole tick is one transaction covering all backends under one lock. Multiple keys add deadlock-ordering complexity for zero benefit (see Q1).
- **Carrying a `last_failed_backend_id` on the file.** Violates D-06. The `cloud_job.attempts` counter provides anti-thrash without per-file backend memory.
- **Converting reconcile to a single-commit-per-tick to "hold" an xact lock.** Breaks the load-bearing delete-after-record ordering (D-04 in `reconcile_cloud_jobs`) and the per-row failure isolation. Take the lock per-row instead (Q1).
- **Marking a cloud-failed file `ANALYSIS_FAILED` directly** (current reconcile behavior). Phase 69 spills it back to `AWAITING_CLOUD`; only local-failure or the global ceiling yields `ANALYSIS_FAILED` (D-04).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Non-overlapping tick serialization | A "is a tick running?" DB flag / SAQ mutex | The existing `pg_advisory_xact_lock(5_000_504)` | Transaction-scoped, auto-released on commit/rollback, already proven in `stage_cloud_window` (WR-04). |
| Cap overshoot prevention | A manual read-then-write counter | Snapshot `in_flight_count()` under the lock + single-commit claim | The lock + single transaction *is* the atomicity; a hand-rolled counter re-opens the over-stage race. |
| "Entered AWAITING_CLOUD at" | A new timestamp column + migration | `FileRecord.updated_at` (Q2) | Verified no writer touches a parked AWAITING_CLOUD row; zero migration. |
| Attempt bounding | A new per-backend counter column | Existing `cloud_job.attempts` (Q3) | Already persistent, already compared to `cloud_submit_max_attempts`, survives `on_conflict_do_update`. |
| FIFO candidate claim | New query | `get_cloud_staging_candidates(session, limit)` | Already FIFO + `FOR UPDATE SKIP LOCKED`. |
| Deterministic dispatch dedup | Manual "already dispatched?" check | SAQ deterministic key (`push_file:<id>` / `submit_cloud_job:<id>`) | Phase-50 tally semantics already ride this. |

**Key insight:** Phase 69 adds *policy*, not *plumbing*. Every concurrency, persistence, and dedup primitive it needs already exists and is battle-tested against prior incidents (the 44.5k over-enqueue, the over-stage DoS). The single genuinely-new artifact is the pure `select_backend` function.

---

## Novel Mechanism 1 (HIGHEST PRIORITY): Drain↔Reconcile Lock Scope (SCHED-02)

**Confidence: HIGH.** `[VERIFIED: codebase — release_awaiting_cloud.py:65,114; reconcile_cloud_jobs.py:124-188,295-320; backends.py:157-169]`

### Current state

- **Drain** (`stage_cloud_window`) holds ONE advisory lock `_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504` via `pg_advisory_xact_lock` for the whole tick, in a single transaction with a single post-loop `commit()`. This serializes overlapping drain ticks: a second tick blocks at the lock until the first commits, then reads committed counts. `[VERIFIED: release_awaiting_cloud.py:110-114,173]`
- **Reconcile** (`reconcile_cloud_jobs` cron and `KueueBackend.reconcile`) holds **NO advisory lock** and commits **per row** (delete-after-record ordering D-04, per-row rollback guard). `[VERIFIED: reconcile_cloud_jobs.py:135,169,184,306-319; backends.py:316 docstring "NO advisory lock this phase (Pitfall 2 — deferred to Phase 69)"]`

### The correctness question, resolved

**Does per-backend count-and-claim need per-backend lock keys?** **No.** The drain tick is a *single transaction covering all backends*. There is no cross-backend interleaving within a tick, and the single lock already prevents two ticks from both snapshotting `cap-k` and both claiming into overshoot. Per-backend keys would only matter if separate ticks handled separate backends concurrently — which is not the design (one cron, one tick, all backends). Keep the single key `5_000_504`.

**Can a reconcile releasing an in-flight slot race a concurrent drain claim into overshoot?** **No — and here is the proof the planner can lift.** Reconcile's only effects on `in_flight_count` are:

| Reconcile outcome | `cloud_job.status` transition | in-flight effect |
|-------------------|-------------------------------|-------------------|
| Succeeded | RUNNING/SUBMITTED → SUCCEEDED (terminal) | **decrement** |
| Failed under ceiling (Phase 69) | RUNNING/SUBMITTED → FAILED + file→AWAITING_CLOUD | **decrement** |
| Failed at global ceiling | → FAILED + file→ANALYSIS_FAILED | **decrement** |
| Re-drive / healthy pending / running | stays in the in-flight set | **no change** |

Reconcile **never increments** in-flight count (it never claims a slot). `[VERIFIED: reconcile_cloud_jobs.py — _record_success sets SUCCEEDED; _handle_no_callback_terminal sets FAILED or re-drives SUBMITTED (already in-flight); no path adds a new in-flight row]`. Therefore, for any drain claim:

```
real_in_flight_after_claim = (count drain snapshotted) − (reconcile decrements that committed since snapshot) + (drain claims)
                           ≤ (count drain snapshotted) + (drain claims)
                           = cap
```

A concurrent reconcile can only make the real count *lower* than the drain assumed → the drain is **conservative** → overshoot is impossible. The only overshoot vector is two concurrent *claims* (two drain ticks), which the single lock already serializes.

### Recommended lock discipline (concrete, plan-ready)

1. **Drain:** keep `pg_advisory_xact_lock(5_000_504)` at the top of the tick's single transaction. Change the count source from `get_cloud_window_count()` (global FileState) to a **per-backend `in_flight_count()` snapshot** taken immediately after acquiring the lock. Claim in the single transaction; single commit releases the lock. **This is the sole load-bearing atomicity for SCHED-02.**
2. **Reconcile:** acquire `pg_advisory_xact_lock(5_000_504)` at the **top of each per-row unit of work** (after the previous row's commit, which released the prior xact lock). The existing per-row structure already re-`session.get()`s each row fresh (`reconcile_cloud_jobs.py:304-308`), so inserting one `SELECT pg_advisory_xact_lock(:key)` at the start of each iteration is a small, localized change. This makes a drain tick and a reconcile row-mutation **mutually exclusive**, satisfying SCHED-02's "reconcile shares the lock discipline" verbatim, while preserving reconcile's per-row commit + delete-after-record ordering + failure isolation.
   - **Why per-row, not whole-tick:** `pg_advisory_xact_lock` auto-releases on *every* commit. Reconcile commits per row (load-bearing D-04 ordering). A whole-tick lock is therefore incompatible; a per-row lock is the correct granularity and is *sufficient* because the overshoot proof above shows reconcile between drain ticks is always cap-safe.
   - **Lock ordering / deadlock:** only ONE key is ever taken, so there is no lock-ordering hazard and no deadlock possibility.

3. **Generalizing count-and-claim to N backends:** it is a **within-transaction loop**, not a restructure. Snapshot all backends, then one candidate loop, then one commit — the exact shape the drain already has (`slots` computed once, then a candidate loop, then one commit at line 173). The only change is `slots`→per-backend `remaining[]` and the single-backend pick→`select_backend()`. `[VERIFIED: release_awaiting_cloud.py:127-173]`

### Verification hooks

- A test that runs a reconcile decrement concurrently with a drain snapshot must show `sum(in_flight) ≤ sum(cap)` always (never overshoot).
- A test that two overlapping drain ticks against the same backend never exceed `cap` (generalizes the existing `test_overlapping_ticks_never_exceed_window`). `[VERIFIED: tests/analyze/core/test_staging_cron.py:390,474]`

---

## Novel Mechanism 2: Staleness "Waited-Since" Signal (D-01/D-02/D-03)

**Confidence: HIGH for the recommendation; the one residual risk is enumerated.** `[VERIFIED: codebase grep of all FileRecord.state writers]`

### Recommendation: use `FileRecord.updated_at` — ZERO migration

`FileRecord` uses `TimestampMixin`: `updated_at = mapped_column(server_default=func.now(), onupdate=func.now())`. `[VERIFIED: models/base.py:27-28]` `onupdate=func.now()` re-stamps `updated_at` on every UPDATE to the row.

**The key finding (verified by grepping every FileRecord state writer):** a file *parked* in `AWAITING_CLOUD` is touched by **no writer** until the drain flips it to `PUSHING`. The complete set of state transitions around `AWAITING_CLOUD`:

| Transition | Writer | Effect on `updated_at` |
|------------|--------|------------------------|
| → `AWAITING_CLOUD` (routing) | `routers/pipeline.py:339` (`_route_discovered_by_duration`) | stamps entry time |
| `AWAITING_CLOUD` → `PUSHING` (dispatch) | `services/backends.py:248,312` (`dispatch`) | leaves AWAITING_CLOUD |
| (fail-back, Phase 69) `PUSHING`/in-flight → `AWAITING_CLOUD` | reconcile (new Phase-69 behavior) | re-stamps = fresh wait clock |

`[VERIFIED: grep "\.state = FileState\|values(state=" across src/phaze — no writer UPDATEs a row while it stays in AWAITING_CLOUD]`

So for a file waiting in `AWAITING_CLOUD`, `updated_at` **equals the moment it entered that state** (the row is otherwise untouched while parked). `now() - updated_at` is exactly the "how long has it waited this stint" the staleness guard needs.

**Behavior on fail-back is desirable:** when a Phase-69 cloud failure returns a file to `AWAITING_CLOUD`, that UPDATE re-stamps `updated_at`, resetting the staleness clock. This means each waiting stint gets a fresh threshold before local becomes eligible — the correct semantics (a file that just failed back off a busy cloud shouldn't instantly dump to slow local; the attempt-exhaustion path (Q3) handles the "cloud is failing" case separately). Consistent with D-06 statelessness.

### The one residual risk (must be a plan-time verification task)

If any *future or overlooked* writer UPDATEs a FileRecord while it sits in `AWAITING_CLOUD` (e.g., a metadata backfill, a relationship write that dirties the row), `onupdate=func.now()` would move `updated_at` and spuriously reset the staleness clock. The grep above found none today, but the coupling is implicit. **Mitigation options for the planner:**
- **(Recommended, zero-cost):** ship `updated_at` + a targeted test asserting no non-drain writer touches an `AWAITING_CLOUD` row, plus a comment documenting the coupling.
- **(Fallback if the coupling is judged too fragile):** add a nullable `awaiting_since` timestamp column to `FileRecord`, stamped in `_route_discovered_by_duration` and on fail-back, `NULL`ed on dispatch. **Cost: one additive migration (030)** — flag explicitly. This milestone has been migration-careful (Phase 68 added 029); prefer the zero-migration `updated_at` path unless verification surfaces a spurious writer.

### Candidates evaluated and rejected

| Candidate | Verdict |
|-----------|---------|
| `FileRecord.updated_at` | **RECOMMENDED** — stable while parked, zero migration |
| `FileRecord.created_at` | ✗ — the file's discovery time, not its AWAITING_CLOUD entry (could be days earlier) |
| `cloud_job.created_at`/`updated_at` | ✗ — a never-dispatched file has **no** `cloud_job` row (written only at dispatch). Can't carry the first-wait signal. `[VERIFIED: cloud_job written in dispatch(), backends.py:249-262/313]` |
| `SchedulingLedger.enqueued_at` | ✗ — the original `process_file` enqueue time (from D-09 backfill), not the AWAITING_CLOUD entry |
| New `awaiting_since` column | Fallback only — costs migration 030 |

---

## Novel Mechanism 3: Black-Hole / Attempt-Counter Mechanics (D-04, SCHED-03)

**Confidence: HIGH on the mechanism; the "per-backend vs total-cloud" reading is flagged ASSUMED for discuss confirmation.** `[VERIFIED: cloud_job.attempts at models/cloud_job.py:87; cap compare at reconcile_cloud_jobs.py:164]`

### The three interacting counters D-04 requires

1. **(a) per-backend dispatch attempt bound** (reuse `cloud_submit_max_attempts`) — stops cloud/Kueue thrash.
2. **(b) fall-to-local** once cloud/Kueue attempts exhaust.
3. **(c) global total-attempt ceiling** → terminal `ANALYSIS_FAILED`.

### The A↔B thrash-break (the crux question)

**Question:** with stateless re-rank (D-06, no per-file memory), once a failed file is back in `AWAITING_CLOUD`, the next tick re-picks lowest-rank-available — which is cloud again. What stops the infinite A↔B loop?

**Answer: the persistent `cloud_job.attempts` counter, surfaced as an eligibility filter.** `cloud_job.attempts` is an `Integer` column (`server_default="0"`) that survives across ticks on the file's `cloud_job` row (the `on_conflict_do_update` in `dispatch` keeps the row id, so the counter persists). `[VERIFIED: cloud_job.py:87; backends.py:257-261 on_conflict keeps row]`. The mechanism:

- **On each non-local dispatch attempt** (or on each fail-back — see "increment site" below), increment `cloud_job.attempts`.
- **In `select_backend`**, a candidate whose `cloud_job.attempts >= cloud_submit_max_attempts` is filtered **out of the cloud/Kueue eligible set** — only `LocalBackend` remains eligible (step 2 of Pattern 2). This deterministically routes the file to local after N cloud attempts.
- **This breaks the thrash while preserving D-06:** the exclusion is derived from a *counter*, not from a remembered `last_failed_backend_id`. The file has no backend memory — it just has a count that says "cloud budget spent, go local." Statelessness (no per-file failure memory of *which* backend) is honored.

### Local is the guaranteed safety net + the global ceiling

- Once forced to local, `LocalBackend.dispatch` enqueues `process_file` on the fileserver (slow full analysis). Local writes **no** `cloud_job` row and does **not** increment the cloud counter. `[VERIFIED: backends.py:189-204 LocalBackend.dispatch — no cloud_job write]`
- **`ANALYSIS_FAILED` only when local itself fails:** local failure flows through the existing `process_file` → `report_analysis_failed` → `ANALYSIS_FAILED` path (`routers/agent_analysis.py:316`). `[VERIFIED]`
- **Global ceiling backstop (c):** a genuinely-processable file never hard-fails from cloud flakiness (it falls to local). The global total-attempt ceiling is the *pathological-loop* terminator — e.g., a file that somehow keeps failing everywhere. The simplest form that satisfies the success criterion "bounded total attempts → ANALYSIS_FAILED, never infinite thrash": once `cloud_job.attempts` exhausts the cloud budget AND local also fails to terminalize the file after its own bounded retries, mark `ANALYSIS_FAILED`. Because local failure already → `ANALYSIS_FAILED`, the concrete ceiling reduces to: **cloud budget spent → local → local's own failure → ANALYSIS_FAILED.** No *separate* global counter column is strictly required if local failure is the terminal path; a distinct global ceiling only adds value as defense against a local-dispatch-hold loop (no fileserver agent), which today is a clean hold, not a failure. **Recommend: do not add a separate global-ceiling column; rely on cloud-budget-exhaustion → local → existing local-failure terminalization.** Flag as ASSUMED for discuss (the alternative is a new `total_attempts` column = migration).

### Where each counter is persisted

| Counter | Storage | Migration? |
|---------|---------|-----------|
| Cloud/Kueue attempt bound (a) | Existing `cloud_job.attempts` | **None** — column exists |
| Fall-to-local trigger (b) | Derived: `cloud_job.attempts >= cfg.cloud_submit_max_attempts` in `select_backend` | None |
| Global ceiling → ANALYSIS_FAILED (c) | Existing local-failure path (`report_analysis_failed`) | None (recommended) |

### The increment site + compute uniformity (the real wiring work)

Today `cloud_job.attempts` is incremented **only** in `reconcile_cloud_jobs._handle_no_callback_terminal` on a Kueue Failed/Evicted re-drive. `[VERIFIED: reconcile_cloud_jobs.py:181]`. Phase 69 changes two things:

1. **At-cap behavior flips from ANALYSIS_FAILED to spill-back.** Currently at cap → `ANALYSIS_FAILED` (`reconcile_cloud_jobs.py:164-174`). Phase 69: at the *cloud* cap, terminalize the `cloud_job` (FAILED, decrement in-flight) and set the file back to `AWAITING_CLOUD` (not ANALYSIS_FAILED). The next tick's `select_backend` sees `attempts >= cap` → routes to local. `[VERIFIED: current terminal at reconcile_cloud_jobs.py:168]`
2. **Compute failures must also count + spill.** Compute terminalization is the `/pushed` callback (`ComputeAgentBackend.reconcile` is a no-op). `[VERIFIED: backends.py:271-273]`. For SCHED-03 uniformity, a compute push failure / analysis failure must also (i) increment `cloud_job.attempts` and (ii) return the file to `AWAITING_CLOUD` for spillover (rather than the current `ANALYSIS_FAILED` at `routers/agent_push.py:186` / `agent_s3.py:183`). This is the least-developed path and the planner should treat it as a distinct task: **wire compute failure → attempt increment + AWAITING_CLOUD return**, mirroring the Kueue reconcile spill.

### FLAGGED for discuss: "per-backend" vs "total-cloud" attempt bound

D-04 says "**per-backend** dispatch attempts." `cloud_job.attempts` is a **single per-file counter** (one `cloud_job` row per file, unique `file_id`). With a file that can try backend A then backend B, this counter is really a **total non-local attempt budget**, not a literal per-(file,backend) count. Two readings:

- **Reading 1 (RECOMMENDED, zero migration):** `cloud_job.attempts` = total cloud/Kueue attempt budget. After N total cloud attempts (across whichever backends), fall to local. Satisfies every success criterion (bounded, no infinite thrash, local safety net). Slightly looser than literal "per-backend."
- **Reading 2 (literal per-backend, costs state):** attempts-per-(file,backend) — needs either a new `attempts_by_backend` JSONB column on `cloud_job` (migration) or one-row-per-(file,backend) restructure. **The one-row-per-(file,backend) question is EXPLICITLY flagged as a Phase 70 research item** (ROADMAP Phase 70 Research: "(a) `cloud_job` one-row-per-file (mutate `backend_id` in place) vs. one-row-per-(file,backend) for attempt-scoping"). Adopting Reading 2 in Phase 69 would pull a Phase-70 decision forward.

**Recommendation:** ship Reading 1 in Phase 69 (total-cloud budget, reuse `cloud_job.attempts`, zero migration), keeping the per-(file,backend) refinement deferred to Phase 70 where it is already scoped. **The planner/discuss must confirm this reading** — it is the one place Phase 69 pragmatically diverges from D-04's literal wording. Logged in Assumptions.

---

## Secondary Mechanism 4: Per-Candidate Rank-First Selection (SCHED-01/04)

**Confidence: HIGH.** Covered by Pattern 2 + the snapshot pattern above. Algorithm confirmed feasible against `resolve_backends()` (returns `list[Backend]` with `id`/`rank`/`cap` + async `is_available()`/`in_flight_count()`). `[VERIFIED: backends.py:361-384,109-141]`

**Efficiency concern is real and resolved:** `is_available()` for Kueue is a network cluster probe. Per-candidate-per-backend probing (N×M) inside the locked transaction would be a probe storm holding the advisory lock. **Snapshot once per tick** (M probes), then decrement `remaining[]` locally per claim. The tie-break `in_flight/cap` reads the snapshot's counts, so equal-rank ties are resolved deterministically and consistently for the whole tick. `[VERIFIED: backends.py:294 network probe]`

**Resolve-backends note:** `resolve_backends()` currently *raises* on >1 non-local backend (the Phase-68 boot guard, `backends.py:378-384`). Phase 69 **removes that guard** — supporting N non-local backends is precisely this phase's job. This is a required, explicit change (also `resolved_non_local_kind` at `backends.py:387-406` and `config._single_non_local` become obsolete for the drain path). `[VERIFIED: backends.py:378-384 ValueError "multi-backend dispatch lands in Phase 69 (SCHED)"]`

---

## Secondary Mechanism 5: SCHED-05 Single-Recovery-Owner

**Confidence: HIGH.** `[VERIFIED: reenqueue.py:190-199,327-340; reconcile_cloud_jobs.py:295-300; backends.py:316-358]`

### The two recovery mechanisms today

| Mechanism | Owns | Scope |
|-----------|------|-------|
| `reconcile_cloud_jobs` (cron) | Kueue `cloud_job` lifecycle (SUBMITTED/RUNNING → terminal/re-drive) | **Global** today — `SELECT cloud_job WHERE status IN (SUBMITTED,RUNNING)`, NOT backend_id-scoped `[VERIFIED: reconcile_cloud_jobs.py:297]` |
| `recover_orphaned_work` (ledger) | Orphaned ledger rows; for held AWAITING_CLOUD files: `process_file` rows → compute agent, `push_file` rows → fileserver | `_get_awaiting_cloud_ids` + held-row partitioning `[VERIFIED: reenqueue.py:335-340]` |

`KueueBackend.reconcile` (Phase 68) is **already** `backend_id`-scoped (`CloudJob.backend_id == self.id`, `backends.py:337`). `ComputeAgentBackend.reconcile` and `LocalBackend.reconcile` are no-ops (compute terminalizes via `/pushed` callback). `[VERIFIED]`

### The double-owner danger (the incident class to not replay)

After Phase 68's BACK-03, **compute pushes now write a `cloud_job` row** (`backend_id` set, `status=SUBMITTED`). `[VERIFIED: backends.py:249-262]`. So a compute file now has BOTH:
- (a) an in-flight `cloud_job` row, AND
- (b) a `process_file`/`push_file` ledger row.

If Phase 69 makes reconcile iterate **all** backends' cloud_job rows to spill failures, a compute file's `cloud_job` would be owned by reconcile *and* its ledger row recovered by `recover_orphaned_work` → **two owners = the 44.5k over-enqueue class.**

### The resolution (plan-ready)

1. **Reconcile becomes per-backend dispatch:** replace the monolithic global loop with `for b in resolve_backends(cfg): await b.reconcile(session, ctx)`. Kueue does real work (scoped `backend_id == self.id`); Compute/Local are no-ops. So compute `cloud_job` rows are **only** touched by the `/pushed` callback — single owner per kind. `[VERIFIED: backends.py:316,271]`
2. **Extend the ledger orphan-exclusion (the AST guard):** in `recover_orphaned_work`, a `process_file`/`push_file` ledger row for a file that currently has an **in-flight `cloud_job` row** (status in `{UPLOADING,UPLOADED,SUBMITTED,RUNNING}`, any backend_id) must be **excluded** from the orphan set — the backend's own reconcile/callback owns it. Add an `in_flight_cloud_job_file_ids` set (one query, mirroring `_get_awaiting_cloud_ids` at `reenqueue.py:190-199`) and exclude those rows in the `orphaned = [...]` comprehension (`reenqueue.py:318`). This is the `backend_id`-aware extension of the existing `is_domain_completed` guard.
3. **Keep the AWAITING_CLOUD held-file path** (`reenqueue.py:335`) for files with **no** in-flight cloud_job (genuinely orphaned, awaiting re-drain) — those correctly re-route to the drain via the release cron.

### Verification hooks

- A compute file with an in-flight `cloud_job` row + a `process_file` ledger row must be recovered by **exactly one** path (assert `recover_orphaned_work` skips it; the callback/reconcile owns it). Extends `tests/analyze/tasks/test_recovery.py`.
- Reconcile must not touch a compute `cloud_job` row (assert `ComputeAgentBackend.reconcile` no-ops it).

---

## Runtime State Inventory

> Phase 69 is behavior-changing but **not** a rename/refactor/migration phase in the string-replacement sense. Included for completeness because it changes reconcile terminal semantics + adds a config knob.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `cloud_job.attempts` (existing column) reused as the cloud attempt budget; `cloud_job.backend_id` (migration 029, Phase 68) now consulted for per-backend counts | Code edit only — no data migration. Existing rows have `attempts` default 0 / `backend_id` per Phase-68 backfill. |
| Live service config | `backends.toml` registry (Phase 67) — `rank`/`cap` per entry now actively enforced (previously read but single-dispatch-path). Operators with a multi-backend `backends.toml` see behavior change on deploy. | Documented in Phase 71 (BEUI-03 runbook); no data change. |
| OS-registered state | None — the `*/5` crons (`stage_cloud_window`, `reconcile_cloud_jobs`) are SAQ cron registrations in `phaze.tasks.controller`, unchanged in identity. | None — verified: cron registration is by function name, not renamed. |
| Secrets/env vars | New config knob `cloud_spill_to_local_after_seconds` (D-02) via `PHAZE_*` alias. No secret. | Code edit (config.py) + docs. |
| Build artifacts | None. | None. |

**Migration cost:** **ZERO new migrations** on the recommended path (Q2 `updated_at` + Q3 Reading 1 both reuse existing columns). If discuss chooses Q2-fallback (`awaiting_since`) or Q3-Reading-2 (`attempts_by_backend`), that adds migration 030 — flagged.

## Common Pitfalls

### Pitfall 1: Probe storm under the advisory lock
**What goes wrong:** calling `is_available()`/`in_flight_count()` per-candidate-per-backend holds the advisory lock through N×M network probes, starving all other ticks.
**How to avoid:** snapshot once per tick (M probes), decrement `remaining[]` locally.
**Warning signs:** tick latency scales with candidate count; Kueue probe called >M times per tick.

### Pitfall 2: Whole-tick advisory lock on reconcile
**What goes wrong:** wrapping reconcile in one `pg_advisory_xact_lock` breaks per-row commits (the lock releases on the first commit) → either the lock silently vanishes mid-tick or you must collapse to one commit, destroying the delete-after-record ordering (D-04) and per-row isolation.
**How to avoid:** take the lock **per-row** (after each commit re-acquire at the top of the next iteration).
**Warning signs:** delete-after-record ordering removed; reconcile tally shows partial-row rollbacks aborting the whole tick.

### Pitfall 3: Cloud-failed file marked ANALYSIS_FAILED directly
**What goes wrong:** keeping the current at-cap `ANALYSIS_FAILED` behavior hard-fails a processable file just because cloud was flaky — violates D-04 ("local is the guaranteed safety net").
**How to avoid:** at the cloud cap, spill the file back to `AWAITING_CLOUD` (decrement in-flight, increment attempts); `select_backend` routes it to local next tick. `ANALYSIS_FAILED` only from local failure or the global ceiling.
**Warning signs:** files reach `ANALYSIS_FAILED` while local backend is online and idle.

### Pitfall 4: Staleness clock reset by an unrelated writer
**What goes wrong:** an overlooked writer UPDATEs a parked `AWAITING_CLOUD` FileRecord → `onupdate=func.now()` resets `updated_at` → the file never reaches the staleness threshold and never spills to local.
**How to avoid:** ship the "no non-drain writer touches AWAITING_CLOUD rows" test; if a spurious writer exists, use the `awaiting_since` column fallback.
**Warning signs:** a file oscillates in AWAITING_CLOUD indefinitely with cloud full; `updated_at` moving without a state change.

### Pitfall 5: A raise escaping the tick
**What goes wrong:** any new selection/spill/black-hole path raising out of the `*/5` cron (violates "cron never raises").
**How to avoid:** `select_backend` returns `None` (hold) rather than raising; wrap dispatch per-candidate (the drain already catches `NoActiveAgentError` per file, `release_awaiting_cloud.py:162-168`); reconcile keeps its per-row rollback guard.
**Warning signs:** cron logs show tracebacks instead of `{staged, skipped}` / hold no-ops.

## Code Examples

### Reconcile per-row lock acquisition (Q1 wiring)
```python
# Source: proposed change to reconcile_cloud_jobs.py loop (mirrors release_awaiting_cloud.py:114)
for cloud_job_id in cloud_job_ids:
    try:
        # Q1: share the drain's lock discipline — mutually exclude this row-mutation from a drain tick.
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                              {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
        cloud_job = await session.get(CloudJob, cloud_job_id)
        if cloud_job is None:
            continue
        tally["reconciled"] += 1
        await _reconcile_one(ctx, session, cloud_job, cap, tally)  # commits per row → releases the xact lock
    except Exception:
        await session.rollback()
        logger.warning("reconcile row failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)
```

### Once-per-tick snapshot in the drain (Q1/Q4 wiring)
```python
# Source: proposed change to stage_cloud_window (release_awaiting_cloud.py), inside the locked txn.
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                      {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
backends = resolve_backends(cfg)  # Phase 69: no longer raises on >1 non-local
snapshot = {}
for b in backends:
    snapshot[b.id] = {
        "backend": b,
        "available": await b.is_available(session),   # M probes, once per tick
        "remaining": b.cap - await b.in_flight_count(session),
        "cap": b.cap,
    }
non_local_slots = sum(max(0, s["remaining"]) for bid, s in snapshot.items()
                      if s["backend"].rank != LOCAL_RANK and s["available"])
# candidates capped by total non-local capacity + (staleness-eligible) local capacity
candidates = await get_cloud_staging_candidates(session, non_local_slots + local_headroom)
for file in candidates:
    b = select_backend(file, snapshot, saq_now(), cfg)  # pure, in-memory
    if b is None:
        continue  # clean hold
    await b.dispatch(file, session, task_router)
    snapshot[b.id]["remaining"] -= 1
await session.commit()  # single commit; releases advisory lock + row locks
```

## State of the Art

| Old Approach (pre-69) | Current Approach (Phase 69) | Impact |
|-----------------------|------------------------------|--------|
| Single non-local backend, `resolve_backends()` raises on >1 | N backends, rank-first per-candidate selection | The moment >1 backend runs simultaneously |
| Window = global `COUNT(state IN {PUSHING,PUSHED})` | Per-backend `in_flight_count()` (cloud_job by backend_id) | Cap is per-backend; global window retired (D-05) |
| At-cap → `ANALYSIS_FAILED` (no fallback) | At cloud-cap → spill to local; ANALYSIS_FAILED only on local failure/global ceiling | Local is the guaranteed safety net (D-04) |
| Reconcile global, no lock | Reconcile per-backend, shares advisory lock per-row | SCHED-02/05 correctness |
| No staleness logic (design §4.3 default) | Staleness guard on full→local (D-01, overrides §4.3) | Long files don't dump to slow local on a blip |

**Deprecated/obsolete this phase:**
- `resolve_backends()`'s >1-non-local boot guard (`backends.py:378-384`) — remove.
- `resolved_non_local_kind` / `config._single_non_local` for the drain path — obsolete once the drain iterates all backends.
- `get_cloud_window_count()` (`pipeline.py:1243`) — retire in favor of per-backend counts.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | "per-backend dispatch attempts" (D-04) is acceptably implemented as a **total-cloud** budget via the single `cloud_job.attempts` counter (Reading 1), deferring literal per-(file,backend) scoping to Phase 70 | Q3 | If discuss requires literal per-backend counts now, Phase 69 needs a new `attempts_by_backend` column (migration 030) or the Phase-70 one-row-per-(file,backend) restructure pulled forward |
| A2 | No separate global-ceiling counter column is needed; the global ceiling reduces to "cloud budget spent → local → local-failure → ANALYSIS_FAILED" | Q3 (c) | If a distinct numeric global ceiling is required (e.g., to bound a no-fileserver local-hold loop), a `total_attempts` column (migration) is needed |
| A3 | `FileRecord.updated_at` is a reliable "waited-since" signal because no writer touches a parked AWAITING_CLOUD row | Q2 | If a future/overlooked writer dirties AWAITING_CLOUD rows, staleness clock resets spuriously → fallback `awaiting_since` column (migration 030) |
| A4 | The staleness guard applies only to the local (rank-99) spill target; higher-rank backends are never staleness-gated | Q3/Pattern 2 | If operators want inter-cloud staleness, the gate needs generalizing (out of D-01 scope) |
| A5 | Reconcile-only-decrements holds for all Phase-69 reconcile outcomes (including the new spill-back), so the single drain lock is sufficient for cap-correctness | Q1 | If a future reconcile path ever *claims* an in-flight slot, the overshoot proof breaks and reconcile would need whole-tick lock semantics |

## Open Questions (RESOLVED)

1. **Per-backend vs total-cloud attempt bound (A1).**
   - What we know: `cloud_job.attempts` is one counter per file; reusing it = total-cloud budget, zero migration.
   - What's unclear: whether D-04's "per-backend" is literal (needs new state) or means "bounded cloud attempts."
   - **RESOLVED (confirmed at `/gsd:plan-phase 69`):** ship total-cloud (Reading 1) via `cloud_job.attempts`; literal per-(file,backend) scoping deferred to Phase 70 (already scoped there).

2. **Global ceiling shape (A2).**
   - What we know: local failure already → ANALYSIS_FAILED.
   - What's unclear: whether a distinct numeric global ceiling is wanted beyond "cloud-exhausted → local → local-failure."
   - **RESOLVED (confirmed at `/gsd:plan-phase 69`):** rely on the local-failure terminal; no distinct numeric global-ceiling state added.

3. **Staleness config name/default (D-02, Claude's discretion).**
   - **RESOLVED:** `cloud_spill_to_local_after_seconds`, default 900 (15 min), bounded `gt=0, lt=86400` (mirror `cloud_route_threshold_sec` at config.py:562).

4. **`LOCAL_RANK` sentinel.** The design assumes local is rank 99. Confirm the registry convention (is 99 enforced, or just conventional?) so `select_backend`'s local-detection uses `isinstance(b, LocalBackend)` (robust) rather than a magic rank number. **RESOLVED:** detect local by type (`isinstance(LocalBackend)`), not by `rank == 99`.

## Environment Availability

> Phase 69 is a control-plane code + config change. No new external tools.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL 16 (advisory locks, cloud_job) | SCHED-02 count-and-claim | ✓ (project constraint) | 16+ | — |
| Ephemeral test PG (port 5433) / Redis (6380) | `just integration-test` | ✓ | per justfile | — |
| Kueue cluster (for `KueueBackend.is_available` live probe) | Live multi-backend E2E only | ✗ in CI (faked via `tests/kube_fakes`) | — | Fake seam (`fake_local_queue`) covers unit/integration; live E2E deferred to Phase 70 (deployment-gated, per Phase-68 precedent) |

**Missing with fallback:** live Kueue/compute backends — unit + integration tests use the existing fake seams (`DedupFakeQueue`, `DedupFakeTaskRouter`, `fake_local_queue`, `seed_active_agent`). Live multi-backend verification is genuinely deployment-gated and belongs to Phase 70 (matches the Phase-68 pattern of deferring live E2E). `[VERIFIED: tests/analyze/core/test_staging_cron.py imports the fakes]`

## Validation Architecture

> `workflow.nyquist_validation: true` in `.planning/config.json` — this section is required. `[VERIFIED: .planning/config.json]`

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio (async), `uv run` prefix mandatory (CLAUDE.md) |
| Config file | `pyproject.toml` (`[tool.pytest]`) + `tests/conftest.py` |
| Quick run command | `uv run pytest tests/analyze/core/test_staging_cron.py -x` |
| Bucket run command | `just test-bucket analyze` (parallel-CI partition; drain/reconcile/backends live in the **analyze** bucket) |
| Full suite command | `just integration-test` (ephemeral PG 5433 + Redis 6380; baseline 2566 passed, 96.89% cov) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SCHED-01 | Rank-first eligible per-candidate; full top rank spills to next | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -x` | ❌ Wave 0 (new pure-function test) |
| SCHED-01 | Drain dispatches across N backends in one tick | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -x` | ✅ (extend — currently single-backend) |
| SCHED-02 | Overlapping drain ticks never overshoot per-backend cap | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k overshoot -x` | ✅ (extend `test_overlapping_ticks_never_exceed_window` to per-backend) |
| SCHED-02 | Reconcile decrement concurrent with drain snapshot stays cap-safe | integration | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k cap_safe -x` | ❌ Wave 0 |
| SCHED-03 | Cloud-failed file returns to AWAITING_CLOUD (not ANALYSIS_FAILED) under ceiling | integration | `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py -k spill_back -x` | ✅ (modify — current asserts ANALYSIS_FAILED at cap) |
| SCHED-03 | Attempt-exhausted file falls to local; no A↔B thrash | unit + integration | `uv run pytest tests/analyze/services/test_backend_selection.py -k attempt -x` | ❌ Wave 0 |
| SCHED-03 | Staleness: full→local gated by threshold; offline→local immediate | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -k stale -x` | ❌ Wave 0 |
| SCHED-04 | Equal-rank tie-break by utilization then id | unit | `uv run pytest tests/analyze/services/test_backend_selection.py -k tiebreak -x` | ❌ Wave 0 |
| SCHED-05 | Compute file with in-flight cloud_job recovered by exactly one path | integration | `uv run pytest tests/analyze/tasks/test_recovery.py -k single_owner -x` | ✅ (extend) |
| SCHED-05 | Reconcile is backend_id-scoped; compute rows untouched by kueue reconcile | integration | `uv run pytest tests/analyze/services/test_backends.py -k reconcile_scope -x` | ✅ (extend) |
| — | No non-drain writer touches AWAITING_CLOUD rows (guards `updated_at` staleness signal) | integration | `uv run pytest tests/analyze/core/test_staging_cron.py -k awaiting_untouched -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/analyze/services/test_backend_selection.py tests/analyze/core/test_staging_cron.py -x` (fast; pure selection + drain)
- **Per wave merge:** `just test-bucket analyze` (full analyze partition; includes reconcile + recovery + backends)
- **Phase gate:** `just integration-test` green (2566+ baseline, no test lost) before `/gsd:verify-work`; 85% coverage floor (CLAUDE.md).

### Wave 0 Gaps
- [ ] `tests/analyze/services/test_backend_selection.py` — the new pure `select_backend` unit suite (rank-first, staleness full/offline, attempt-exclusion, tie-break) — covers SCHED-01/03/04
- [ ] `tests/analyze/core/test_staging_cron.py` — add multi-backend drain + per-backend overshoot + `awaiting_untouched` cases (extend existing)
- [ ] `tests/analyze/tasks/test_reconcile_cloud_jobs.py` — add cap-safe-under-concurrent-drain + spill-back-not-ANALYSIS_FAILED (modify existing at-cap assertion)
- [ ] `tests/analyze/tasks/test_recovery.py` — single-owner assertion for a compute file with an in-flight cloud_job (extend)
- [ ] Config field `cloud_spill_to_local_after_seconds` default/bounds test in `tests/shared/config/` (mirror `test` for `cloud_route_threshold_sec`)

## Security Domain

> `security_enforcement` absent in config → treated as enabled. Phase 69 is an internal control-plane scheduler with no new external-input surface.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth surface (internal cron; agent HTTP surface untouched, design §5) |
| V3 Session Management | no | — |
| V4 Access Control | no | Control/agent DB boundary preserved (DIST-01); scheduler is control-only |
| V5 Input Validation | yes (config only) | The one new knob is a bounded pydantic `int` field (`gt=0, lt=86400`) — same fail-fast pattern as existing cloud knobs |
| V6 Cryptography | no | No crypto; presigned-URL leg untouched (Phase 70 territory) |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via backend/config values | Tampering | ORM + bound params + `pg_advisory_xact_lock(:key)` bound literal; no f-string SQL (existing T-49-02/T-50 discipline) |
| Resource exhaustion (over-dispatch beyond cap) | DoS | Per-backend cap count-and-claim under advisory lock (SCHED-02); the historical over-stage-DoS guard generalized |
| Config-driven unbounded retry storm | DoS | `cloud_submit_max_attempts` bounded `gt=0, lt=20`; staleness knob bounded; no unbounded loop (global ceiling → ANALYSIS_FAILED) |
| Secret leakage in logs | Info disclosure | Backends log only `{id, kind, rank, cap}` (Phase-68 T-68-04 discipline); never `SecretStr`/tokens |

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively**; **`uv` only** — every command `uv run …`; never bare `pip`/`python`/`pytest`/`mypy`.
- **Ruff** (line 150, `py313` target intentionally): enabled sets include `S` (bandit), `PTH`, `SIM`, `TCH`, `B`, `ARG`. New pure `select_backend` must satisfy `ARG`/`SIM`; keep type-only imports under `TYPE_CHECKING` per the codebase idiom.
- **Mypy strict** (`disallow_untyped_defs`, `warn_unreachable`, etc.); tests excluded. All new functions fully typed (`Backend | None`, snapshot `dict[str, ...]`).
- **85% coverage floor**, Codecov with the `analyze` flag.
- **Pre-commit frozen SHAs**, all hooks pass; **never `--no-verify`** (including parallel executor agents).
- **PR per phase** on an own worktree branch — never a direct commit to `main`.
- **Workflows delegate to `just`**; keep `justfile`/`scripts/update-project.sh`/READMEs current with any new module.
- **Frequent commits** during execution, not batched at the end.

## Sources

### Primary (HIGH confidence — codebase, this session)
- `src/phaze/tasks/release_awaiting_cloud.py` — drain tick, advisory lock `5_000_504`, single-commit boundary, cron no-op discipline
- `src/phaze/services/backends.py` — `Backend` protocol, `resolve_backends()`, `in_flight_count()`, `is_available()` (Kueue network probe), `dispatch`, `KueueBackend.reconcile` (backend_id-scoped)
- `src/phaze/tasks/reconcile_cloud_jobs.py` — per-row commit + delete-after-record ordering, `cloud_job.attempts` cap compare, at-cap ANALYSIS_FAILED (to change)
- `src/phaze/tasks/reenqueue.py` — ledger recovery, `_get_awaiting_cloud_ids`, `is_domain_completed`/`_natural_id` AST guard, held-row partitioning
- `src/phaze/services/pipeline.py` — `get_cloud_window_count` (retire), `get_cloud_staging_candidates` (FIFO claim)
- `src/phaze/models/cloud_job.py` — `attempts`, `backend_id` (nullable, migration 029), status enum + in-flight set
- `src/phaze/models/file.py` + `models/base.py` — FileState (AWAITING_CLOUD/PUSHING/PUSHED/ANALYSIS_FAILED), `TimestampMixin.updated_at onupdate=func.now()`
- `src/phaze/routers/pipeline.py:278` — `_route_discovered_by_duration` (AWAITING_CLOUD entry point; untouched, design §5)
- `src/phaze/config.py` — `cloud_submit_max_attempts` (reuse), retired flat cloud fields (Phase 67)
- grep of all `FileRecord.state` writers — confirms no writer touches a parked AWAITING_CLOUD row
- `.planning/phases/69-CONTEXT.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md` (Phase 69 + Phase 70 research flags), `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §4.3–4.5/§5/§6
- `.planning/config.json` (nyquist_validation true), `tests/BUCKETS.md`/`tests/buckets.json` (analyze bucket), `tests/analyze/core/test_staging_cron.py` (drain test patterns + fake seams)

### Secondary (design intent)
- Phase 67-CONTEXT / 68-CONTEXT (registry rank/cap; uniform in_flight_count; compute writes+terminalizes a cloud_job; retained accessors)

### Tertiary
- None — no WebSearch needed; the phase is fully grounded in the codebase (zero new dependencies).

## Metadata

**Confidence breakdown:**
- Lock scope (Q1): HIGH — reconcile-only-decrements proof is mechanical; single-lock sufficiency verified against the drain's existing single-transaction shape.
- Staleness signal (Q2): HIGH — `updated_at` verified stable via exhaustive state-writer grep; one enumerated residual risk with a clear fallback.
- Black-hole (Q3): HIGH on mechanism; the per-backend-vs-total reading is an explicit ASSUMED decision (A1) for discuss, cleanly deferrable to the already-scoped Phase-70 question.
- Selection + tie-break (Q4): HIGH — pure function over existing protocol; snapshot pattern resolves the efficiency concern.
- SCHED-05 (Q5): HIGH — the double-owner vector and its `backend_id`-aware fix are concrete and testable.

**Research date:** 2026-07-04
**Valid until:** ~2026-08-04 (stable; internal-codebase-grounded — invalidated only by intervening changes to `backends.py` / `reconcile_cloud_jobs.py` / the `cloud_job` schema).
