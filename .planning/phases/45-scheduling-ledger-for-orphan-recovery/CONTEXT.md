---
phase: 45
name: scheduling-ledger-for-orphan-recovery
created: 2026-06-18
---

# Phase 45 Context: Scheduling Ledger for Orphan Recovery

## Problem (live incident, 2026-06-18)

The manual "Recover orphaned work" button calls `recover_orphaned_work(ctx, force=True)`.
`force=True` bypasses the `count_inflight_jobs == 0` loss-detection gate and runs the
all-stages RECONCILE pass unconditionally. That pass derives its work from the
`services/pipeline.py` pending-set queries (`get_files_by_state(DISCOVERED)`,
`get_metadata_pending_files`, `get_untracked_files`, `get_proposal_pending_batches`,
`get_scrape_pending_tracklists`, `get_match_pending_tracklists`) — all of which are the
**complement of done**: "everything that hasn't finished this stage." There is **no record
anywhere that a stage was ever scheduled for an item**. So clicking Recover swept in
~11,400 never-scheduled `DISCOVERED` files and detonated the queue to ~44,500 jobs
(extract_file_metadata 11,428 + scan_live_set 11,428 + process_file 11,375 +
search_tracklist 10,244). Confirmed live in lux `saq_jobs`.

## Operator principle (the spec)

Recovery must only re-queue work that was **previously scheduled and then lost**. Work that
was never scheduled (e.g. a `DISCOVERED` file awaiting a manual DAG trigger) is **not yet
orphaned** and must be left alone.

## Approach (agreed)

Add a durable **scheduling ledger** that records "this `<task>:<natural_id>` was enqueued"
at the single `before_enqueue` chokepoint, and clears it on completion. Recovery then
re-enqueues exactly:

```
orphaned = (ledger entries) − (live saq_jobs keys, status in queued/active) − (completed)
```

- Write site: `tasks/_shared/deterministic_key.py::apply_deterministic_key` (the universal
  `before_enqueue` hook that already stamps `<task>:<natural_id>` keys). One ledger upsert
  keyed by `job.key`.
- Clear site: the completion path — `tasks/_shared/deterministic_key.py::increment_completed`
  (the `after_process` hook) — deletes/marks the ledger row done.
- Recovery producer: `tasks/reenqueue.py::recover_orphaned_work` reads the ledger instead of
  the complement-of-done pending sets, intersects against live `saq_jobs` keys, and
  re-enqueues the remainder through the EXISTING keyed producers (so manual + startup paths
  stay aligned and deterministic-key dedup keeps it idempotent — Phase-32/35 invariants).
- The ledger lives **outside `saq_jobs`** so it survives a broker truncate/restore — the only
  genuine post-Phase-36 (Postgres broker) loss case. `force` now means "reconcile the ledger
  now," not "sweep the domain backlog."

## Locked operator decisions (2026-06-18)

1. **Terminal `failed` clears the ledger (NO poison re-queue).** When a job exhausts its
   retry budget and goes `failed`, its ledger entry is cleared — the work WAS scheduled and
   ran, just unsuccessfully. Recovery never re-queues `failed` jobs; re-running a failed stage
   stays a deliberate manual action. This is the exact failure mode behind the recent
   incident (the proposal auth / fingerprint connect failures would otherwise re-flood on
   every recovery). Requires hooking SAQ's terminal-failure path (after_process / a failure
   hook), not just the success completion hook.
2. **Ledger tracks ALL keyed job types** — every job through the `before_enqueue`
   deterministic-key chokepoint (all 8 stages: analyze/process_file, extract_file_metadata,
   fingerprint_file, scan_live_set, generate_proposals, search_tracklist,
   scrape_and_store_tracklist, match_tracklist_to_discogs). Uniform single-chokepoint write;
   recovery covers the whole pipeline with one model.
3. **Backfill from live `saq_jobs` at deploy.** On first startup after the migration, seed the
   ledger from the current `queued`/`active` `saq_jobs` rows so in-flight work (and the current
   ~44.5k cohort, if still present) remains recoverable immediately — no blind window. One-time
   transition; idempotent (safe to re-run, keyed by deterministic key).

## Constraints / invariants to preserve

- Control-only module boundary: `reenqueue.py` stays control-side (Postgres + task_router);
  never imported by the agent worker (import-boundary test enforces this).
- Manual `force=True` path and the startup recovery path call the SAME producer — no drift
  (Phase 42 D-03).
- Idempotency via deterministic-key dedup: a re-enqueue of a still-live item returns `None`
  and counts as skipped — recovery can never double the queue (Phase-32 class).
- Keep every enqueue through the keyed producers (never a raw random-key `queue.enqueue`).
- 85% coverage; new Alembic migration (async template); migration is reversible.

## Out of scope

- Bug A (Anthropic key → litellm) — shipped separately (PR #145).
- Bug B (nox panako/audfprint host alias) — homelab deploy fix, done separately.
- Cloud-burst analysis (roadmap backlog item).
