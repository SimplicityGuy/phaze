# Phase 83: Cloud-Routing Sidecar Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-09
**Phase:** 83-cloud-routing-sidecar-cutover
**Areas discussed:** Awaiting-row lifecycle, Drain re-pick hazard, `/upload-failed` CAS, PUSHING/PUSHED derivation, Scope boundary (added mid-discussion)

---

## Area selection

All four presented gray areas were selected. Areas were then taken in **dependency order** (awaiting-row lifecycle before the drain query), since the drain's correctness depends on what the `awaiting` row means and when it disappears.

**Pre-discussion finding surfaced by the codebase scout:** there is no go-forward writer of `cloud_job.status='awaiting'` — `routers/pipeline.py:346` holds a long file with a bare `file.state = AWAITING_CLOUD` and never imports `CloudJob`. Only migration `032`'s backfill ever wrote such a row. This means the *hard* shadow invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` (`shadow_compare.py:131`, `soft=False`) is violated by every file held since `032`, and that Phase 83 is not a pure reader cutover.

---

## Awaiting-row lifecycle

### Q1 — Where should the go-forward `cloud_job(status='awaiting')` INSERT live?

| Option | Description | Selected |
|--------|-------------|----------|
| Shared service helper | `hold_for_cloud(session, file)` stamping state + upserting the row in the caller's txn; reused by the hold path and both spill paths | |
| Inline in `trigger_analysis` | `pg_insert` upsert directly in the `is_long` branch; three hand-written copies | |
| Bulk upsert after the loop | One bulk statement before the existing `if held: commit()` | |
| You decide | Lock only that a shared go-forward writer must exist | ✓ |

**User's choice:** You decide.
**Notes:** Recorded as binding regardless of mechanism — one writer, shared by the hold path *and* both spill paths, not three copies. Shared helper recommended.

### Q2 — What does a spilled file's `cloud_job` row look like?

| Option | Description | Selected |
|--------|-------------|----------|
| Re-stamp to `awaiting` | `status='awaiting'`, keep `attempts` spent as the budget marker | |
| Keep `FAILED`, widen drain | Drain selects `status IN ('awaiting','failed')` | |
| Terminal `FAILED` + new awaiting row | Blocked by `uq_cloud_job_file_id` | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** Two options ruled out on constraints surfaced in the question itself — "keep FAILED, widen drain" breaks the hard shadow invariant; "new awaiting row" violates `uq_cloud_job_file_id` (one row per file, 77 D-04). Re-stamp recommended, with a check that `'awaiting' ∉ IN_FLIGHT` (`backends.py:76`).

### Q3 — How do already-held `AWAITING_CLOUD` files get their sidecar row?

| Option | Description | Selected |
|--------|-------------|----------|
| Repair migration `034` | Re-run `032`'s backfill with `ON CONFLICT DO NOTHING`; forces Phase 90 renumber `034→035` | |
| Idempotent upsert in the drain | Self-healing, but the drain becomes a writer of what it reads and keeps a `state` read | |
| Rely on quiesce + drain | `--profile drain` empties `PUSHING`/`uploading`, not the parked `AWAITING_CLOUD` set | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** Repair migration recommended. The drain-upsert option violates SC#1; the quiesce option is unsound because `AWAITING_CLOUD` is refilled by every `trigger_analysis` and Phase 83 lands long before Phase 90's rollout.

**Q4 not asked.** Whether the hold path keeps dual-writing `FileRecord.state` is already settled by 81 D-05 (writers dual-write; the write dies in Phase 90) and SC#1's restriction to *routing reads*. Carried forward rather than re-asked.

---

## Drain re-pick hazard

**Reframed before questioning.** `stage_cloud_window` already takes a fixed transaction-scoped advisory lock (`release_awaiting_cloud.py:135`, WR-04/SCHED-02) serializing overlapping ticks under one post-loop commit. So the hazard is **sequential-tick**, not concurrent: tick *N* dispatches locally, tick *N+1* finds the `awaiting` row still there.

### Q1 — What keeps a locally-dispatched file out of the next tick's candidate set?

| Option | Description | Selected |
|--------|-------------|----------|
| Predicate conjunct | `awaiting AND NOT inflight(analyze) AND NOT domain_completed(analyze)`; local stays a no-row writer | ✓ |
| Delete the row on dispatch | Mirrors compute/kueue upsert-promotion; fails on the rollback path | |
| Both — delete + conjunct | Defense in depth | |
| You decide | | |

**User's choice:** Predicate conjunct (recommended).
**Notes:** Decided on the rolled-back-tick argument specifically. The drain rolls back the whole tick on a poisoned txn (`:264`), while `process_file`'s ledger row is committed independently by the `before_enqueue` hook's own session — so a rolled-back tick leaves a queued job *plus* a restored `awaiting` row, and row deletion would let tick *N+1* re-pick a file with analysis in flight and dispatch it to a **cloud** backend. The conjunct survives this because the committed ledger row alone re-excludes the file. Phase 81's `domain_completed(analyze)` is what excludes a terminally-failed local analyze.

### Q2 — Which rows does `FOR UPDATE ... SKIP LOCKED` lock?

| Option | Description | Selected |
|--------|-------------|----------|
| Lock `cloud_job` | Lock the table the candidacy predicate lives on; preserves the post-lock EvalPlanQual re-check | ✓ |
| Lock both | Strictly safest; second row lock per candidate | |
| Lock `files` (status quo) | Minimal diff; candidacy column left on an unlocked table | |
| You decide | | |

**User's choice:** Lock `cloud_job` (recommended).
**Notes:** The tick advisory lock does not cover the callback routers (`/uploaded`, `/pushed`) or the reconcile cron, all of which mutate `cloud_job` concurrently — so a stale read of the deciding column is reachable without the re-check.

### Q3 — Where do the FIFO key and staleness clock read from?

| Option | Description | Selected |
|--------|-------------|----------|
| FIFO on `files`, clock on sidecar | Byte-identical FIFO; clock survives Phase 90 | |
| Both move to `cloud_job` | One table, but changes FIFO ordering for old-discovery/new-hold files | |
| Both stay on `files` | Zero behavior change now; hands Phase 90 a silent clock break | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** The Phase-90 clock hazard recorded as a **must-address** constraint: once the dual-written `state` disappears, nothing stamps `file.updated_at` at lane entry and `cloud_route_max_wait_sec` silently measures the wrong thing.

### Q4 — What proves SC#3 (no double-dispatch / re-pick window)?

| Option | Description | Selected |
|--------|-------------|----------|
| Hermetic now + live deferred | Mirrors 79 D-01/D-02 | |
| Hermetic only | Leaves the 200K query plan unmeasured | |
| Hermetic + EXPLAIN assertion | Catches the two-`EXISTS` perf regression; brittle across PG versions | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** ROADMAP designates this a *hard gate*. Hermetic-now + live-deferred recommended, driving two sequential ticks across three outcomes (local dispatch; rolled-back tick with a committed ledger row; terminally-failed local analyze). Must pass via `just test-bucket integration` in isolation.

---

## `/upload-failed` CAS

### Q1 — What is the CAS anchor?

| Option | Description | Selected |
|--------|-------------|----------|
| `cloud_job.status` | Sidecar-native; survives Phase 90; a `state` CAS is still a routing read | ✓ |
| `FileRecord.state == PUSHING` | Byte-for-byte symmetry with `report_push_mismatch` | |
| Both, sidecar gates | Belt-and-braces, as `report_uploaded` does today | |
| You decide | | |

**User's choice:** `cloud_job.status` (recommended).
**Notes:** CAS on `status IN ('uploading','uploaded')`; `rowcount == 0` → idempotent no-op; the `FileRecord` dual-write is gated behind that rowcount. Covers the named bug: an already-`ANALYZED` file's `cloud_job` reads `RUNNING`/`SUCCEEDED`, so the CAS matches 0 rows.

### Q2 — On `rowcount == 0`, what does the handler still do?

| Option | Description | Selected |
|--------|-------------|----------|
| Full no-op | Mirrors `report_push_mismatch`; no S3 ops, no ledger clear | ✓ |
| No-op writes, keep S3 cleanup | Could delete an object a `RUNNING` Kueue job is mid-download on | |
| No-op writes, clear ledger only | Clears a row whose upload may still be legitimately in flight | |
| You decide | | |

**User's choice:** Full no-op (recommended).
**Notes:** Verified during discussion that `_delete_staged_object_if_cloud` is called on **both** analyze-terminal paths — `put_analysis` (`agent_analysis.py:264`) and `report_analysis_failed` (`:381`) — so `/upload-failed` is not the last line of defense against an object leak, and the full no-op is the only variant that cannot destroy a live download.

### Q3 — Is the missing `pg_advisory_xact_lock` on the attempt RMW in scope?

| Option | Description | Selected |
|--------|-------------|----------|
| In scope — add it | SIDECAR-01 says "preserved or strengthened"; exact donor at `agent_push.py:240` | |
| Defer — own quick task | It is an attempt-counter race, not a CAS guard | |
| In scope, no new test | Ships an untested mitigation | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** Recommended **in scope, with the concurrency regression test**. The "add the lock, skip the test" variant explicitly ruled out. `/upload-failed`'s under-cap path calls `redrive_upload` → `stage_file_to_s3`, which enqueues on the same `s3_upload:<file_id>` key, so the `before_enqueue` hook upserts the same ledger row from its own session — the exact self-deadlock `/mismatch`'s advisory lock exists to avoid.

**Q4 not asked.** What counts as "already-advanced" in the SC#2 regression test is determined by the two locked answers: `cloud_job.status ∉ {uploading, uploaded}`.

---

## PUSHING/PUSHED derivation

**Framing.** The two lifecycles collide on status values: `SUBMITTED` means "still pushing" for compute and "already pushed" for kueue; `SUCCEEDED` means "pushed, analysis running" for compute but "the k8s Job finished" for kueue. This is why Phase 79 loosened both invariants to bare row-existence.

### Q1 — Universal predicate, or per-endpoint kind-specific CAS?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-endpoint CAS, no predicate | Each callback is already backend-kind-exclusive; keeps `enums/stage.py` DB-free *and* config-free | ✓ |
| Universal clause in `stage_status` | Needs the Phase-67 registry → `backends.toml` dependency in the predicate module | |
| New orthogonal column | `cloud_job.push_done_at`; migration churn, contradicts 77 D-04 | |
| You decide | | |

**User's choice:** Per-endpoint CAS, no predicate (recommended).
**Notes:** `/pushed` + `/mismatch` CAS on `'submitted'` (compute's single in-flight status); `/uploaded` on `'uploading'`; `/upload-failed` on `{'uploading','uploaded'}`. A kueue file cannot reach `/pushed` — `resolve_compute_backend` returns `None` and the handler already holds with a clean 200. The only reader wanting a universal distinction is the pair of UI count cards.

### Q2 — What happens to `LocalBackend.dispatch`'s `LOCAL_ANALYZING` flip?

| Option | Description | Selected |
|--------|-------------|----------|
| Keep it (dual-write) | 81 D-05; keeps the dashboard honest | |
| Drop the flip | Inflates `awaiting_cloud_count` with locally-analyzing files | |
| Keep it, drop the awaiting row too | Already rejected in area 2 | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** Keep the flip recommended. Safe under 79 D-04's implication-not-equality contract — a `LOCAL_ANALYZING` file carrying an `awaiting` row violates nothing, because `awaiting_cloud`'s invariant runs one direction only.

### Q3 — Who reaps the inert `awaiting` row?

| Option | Description | Selected |
|--------|-------------|----------|
| Analyze-terminal seams | `put_analysis` + `report_analysis_failed` DELETE `WHERE status='awaiting'` | |
| Leave inert, widen the index | Dead set still grows without bound | |
| Reap in the drain | Drain becomes a writer; only reaches rows inside the `LIMIT` window | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** Analyze-terminal seams recommended (they already call `_delete_staged_object_if_cloud`). The **index-growth hazard** recorded as a must-address constraint: `ix_cloud_job_awaiting` is partial on `status='awaiting'` alone, so every long file ever analyzed locally stays in it permanently and the `*/5` drain tick's scan degrades at 200K.

---

## Scope boundary (added mid-discussion)

Raised by Claude after the four selected areas; user chose "Explore more gray areas".

**Resolved by evidence, not preference:** Phase 80's CONTEXT (read via `git show SimplicityGuy/phase-80:…`, since it is not in this worktree) states in its **D-04** that "Phase 80 owns its two named files end-to-end … zero `FileRecord.state` reads." That puts `tasks/reenqueue.py`'s `_select_done_push_ids` (`:190`) and `_get_awaiting_cloud_ids` (`:200`) — both cloud-routing `state` reads — unambiguously in Phase 80. No overlap with Phase 83.

### Q — Who fixes the three cloud-lane count cards?

| Option | Description | Selected |
|--------|-------------|----------|
| 83 takes the awaiting card only | 83 introduces the divergence, so 83 closes it; reuse the drain's clause builder | |
| 83 takes all three | Resurrects the universal PUSHING/PUSHED predicate D-12 rejected | |
| Leave all three; flag the gap | Ships a dashboard that visibly disagrees with the drain | |
| You decide | | ✓ |

**User's choice:** You decide.
**Notes:** `get_awaiting_cloud_count` / `get_pushing_count` / `get_pushed_count` (`services/pipeline.py:1113,1207,1225`) all read `FileRecord.state` and **no requirement in any phase names them** — READ-02 names `get_pipeline_stats` specifically, and Phase 82's SC#2 is about four-bucket *per-stage* counts. Phase 90 drops the column they read.

Additionally, Phase 83's conjunct decision (D-05) makes `get_awaiting_cloud_count` **disagree with the drain**: a locally-analyzing long file still counts as "awaiting cloud" on the dashboard while the drain correctly skips it. Recommended that 83 close the awaiting card only (reusing the drain's clause builder), leaving `pushing`/`pushed` as a recorded unowned gap and a hard Phase-90 blocker. Taking all three ruled out.

---

## Claude's Discretion

Delegated by the operator, each with binding constraints recorded in CONTEXT.md:

- **D-02** — the awaiting writer's call site (shared helper recommended; one writer, three call sites)
- **D-03** — the spilled file's `cloud_job` status (re-stamp to `awaiting` recommended; two options ruled out on constraints)
- **D-04** — repairing the already-held corpus (repair migration `034` recommended; `034→035` renumber accepted)
- **D-07** — FIFO key + staleness clock (Phase-90 clock hazard is must-address)
- **D-08** — SC#3 gate shape (hard gate; hermetic-now + live-deferred recommended)
- **D-11** — the `/upload-failed` advisory lock (in scope *with* test recommended; untested-mitigation variant ruled out)
- **D-13** — the `LOCAL_ANALYZING` flip (keep it, per 81 D-05)
- **D-14** — the awaiting-row reaper (analyze-terminal seams; index-growth hazard is must-address)
- **D-15** — the count cards (83 closes the awaiting card only)

Not raised, left to research: whether `report_uploaded`'s now-redundant `state == PUSHING` guard moves to the sidecar anchor; where the writer helper lives; whether migration `034` + the Phase-90 renumber land in this PR or its own; whether the shadow gate gains a new invariant.

## Deferred Ideas

- `get_pushing_count` / `get_pushed_count` — an **unowned gap** and a hard Phase-90 blocker; no requirement in any phase names them.
- The Phase-90 staleness-clock hazard (D-07).
- `ix_cloud_job_awaiting` unbounded growth if no reaper ships (D-14).
- `report_uploaded`'s redundant `FileRecord.state == PUSHING` guard (`agent_s3.py:128`).
- A shadow-gate invariant asserting the converse implication for the new awaiting writer — blocked by D-13's `LOCAL_ANALYZING` files carrying an awaiting row until D-14's reaper runs.
- `MAX_FINGERPRINT_ATTEMPTS` and the mixed-engine fingerprint retry hole — inherited unchanged from 81-CONTEXT; untouched here.

**Scope creep:** none. Discussion stayed inside the cloud-routing domain; the one boundary question (D-11's advisory lock) was resolved against SIDECAR-01's "preserved or strengthened" wording rather than by expanding scope.
</content>
