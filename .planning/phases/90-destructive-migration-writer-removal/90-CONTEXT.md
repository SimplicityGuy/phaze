# Phase 90: Destructive Migration & Writer Removal - Context

**Gathered:** 2026-07-12
**Status:** Ready for planning

<domain>
## Phase Boundary

The **irreversible finale** of the 2026.7.5 Parallel-Enrich-DAG milestone (MIG-04). Retire the linear
`FileState` model entirely:

1. Remove the remaining `.state=` **writer** sites (~9, all pure dual-writes deliberately kept alive
   through Phase 89 with `D-XX` markers, each annotated "dies in Phase 90").
2. Convert the surviving **live `FileRecord.state` readers** (dashboard count cards + analyze workspace
   table — see the "SCOPE DISCOVERY" note in `<code_context>`) to derived sources, so the dashboard
   stays green after the column is gone.
3. The destructive migration `039`: drop `ix_files_state`, drop `files.state`, delete the `FileState`
   Python enum class.

**Gating (locked, from MIG-04 / design §6.2):** lands **last** — only after the Phase 79 shadow-compare
is green on the live corpus **and** the cloud-push lanes are drained/quiesced (`--profile drain`), so the
backfill/guard never snapshots a moving target.

**Column reality:** `files.state` is a plain `String(30)` (`models/file.py:86`), **not** a native PG enum
— so "delete the `FileState` enum" is a pure Python-code deletion; there is no PG enum type to `DROP TYPE`.
The destructive migration is just `op.drop_index('ix_files_state')` + `op.drop_column('files','state')`.

**Latest migration on branch is `038`** (Phase 89) → this phase's destructive migration is **`039`**.

**Out of scope:** any new derived-status behavior, denormalized stage-bitmap column (explicit YAGNI per
design §5), or re-litigating the derivation layer (Phase 78) / markers (Phase 81) it consumes.
</domain>

<decisions>
## Implementation Decisions

### PR / Blast-Radius Structure (the milestone's hard "one shippable PR per seam" rule)
- **D-01:** Ship Phase 90 as **THREE sequential PRs**, each independently green & shippable, destructive
  change isolated last:
  - **PR-1 — Pure writer removal.** Delete ONLY the ~9 `.state=` write statements (plus any now-unused
    `FileState` imports / dead branches those writes fed). The column, `ix_files_state` index, ORM
    mapping, and `FileState` enum class stay **100% intact**. Behavior-preserving (no reader consumes
    `state`). Cleanest bisect boundary; nothing irreversible.
  - **PR-2 — Reader cutover.** Convert every surviving live `FileRecord.state` reader (see SCOPE
    DISCOVERY in `<code_context>`) to derived sources while the column is still present, so the dashboard
    stays green. Reversible; no DDL.
  - **PR-3 — Destructive.** The `039` migration + `op.drop_index` + `op.drop_column` + delete the
    `FileState` class + final cleanup.
- **D-02:** **All reader cutovers (PR-2) MUST land before the destructive drop (PR-3).** Any surviving
  `FileRecord.state` read is a hard blocker for `op.drop_column` — it breaks at ORM-mapping/query time.

### Downgrade Strategy for `039` (MIG-04: "reconstruct the enum from derived sources + document lossiness")
- **D-03:** **Best-effort backfill** (not the Phase-89 `NotImplementedError` posture). `downgrade()`
  recreates the column + `ix_files_state`, then backfills a representative `FileState` per file from the
  derived markers (metadata / analysis / proposals / dedup / cloud sidecar). Honors MIG-04 literally.
- **D-04:** **Collapse precedence = furthest-along linear pipeline stage.** Because derivation is *more*
  informative than the scalar (a file can be metadata-done AND analyze-done AND have a proposal at once),
  walk the original linear order
  (`DISCOVERED < METADATA_EXTRACTED < FINGERPRINTED < ANALYZED < PROPOSAL_GENERATED < APPROVED/EXECUTED/…`)
  and pick the most-advanced stage reached.
- **D-05:** **Durable markers override the ladder; transients are lost.** Where a durable marker exists it
  wins over the linear rank: analyze-failure marker → `ANALYSIS_FAILED`; dedup marker → `DUPLICATE_RESOLVED`;
  `proposals.status = rejected` → `REJECTED`. The transient states (`LOCAL_ANALYZING`, `PUSHING`, `PUSHED`,
  `AWAITING_CLOUD`, and the rollback-`FINGERPRINTED` documented divergence from design §6.1) are
  **unrecoverable** — collapse to the nearest durable stage, each enumerated as a lossy case in the
  migration docstring. The round-trip test asserts **only** the durable/reconstructable cases.

### Destructive-Migration Self-Guard (`039` upgrade preconditions)
- **D-06:** `039.upgrade()` **self-guards, but only when data exists.** It `RAISE`s (aborts the txn) if it
  finds **mid-flight rows** (`files.state IN ('pushing','uploading')` OR non-terminal `cloud_job` rows) OR
  if the **shadow-compare implication invariants fail** on the live corpus. On an **empty corpus / fresh
  DB** the checks find nothing and the migration proceeds cleanly — **explicitly avoiding the Phase-89
  `038` footgun** (CR-02: `038` wrongly hard-aborted `upgrade head` on a fileserver-less fresh DB).
- **D-07:** The shadow-compare precondition is re-expressed as **inline sync SQL inside `upgrade()`**
  (plain `op.*` / `op.get_bind().execute(...)` counting invariant violations) — **NOT** an import of the
  Phase 79 `services/shadow_compare.py` app-layer check. A versioned migration must be frozen-in-time and
  must not couple to mutable app code (and stays sync, never references `saq_jobs`, per constraints).
  Accepts some SQL duplication with the Phase 79 check as the cost of decoupling.

### Anti-Drift Guard (post-deletion regression protection)
- **D-08:** **Type checker is the primary guard** — once `FileState` and the `state` mapping are deleted,
  any reintroduced `.state` read/write fails `mypy`/`ruff`/import immediately (the compiler *is* the guard).
  Add **ONE thin source-grep test** forbidding `FileState` / `files.state` / `.state =` from reappearing in
  `src/`, and **mutation-test it** (add a fake `.state=` line, watch it go RED, restore — a GREEN guard
  proves nothing per project memory `feedback_mutation_test_guard_tests`). No full behavioral schema-absence
  suite (redundant surface for a one-way migration).

### Claude's Discretion
- The `039` migration revision number is mechanically the next after `038` (assign at plan time).
- Batching/lock strategy for the `downgrade()` backfill `UPDATE` over the ~11,428-file prod corpus
  (planner/researcher decides; not user-facing).
- Exact abort-message wording for the mid-flight / shadow-compare-fail guard, and the docstring prose
  enumerating the lossy downgrade cases.
- The precise PR-2 reader-conversion boundaries (which functions delete-as-dead vs convert-to-derived)
  once research completes the surviving-reader enumeration — provided every live reader is cut over before
  PR-3 and each PR is independently green.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design & requirement (locked)
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §5 (drop `ix_files_state`), §6.1 (backfill mapping
  table — the exact legacy-state → derivation-source map that D-04/D-05 collapse *back through*), §6.2
  (two-step-plus-destructive migration + shadow-compare invariant list), §7 (call-site inventory — **note:
  its reader list is STALE, see SCOPE DISCOVERY**), §8 (constraints: sync migrations, never `saq_jobs`,
  90% cov, per-bucket isolation) — the governing contract.
- `.planning/REQUIREMENTS.md` MIG-04 (the destructive migration requirement; `downgrade()` documents enum
  reconstruction + lossiness) + MIG-02 (shadow-compare, the gate this phase depends on being green).
- `.planning/ROADMAP.md` "Phase 90" line — the one-line scope anchor.

### Prior-phase decision records this phase depends on / carries forward
- `.planning/phases/89-legacy-scan-path-deletion-.../89-CONTEXT.md` — D-10 precedent (irreversible
  migrations → `NotImplementedError`); **superseded here** by D-03 (best-effort backfill) per MIG-04's
  explicit wording. Also CR-02 fresh-DB-abort footgun that D-06 must avoid.
- Phase 78 derivation layer: `src/phaze/enums/stage.py` + `src/phaze/services/stage_status.py`
  (`stage_status()` / `eligible()`) — the derived-status source PR-2 readers convert to.
- Phase 79 shadow-compare: `src/phaze/services/shadow_compare.py` — the invariant logic D-07 re-expresses
  as inline sync SQL (reference, do NOT import from the migration).
- Phase 81 failure markers + Phase 83 cloud sidecar — the durable markers D-05 / PR-2 read
  (`analysis.analysis_completed_at` / analyze-failure marker; `cloud_job` sidecar for pushing/pushed/awaiting).

### Migration conventions
- `alembic/versions/038_retire_legacy_sentinel.py` — latest head; the `039` template/precedent for sync
  `upgrade`/`downgrade`, mirrored downgrade, and the `-x` override / guard-abort pattern.
- Migration integration-test dir `tests/integration/test_migrations/` — every migration needs a test.
</canonical_refs>

<code_context>
## Existing Code Insights

### ⚠️ SCOPE DISCOVERY — surviving live `FileRecord.state` READERS (design §7 is STALE)
The design implied only writers + DDL remained. **False.** These live readers still consume `files.state`
and **each breaks the moment the column drops** — PR-2 must convert them to derived sources:

- `services/pipeline.py:1002 get_analyze_stage_files` — `WHERE state.in_(_ANALYZE_STAGE_STATES)` (line 993:
  `[ANALYZED, AWAITING_CLOUD, PUSHING, PUSHED, ANALYSIS_FAILED]`) + `completed = state == ANALYZED`
  (line 1070). Wired to analyze workspace: `routers/pipeline.py:629`.
- `services/pipeline.py:981 get_files_by_state` — generic `WHERE state == :state`; reused by
  `get_analysis_failed_files` (`:1292`, `state == ANALYSIS_FAILED`) → `routers/pipeline.py:1095`.
- `services/pipeline.py:1296 get_analysis_failed_count` — `state == ANALYSIS_FAILED` → dashboard
  (`routers/pipeline.py:590,727`) + `templates/pipeline/partials/straggler_failed_card.html`.
- `services/pipeline.py:1462 get_pushing_count` — `state == PUSHING` → `routers/pipeline.py:602,735` +
  `templates/pipeline/partials/staged_pushing_card.html`.
- `services/pipeline.py:1480 get_pushed_count` — `state == PUSHED` → `routers/pipeline.py:603,736` +
  `templates/pipeline/partials/analyzing_cloud_card.html`.
- `services/pipeline.py:1707` — `WHERE state.in_([ANALYZED, METADATA_EXTRACTED])` (trace the owning fn).
- `templates/pipeline/partials/analyze_workspace.html:100,102` — `f.state == 'awaiting_cloud'` /
  `'analysis_failed'` comparisons (fed by `get_analyze_stage_files`' dict; NOT raw-enum renders, but they
  still depend on a `state` value in the row dict → must switch to derived flags).

**Conversion precedent already in-tree** (research should mirror it): `get_awaiting_cloud` was cut over from
`state == AWAITING_CLOUD` to the `cloud_job` sidecar (`services/pipeline.py:1361,1506`); pushing/pushed have
the identical sidecar source; `analysis_failed` derives from the Phase 81 analyze-failure marker; `analyzed`
from `analysis.analysis_completed_at`.

### Writers to remove (PR-1) — all pure dual-writes, each annotated "dies in Phase 90"
- `routers/agent_analysis.py:380` — `state = ANALYSIS_FAILED` (D-05 dual-write; the "three live readers"
  it names were cut over in Phases 80/82 — verify none remain before deleting).
- `services/backends.py:124` (`AWAITING_CLOUD`, D-00c), `:304` (`LOCAL_ANALYZING`), `:395` & `:508`
  (`PUSHING`).
- `services/dedup.py:274` — `state = DUPLICATE_RESOLVED`.
- `routers/pipeline.py:999` (`DISCOVERED`), `:1129` & `:1273` (`FINGERPRINTED`) — retry/rescan paths;
  confirm the derived equivalent already covers the behavior (nothing reads these writes).

### Model surface to delete (PR-3)
- `src/phaze/models/file.py:86` (`state: Mapped[str] = mapped_column(String(30), …)`), `:97`
  (`Index("ix_files_state", "state")` in `__table_args__`), and the `FileState(enum.StrEnum)` class.
- `src/phaze/config.py:619` — a comment-only `FileState.AWAITING_CLOUD` reference (no code dep; tidy).
- 26 `src/phaze/**` files reference `FileState` today (`grep -rl`) — planner enumerates the full removal set.

### Established Patterns / constraints
- Sync migrations, mirrored `downgrade()`, integration test per migration, **never reference `saq_jobs`**.
- Per-bucket test isolation (`just test-bucket <bucket>`); DB tests need `TEST_DATABASE_URL`/`PHAZE_QUEUE_URL`
  at `:5433`; 90% coverage floor; `ruff`/`mypy` strict clean; never `--no-verify`.
</code_context>

<specifics>
## Specific Ideas

- The three-PR shape (writers → readers → destructive) is deliberate: the only irreversible action
  (column drop + enum deletion) is quarantined in PR-3, behind two independently-green, fully-reversible
  PRs. This matches the milestone's live-corpus caution and its per-seam PR discipline exactly.
- `downgrade()` is best-effort *because MIG-04 asks for it literally* — but the honest lossiness
  (transient/rollback states) must be spelled out in the docstring, not silently dropped.
</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (The reader-cutover expansion is NOT scope creep: converting
surviving `files.state` readers is a mandatory prerequisite for MIG-04's column drop.)
</deferred>

---

*Phase: 90-destructive-migration-writer-removal*
*Context gathered: 2026-07-12*
