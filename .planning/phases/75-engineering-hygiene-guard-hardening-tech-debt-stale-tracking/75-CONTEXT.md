# Phase 75: Engineering Hygiene — Guard Hardening, Tech-Debt & Stale-Tracking Cleanup - Context

**Gathered:** 2026-07-06
**Status:** Ready for planning

<domain>
## Phase Boundary

Cross-milestone engineering-hygiene sweep that closes milestone 2026.7.2. Reconciles the
cross-milestone deferred-item backlog (STATE.md "Deferred Items", MILESTONES.md:18) against what
shipped in Phases 67-74. **No user-facing behavior change.** Ships as its own PR on a worktree branch.

**Critical framing established during discussion:** three of the five HYG requirements were authored
2026-07-06 from a stale snapshot and their premises have already been overtaken by shipped code. The
phase therefore does MORE reconciliation (marking items satisfied/superseded, correcting requirement
text) and LESS net-new implementation than the requirement text implies. Only **HYG-04** is genuine
net-new code/test work.

Scope, per item:
- **HYG-01** — reconcile-only (already satisfied by shipped code)
- **HYG-02** — 2-line docker-compose comment deletion
- **HYG-03** — reconcile-only (superseded by shipped N-compute)
- **HYG-04** — the one genuine deliverable: force-local gate regression test
- **HYG-05** — pure tracking bookkeeping
</domain>

<decisions>
## Implementation Decisions

### HYG-01 — traceability-guard hardening (reconcile as already-satisfied)
- **D-01:** **Close as already-satisfied; no new code, no new test.** The `FileNotFoundError`-on-absent-
  `REQUIREMENTS.md` fix already landed in PR #207 (`ec80a53a`, 2026-07-05, *before* Phase 75 was
  appended): `tests/shared/core/test_requirements_traceability.py:64` defines
  `_NO_ACTIVE_MILESTONE = not _REQUIREMENTS.exists()` and `@pytest.mark.skipif(_NO_ACTIVE_MILESTONE, …)`
  gates all three active-milestone tests (`:257/:266/:276`). The between-milestones `git rm REQUIREMENTS.md`
  close path already keeps the required code-quality check green.
- **D-02:** The requirement's premise ("reads REQUIREMENTS.md with no existence check") is **stale**.
  Reconcile HYG-01's requirement text + traceability status to reflect that PR #207 satisfies it. The
  "regression test covers the archived/no-active-milestone case" clause is treated as met by the existing
  module-level skip mechanism + `test_archived_milestones_internally_consistent` — no additional test.

### HYG-02 — stale `cloud_target` docker-compose comments (delete)
- **D-03:** **Delete both comment lines** at `docker-compose.yml:24` (`api` service) and
  `docker-compose.yml:52` (`worker` service): the two-line "Replaces the removed cloud_target selector
  + flat s3_*/kube_*/compute_* fields (Phase 67 …)" breadcrumbs. Keep the surrounding backends.toml mount
  explainer intact.
- **D-04:** Premise correction: there is **no `PHAZE_CLOUD_TARGET` env line** anywhere in the repo
  (`git grep` clean) — the deferred item's "env + comment lines" wording is inaccurate; only the two
  comments exist. Nothing is silently dropped by Pydantic `extra=ignore` because no such env key is set.
  The executor removes the comments only; there is no env line to delete.

### HYG-03 — `>1`-compute fail-fast (drop as superseded — NO code change)
- **D-05:** **Drop HYG-03 as superseded; make NO code change.** The `>1`-compute fail-fast this requirement
  asks to "promote from lazy to boot-time" was **deleted outright** by Phase 72 (D-03) to enable the
  N-compute capability that is the entire deliverable of milestone 2026.7.2 (MCOMP-01..07). Re-adding a
  `>1`-compute boot reject would break Phases 72-74's shipped-and-verified behavior, tests, and docs.
- **D-06:** The correct boot guard **already exists**: `config.py:_validate_registry:437` boot-rejects a
  *duplicate `agent_ref`* (D-04 from Phase 72) while permitting N *distinct* compute backends.
  `resolved_non_local_kind:573` returns `"compute"` for any N; single-/zero-compute paths are byte-identical.
- **D-07:** Reconcile HYG-03's requirement text + `STATE.md:246` deferred row to **SUPERSEDED**, citing
  Phase 72 D-03 and the shipped N-compute capability. This is a documentation/tracking reconciliation, not
  an implementation.
- **D-08:** The genuinely-open robustness gap in this area — **WR-01** (`_probe_availability` fires N≥2
  concurrent `session.execute` on one shared `AsyncSession`) — is **NOT fixed in this phase** by user
  decision. It stays a tracked deferred item (see Deferred Ideas). It is bounded: a raced probe flaps one
  lane's `available` flag for a single 5s poll and self-heals (`_probe_one` contains the fault); no data
  loss, does not affect boot/golden/≤1-compute paths.

### HYG-04 — force-local duration-router gate regression test (the one genuine deliverable)
- **D-09:** **Add a real-route regression test** in `tests/shared/routers/test_pipeline.py` covering the
  three force-local gate sites (`pipeline.py:396`, `:718`, `:793`). Drive the actual endpoints (the two
  duration-router triggers + the backfill route) with the persisted `get_route_control` toggle.
- **D-10:** Assertions: with force-local **True**, every file routes local — **zero** `AWAITING_CLOUD`
  rows held, byte-identical to an all-local registry; with force-local **False**, long files are held for
  the cloud drain (registry honored). Backfill under force-local is a clean zero-mutation no-op (per the
  `pipeline.py:789-793` T-71-08 comment). Highest fidelity to the three live gate sites; exact fixture
  mechanics are planner discretion so long as the real `effective_cloud_enabled` fold is exercised.

### HYG-05 — stale 2026.7.0/.1 tracking reconciliation (pure bookkeeping)
- **D-11:** Flip `63-UAT` to complete (`STATE.md:234` — currently `partial`, **0 pending scenarios**;
  parallel-CI work shipped in PR #193). Mark quick-tasks `260628-wzq` (JOB-ENV-CONTRACT) and `260629-eev`
  (ASCII→mermaid diagrams, committed `267109b`) complete. Both quick-tasks are already committed; this is
  status reconciliation only.

### Claude's Discretion
- Exact wording of the reconciled requirement text for HYG-01 (satisfied) and HYG-03 (superseded), and
  the exact STATE.md deferred-row edits — planner/executor choose phrasing that faithfully records the
  "overtaken by shipped code" reality.
- HYG-04 test fixture mechanics (helper reuse, session setup) within the real-route constraint of D-09/D-10.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & tracking (the reconciliation targets)
- `.planning/REQUIREMENTS.md` — HYG-01..05 definitions + Traceability table (rows to reconcile: HYG-01
  satisfied, HYG-03 superseded).
- `.planning/ROADMAP.md` §"Phase 75" (detail ~L1094) + the HYG bullet L25 — success criteria; the
  stale HYG-03 premise lives here too and needs correcting.
- `.planning/STATE.md` — L234 (`63-UAT partial`→complete), L245 (HYG-02 comment item), L246 (HYG-03
  `>1`-compute deferred row → superseded), L247 (HYG-04 force-local test item). Deferred Items section L190+.
- `.planning/MILESTONES.md` §L18 — the "4 known deferred items at close" list that seeded HYG-02/03/04.

### HYG-01 — traceability guard (already satisfied)
- `tests/shared/core/test_requirements_traceability.py` — `_NO_ACTIVE_MILESTONE` L64, skipif L256/265/275,
  between-milestones tolerance comment L61-65. Landed in PR #207 (`ec80a53a`).

### HYG-02 — docker-compose comments (delete)
- `docker-compose.yml` — L24 (`api` service comment), L52 (`worker` service comment).

### HYG-03 — N-compute (superseded; read to confirm, do not change)
- `src/phaze/services/backends.py` — `resolve_backends:506`, `resolved_non_local_kind:550-573` (Phase 72
  D-03 retirement), `resolve_compute_backend:532` (per-file authoritative resolver). Module docstring D-07 L26.
- `src/phaze/config.py` — `_validate_registry:415-467` (existing boot guard: duplicate-`agent_ref` D-04
  L437-451; empty-registry, bucket-id, cluster-specific-bucket invariants).
- `.planning/phases/72-per-entry-compute-binding-fail-fast-retirement/72-VERIFICATION.md` — proves the
  `≤1`-compute fail-fasts were retired + N distinct-`agent_ref` compute boots cleanly.
- `.planning/phases/74-docs-runbook-n-lane-compute-ui-verification/74-REVIEW.md` §WR-01 — the real open
  probe-concurrency gap (deferred, not this phase). Companion pointer: `src/phaze/services/backends.py`
  `_probe_availability:665` + docstring L651-666.

### HYG-04 — force-local gate (add test)
- `src/phaze/routers/pipeline.py` — gate sites L396, L718, L793; `effective_cloud_enabled` fold comments
  L392-395 / L716-717 / L789-793; `get_route_control` reader.
- `tests/shared/routers/test_pipeline.py` — target test file (no existing force-local coverage).

### HYG-05 — tracking (bookkeeping)
- `.planning/quick/260628-wzq-fix-job-env-contract-inject-pod-runtime-/SUMMARY.md`
- `.planning/quick/260629-eev-convert-the-two-ascii-architecture-at-a/SUMMARY.md`
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `get_route_control(session)` / `set_route_control(session, …)` — the persisted force-local master
  toggle (Phase 71 BEUI-02); the HYG-04 test drives routing behavior through it.
- `_validate_registry` (config.py) already holds the id-tagged, Counter-based boot-guard idiom
  (duplicate bucket ids, duplicate `agent_ref`) — the correct existing home for boot invariants, and
  proof HYG-03's "add a boot fail-fast" is unnecessary (the right one already lives here).
- `test_requirements_traceability.py`'s `_NO_ACTIVE_MILESTONE` module constant + skipif pattern — the
  mechanism that already satisfies HYG-01.

### Established Patterns
- **Generalize-not-descope for fail-fasts** (WR-01/Phase 70, carried into Phase 72): the milestone's
  consistent direction was to *retire* `≤1`-compute limits, not reintroduce them — which is exactly why
  HYG-03's literal reading is wrong and "superseded" is the correct disposition.
- **Requirement/tracking reconciliation at milestone close** — marking items satisfied/superseded with a
  cited reason (shipping PR / phase decision) is the project's normal close-out hygiene, not scope creep.

### Integration Points
- HYG-01/03/05 edits touch only `.planning/` docs + (HYG-05) STATE tracking; HYG-02 touches
  `docker-compose.yml`; HYG-04 adds to `tests/shared/routers/test_pipeline.py`. No `src/` behavior change
  anywhere in the phase.
</code_context>

<specifics>
## Specific Ideas

- The milestone-close `git rm REQUIREMENTS.md` path is the concrete reason HYG-01 mattered — with PR
  #207 in place it now stays green, so the close can `git rm` the active REQUIREMENTS.md when 2026.7.2 is
  completed (immediately after this phase). Keep this in mind so HYG-01's reconciliation is worded to
  confirm the close path, not just "tests pass".
- HYG-04 must assert the **absence** of `AWAITING_CLOUD` rows under force-local (not just a routing
  count) — the T-71-08 comment at `pipeline.py:789-793` is explicit that a forced backfill must be a
  zero-mutation no-op, which is the subtle regression the test guards against.
</specifics>

<deferred>
## Deferred Ideas

- **WR-01 — serialize the N-compute probe fan-out** (`_probe_availability`, `backends.py:665`). The one
  genuinely-open robustness gap adjacent to HYG-03: N≥2 online compute backends drive concurrent
  `session.execute` on one shared `AsyncSession`, which SQLAlchemy forbids; the current test
  (`test_lane_snapshot.py:498`) asserts the race's *absence* on empirical/timing grounds → theoretical
  CI-flake exposure. User chose **not** to fix it in Phase 75. Keep tracked (STATE deferred item / a
  future quick task): fix = serialize `_probe_one` calls (structural guarantee) or give each compute
  probe its own session, then reword the `_probe_availability` docstring from "proven race-free in
  practice" to the structural claim. Bounded impact: flaps one lane for one 5s poll, self-heals.
- **PROV-02 / PROV-03** (v2 requirements, already in REQUIREMENTS.md) — capability-aware routing +
  on-demand compute provisioning. Explicitly out of scope for 2026.7.2; future milestone.

### Reviewed Todos (not folded)
None — no pending todos matched this phase's scope.
</deferred>

---

*Phase: 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking*
*Context gathered: 2026-07-06*
