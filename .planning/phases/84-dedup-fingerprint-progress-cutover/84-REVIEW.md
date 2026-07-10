---
phase: 84-dedup-fingerprint-progress-cutover
reviewed: 2026-07-10T00:00:00Z
depth: standard
files_reviewed: 13
files_reviewed_list:
  - alembic/versions/035_reconcile_dedup_resolution.py
  - src/phaze/models/dedup_resolution.py
  - src/phaze/services/dedup.py
  - src/phaze/services/fingerprint.py
  - src/phaze/services/stage_status.py
  - tests/discovery/services/test_dedup.py
  - tests/fingerprint/routers/test_pipeline_fingerprint.py
  - tests/fingerprint/services/test_fingerprint.py
  - tests/integration/test_dedup_divergence.py
  - tests/integration/test_dedup_resolve_undo_shadow.py
  - tests/integration/test_fingerprint_progress.py
  - tests/integration/test_migrations/test_migration_035_reconcile_dedup_resolution.py
  - tests/shared/test_dedup_fingerprint_source_scan.py
findings:
  critical: 0
  warning: 2
  info: 3
  total: 5
status: resolved
resolution_commit: 5215d82c
resolution_note: "WR-01 and WR-02 fixed in 5215d82c with 3 mutation-verified regression tests. Info nits folded into the same refactor."
---

# Phase 84: Code Review Report

**Reviewed:** 2026-07-10
**Depth:** standard (per-file, language-aware; cross-referenced imports and the shadow invariant)
**Files Reviewed:** 13
**Status:** issues_found (2 Warnings, 3 Info — no Blockers)

## Summary

Phase 84 is a genuinely strong, well-verified implementation. Every load-bearing concern from
`84-CONTEXT` / `84-RESEARCH` was checked directly against source:

- **Writer (`resolve_group`)** — `id` is stamped per-row via a list comprehension (not first-row only),
  `canonical_file_id` carries the operator's pick (D-03), `resolved_at` rides its `server_default`, the
  `pg_insert` is guarded by `if files:` (no empty `.values([])`), and it `flush()`es without committing
  (caller-owned txn). All correct.
- **`undo_resolve` CAS** — `DELETE … RETURNING file_id` is the sole authority; `returned` is collected
  via `.scalars().all()` and used to gate the restore; `synchronize_session=False` present;
  `previous_state` is coerced through `FileState(...)` with a `ValueError` skip; stale replay no-ops.
  Two ordering/robustness gaps found (WR-01, WR-02), neither a data-loss blocker.
- **`get_fingerprint_progress`** — all three keys share the exact `(file_type IN MUSIC_VIDEO_TYPES,
  ~dedup_resolved_clause())` denominator (D-17), so `completed ⊆ total` and `failed ⊆ total` hold;
  `done_clause`/`failed_clause(Stage.FINGERPRINT)` are consumed unchanged (index-matching spelling
  preserved); DB imports are function-local. Correct.
- **Migration `035`** — both statements are static and parameter-free; the orphan `DELETE` is scoped to
  `f.state <> 'duplicate_resolved'` so it can never remove a legitimately-resolved file's marker; the
  insert half is idempotent; no `saq_jobs`; `down_revision="034"`; no-op downgrade documented.
- **`dedup_resolved_clause()`** — correlated `exists(select(DedupResolution.id).where(file_id ==
  FileRecord.id))`, kept out of the `Stage` dispatch ladders. Correct.
- **Nine reader flips** — each `~dedup_resolved_clause()` sits in the exact logical position of the
  `state != DUPLICATE_RESOLVED` clause it replaced; the positional-arg sites (`:224`, `:238`, `:263`)
  and the correlated-exists-inside-`having`/aggregate sites preserve counts.
- **Tests** — the divergence test seeds a genuinely inconsistent corpus (marker+`analyzed` excluded;
  `duplicate_resolved`+no-marker included); the AST source scan walks both positional and keyword
  `Call` args, is not fooled by the docstring `FINGERPRINTED` token, and encodes both mutation
  directions; the old toothless mock stub was deleted (D-15); the router-level progress test has teeth
  (misleading `state` values inverted). No green-if-reverted tests found.

Items explicitly listed as intentional-by-design (the surviving `f.state` dual-write, function-local
imports, no-op downgrade, `ON CONFLICT DO NOTHING`, the pre-existing `LIMIT/OFFSET`-without-`ORDER BY`,
the `completed`/`failed` number shifts) were confirmed present and are **not** reported below.

## Warnings

### WR-01: `undo_resolve` deletes the marker before validating `previous_state`, so an unrestorable state leaves a marker-less `duplicate_resolved` file (hard shadow-invariant divergence)

**File:** `src/phaze/services/dedup.py:312-329`

**Issue:** The bulk `DELETE … RETURNING file_id` (line 312-317) removes the marker for **every**
payload id that carries one, *before* each `previous_state` is coerced (line 326). If the coercion
raises `ValueError`, the code `continue`s (line 328) — skipping the state restore — but the marker is
already gone and the transaction still commits (the exception is swallowed, not re-raised). The result
is a file with `state = 'duplicate_resolved'` and **no** `dedup_resolution` marker, which is exactly the
HARD invariant `services/shadow_compare.py:135` (`soft=False`) exists to forbid — the invariant this
phase's own SC#3 must keep green.

**Failure scenario:** A currently-resolved file `F` (marker present, `state='duplicate_resolved'`).
A crafted/corrupted undo payload `[{"id": "<F.id>", "previous_state": "not_a_state"}]` (or
`"previous_state": null`) reaches `undo_resolve`. Line 312 deletes `F`'s marker (F.id is returned).
Line 326 raises `ValueError`, line 328 `continue`s — `F.state` stays `duplicate_resolved`. On the next
`just shadow-compare`, `hard_fail_total >= 1` (RED). The committed D-16.1 test never exercises this
because it only supplies valid `FileState` values. Not reachable through the normal browser flow (which
always echoes a captured, valid state), so this is an adversarial/corruption-input gap, not an
everyday bug — hence Warning, not Blocker.

**Fix:** Validate/parse everything first, then scope the `DELETE` to only the ids that will actually be
restored (this also fixes WR-01's sibling, WR-02, and the mass-assignment surface stays intact):

```python
restore_map: dict[uuid_mod.UUID, FileState] = {}
for entry in file_states:
    try:
        fid = uuid_mod.UUID(entry["id"]) if isinstance(entry["id"], str) else entry["id"]
        restore_map[fid] = FileState(entry["previous_state"])
    except (ValueError, KeyError, TypeError):
        continue  # malformed id or non-member state: neither delete nor restore
if not restore_map:
    return 0

result = await session.execute(
    delete(DedupResolution)
    .where(DedupResolution.file_id.in_(restore_map))
    .returning(DedupResolution.file_id)
    .execution_options(synchronize_session=False)
)
returned = set(result.scalars().all())

count = 0
for fid in returned:
    await session.execute(update(FileRecord).where(FileRecord.id == fid).values(state=restore_map[fid]))
    count += 1
await session.flush()
return count
```

### WR-02: `undo_resolve` raises an unhandled `ValueError` on a malformed UUID in the payload

**File:** `src/phaze/services/dedup.py:306` (and the redundant re-parse at `:322`)

**Issue:** `ids = [uuid_mod.UUID(e["id"]) if isinstance(e["id"], str) else e["id"] for e in file_states]`
calls `uuid_mod.UUID(...)` with no guard. A non-UUID string (or a missing `"id"` key → `KeyError`)
propagates out of `undo_resolve`, up through `undo_resolve_endpoint` / `bulk_undo`
(`routers/duplicates.py:177,242`), which do not catch it → FastAPI 500. The design docstring
(line 300-304) claims a crafted payload "restores nothing," but a *malformed* id never reaches the
CAS — it aborts before the DELETE.

**Failure scenario:** POST `/duplicates/{hash}/undo` with `file_states='[{"id":"xyz","previous_state":"discovered"}]"`
→ `ValueError: badly formed hexadecimal UUID string` → 500 response. Not data loss (the txn rolls back),
but an unhandled-exception / minor-DoS surface on browser-supplied JSON.

**Fix:** Covered by the WR-01 fix above (the `try/except (ValueError, KeyError, TypeError)` wraps the
UUID parse). If WR-01 is deferred, at minimum wrap the line-306 comprehension in a per-entry `try`.

## Info

### IN-01: `undo_resolve` parses each id twice

**File:** `src/phaze/services/dedup.py:306` and `:322`

**Issue:** Each `entry["id"]` is converted to a `UUID` once when building `ids` (306) and again inside
the restore loop (322). Redundant work and a second, independent raise site for the same malformed
input. The WR-01 fix collapses this to a single parse.

**Fix:** Parse once into a map/dict (see WR-01 snippet) and index it in the loop.

### IN-02: duplicate payload entries can inflate the returned `count`

**File:** `src/phaze/services/dedup.py:320-330`

**Issue:** If the same id appears twice in `file_states`, the `DELETE … IN (ids)` returns it once, but
the restore loop iterates both entries, both find the id in `returned`, both run a (harmless, idempotent)
`UPDATE`, and `count += 1` fires twice — so `count` can exceed the number of files actually restored.
Benign today (callers ignore the return value for control flow, per the docstring), but the number is
misleading. The WR-01 fix (iterating the de-duplicated `returned` set) removes this.

**Fix:** Iterate `returned` (a set) rather than `file_states` when counting/restoring.

### IN-03: divergence test does not pin `recoverable_bytes`

**File:** `tests/integration/test_dedup_divergence.py:192-197`

**Issue:** `test_get_duplicate_stats_marker_is_authority` asserts `groups` and `total_files` invert
under the marker→state mutation, but not `recoverable_bytes`. `recoverable_bytes` is derived from the
same `~dedup_resolved_clause()`-filtered `max_per_group_subq` (`dedup.py:232-242`), so a wrong marker
predicate there would go uncaught by this test (the `total_files`/`groups` asserts would still fail, so
the mutation is caught overall — this is a coverage nit, not a hole). Low value; noting for completeness.

**Fix:** Add one assertion on `stats["recoverable_bytes"]` with a computed expected value for the
inconsistent corpus, so the `max_per_group_subq` clause has its own teeth.

---

_Reviewed: 2026-07-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_

---

## Resolution (orchestrator, 2026-07-10)

Both Warning findings were fixed in `5215d82c` before the phase closed out.

| ID | Finding | Disposition |
|----|---------|-------------|
| WR-01 | `undo_resolve` deleted the marker before coercing `previous_state`; a coercion failure left `state='duplicate_resolved'` with no marker — the hard `shadow_compare.py:135` divergence this phase's SC#3 must keep green | **Fixed.** Payload is now fully parsed and validated into `restore_by_id` before any write; the `DELETE` is scoped to only those ids, so a marker is removed only when its state restore is guaranteed to follow. |
| WR-02 | A malformed UUID or missing `"id"` key raised an unhandled `ValueError`/`KeyError` → HTTP 500 before reaching the CAS | **Fixed.** Non-UUID and non-`str` ids are dropped during validation; a mixed payload restores the valid entries and drops the rest. |
| Info ×3 | double-parse of ids, duplicate-entry count inflation, minor coverage gap | **Folded into the same refactor.** The dict collapses duplicates and the return value is now the `RETURNING` cardinality rather than a loop counter. |

The D-06 CAS is unchanged: `DELETE … RETURNING` still decides what is written, so a stale-tab
replay against a since-re-resolved file still matches zero rows and no-ops.

**Mutation evidence.** Three regression tests were added to
`tests/integration/test_dedup_resolve_undo_shadow.py`. Run against the pre-fix implementation
(`a67ed16a`) all three go **RED**; against the fix all five tests in the file are **GREEN**:

```
test_undo_with_invalid_previous_state_keeps_marker_and_gate_green  FAILED -> passed
test_undo_with_malformed_uuid_does_not_raise                       FAILED -> passed
test_undo_duplicate_entries_do_not_inflate_count                   FAILED -> passed
```

Post-fix: integration 179, discovery 204, fingerprint 82, review 421 — all passing.
`uv run ruff check .` and `uv run mypy .` clean.
