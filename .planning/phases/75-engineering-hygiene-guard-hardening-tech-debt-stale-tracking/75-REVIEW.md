---
phase: 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking
reviewed: 2026-07-06T00:00:00Z
depth: standard
files_reviewed: 2
files_reviewed_list:
  - tests/shared/routers/test_pipeline.py
  - docker-compose.yml
findings:
  critical: 0
  warning: 1
  info: 1
  total: 2
status: resolved
resolution: "WR-01 fixed in 049638af (with_ledger=True + len(rows)==1; anti-cheat mutation-verified). IN-01 accepted (backstopped by DB-state assertion)."
---

# Phase 75: Code Review Report

**Reviewed:** 2026-07-06
**Depth:** standard
**Files Reviewed:** 2
**Status:** resolved (WR-01 fixed in `049638af`; IN-01 accepted as informational)

## Summary

Phase 75 is a hygiene sweep with intentionally ZERO `src/` changes (confirmed via
`git diff --stat 707fd0b7..HEAD` — only `.planning/`, `docker-compose.yml`, and the test file
changed). The review covers only the added force-local regression region in
`tests/shared/routers/test_pipeline.py` (L2298-2451) and the two comment deletions in
`docker-compose.yml`.

`docker-compose.yml` is a clean comment-only deletion (YAML re-validates via `yaml.safe_load`;
the `backends.toml` explainer and the operative `PHAZE_BACKENDS_CONFIG_FILE` semantics are
preserved). No src modification exists — the phase's zero-src constraint holds.

The test region reuses existing fixtures/helpers correctly (`_persist_files_with_duration`,
`_persist_failed_with_duration`, `wire_fakes`, `seed_active_agent`, the autouse
`_cloud_compute_registry`), imports the real `RouteControl` model, seeds the override via a direct
row insert + commit (no fictional `set_route_control`, no monkeypatch of `get_route_control`), and
carries `-> None` type hints throughout. Three of the four new cases are sound. The fourth — the
L793 backfill no-op — has a defeated anti-cheat: it passes whether or not the gate clause exists.

## Warnings

### WR-01: Backfill force-local test does NOT actually guard gate L793 (anti-cheat defeated by `with_ledger=False`)

**File:** `tests/shared/routers/test_pipeline.py:2415-2451` (`test_force_local_backfill_zero_mutation_no_op`)

**Issue:**
The test seeds its candidate with `_persist_failed_with_duration(session, [_LONG], with_ledger=False)`.
The backfill candidate query (`_backfill_candidates_stmt`, `src/phaze/services/pipeline.py:1279-1288`)
requires an `EXISTS (scheduling_ledger key = 'process_file:<id>')` predicate. With `with_ledger=False`
there is no ledger row, so the candidate is **filtered out regardless of the force-local gate**.

Trace the mutation the docstring claims to catch (removing `or await get_route_control(session)` from
the L793 early-return, `src/phaze/routers/pipeline.py:789-793`):
1. Autouse registry => `settings.cloud_enabled` is `True`, so `not settings.cloud_enabled` is `False`.
2. Without the route-control clause the guard is `if False:` => no early return.
3. Execution reaches `count_backfill_candidates` => candidate has no ledger row => `count == 0`.
4. The `count == 0` branch returns a no-op (nothing reset, nothing enqueued, no ledger seeded).
5. All three assertions still hold: `capture == []`, `state == ANALYSIS_FAILED`, ledger select `== []`.

So the test **passes even with the L793 gate clause deleted** — the exact opposite of its stated
anti-cheat guarantee (docstring L2424-2425: "the case fails if the `or await get_route_control(session)`
clause were removed") and the plan's explicit requirement (`75-02-PLAN.md:167`). It also deviates from
the plan's specified interface, which calls for `_persist_failed_with_duration([_LONG])` — i.e. the
default `with_ledger=True` (`75-02-PLAN.md:166`). The companion `test_backfill_enabled_resets_and_holds`
(L911) proves the point: it uses the default `with_ledger=True` precisely because a ledger row is
mandatory for the file to become a real candidate and get reset+held.

The author appears to have copied `with_ledger=False` from `test_backfill_disabled_when_cloud_local`
(L892), but that reference test is discriminated by the **all-local registry** (`not settings.cloud_enabled`
is `True` there), so its early-return fires before the candidate query — `with_ledger` is irrelevant to
it. Under the cloud-ON registry used here, `with_ledger` becomes load-bearing and the copy silently
neutralizes the only discriminator. Net effect: HYG-04's backfill (L793) coverage is a passing test that
guards nothing.

**Fix:**
Make the candidate a real candidate (`with_ledger=True`) so removing the gate clause would reset it to
`DISCOVERED` and hold it in `AWAITING_CLOUD`, tripping the state assertion. Since a pre-seeded ledger
row now exists, drop the "no ledger seeded" signal (it is unassertable with a pre-existing row) or assert
the row count stays exactly 1 (not that it is empty). The state assertion becomes the real discriminator:

```python
# with_ledger=True => a genuine candidate; the gate is now the ONLY thing preventing reset+hold.
(long_failed,) = await _persist_failed_with_duration(session, [_LONG])  # default with_ledger=True
...
await _drain_background()
assert capture == []
await session.refresh(long_failed)
# Anti-cheat: with the L793 force-local clause dropped this would flip to AWAITING_CLOUD.
assert long_failed.state == FileState.ANALYSIS_FAILED
# Ledger row is the pre-existing one, not a new backfill-held seed (count unchanged, not empty).
rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
assert len(rows) == 1
```

Also correct the docstring, which currently asserts a guarantee the test does not provide.

## Info

### IN-01: UI gate assertion relies on loose substring match

**File:** `tests/shared/routers/test_pipeline.py:2377-2379` (`test_force_local_analyze_ui_routes_local_no_hold`)

**Issue:**
The L718 case asserts `"1 local" in text` and `"0 awaiting cloud" in text` against the rendered
`trigger_response.html`. These substrings are stable for the current template
("Enqueued 1 local, 0 cloud, 0 awaiting cloud for analysis."), and the case is backstopped by a real
`FileRecord`-state anti-cheat afterward (the `select(...state == AWAITING_CLOUD)` check at L2382-2385),
so correctness is not at risk. The note is only that the HTML substring is presentation-coupled and
would silently pass on a future copy change; the DB-state assertion is the load-bearing one. No change
required — recorded for awareness only.

**Fix:** Optionally lean on the existing DB-state assertion as the primary signal and treat the HTML
substrings as secondary, or tighten to the full rendered phrase if template drift becomes a concern.

---

_Reviewed: 2026-07-06_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
