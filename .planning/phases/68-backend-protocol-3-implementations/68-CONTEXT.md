# Phase 68: Backend Protocol + 3 Implementations - Context

**Gathered:** 2026-07-03
**Status:** Ready for planning

<domain>
## Phase Boundary

Introduce **one internal `Backend` protocol** (`is_available` / `in_flight_count` / `dispatch` /
`reconcile`) with `LocalBackend` / `ComputeAgentBackend` / `KueueBackend` implementations that
**re-home the existing staging / push / submit / reconcile logic as protocol-method bodies — not a
rewrite** — replacing the `if active_cloud_kind == compute/kueue` switch that Phase 67 left behind as
a `# TRANSITIONAL — Phase 68` shim. Add a `cloud_job.backend_id` column (additive migration) and make
per-backend in-flight accounting uniform across compute and Kueue.

**Behavior-preserving.** Phase 68 is still a **single-dispatch-path** phase: exactly one non-local
backend (+ local) is resolved and dispatched, identical to today. True N-backend multiplicity — rank
tiering, spillover, per-backend cap enforcement — is **Phase 69** (SCHED). This phase lays and *proves*
the substrate; it does not turn on multiplicity.

Covers **BACK-01..04**. Full requirements in `.planning/REQUIREMENTS.md` §BACK — but note BACK-04's
"byte-identical" wording and BACK-02's "backfill" wording are **reinterpreted** by D-01 and D-04 below
(both stem from Phase 67's D-11..D-14 no-back-compat pivot: the a1/k8s paths were never deployed live).

</domain>

<decisions>
## Implementation Decisions

### BACK-04 acceptance gate — golden side-effect snapshot vs current code (D-01)
- **D-01:** The "byte-identical characterization test" is a **golden side-effect snapshot** whose
  **baseline is the current post-67 code**, NOT a production trace. Rationale: the a1/k8s dispatch paths
  were **never deployed live** (Phase 67 D-11), so there is no prod behavior to match — the code *is* the
  reference. Mechanics: over a matrix of **{compute, kueue, local} × {agent up / agent down}**, record the
  **observable side-effect sequence** — which agent gate is checked vs deliberately skipped, the staging
  call made, the `FileState` transition, the `cloud_job` upsert, the enqueue — capture the snapshot on
  today's code (it passes), then perform the protocol refactor and assert the snapshot is **unchanged**.
- **D-01a:** The gate MUST explicitly preserve the asymmetry BACK-04 names: **compute requires a live
  compute agent** (GATE 1 in `stage_cloud_window`), **Kueue deliberately skips that gate** (ephemeral
  pods, no persistent compute agent). This asymmetry is a first-class assertion in the snapshot matrix.

### 68↔69 in-flight boundary — lay + prove the substrate, defer the drain flip to 69 (D-02)
- **D-02:** Phase 68 **lays and proves** the uniform in-flight substrate; it does **not** flip the drain
  onto it. Concretely, Phase 68:
  1. Adds `cloud_job.backend_id` (D-04) and starts **recording compute-agent pushes in `cloud_job`**
     (today `tasks/push.py` writes **no** `cloud_job` row — grep-confirmed — so the compute row is a
     brand-new artifact per research Pitfall 1; BACK-03).
  2. Defines `in_flight_count(backend)` as the **`cloud_job`-derived per-backend count**
     (`COUNT(cloud_job WHERE backend_id = :id AND status IN {non-terminal in-flight states})`).
  3. Asserts the **equivalence invariant** `sum(in_flight_count(b) for b in backends) ==
     get_cloud_window_count()` (the FileState `{PUSHING, PUSHED}` window, `services/pipeline.py:1243`).
     A divergence is a double/under-count bug (Pitfall 1). This invariant IS the characterization proof
     that the new substrate matches the old count for the single-backend case.
- **D-02a:** The **drain (`stage_cloud_window`) keeps reading `get_cloud_window_count`** (the FileState
  window) for its slot math in Phase 68 — nothing consults per-backend `in_flight_count` for **cap
  consumption** yet. This keeps Phase 68 behavior-preserving and **avoids Pitfall 1's double-count and
  Pitfall 2's unlocked-reconcile race**, which only bite once per-backend counts drive dispatch. **SCHED-02
  (Phase 69) owns the actual flip** of the drain to per-backend caps under the advisory lock.

### In-flight write ordering — one transaction, row-before-or-with the state flip (D-03)
- **D-03 (structural rule, applies in Phase 68 even in lay+prove mode):** `dispatch(file)` owns **both**
  the `FileState → PUSHING` flip **and** the `cloud_job` (backend_id) upsert in **one transaction/session
  passed in by the caller** — the `cloud_job` row is written **in the same transaction and before/with**
  the state flip, **never after a separate commit** (research Pitfall 4: a committed `PUSHING` with no
  reconcilable `cloud_job` row silently strands the file / shrinks effective capacity). This ordering is
  what keeps the D-02 equivalence invariant true and makes the eventual Phase-69 cap flip safe. The
  scheduler must never flip state while the backend writes its row on a separate commit boundary.

### kube_staging.py — pure single-cluster re-home, defer per-cluster parameterization to Phase 70 (D-04-scope)
- **D-05:** Phase 68 wraps **today's single-cluster `kube_staging`** as the `KueueBackend.dispatch` /
  `reconcile` bodies **verbatim** — **no** per-cluster kubeconfig/context parameterization, and the
  unsafe post-construction **token-mutation hack is left as-is** (it works for the single deployed-shape
  cluster). Per-cluster kr8s clients (`kr8s.asyncio.api(kubeconfig=/context=)` per entry) and retiring
  the token hack land in **Phase 70 (MKUE-01)**, matching the roadmap's **BACK→68 / MKUE→70** 1:1 mapping
  and the behavior-preserving discipline. **This OVERRIDES `.planning/research/SUMMARY.md` item 5/§62**,
  which had assigned "parameterized `kube_staging`" to Phase 68 — the planner must not pull that forward.

### cloud_job.backend_id migration — nullable, no backfill (D-06)
- **D-06:** `cloud_job.backend_id` is added as a **nullable** column via an **additive migration** with
  **no meaningful backfill**: the a1/k8s paths were never deployed, so there are ~zero live `cloud_job`
  rows to backfill, and `backend_id` is **config-derived** (a migration cannot reliably know a registry
  entry id). New rows **stamp `backend_id` at dispatch** going forward. This **reinterprets BACK-02's
  literal "backfill of existing rows"** — there is nothing live to backfill. (If any stray rows exist,
  they are already terminal/irrelevant.)

### Transitional-shim removal + single-non-local invariant preserved (D-07)
- **D-07:** Phase 68 **removes the `active_cloud_kind` / `active_cap` transitional `@property` accessors**
  (`config.py:481` / `:489`, tagged `# TRANSITIONAL — Phase 68` by Phase 67) and replaces the
  `if active_cloud_kind == compute/kueue` fork in `stage_cloud_window` with `backend.dispatch()`
  protocol calls. **`cloud_enabled` (`config.py:454`) STAYS** — it remains the registry on/off gate and
  the structural foundation for BEUI-02's future master toggle. Because the drain stays single-path
  (D-02a) and `kube_staging` stays single-cluster (D-05), Phase 68 **preserves the single-non-local
  invariant**: the registry still resolves to **exactly one** non-local backend (+ local), and the
  **raise-on-`>1`-non-local guard is kept** (relocated into Backend resolution / boot, per planner's
  call). The protocol methods are per-backend and unit-tested as such, but the scheduler wiring does not
  yet enumerate/tier N backends — that is Phase 69.

### Claude's Discretion
- Exact module location for the protocol + implementations (research SUMMARY suggests a new
  `services/backends.py` — planner confirms).
- The precise set of `CloudJobStatus` values that count as **non-terminal / in-flight** for
  `in_flight_count()`.
- Snapshot fixture shape and serialization format for the D-01 golden characterization test.
- Whether the raise-on-`>1`-non-local guard (D-07) lives in Backend resolution or stays in the scheduler.
- Whether `KueueBackend`/`ComputeAgentBackend` are instantiated per-registry-entry or resolved lazily.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap (authoritative scope — with the reinterpretations noted)
- `.planning/REQUIREMENTS.md` §BACK (BACK-01..04) — the backend protocol + implementations requirements.
  **NOTE:** BACK-04's "byte-identical" is realized as the **golden side-effect snapshot vs current code**
  (D-01, not a prod trace); BACK-02's "backfill existing rows" is **nullable-no-backfill** (D-06, no live
  rows exist). BACK-03's uniform `in_flight_count` is **defined + proven** this phase but **not yet
  consulted for cap** (D-02/D-02a — the flip is SCHED-02/Phase 69).
- `.planning/ROADMAP.md` — Phase 68 line + the 2026.7.1 execution discipline (PR-per-phase on a worktree
  branch; dependency-strict 67→71; **each phase its own PR, never a direct commit to `main`**). The
  Phase 68 line's "byte-identical characterization test" phrasing = D-01's golden-snapshot-vs-current-code.

### Prior-phase context (the 67↔68 boundary that this discussion resolves)
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` — **D-11..D-14** (no back-compat,
  `cloud_target` removed, call sites already rewired to the transitional shim) and the **explicit D-14
  hand-off** of the 67↔68 boundary + BACK-04 gate reinterpretation to *this* plan-time (resolved by
  D-01/D-07 above).

### Design spine
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` (PR #182):
  - **§4.2** The `Backend` protocol (the seam that removes the `if/elif`) — authoritative protocol shape.
  - **§4.4** In-flight registry — per-backend (`cloud_job.backend_id`, generalize from Kueue-only to
    compute too).
  - **§4.5** Failure / spillover (mechanics land in Phase 69; context here).
  - **§5** What stays untouched — `put_analysis` result return, duration gating, agent HTTP surface,
    shared S3 staging leg, windowed analysis: **all unchanged this phase**.

### Research (read before planning — the sharpest correctness edge lives here)
- `.planning/research/PITFALLS.md`:
  - **Pitfall 1** — double/under-counting compute in-flight across FileState window + new `cloud_job` row
    (drives D-02/D-03: pick ONE substrate, write the row in-txn before/with the flip).
  - **Pitfall 2** — unlocked `reconcile_cloud_jobs` cron racing a per-backend-count drain (**flagged in
    Phase 68**, the advisory-lock change **lands in Phase 69** with the cap flip — do not attempt it here).
  - **Pitfall 4** — dispatch-partial limbo (the D-03 write-ordering rule + a reconcile sweep for
    in-flight-FileState-without-a-live-row).
- `.planning/research/SUMMARY.md` — integration map (items §51-63): the protocol is a thin adapter over
  already-isolated async functions. **NOTE:** SUMMARY §62's "parameterized `kube_staging` in Phase 68" is
  **OVERRIDDEN by D-05** (per-cluster work is Phase 70).

### Existing code to modify / re-home (not rewrite)
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` (~L119-196): the `if active_cloud_kind
  == compute/kueue` fork (GATE 1 compute-agent check; the kueue-skips-GATE-1 asymmetry; the
  `_stage_file_to_s3` vs `_enqueue_push_file` branch) that D-07 replaces with `backend.dispatch()`. Keeps
  reading `get_cloud_window_count` for slot math (D-02a).
- `src/phaze/services/pipeline.py:1243` — `get_cloud_window_count` (FileState `{PUSHING, PUSHED}` window;
  **stays the drain's count in Phase 68**, and the RHS of the D-02 equivalence invariant).
- `src/phaze/models/cloud_job.py` — `CloudJob` model + `CloudJobStatus` (add nullable `backend_id`;
  identify the non-terminal in-flight status set for `in_flight_count`).
- `src/phaze/services/cloud_staging.py` — the `cloud_job` upsert path (compute pushes must now also write
  here, per Pitfall 1 / D-03).
- `src/phaze/tasks/push.py` — compute rsync push (today writes **no** `cloud_job` row → becomes
  `ComputeAgentBackend.dispatch` body + gains the in-txn `cloud_job` write, D-03).
- `src/phaze/services/s3_staging.py`, `src/phaze/services/kube_staging.py`,
  `src/phaze/tasks/submit_cloud_job.py`, `src/phaze/tasks/reconcile_cloud_jobs.py` — the Kueue
  staging/submit/reconcile bodies re-homed into `KueueBackend.dispatch` / `reconcile` (single-cluster,
  verbatim — D-05).
- `src/phaze/config.py:481/489` — `active_cloud_kind` / `active_cap` transitional accessors **removed**
  (D-07); `config.py:454` `cloud_enabled` **kept**.
- `src/phaze/config_backends.py` — the Phase-67 registry submodels the Backend implementations bind to.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **The dispatch bodies already exist as isolated async functions** (research SUMMARY §51): compute push
  (`tasks/push.py`), Kueue submit (`services/cloud_staging.py` + `tasks/submit_cloud_job.py`), local
  (`process_file`). The protocol is a thin adapter — re-home, don't rewrite. This is precisely why
  Phase 68 can be behavior-preserving.
- **`get_cloud_window_count`** (`pipeline.py:1243`) — the existing FileState-window count; reused verbatim
  as the drain's count AND as the RHS of the D-02 equivalence assertion.
- **The advisory-lock / FIFO-claim / single-post-loop-commit skeleton** in `stage_cloud_window` stays;
  only the per-kind fork inside it becomes `backend.dispatch()`.

### Established Patterns
- **Fail-fast-at-startup posture** (Phase 67 discriminated-union validators): the retained
  raise-on-`>1`-non-local guard (D-07) follows the same style.
- **`cloud_job` is a per-`file_id` sidecar** with a unique FK (one active burst per file) and a
  string-backed `CloudJobStatus` whose membership is a CHECK constraint — new in-flight semantics need no
  Postgres enum migration.
- **Cron no-op discipline** (`stage_cloud_window` never raises; every early return is a clean hold): the
  protocol re-home must preserve this — `dispatch`/`is_available` failures degrade to holds, not raises.

### Integration Points
- **Phase 67 transitional shims** (`active_cloud_kind`/`active_cap`, `config.py:481/489`) are the exact
  seam Phase 68 removes; `cloud_enabled` (`config.py:454`) is the gate it keeps.
- **`reconcile_cloud_jobs`** (`tasks/reconcile_cloud_jobs.py:282`) becomes `backend_id`-aware this phase
  (so reconcile knows which backend owns each row) — but its **advisory-lock change** against the drain is
  **Phase 69** (Pitfall 2), not here.
- **Result return** (`put_analysis`, `routers/agent_analysis.py`) is already backend-agnostic — untouched.

</code_context>

<specifics>
## Specific Ideas

The `Backend` protocol shape from design §4.2 (authoritative; planner finalizes signatures):

```python
class Backend(Protocol):
    id: str
    rank: int
    cap: int

    async def is_available(self, ...) -> bool      # compute: agent heartbeat; kueue: cluster probe; local: always
    async def in_flight_count(self, ...) -> int     # COUNT(cloud_job WHERE backend_id=:id AND status IN {in-flight})
    async def dispatch(self, file, session, ...) -> None  # compute: rsync push; kueue: S3 + kr8s submit; local: process_file
                                                          # writes cloud_job row IN-TXN, before/with the FileState flip (D-03)
    async def reconcile(self, ...) -> None           # kueue: cron read; compute: existing /pushed + callback path
```

Phase-68 acceptance shape (D-01 golden snapshot matrix):

```
for kind in {compute, kueue, local}:
  for agent in {up, down}:
    assert side_effects(protocol_dispatch) == snapshot(current_code_dispatch)
    # compute+down -> no-op hold (GATE 1); kueue+down -> proceeds (GATE 1 skipped)  [D-01a asymmetry]
assert sum(in_flight_count(b) for b in backends) == get_cloud_window_count()        [D-02 invariant]
```

</specifics>

<deferred>
## Deferred Ideas

- **Drain flip to per-backend caps under the advisory lock** — SCHED-02, **Phase 69**. Phase 68 only
  defines + proves `in_flight_count`; the drain keeps `get_cloud_window_count` (D-02a).
- **Advisory-lock ordering between `reconcile_cloud_jobs` and the drain** — research Pitfall 2. Flagged in
  Phase 68 (reconcile becomes `backend_id`-aware here) but the lock change lands with the cap flip in
  **Phase 69**.
- **Rank tiering, spillover, black-hole guard, equal-rank tie-break, single-recovery-owner-per-kind** —
  SCHED-01/03/04/05, **Phase 69** (the first behavior-changing phase).
- **Per-cluster `kube_staging` parameterization + retiring the token-mutation hack** — MKUE-01,
  **Phase 70** (D-05 overrides research SUMMARY §62 which had it in 68).
- **Attempt-budget / per-backend cooldown split** (global dispatch budget vs per-backend affinity backoff)
  — research Pitfall 5; schema hooks (`last_dispatched_at`) may be *prepared* here at planner's discretion
  but the logic is **Phase 69**.
- **N-lane admin UI + master revert-to-all-local toggle** — BEUI-01/02, **Phase 71** (`cloud_enabled`
  kept in D-07 is the toggle's structural foundation).

### Reviewed Todos (not folded)
None — no pending todos matched this phase.

</deferred>

---

*Phase: 68-backend-protocol-3-implementations*
*Context gathered: 2026-07-03*
