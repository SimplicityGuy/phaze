# Phase 79: Shadow-Compare Gate (live corpus) - Context

**Gathered:** 2026-07-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Deliver a **committed, re-runnable shadow-compare gate**: a check that asserts per-file
*implication* invariants between the legacy `files.state` scalar and the derived representation
(the output tables + the Phase-77 `032` backfilled markers), across the whole corpus, reporting
every divergence.

This is the **standing gate** that sits between the additive `032` migration and the destructive
`033`: it must pass before any reader cutover (phases 80–86) and before `033` (phase 90), and every
later cutover phase requires it to "stay green." Its rule is **implication, not equality** —
derivation is deliberately *more* informative than the scalar (a file can be `metadata`-done *and*
`analyze`-done, which no single enum value encodes).

**Requirements:** MIG-02.

**Purely a verification/harness deliverable.** No reader or writer cuts over to the derived model
in this phase; no schema change. Cutover begins Phase 80.
</domain>

<decisions>
## Implementation Decisions

### Check form & harness (D-01, D-02)
- **D-01:** **One shared assertion core** (the invariant set + comparison logic authored once) exposed
  through **two entry points** — (a) a **hermetic pytest** over a crafted **fixture corpus** in the
  `integration` bucket, which is the **standing CI gate** phases 80–90 keep green, and (b) a thin
  **`just shadow-compare`** CLI/module that runs the *same core* against any DB it is pointed at
  (a live-corpus restore). No assertion logic is duplicated between the two. (Rejected pytest-only —
  no first-class live-DB path; and CLI-only — needs extra fixture-DB plumbing for CI.)
- **D-02 [deferred]:** The **live 200K-corpus restore run is deferred** to the next homelab rollout
  and recorded in the phase VERIFICATION when performed (consistent with this project's other
  deployment-gated UAT items). This phase ships the check + hermetic fixture tests **green**. The gate
  remains a hard precondition for `033` (phase 90) regardless of when the live run happens.
  *(Tagged `[deferred]`: a deferral/scoping note — deliberately NOT a buildable plan task; tracked
  instead as the sole Manual-Only verification in `79-VALIDATION.md`, and referenced in `79-02-PLAN.md`.)*

### Derived-side source (D-03)
- **D-03:** The "derived representation" side of the comparison is built by **reusing Phase 78's
  predicates** — `services/stage_status.py` `ColumnElement[bool]` builders / `enums/stage.py` resolver.
  The gate therefore **doubles as a guard on the derivation layer** — the exact predicates phases 80–90
  cut over to. The residual circularity is acceptable and understood: `032` backfilled the
  `DUPLICATE_RESOLVED` / failure / cloud-sidecar markers *from* `files.state`, but `ANALYZED` /
  `METADATA_EXTRACTED` / `PROPOSAL_GENERATED` / apply-outcome states derive from **pre-existing output
  rows**, so the gate still catches real state↔data drift. (Rejected independent raw-column SQL — it
  duplicates predicate logic and would not guard the derivation layer the later phases depend on.)

### Invariant scope (D-04)
- **D-04:** **Comprehensive** — assert an implication for **every `FileState` value** in the design
  §6.1 table, not just the ~6 risky/backfilled ones. This includes the "no-backfill" completion states:
  - `ANALYZED ⇒ analysis.analysis_completed_at IS NOT NULL`
  - `METADATA_EXTRACTED ⇒ metadata row exists`
  - `ANALYSIS_FAILED ⇒ analyze failure marker exists`
  - `DUPLICATE_RESOLVED ⇒ dedup marker exists`
  - `AWAITING_CLOUD ⇒ cloud sidecar row exists`
  - `PUSHING`/`PUSHED ⇒ cloud_job row with the corresponding status`
  - `PROPOSAL_GENERATED ⇒ proposals row`; `APPROVED`/`REJECTED ⇒ proposals.status`;
    `EXECUTED`/`MOVED`/`UNCHANGED`/`FAILED ⇒ execution_log + proposals.status`
  Rationale: this is *the* gate before dropping the column — a completion state with no backing row is
  exactly the drift worth catching. (Rejected the risky-subset-only option.)

### Divergence output & fail semantics (D-05, D-06)
- **D-05:** Output = **per-invariant divergent-file count + a capped sample of `file_id`s** (e.g. first
  20) so an operator can investigate without a full dump, plus a totals line. Any hard-fail divergence
  → **nonzero exit code / pytest failure**. A `--verbose`/`--dump` flag emits the full divergence set on
  demand. (Rejected always-full-dump — noisy at 200K scale; and count-only — forces a manual re-query
  to find offenders.)
- **D-06:** The two known-soft divergences are an **explicit, code-commented allowlist** referencing
  design §6.1: **`FINGERPRINTED`** (documented-expected — its only writer is
  `routers/pipeline.py:935` `retry_analysis_failed`, so it need not imply a fingerprint success) and
  **`LOCAL_ANALYZING`** (design D-03 "probably no stored marker"). Their divergences are **counted and
  printed as "expected divergence"** but **never flip the exit code**. **Every other** divergence is a
  hard fail. The allowlist is commented back to §6.1 so it can't silently grow.

### Claude's Discretion
- Exact fixture-corpus construction (how the fixture rows are seeded to exercise each invariant + each
  allowlisted soft case), the internal signature/shape of the shared assertion core, the precise
  `just`/CLI invocation surface, and the sample-cap number are left to research + planning.
- `LOCAL_ANALYZING`'s real writer behavior should be verified against `routers/backends`/push code
  during research to confirm the allowlist entry is still correct (design flagged it as uncertain).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 79: Shadow-Compare Gate (live corpus)" — goal, 3 success criteria.
- `.planning/REQUIREMENTS.md` — MIG-02 (full text).
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §6.1 (the state→derivation-source table +
  the `FINGERPRINTED` divergence callout), §6.2 (the two-step migration + shadow-compare gate
  spec, incl. the exact invariant list), §8 (constraints: sync migrations, per-bucket test isolation,
  `:5433` test DB, 90% coverage), §6.2 "Quiesce requirement" (drain cloud-push lanes before `033`).

### Upstream phases (the derived model this gate compares against)
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  predicate module decisions (D-03 `done(metadata)` = row present AND `failed_at IS NULL`; D-04
  two-module split; the DERIV-04 SQL⇔Python equivalence test).
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — what `032`
  backfilled vs skipped (metadata NOT backfilled), the failure markers, `dedup_resolution`,
  `cloud_job.AWAITING`, and the partial indexes.
- `alembic/versions/032_add_derived_status_schema.py` — the additive schema this gate reads.

### Existing code (the derived-side predicates to REUSE, per D-03)
- `src/phaze/services/stage_status.py` — the `ColumnElement[bool]` builders that form the derived side
  of the comparison (reuse, don't reinvent).
- `src/phaze/enums/stage.py` — the DB-free `Stage`/`Status` enums + pure-Python resolver.
- `src/phaze/models/` — `analysis.py`, `metadata.py`, `fingerprint.py`, `cloud_job.py`,
  `dedup_resolution.py`, `proposals`/`execution_log` models — the output tables the invariants assert.
- `src/phaze/enums/execution.py` (or wherever `FileState` lives) — the full legacy-state enumeration the
  comprehensive scope (D-04) must cover.
- `routers/pipeline.py:935` (`retry_analysis_failed`) — the sole `FINGERPRINTED` writer that justifies
  the D-06 allowlist.

### Test harness conventions
- `tests/buckets.json` + `tests/shared/test_partition_guard.py` — the gate's pytest lands in the
  `integration` bucket and must pass via `just test-bucket integration` **in isolation**.
- `tests/integration/test_migrations/` — the established per-migration integration-test location/pattern
  (the fixture-corpus test is a sibling in spirit).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`services/stage_status.py` / `enums/stage.py` (Phase 78)** — the derived-side predicates; the gate
  composes these into its per-invariant `.where(...)` comparisons (D-03). This is the primary reuse.
- **Phase 77 partial indexes** (`ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`,
  `ix_cloud_job_awaiting`, `ix_fprint_success`) — index support for the corpus-wide divergence scans.
- **`just` command runner + `tests/buckets.json`** — the CLI entry point (`just shadow-compare`) and the
  CI-bucket wiring both already have established homes.

### Established Patterns
- **Fingerprint "done" spelling** `status IN ('success','completed')` (Phase-59 WR-02 / PR #189) — reuse
  the exact spelling if the gate touches fingerprint (note: `FINGERPRINTED` state itself is allowlisted,
  not asserted).
- **Per-migration integration test** pattern in `tests/integration/test_migrations/` — the model for a
  DB-backed, fixture-seeded gate test.
- **Implication (not equality)** is already the design's stated contract — the gate must never assert
  `state ⇔ derived`, only `state ⇒ derived`.

### Integration Points
- The pytest wrapper feeds CI (`just test-bucket integration`) and becomes the green-gate every
  cutover phase (80–90) re-runs.
- The `just shadow-compare` CLI is the operator/rollout entry that runs the same core against a live
  restore (D-01/D-02), and its output is what SC-3 records in VERIFICATION.

</code_context>

<specifics>
## Specific Ideas

- The invariant list is **not open for redesign** — it is locked by design §6.2 and extended to the full
  §6.1 state set per D-04. Discussion was entirely about the *harness, source, output, and pass/fail
  contract* around that fixed list.
- Circularity is a known, accepted property (D-03) — call it out honestly in the check's docstring
  (which states derive-from-pre-existing-rows vs derive-from-backfill per state), mirroring how the
  design docstrings the `FINGERPRINTED` divergence rather than "fixing" it.

</specifics>

<deferred>
## Deferred Ideas

- **Live 200K-corpus restore run + VERIFICATION evidence** — deferred to the next homelab rollout (D-02).
  Not a separate phase; it's a recorded verification step against this same committed gate.
- **Cloud-push lane drain (`--profile drain`) quiesce** before the destructive `033` — belongs to
  Phase 90's rollout runbook, not here; noted so the gate's live run isn't taken against a moving target.

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 79-shadow-compare-gate-live-corpus*
*Context gathered: 2026-07-08*
