# Phase 84: Dedup & Fingerprint-Progress Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-09
**Phase:** 84-dedup-fingerprint-progress-cutover
**Areas discussed:** Marker writer + corpus repair, Undo semantics under dual-write, Fingerprint progress fate, Predicate home + anti-drift guard

---

## Pre-discussion discoveries (scout)

1. **There is no go-forward writer of `dedup_resolution`.** `resolve_group` (`services/dedup.py:268`)
   stamps `f.state` and nothing else. Only migration `032`'s backfill ever inserted a marker; only
   `scan_deletion.py:108` ever deletes one. Every group resolved since `032` violates the **hard**
   invariant at `shadow_compare.py:135`. Mirrors Phase 83's D-01 exactly. This reshaped the phase from
   a reader cutover into writer + repair + cutover.
2. **The "fingerprint progress bar" has no UI consumer.** `/api/v1/fingerprint/progress` is referenced
   only by `justfile:500`, `docs/api.md:35`, and one mock test. Its `completed` key reads
   `state == FINGERPRINTED`, whose sole writer is `retry_analysis_failed` (`routers/pipeline.py:954`)
   — so it counts approximately nothing. Same class of bug as the `get_stage_progress` one fixed in
   PR #189, still live in a second function.
3. **Sequencing.** ROADMAP declares `Depends on: Phase 82`, but `SimplicityGuy/phase-82` does not exist
   and the file sets are disjoint.

---

## Marker writer + corpus repair

### Q1 — How should `resolve_group` insert the markers?

| Option | Description | Selected |
|--------|-------------|----------|
| Bulk insert + ON CONFLICT DO NOTHING | One `postgresql.insert(DedupResolution)` for the group, `.on_conflict_do_nothing(index_elements=['file_id'])`. Idempotent against HTMX double-submit; matches 032/034's backfill idiom. | ✓ |
| ORM `session.add` per file | Simplest diff, but a concurrent double-submit raises `IntegrityError` on the unique FK and poisons the transaction rather than no-opping. | |
| You decide | Constraint either way: one writer, caller-owned txn, never commits. | |

**User's choice:** Bulk insert + ON CONFLICT DO NOTHING

### Q2 — Should the writer populate `canonical_file_id`?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — write the operator's choice | `resolve_group` already receives `canonical_id`. Go-forward rows become strictly better than 032's `ORDER BY c.id LIMIT 1` guess. Unblocks the "duplicate of X" UI. | ✓ |
| No — leave NULL | Keeps the phase a pure marker-existence cutover. | |
| Yes, and re-derive the backfilled NULLs | Re-guesses rather than recovers the unrecoverable original keeper. | |

**User's choice:** Yes — write the operator's choice (backfilled NULLs left alone)

### Q3 — How to repair post-`032` resolved-without-marker files?

| Option | Description | Selected |
|--------|-------------|----------|
| Migration 035, mirroring 83's 034 | Sync, data-only `op.execute` of 032's `_BACKFILL_DEDUP` verbatim. No DDL, empty autogenerate. Lands before the reader flip. | ✓ |
| Repair inside the reader-cutover commit | A startup/reconcile task. Avoids a lossy downgrade, but the hard invariant is transiently violated in prod and no migration test pins it. | |
| You decide | Constraint: idempotent, before the flip, empty autogenerate. | |

**User's choice:** Migration 035, mirroring 83's 034

### Q4 — What about orphaned markers (marker exists, state ≠ duplicate_resolved)?

| Option | Description | Selected |
|--------|-------------|----------|
| 035 syncs both directions | Insert missing + delete orphans, so `marker ≡ state` at the cutover instant. Safe failure mode: a wrongly-deleted marker resurfaces the file for review; a wrongly-kept one hides it forever, unreachably. | ✓ |
| Insert only; add a reverse soft invariant | Surfaces divergence without deleting operator data, but the Phase-79 gate dies in Phase 90 and a live orphan still hides a file meanwhile. | |
| Both — delete in 035 and add the soft invariant | One-shot reconcile plus a standing drift detector (~3 lines). | |

**User's choice:** 035 syncs both directions

---

## Undo semantics under dual-write

**Framing:** ROADMAP SC#1 says undo becomes a plain `DELETE`, but `resolve_group` must keep stamping
`state = DUPLICATE_RESOLVED` (D-00a dual-write). A bare `DELETE` leaves `state` without a marker —
the hard invariant at `shadow_compare.py:135`. 77's D-07 already rejected a `previous_state` column on
the marker, so the prior state must come from the browser-held payload.

### Q1 — What does `undo_resolve` become?

| Option | Description | Selected |
|--------|-------------|----------|
| DELETE marker + restore previous_state | Undo's *dedup semantics* become a plain DELETE; the state restore is dual-write bookkeeping that dies in Phase 90. Payload shape unchanged, no template churn. | ✓ |
| Stop stamping state on resolve | Makes SC#1 literally true, but front-runs Phase 86 — `proposal.py:39 _TERMINAL_FILE_STATES` stops excluding new duplicates, so proposals get generated for them. Breaks readers this phase doesn't own. | |
| DELETE marker + re-derive state | Needs a `linearize` helper that exists only to be deleted in Phase 90, and silently rewrites states the operator never chose. | |

**User's choice:** DELETE marker + restore previous_state

### Q2 — Should undo CAS on the marker before restoring state?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — `DELETE … RETURNING file_id` gates the restore | A stale-tab replay finds no marker, returns zero rows, does nothing. Marker becomes the single CAS domain (83 D-09 analogue). Survives Phase 90. | ✓ |
| No — unconditional DELETE + per-file UPDATE | Simplest diff, but a stale replay silently rewrites state on files that moved on — the same class of bug as the missing `/upload-failed` guard Phase 83 closed. | |
| You decide | Constraint: a replayed undo must be idempotent. | |

**User's choice:** Yes — `DELETE … RETURNING file_id` gates the restore

### Q3 — When a canonical file is deleted, what happens to its duplicates?

Surfaced as a consequence of Q2's canonical decision: `scan_deletion.py:108` deletes markers matching
**either** FK, so deleting a keeper's scan batch un-resolves its duplicates. Populating
`canonical_file_id` on every go-forward resolve widens that exposure from "some backfilled rows" to
"all new rows".

| Option | Description | Selected |
|--------|-------------|----------|
| Accept — un-resolve them, leave scan_deletion as-is | The keeper is gone, so the decision no longer holds; duplicates reappear for re-review. Safe, zero new code. Needs a docstring note so it isn't rediscovered as a bug. | ✓ |
| Preserve — NULL the pointer instead of deleting the marker | Resolution survives as a bare marker (the model's stated primary job), but touches a file outside the phase's named scope. | |
| Out of scope — defer | Note the widened exposure, change nothing. | |

**User's choice:** Accept — un-resolve them (leave `scan_deletion.py` as-is)

---

## Fingerprint progress fate

### Q1 — What happens to `get_fingerprint_progress` and its endpoint?

| Option | Description | Selected |
|--------|-------------|----------|
| Keep 3-key contract, redefine over derived predicates | Zero API break, zero overlap with Phase 82's four-bucket counts, READ-04 satisfied literally. `completed` stops being ~0. | ✓ |
| Reshape to four buckets | Duplicates READ-02's deliverable and drags `scheduling_ledger` in-flight detection into this phase. | |
| Delete the endpoint | No UI consumer exists, but it retires a documented public API and reads READ-04's "derive" as "delete". | |

**User's choice:** Keep 3-key contract, redefine over derived predicates

### Q2 — What should `total` count?

| Option | Description | Selected |
|--------|-------------|----------|
| Music/video files, excluding dedup-resolved | The true fingerprint universe; keeps `completed/total` reachable; ties both halves of the phase to one clause. | ✓ |
| Music/video files, all of them | Matches the enqueue scope exactly, but resolved duplicates sit permanently in the denominator as never-completing work. | |
| Every file in the corpus | Meaningless the moment a non-audio file is scanned. | |

**User's choice:** Music/video files, excluding dedup-resolved

### Q3 — What should `failed` count?

Surfaced during scout: `total` and `completed` are file counts but `failed` is a **row** count over
`fingerprint_results` (`fingerprint.py:292`). Two engines ⇒ a doubly-failed file counted twice; a
one-success-one-failure file counted as failed even though DERIV-05 says it is `done`.

| Option | Description | Selected |
|--------|-------------|----------|
| Files, via `failed_clause(FINGERPRINT)` | All three keys in one consistent unit. `failed` will visibly drop and `completed` visibly jump — the fix, not a regression. | ✓ |
| Keep the engine-attempt row count | Preserves the number and a real ops signal, but leaves one key in different units from the other two. | |
| Both — files failed, plus engine attempts | Keeps the ops signal without mixing units, but widens the 3-key contract just preserved. | |

**User's choice:** Files, via `failed_clause(FINGERPRINT)`

### Q4 — Does SC#2's "per-engine coverage predicate" mean a per-engine breakdown?

| Option | Description | Selected |
|--------|-------------|----------|
| No — it means any-engine `done_clause` | Names what `done_clause(FINGERPRINT)` already is, backed by `ix_fprint_success`. Per-engine visibility belongs to Phase 87's UI-02. | ✓ |
| Yes — add a per-engine breakdown | Cheap (`engine` column exists) and nothing surfaces "audfprint is down" today, but widens the contract and overlaps Phase 87. | |

**User's choice:** No — it means any-engine `done_clause`

---

## Predicate home + anti-drift guard

**Constraint surfaced during scout:** `services/fingerprint.py` is imported by the agent worker, which
must not import `phaze.database` / `phaze.models` — hence its function-local DB imports
(`fingerprint.py:263-267`, Phase 26 Plan 10/11). Any predicate it consumes must be imported inside the
function.

### Q1 — Where does the dedup-resolved predicate live?

| Option | Description | Selected |
|--------|-------------|----------|
| `services/stage_status.py` | The single-source predicate module 78 established. Caveat: dedup isn't a Stage — name it a file-level predicate, keep it out of the Stage dispatch ladders. | ✓ |
| Local helper in `services/dedup.py` | Smallest blast radius, but `fingerprint.py` would then depend on the UI-facing dedup service to build its denominator. | |
| New `services/dedup_status.py` | Keeps `stage_status.py` purely Stage-keyed, but a whole module for one clause fragments 78's singular answer. | |

**User's choice:** `services/stage_status.py`

### Q2 — What guards the nine replaced read sites?

**Framing:** on a *consistent* corpus (`marker ≡ state`) no test can distinguish "reads the marker"
from "reads `state`" — both return identical rows. Teeth require a deliberately **inconsistent**
corpus: a file with a marker but `state='analyzed'` (must be excluded) and a file with
`state='duplicate_resolved'` but no marker (must be included).

| Option | Description | Selected |
|--------|-------------|----------|
| Divergence test (load-bearing) + source scan (insurance) | Behavioral test over an inconsistent corpus, plus a symbol-absence scan catching a state read reintroduced at a new site. Both mutation-tested; the scan must survive the SQLAlchemy-splits-the-call failure that made 83's grep toothless. | ✓ |
| Divergence test only | Nothing stops a tenth state read appearing in an unexercised function. | |
| Source/AST scan only | Exactly the shape that shipped green-but-toothless twice in Phase 83. | |

**User's choice:** Divergence test (load-bearing) + source scan (insurance)

### Q3 — What happens to the existing mock-based progress test?

`tests/fingerprint/services/test_fingerprint.py:295` stubs three `session.execute` calls with a
`side_effect` list and asserts the dict it fed in. It stays green through any rewrite, including a
wrong one.

| Option | Description | Selected |
|--------|-------------|----------|
| Replace with a real-DB integration test | Pins the denominator, the units change, and DERIV-05 aggregation in one test. Must go RED if `completed` reverts to `state == FINGERPRINTED`. | ✓ |
| Keep it, add an integration test alongside | It encodes an execute-call count the rewrite changes anyway, and still asserts nothing real. | |
| You decide | Constraint: a test must exist that goes RED on the revert. | |

**User's choice:** Replace with a real-DB integration test

### Q4 — How is SC#3 ("the shadow-compare gate stays green") actually proven?

| Option | Description | Selected |
|--------|-------------|----------|
| Both — CI test + a live-corpus run before merge | The CI test gates the go-forward paths; only the live run proves 035's repair covered every real post-032 row. Phase 79 deferred its live run (79 D-02) — which is why D-01 went unnoticed. | ✓ |
| CI integration test only | Validates the go-forward paths, not the repair. | |
| Live-corpus run only | Proves the repair, but nothing stops a future change reintroducing divergence. | |

**User's choice:** Both — CI test + a live-corpus run before merge

---

## Claude's Discretion

- **`035`'s `downgrade()`** — follow `034`'s documented-lossy `DELETE` precedent, or make it a no-op.
  Must be documented in the migration docstring either way.
- **Statement shape for the gated `state` restore in `undo_resolve`** — N per-file `UPDATE`s vs one
  `UPDATE … FROM (VALUES …)`. Constraint: only ids returned by the `DELETE` may be written.
- **Plan/PR decomposition** — natural seams are (a) migration `035` + its test, (b) writer + undo +
  the nine readers + the divergence guard, (c) `get_fingerprint_progress` + its integration test.
  Constraint: `035` lands before (b).
- **Base branch** — confirm whether Phase 82 is a real prerequisite; the file sets appear disjoint.
- **`ON CONFLICT` stays `DO NOTHING`** (not `DO UPDATE SET canonical_file_id`) — reasoned through
  during discussion, recorded as D-07 rather than asked.

## Deferred Ideas

- `eligible()` has no dedup notion — Phase 82's pending sets will enqueue dedup-resolved duplicates.
  The predicate this phase adds to `stage_status.py` is the one 82 needs. → flag for Phase 82 (READ-01).
- ROADMAP calls Phase 90's destructive migration `034`; that revision is taken (83) and this phase
  takes `035`. Phase 90's is `036`+. → roadmap hygiene.
- `find_duplicate_groups`' `dup_hashes` subquery applies `LIMIT`/`OFFSET` with no `ORDER BY`
  (`dedup.py:81`, `:131`, `:207`) — nondeterministic pagination. Pre-existing. → own quick task.
- `get_pushing_count` / `get_pushed_count` remain an unowned gap (carried from 83-CONTEXT).
- Preserving a resolution when its canonical file is deleted (split `scan_deletion.py:108`) — rejected
  here as out of scope (D-08); revisit if operators complain.
- Per-engine fingerprint coverage ("audfprint has been down for a week") — → Phase 87, UI-02.
