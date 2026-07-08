# Phase 79: Shadow-Compare Gate (live corpus) - Research

**Researched:** 2026-07-08
**Domain:** Data-migration verification harness (SQLAlchemy anti-join invariants over Postgres) + pytest/CLI dual-entry test infrastructure
**Confidence:** HIGH (every claim below is grounded in the actual tree at this branch; no library-version guesswork — the phase adds zero dependencies)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** ONE shared assertion core (invariant set + comparison logic authored once) exposed through TWO entry points — (a) a hermetic **pytest** over a crafted fixture corpus in the `integration` bucket (the standing CI gate phases 80–90 keep green), and (b) a thin **`just shadow-compare`** CLI/module running the *same core* against any DB it is pointed at (a live-corpus restore). No assertion logic duplicated. (Rejected pytest-only and CLI-only.)
- **D-02:** The live 200K-corpus restore run is **DEFERRED** to the next homelab rollout and recorded in the phase VERIFICATION when performed. This phase ships the check + hermetic fixture tests **green**. The gate remains a hard precondition for `033` (phase 90) regardless of when the live run happens.
- **D-03:** The "derived" side REUSES Phase 78's `services/stage_status.py` `ColumnElement[bool]` builders / `enums/stage.py` resolver — the gate doubles as a guard on the derivation layer. The residual circularity (`032` backfilled DUPLICATE_RESOLVED / failure / cloud-sidecar markers *from* `files.state`, while ANALYZED / METADATA_EXTRACTED / PROPOSAL_GENERATED / apply-outcome states derive from **pre-existing output rows**) is accepted and understood. (Rejected independent raw-column SQL.)
- **D-04:** COMPREHENSIVE scope — assert an implication for EVERY `FileState` value in design §6.1, including the no-backfill completion states (PROPOSAL_GENERATED, APPROVED/REJECTED, EXECUTED/MOVED/UNCHANGED/FAILED).
- **D-05:** Output = per-invariant divergent-file count + a capped sample of `file_id`s (e.g. first 20) + a totals line. Any hard-fail divergence → nonzero exit / pytest failure. A `--verbose`/`--dump` flag emits the full divergence set. (Rejected always-full-dump and count-only.)
- **D-06:** The two known-soft divergences are an explicit, code-commented allowlist referencing design §6.1: **`FINGERPRINTED`** (its only writer is `routers/pipeline.py:937` `retry_analysis_failed`, so it need not imply a fingerprint success) and **`LOCAL_ANALYZING`** (design "probably no stored marker"). Their divergences are counted and printed as "expected divergence" but never flip the exit code. Every other divergence is a hard fail. The allowlist is commented back to §6.1 so it can't silently grow.

### Claude's Discretion
- Exact fixture-corpus construction (how rows are seeded to exercise each invariant + each allowlisted soft case), the internal signature/shape of the shared assertion core, the precise `just`/CLI invocation surface, and the sample-cap number.
- `LOCAL_ANALYZING`'s real writer behavior verified against `services/backends`/push code during research (design flagged it uncertain). **→ Verified below (Finding 3): allowlist entry is CORRECT.**

### Deferred Ideas (OUT OF SCOPE)
- Live 200K-corpus restore run + VERIFICATION evidence — deferred to the next homelab rollout (D-02). Recorded verification step against this same committed gate, not a separate phase.
- Cloud-push lane drain (`--profile drain`) quiesce before the destructive `033` — belongs to Phase 90's rollout runbook, not here.
- No reader/writer cutover, no schema change this phase.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| MIG-02 | A committed, re-runnable shadow-compare check asserting per-file *implication* invariants (e.g. `state=ANALYZED ⇒ analysis_completed_at IS NOT NULL`; `state=DUPLICATE_RESOLVED ⇒ dedup marker`) across the live corpus, with `FINGERPRINTED` documented as the one expected divergence; must pass before any reader cutover and before the destructive migration. | The full FileState→invariant map (§ Architecture, "The 17-value invariant table") gives every implication with its concrete derived-side column/table; the shared-core + dual-entry shape (§ Architecture Patterns) satisfies "committed, re-runnable"; the existing `test_stage_status_equivalence.py` fixture pattern (§ Code Examples) is the hermetic-corpus template; D-02 records the live-corpus run in VERIFICATION. |
</phase_requirements>

## Summary

This is a **pure verification/harness phase** — no schema change, no reader/writer cutover, no new dependency. The deliverable is one **shared assertion core** that, for each legacy `FileState` value, runs a corpus-wide **anti-join** (`WHERE state = X AND NOT (<derived-condition>)`) and reports the count + a capped sample of divergent `file_id`s. It runs from two entry points against the *same* core: a hermetic pytest in the `integration` bucket (the standing CI gate) and a `just shadow-compare` CLI pointed at any DB.

The single most important architectural fact: **the derived side must reuse `services/stage_status.py`'s `done_clause(stage)` / `failed_clause(stage)` builders directly — NOT `stage_status_case(stage)`.** The CASE ladder puts `in_flight ≻ done`, so a file that is legitimately `ANALYZED` *and* has a queued re-analysis (scheduling-ledger row) would resolve to `in_flight` under the ladder and falsely flag as divergent. The gate asserts membership implications (`state=X ⇒ done(...)`), which map to the un-laddered `done_clause`/`failed_clause` correlated `exists()` predicates. This is the "implication, not equality" contract at the SQL level.

The second fact: **Phase 78 covers only ~half the invariants.** The enrich-completion states (METADATA_EXTRACTED, ANALYZED, ANALYSIS_FAILED, PROPOSAL_GENERATED) and the execution_log apply outcomes reuse Phase-78 clauses cleanly and are the *real* drift-catchers (they derive from pre-existing output rows). But the cloud-sidecar states (AWAITING_CLOUD/PUSHING/PUSHED), DUPLICATE_RESOLVED, and the proposal-status states (APPROVED/REJECTED and the MOVED/UNCHANGED apply-outcome status) have **no Phase-78 predicate** and must be asserted with raw ORM columns. Those raw-column invariants are near-tautological on a freshly-`032`-backfilled corpus (the row was created *from* the state) — that is the accepted D-03 circularity, and it must be called out in the check's docstring.

**Primary recommendation:** Author one module `src/phaze/services/shadow_compare.py` exposing an `INVARIANTS` registry (name, state value, derived predicate factory, soft/hard flag, §6.1 doc-ref) + an async `run_shadow_compare(session, *, sample_cap, verbose) -> Report`. Wrap it in (a) `tests/integration/test_shadow_compare.py` (fixture corpus reusing the `test_stage_status_equivalence.py` seed pattern; one RED cell + one consistent-corpus GREEN cell per invariant, plus soft-allowlist cells) and (b) a thin `src/phaze/cli/shadow_compare.py` argparse runner invoked by a `[group('db')] shadow-compare` justfile recipe via `uv run python -m phaze.cli.shadow_compare`.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Invariant definitions + divergence queries | API/Backend (`services/`) | — | Pure SQLAlchemy over the ORM models; reuses `services/stage_status.py`; no HTTP, no agent. Lives beside its Phase-78 dependency. |
| Hermetic corpus assertion (CI gate) | Test harness (`tests/integration/`) | Database (`:5433`) | DB-backed, fixture-seeded; lands in the `integration` bucket per D-01/CONTEXT canonical refs. |
| Live-DB operator run | CLI (`cli/shadow_compare.py`) | Database (restore) | Ops entry (D-01); reads a target-DB DSN from env/flag, prints report, sets exit code. Mirrors the existing `phaze` argparse CLI. |
| Derived-side predicates | API/Backend (`services/stage_status.py`, Phase 78) | — | REUSED, not reinvented (D-03). |

## Standard Stack

**No new dependencies (milestone non-goal §8: "No new dependencies").** Everything below is already in `pyproject.toml` and in active use in the exact files cited.

### Core (all already present)
| Library | Role in this phase | Evidence |
|---------|--------------------|----------|
| SQLAlchemy 2.0 (async) | `ColumnElement[bool]` anti-join builders, `select`, `func.count`, correlated `exists()`/`~exists()` | `[VERIFIED: src/phaze/services/stage_status.py]` — the reused builders |
| asyncpg (`postgresql+asyncpg://`) | async DB driver for the session | `[VERIFIED: tests/integration/test_stage_status_equivalence.py:73]` |
| pytest + pytest-asyncio (`asyncio_mode = "auto"`) | the hermetic gate | `[VERIFIED: pyproject.toml:137]` |
| psycopg (sync, for connectivity probe) | `pytest.skip` when PG down | `[VERIFIED: tests/integration/conftest.py:114]` |
| argparse (stdlib) | the `phaze` CLI subcommand pattern | `[VERIFIED: src/phaze/cli/__init__.py]` |
| structlog | degrade-safe warning logging (if any) | `[VERIFIED: src/phaze/services/stage_status.py:81]` |

**Installation:** none — `uv sync` already provides all of the above.

## Package Legitimacy Audit

**N/A — this phase installs no external packages** (milestone non-goal §8; verified: no `uv add`/`pip install` implied by any invariant). The shadow-compare core is pure first-party code over already-installed SQLAlchemy/pytest. No slopcheck/registry verification required.

## Architecture Patterns

### System Architecture Diagram

```
                          ┌──────────────────────────────────────────────┐
                          │  src/phaze/services/shadow_compare.py          │
                          │  (THE SHARED ASSERTION CORE — authored once)   │
                          │                                                │
   INVARIANTS registry ──▶│  INVARIANTS: list[Invariant]                   │
   (17 FileState values,  │    each = (name, state_value,                  │
    §6.1-doc-ref'd)       │            derived_predicate_factory,          │
                          │            hard|soft, §6.1 ref)                │
                          │                                                │
                          │  run_shadow_compare(session, sample_cap,       │
                          │                     verbose) -> Report         │
                          │    for inv in INVARIANTS:                       │
                          │      div = select(FileRecord.id).where(         │
                          │        FileRecord.state == inv.state,           │
                          │        ~inv.derived_predicate())  ◀── anti-join │
                          │      count = COUNT(*) ; sample = LIMIT cap      │
                          └───────┬──────────────────────────┬─────────────┘
                                  │ reuses (D-03)             │
                    ┌─────────────▼──────────────┐            │
                    │ services/stage_status.py    │           │
                    │ done_clause / failed_clause │           │
                    │ (Phase 78 ColumnElement)    │           │
                    │ + raw ORM cols for the      │           │
                    │   cloud/dedup/proposal gaps │           │
                    └─────────────────────────────┘           │
                                                               │
        ┌──────────────────────────────────┐   ┌──────────────▼───────────────────┐
        │ ENTRY A: pytest (CI gate)          │   │ ENTRY B: CLI (operator/rollout)    │
        │ tests/integration/                 │   │ cli/shadow_compare.py              │
        │   test_shadow_compare.py           │   │   argparse: --verbose --sample-cap │
        │ seeds a FIXTURE CORPUS             │   │             [--database-url]        │
        │ (reuse test_stage_status_          │   │ opens async_session on target DB   │
        │  equivalence.py seed helpers)      │   │ prints Report; sys.exit(nonzero    │
        │ asserts report.hard_fail == 0      │   │   iff any HARD invariant count>0)  │
        │ + soft cells counted, not failed   │   │ ← `just shadow-compare`            │
        └──────────────┬───────────────────┘   └──────────────┬─────────────────────┘
                       │                                       │
                 ephemeral PG :5433                     live-corpus restore DB
                 (TEST_DATABASE_URL)                    (env DSN / --database-url)
```

### Recommended Project Structure
```
src/phaze/
├── services/
│   └── shadow_compare.py      # NEW — the shared core: INVARIANTS + run_shadow_compare + Report
└── cli/
    └── shadow_compare.py      # NEW — thin argparse runner; `python -m phaze.cli.shadow_compare`
tests/integration/
└── test_shadow_compare.py     # NEW — hermetic fixture-corpus gate (integration bucket)
justfile                       # EDIT — add `[group('db')] shadow-compare:` recipe
```

### Pattern 1: Reuse `done_clause`/`failed_clause`, NEVER `stage_status_case`
**What:** The implication `state=X ⇒ <derived>` maps to the *un-laddered* correlated predicate, not the 4-way status label.
**Why it matters:** `stage_status_case` (`stage_status.py:170`) evaluates `in_flight` first; a legitimately-`ANALYZED` file with a queued re-analysis ledger row resolves `in_flight`, not `done`, and would falsely flag. Reusing `done_clause(Stage.ANALYZE)` (the raw `exists(analysis_completed_at IS NOT NULL)`) sidesteps the precedence and expresses the true membership implication.
**Example:**
```python
# Source: composes phaze.services.stage_status.done_clause (Phase 78)
from phaze.enums.stage import Stage
from phaze.services.stage_status import done_clause
from phaze.models.file import FileRecord, FileState

# state=ANALYZED ⇒ done(analyze).  Divergent = state matches AND derived FALSE.
divergent = select(FileRecord.id).where(
    FileRecord.state == FileState.ANALYZED.value,
    ~done_clause(Stage.ANALYZE),          # correlated ~exists — never stage_status_case
)
```

### Pattern 2: Invariant as data (registry), not code (branch)
**What:** Each invariant is a small dataclass/tuple in an `INVARIANTS` list; `run_shadow_compare` iterates. Soft allowlist is a boolean field on the entry, commented back to §6.1 (D-06).
**When to use:** here — it keeps the pytest side (parametrize over `INVARIANTS`) and the CLI side (iterate `INVARIANTS`) sharing one definition (D-01 "no logic duplicated"), and makes the allowlist un-growable-by-accident (a new soft entry is a visible diff).

### Pattern 3: count + capped-sample per invariant (D-05)
**What:** For each invariant run two cheap statements against the same anti-join predicate: `SELECT count(*)` and `SELECT id ... LIMIT sample_cap`. `--verbose` drops the LIMIT.
**Why:** avoids materializing a 200K-row divergence set; the count answers "is the gate green?", the sample answers "which files to investigate?".

### Anti-Patterns to Avoid
- **Using `stage_status_case`/`resolve_status` for the derived side** — reintroduces `in_flight` precedence and false-flags in-flight-over-done files (Pattern 1).
- **Asserting equality (`state ⇔ derived`)** — the derivation is deliberately MORE informative than the scalar (a file can be metadata-done AND analyze-done). Only assert `state ⇒ derived`. (Design §6.2; MIG-02.)
- **Asserting anything for `DISCOVERED`** — see Pitfall 2; a rescan-wiped-then-reprocessed file legitimately has output rows while `state='discovered'`. `DISCOVERED` gets NO invariant (documented, not silently omitted).
- **`LEFT JOIN ... IS NULL` / `NOT IN (subquery)` anti-joins** — house style is correlated `~exists(...)` only (`stage_status.py` docstring; a grep guard `LEFT JOIN|not_in\(` is used in 78-02). Match it.
- **String-interpolating the target DSN or state values into raw SQL** — use ORM columns / bound params (V5, Security Domain).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| "is this stage done/failed" per file | fresh raw SQL per state | `services/stage_status.done_clause` / `failed_clause` | D-03 mandate; it's the exact predicate phases 80–90 cut over to, and the gate must guard it |
| in_flight detection | reading `saq_jobs` | (don't — the gate needs no in_flight; use `done_clause`/`failed_clause` only) | avoids broker coupling AND the precedence false-flag (Pattern 1) |
| DB-backed test harness (session, PG skip-if-down, table create, per-test rollback) | new fixture from scratch | copy `tests/integration/test_stage_status_equivalence.py` `db_session` fixture + `_new_file`/seed helpers | proven, real-PG, bucket-correct; the seed helpers already cover metadata/analysis/fingerprint/proposal/execution rows |
| CLI arg parsing + async session | click/typer (new dep) | stdlib `argparse` + `phaze.database.async_session` | matches `src/phaze/cli/__init__.py`; non-goal: no new deps |

**Key insight:** 100% of the derived-side query logic already exists in `stage_status.py`; this phase is *composition + reporting + a fixture corpus*, not new derivation.

## The 17-value invariant table (the core deliverable — maps §6.1 to real columns)

`FileState` (`src/phaze/models/file.py:20-71`) has **17 members**. For each, the implication and its concrete derived source. "P78 clause" = a `stage_status.py` builder is reusable directly; "raw" = no Phase-78 predicate exists, assert with an ORM column (a Phase-78 gap, per research question 2). `[VERIFIED: src/phaze/models/*.py + alembic/versions/032_*.py backfill]`.

| # | FileState value | Implication `state ⇒ …` | Derived source (real column/table) | Reuse | Hard/Soft |
|---|-----------------|--------------------------|------------------------------------|-------|-----------|
| 1 | `DISCOVERED` "discovered" | **none (vacuous)** — baseline; derivation is more informative | — | — | **omit** (documented) |
| 2 | `METADATA_EXTRACTED` "metadata_extracted" | `metadata` row exists AND `failed_at IS NULL` | `metadata` | `done_clause(METADATA)` | HARD |
| 3 | `FINGERPRINTED` "fingerprinted" | **ALLOWLIST** — need not imply fingerprint success | `fingerprint_results` | — (never asserted) | **SOFT** |
| 4 | `ANALYZED` "analyzed" | `analysis.analysis_completed_at IS NOT NULL` | `analysis` | `done_clause(ANALYZE)` | HARD |
| 5 | `ANALYSIS_FAILED` "analysis_failed" | `analysis.failed_at IS NOT NULL` | `analysis` (032-backfilled) | `failed_clause(ANALYZE)` | HARD (circular*) |
| 6 | `AWAITING_CLOUD` "awaiting_cloud" | `cloud_job` row `status='awaiting'` | `cloud_job` | **raw** (no Stage) | HARD (circular*) |
| 7 | `PUSHING` "pushing" | `cloud_job` row `status='uploading'` | `cloud_job` | **raw** | HARD (circular*) |
| 8 | `PUSHED` "pushed" | `cloud_job` row `status='uploaded'` | `cloud_job` | **raw** | HARD (circular*) |
| 9 | `LOCAL_ANALYZING` "local_analyzing" | **ALLOWLIST** — no durable stored marker (Finding 3) | (transient ledger only) | — | **SOFT** |
| 10 | `PROPOSAL_GENERATED` "proposal_generated" | `proposals` row exists | `proposals` | `done_clause(PROPOSE)` | HARD |
| 11 | `APPROVED` "approved" | `proposals` row `status='approved'` | `proposals` | **raw** (P78 REVIEW done = *any* proposal) | HARD |
| 12 | `REJECTED` "rejected" | `proposals` row `status='rejected'` | `proposals` | **raw** | HARD |
| 13 | `EXECUTED` "executed" (legacy) | `proposals` row `status='executed'` | `proposals` | **raw** | HARD |
| 14 | `FAILED` "failed" (legacy, 0 writers**) | `proposals` row `status='failed'` | `proposals` | **raw** | HARD |
| 15 | `DUPLICATE_RESOLVED` "duplicate_resolved" | `dedup_resolution` row exists | `dedup_resolution` | **raw** (no Stage) | HARD (circular*) |
| 16 | `MOVED` "moved" | `proposals` row `status='executed'` (joint-write) | `proposals` | **raw** | HARD |
| 17 | `UNCHANGED` "unchanged" | `proposals` row `status='failed'` (joint-write) | `proposals` | **raw** | HARD |

\* **circular (D-03):** invariants 5,6,7,8,15 assert rows that `032` *created from* `files.state`, so they pass near-tautologically on a fresh backfill — call this out in the check docstring (CONTEXT specifics). The genuine drift-catchers are the pre-existing-output-row states: 2, 4, 10, 11–14, 16, 17.

\*\* `FileState.FAILED` has zero writers in `src/` (design §4.1 item 2); its invariant is still authored (D-04 comprehensive) but will simply find zero matching-state rows.

### Apply-outcome mapping — verified joint-write semantics
`routers/agent_proposals.py:47-49` `_FILE_FOLLOW`: `ProposalStatus.EXECUTED → FileState.MOVED`, `ProposalStatus.FAILED → FileState.UNCHANGED`, set jointly in one PATCH `[VERIFIED: src/phaze/routers/agent_proposals.py:114-116]`. Therefore the **authoritative** derived source for the apply-outcome states is `proposals.status` (design §4: "already authoritative — file state is a redundant cascade"), NOT `execution_log`.
- **Recommendation:** assert MOVED/UNCHANGED/EXECUTED/FAILED against `proposals.status` (raw `exists(RenameProposal WHERE file_id AND status=…)`), because the `execution_log` audit row can legitimately be absent (e.g. a proposal marked failed before any execution_log write) — `done_clause(APPLY)`/`failed_clause(APPLY)` go through `execution_log.status='completed'/'failed'` and would over-flag. `[ASSUMED]` that `proposals.status` is the safer target — planner should confirm during plan-check against a sampled live restore, but design §4 supports it.

## Runtime State Inventory

Not a rename/refactor/migration phase — this phase is an **additive verification harness** (no schema change, no data mutation, no code cutover). Section omitted per output spec. (The destructive data migration is Phase 90; the additive one was Phase 77.)

## Common Pitfalls

### Pitfall 1: Using `stage_status_case` for the derived side
**What goes wrong:** files that are legitimately done but have an in-flight retry ledger row resolve `in_flight` under the ladder and false-flag as divergent.
**Why:** `stage_status_case` precedence is `in_flight ≻ done ≻ failed` (`stage_status.py:184-189`).
**How to avoid:** reuse `done_clause`/`failed_clause` (un-laddered correlated `exists`) — never the CASE.
**Warning sign:** a GREEN consistent-corpus fixture cell fails only when you also seed a scheduling-ledger row.

### Pitfall 2: Asserting `DISCOVERED ⇒ no output rows`
**What goes wrong:** flags files that were rescan-wiped to `discovered` in the past (pre-Phase-77) but still carry orphaned output rows — exactly the historical drift the derivation *fixes*, not a bug.
**Why:** implication is one-directional; the derived side is deliberately more informative.
**How to avoid:** `DISCOVERED` gets NO invariant. Document it as intentionally-vacuous in the registry (a commented placeholder, not a silent gap) so "comprehensive" (D-04) is auditable.

### Pitfall 3: `fingerprint_results.status IN` spelling / index
**What goes wrong:** a bare `status IN ('success','completed')` predicate can miss `ix_fprint_success` if spelled differently from the index's `= ANY (ARRAY[...])`.
**Why:** the partial index is `postgresql_where=text("status = ANY (ARRAY['success','completed'])")` (`models/fingerprint.py:30`).
**How to avoid:** `FINGERPRINTED` is allowlisted (never asserted), so this doesn't bite here — but if any invariant touches fingerprint success, reuse `stage_status._DONE_FP` and `.in_(_DONE_FP)` (renders `= ANY`), matching Phase-59 WR-02.

### Pitfall 4: `execution_log` has no `file_id`
**What goes wrong:** joining execution rows to files directly fails.
**Why:** `ExecutionLog` FKs `proposal_id` only (`models/execution.py:30`).
**How to avoid:** join through `proposals` (as `done_clause(APPLY)` does) — but per the apply-outcome recommendation above, prefer `proposals.status` directly and avoid `execution_log` entirely for these invariants.

### Pitfall 5: CLI pointed at the wrong DB / `saq_jobs` guard
**What goes wrong:** the CLI silently runs against the dev DB, or a query references `saq_jobs`.
**How to avoid:** the CLI must read a target DSN from env (`DATABASE_URL`/app settings) with an optional `--database-url` override for a restore (D-02). The gate needs NO `saq_jobs` access at all (it uses `done/failed_clause` only) — keep it that way (Alembic-banner discipline is about migrations, but staying `saq_jobs`-free here also keeps the check deterministic on a restore where the broker table may be empty/absent).

## Code Examples

### Reused fixture-corpus seed pattern (the hermetic gate template)
The existing DERIV-04 test is the exact template — it already seeds `FileRecord` + every output table with per-test rollback and PG-skip-if-down. Reuse its `db_session` fixture and `_new_file`/`seed_*` helpers verbatim; add `state=` on the FileRecord and the cloud_job/dedup_resolution rows the new invariants need.
```python
# Source: tests/integration/test_stage_status_equivalence.py:78-131 (VERIFIED, in-tree)
@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    # probes BROKER_DSN; pytest.skip if PG down; Base.metadata.create_all;
    # seeds legacy-application-server Agent (FK); yields session; rollback at teardown
    ...

async def _new_file(session, *, state="discovered") -> uuid.UUID:
    fid = uuid.uuid4()
    session.add(FileRecord(id=fid, sha256_hash=uuid.uuid4().hex, original_path=f"/media/{fid}.mp3",
                           original_filename=f"{fid}.mp3", current_path=f"/media/{fid}.mp3",
                           file_type="mp3", file_size=1234, state=state))
    await session.flush()
    return fid
```

### The divergence query (per invariant)
```python
# count
count = (await session.execute(
    select(func.count(FileRecord.id)).where(
        FileRecord.state == inv.state, ~inv.predicate())
)).scalar_one()
# capped sample (drop .limit() when --verbose)
sample = (await session.execute(
    select(FileRecord.id).where(
        FileRecord.state == inv.state, ~inv.predicate()).limit(sample_cap)
)).scalars().all()
```

### CLI shape (mirror the existing `phaze` CLI)
```python
# Source pattern: src/phaze/cli/__init__.py (argparse + asyncio.run + async_session), VERIFIED
# invoked as: uv run python -m phaze.cli.shadow_compare [--verbose] [--sample-cap N] [--database-url DSN]
def main(argv=None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    report = asyncio.run(_run(args.database_url, args.sample_cap, args.verbose))
    print(report.render(verbose=args.verbose))
    return 1 if report.hard_fail_total else 0

if __name__ == "__main__":
    raise SystemExit(main())
```

### justfile recipe (group `db`, matching `db-upgrade` et al.)
```make
[doc('Run the state↔derived shadow-compare gate against the target DB (MIG-02). Exit nonzero on hard divergence.')]
[group('db')]
shadow-compare *ARGS:
    uv run python -m phaze.cli.shadow_compare {{ARGS}}
```

## State of the Art

Not applicable — no fast-moving library surface. The relevant "state of the art" is entirely in-repo and current as of this branch (`ce0c6434` + Phases 77, 78 merged):
- Phase 77 (`032`): failure-marker columns, `dedup_resolution`, `cloud_job.status='awaiting'`, 5 partial indexes — all present and mirrored in ORM `__table_args__`.
- Phase 78: `enums/stage.py` (`Stage`, `Status`, `resolve_status`, `eligible`) + `services/stage_status.py` (`done_clause`, `failed_clause`, `inflight_clause`, `stage_status_case`, `saq_detail`) — present, 100% covered, DERIV-04-locked.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Apply-outcome states (MOVED/UNCHANGED/EXECUTED/FAILED) are best asserted via `proposals.status`, not `execution_log` | The 17-value table / apply-outcome mapping | If a live corpus has apply-outcome files with NO proposals row (only an execution_log row, or neither), those invariants over- or under-flag. Mitigate: plan-check samples a live restore; the joint-write in `agent_proposals.py:114` strongly implies a proposals row always exists. |
| A2 | The `just shadow-compare` recipe belongs in the `db` group and the CLI belongs at `phaze.cli.shadow_compare` | Architecture Patterns | Cosmetic — a different home works identically; no functional risk. |
| A3 | `PUSHING⇒status='uploading'` and `PUSHED⇒status='uploaded'` (not `'submitted'`/`'running'`) | The 17-value table (rows 7-8) | `032` backfill sets exactly these (`alembic/versions/032:106,114`), so on a `032`-backfilled corpus it holds. But a *live-cloud-path* file (row created by the real push, not backfill) could carry `submitted`/`running`/`succeeded`. Implication may need to widen to `cloud_job row exists` (any status) for PUSHING/PUSHED, OR the invariant accepts the broader `status IN (...)`. **Planner must decide** — recommend asserting mere `cloud_job` row existence for PUSHING/PUSHED (design §6.2 says "cloud_job row exists with the corresponding status" — the safe reading is *a row exists*), and reserve the exact-status check for AWAITING_CLOUD (`status='awaiting'` is unambiguous). |

**Note:** claims A1–A3 are the only non-fully-verified points; everything else in this document is grounded in cited in-tree code.

## Open Questions (RESOLVED)

1. **PUSHING/PUSHED exact status vs. row-existence** (see A3)
   - What we know: `032` backfills `uploading`/`uploaded`; the live cloud path drives `submitted→running→succeeded`.
   - What's unclear: whether the live corpus at restore time has PUSHING/PUSHED files whose `cloud_job.status` has advanced past `uploading`/`uploaded`.
   - Recommendation: assert **`cloud_job` row exists** (any status) for PUSHING and PUSHED (matches design §6.2's spirit and survives a live cloud row); keep the exact `status='awaiting'` check only for AWAITING_CLOUD. Document the loosening in the invariant comment.
   - **RESOLVED:** adopted in `79-01-PLAN.md` Task 1 — PUSHING/PUSHED assert `cloud_job` row-existence only; AWAITING_CLOUD keeps the exact `status='awaiting'` check.

2. **Sample-cap default number** (Claude's discretion, D-05)
   - Recommendation: `20` (matches the CONTEXT "e.g. first 20"); expose `--sample-cap` to override and `--verbose` to uncap.
   - **RESOLVED:** adopted — default `20`, `--sample-cap` override + `--verbose` uncap wired in `79-02-PLAN.md` (CLI) over the `79-01-PLAN.md` core.

3. **Does the gate assert the soft-allowlist divergences are *nonzero-tolerated* or *present-and-counted*?**
   - Recommendation: count them unconditionally, print as "expected divergence (§6.1)", and assert only that they never contribute to `hard_fail_total`. The pytest side should have a positive cell proving a seeded FINGERPRINTED/LOCAL_ANALYZING divergence is counted-but-green (non-vacuous soft-allowlist proof).
   - **RESOLVED:** adopted in `79-01-PLAN.md` — soft allowlist `{fingerprinted, local_analyzing}` is counted, printed as "expected divergence (§6.1)", never contributes to `hard_fail_total`; a positive test cell proves a seeded soft divergence is counted-but-green.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | all commands (mandated) | ✓ | 0.11.26 | none (project constraint) |
| Docker | ephemeral PG/Redis `:5433`/`:6380` via `just test-db` | ✓ | present | CI provides; `pytest.skip` if PG down (bare run skips, not errors) |
| just | recipe runner | ✓ | 1.55.1 | direct `uv run` invocation |
| PostgreSQL 18 (test) | the integration gate + CLI target | ✓ (via `just test-db`) | postgres:18-alpine | test skips if absent; live run is deferred (D-02) |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** the integration test `pytest.skip`s when no PG is reachable (`tests/integration/conftest.py:116-121` idiom) — so a bare `uv run pytest` on a dev box without `just test-db` skips rather than errors; CI runs it against `:5433`.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`asyncio_mode = "auto"`) `[VERIFIED: pyproject.toml:137]` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`; bucket map `tests/buckets.json` |
| Quick run command | `uv run pytest tests/integration/test_shadow_compare.py -x` (needs `just test-db` up + `TEST_DATABASE_URL`/`PHAZE_QUEUE_URL` at `:5433`) |
| Full suite command | `just test-bucket integration` (in isolation — the standing CI gate) |
| Bucket | `integration` (per CONTEXT canonical refs; `test_partition_guard.py` enforces one-bucket-per-file) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MIG-02 | every HARD invariant flags a seeded divergence (RED cell) | integration | `uv run pytest tests/integration/test_shadow_compare.py -k divergent -x` | ❌ Wave 0 |
| MIG-02 | every HARD invariant passes on a consistent corpus (GREEN cell) | integration | `uv run pytest tests/integration/test_shadow_compare.py -k consistent -x` | ❌ Wave 0 |
| MIG-02 | SOFT allowlist (FINGERPRINTED, LOCAL_ANALYZING) counted but never fails | integration | `uv run pytest tests/integration/test_shadow_compare.py -k allowlist -x` | ❌ Wave 0 |
| MIG-02 | implication-not-equality: a more-derived-than-scalar file does NOT flag | integration | `uv run pytest tests/integration/test_shadow_compare.py -k implication -x` | ❌ Wave 0 |
| MIG-02 | hard-fail → nonzero exit (CLI) | integration/subprocess | invoke `run_shadow_compare` on a seeded-divergent corpus; assert `report.hard_fail_total > 0` and CLI `main()` returns 1 | ❌ Wave 0 |
| MIG-02 | one shared core, two entry points (D-01) | integration | assert the pytest and CLI both import `phaze.services.shadow_compare.run_shadow_compare` (no duplicated logic) | ❌ Wave 0 |
| MIG-02 | `DISCOVERED` has no invariant (documented) | shared (DB-free) | assert `DISCOVERED` absent from `INVARIANTS` state set (registry unit test) | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/integration/test_shadow_compare.py -x` (+ `uv run ruff check` / `uv run mypy` on touched files)
- **Per wave merge:** `just test-bucket integration` in isolation (+ `tests/shared/test_partition_guard.py`)
- **Phase gate:** full `integration` bucket green before `/gsd:verify-work`; the live-corpus CLI run recorded in VERIFICATION is DEFERRED (D-02).

### Observable / testable properties of this gate
1. **Soundness of each HARD invariant:** a seeded divergent row (state=X, derived-condition false) is counted and drives `hard_fail_total>0`.
2. **No false positives:** a consistent corpus (state=X with the correct derived rows) AND a *more-derived* corpus (state=METADATA_EXTRACTED but also analysis-completed) → zero HARD divergence (implication, not equality).
3. **Soft-allowlist non-flip:** seeded FINGERPRINTED/LOCAL_ANALYZING divergences increment their count but leave `hard_fail_total==0` and exit 0.
4. **Report shape (D-05):** per-invariant count + sample capped at `sample_cap`; `--verbose` uncaps; a totals line.
5. **Single-core identity (D-01):** both entry points call the same `run_shadow_compare`.

### Wave 0 Gaps
- [ ] `src/phaze/services/shadow_compare.py` — the shared core (INVARIANTS registry + `run_shadow_compare` + `Report`) — covers MIG-02
- [ ] `src/phaze/cli/shadow_compare.py` — thin argparse runner (`python -m phaze.cli.shadow_compare`)
- [ ] `tests/integration/test_shadow_compare.py` — hermetic fixture-corpus gate (reuse `test_stage_status_equivalence.py` `db_session`/seed helpers)
- [ ] `justfile` — `[group('db')] shadow-compare` recipe
- [ ] (optional) a DB-free registry unit test in `tests/shared/` asserting DISCOVERED omitted + allowlist == {FINGERPRINTED, LOCAL_ANALYZING} — if authored, it must import no SQLAlchemy to stay in `shared`; otherwise fold into the integration test.
- Framework install: **none** — pytest/pytest-asyncio/psycopg/asyncpg all present.

## Security Domain

`security_enforcement` is absent in `.planning/config.json` → treated as enabled. This is a **read-only DB verification harness** with a tiny surface.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | no auth surface (ops CLI + CI) |
| V3 Session Management | no | — |
| V4 Access Control | no | — |
| V5 Input Validation | **yes** | `--sample-cap` parsed as `int` via argparse `type=int`; `--database-url` passed to the async engine, never string-concatenated into SQL. All divergence queries are ORM `select()` with bound params / `ColumnElement` predicates — no interpolation. |
| V6 Cryptography | no | — |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via state value / sample-cap / DSN | Tampering | ORM-only queries + bound params (reuse `stage_status.py` builders); `sample_cap` is `int`; DSN goes to `create_async_engine`, not into a query string |
| Secret leakage (DSN with password) in CLI output/logs | Information Disclosure | Do NOT print the full DSN; the existing CLI prints secrets via `print()` only and never to a logger — here, print no DSN at all (only host/db name if anything). `[CITED: src/phaze/cli/__init__.py:16-17 token-never-logged discipline]` |
| Gate silently green (vacuous) | Repudiation / false assurance | non-vacuous meta-cells: every HARD invariant has a RED (seeded-divergent) cell; the soft allowlist has a "counted-but-green" cell (mirrors the DERIV-04 non-vacuous ELIG-04 pattern) |

## Project Constraints (from CLAUDE.md)

- **Python 3.14, `uv` only** — every command `uv run …`; never bare `pip`/`python`/`pytest`/`mypy`.
- **ruff** clean (line length 150); **mypy** strict (excludes `tests/`); **90% per-module coverage** floor + 95% combined gate (`coverage-combine`). The new `shadow_compare.py` must hit ≥90% (achievable — pure query logic, fully exercised by the fixture corpus, mirroring `stage_status.py`'s 100%).
- **Per-bucket isolation:** the new test must pass via `just test-bucket integration` *in isolation* (not merely the full suite); `tests/shared/test_partition_guard.py` enforces one-bucket-per-file.
- **DB tests** need `TEST_DATABASE_URL`/`PHAZE_QUEUE_URL` at `:5433` (conftest defaults to `:5432`); use `just integration-test` / `just test-db`.
- **Never `--no-verify`**; pre-commit (frozen-SHA hooks incl. bandit `-x tests -s B608`) must pass. The raw-`text()` SQL discipline: if any invariant uses `text()` (it shouldn't — ORM only), bandit B608 could flag it; prefer `ColumnElement`/`select()` throughout to avoid it.
- **PR per phase, worktree per phase, never push to `main`.**
- **Migrations never reference `saq_jobs`** — N/A (no migration this phase); the gate also needs no `saq_jobs` access.
- Keep `scripts/update-project.sh` / READMEs current if a new service surface is added (the CLI subcommand is a doc-worthy surface).

## Sources

### Primary (HIGH confidence) — all in-tree, VERIFIED this session
- `src/phaze/models/file.py:20-101` — the 17-member `FileState` enum + `ix_files_state`
- `src/phaze/services/stage_status.py` — `done_clause`/`failed_clause`/`inflight_clause`/`stage_status_case`/`saq_detail` (the D-03 reuse target)
- `src/phaze/enums/stage.py` — `Stage`/`Status`/`resolve_status`/`eligible` (DB-free twin)
- `alembic/versions/032_add_derived_status_schema.py` — backfill mappings (analysis_failed→failed_at; duplicate_resolved→dedup_resolution; awaiting_cloud→'awaiting'; pushing→'uploading'; pushed→'uploaded') + the 5 partial indexes
- `src/phaze/models/{analysis,metadata,fingerprint,cloud_job,dedup_resolution,proposal,execution}.py` — column shapes + partial indexes
- `src/phaze/routers/agent_proposals.py:47-116` — apply-outcome joint-write (`EXECUTED→MOVED`, `FAILED→UNCHANGED`, proposals.status authoritative)
- `src/phaze/routers/pipeline.py:885-945` — `retry_analysis_failed` (the sole FINGERPRINTED writer, D-06 justification)
- `src/phaze/services/backends.py:206-245` — `LocalBackend.dispatch` (LOCAL_ANALYZING: state flip + enqueue, **no cloud_job row**, Finding 3)
- `tests/integration/test_stage_status_equivalence.py` — the fixture-corpus + real-PG `db_session` template to reuse
- `tests/integration/conftest.py`, `tests/shared/test_partition_guard.py`, `tests/buckets.json` — bucket/harness conventions
- `justfile:75-209` — test/db recipe groups + `test-bucket`/`test-db`/`integration-test`
- `src/phaze/cli/__init__.py`, `pyproject.toml:[project.scripts] phaze` — the argparse CLI pattern
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §6.1/§6.2/§8 — the invariant list + two-step migration story
- `.planning/REQUIREMENTS.md:96` (MIG-02), `.planning/ROADMAP.md:324-333` (Phase 79 goal + 3 success criteria)

### Secondary / Tertiary
- None — no web/Context7 lookup needed; the phase adds no external library and every fact is in-repo.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; all reused code cited and present.
- Architecture (invariant map, dual-entry core, done/failed-clause reuse): HIGH — grounded in the actual `stage_status.py`, models, and `032` backfill. The two genuinely-open points (apply-outcome source A1; PUSHING/PUSHED exact-status vs row-existence A3) are flagged for plan-check.
- Pitfalls: HIGH — each derives from a specific in-tree precedence/schema fact (CASE precedence, execution_log has no file_id, rescan-wipe history).
- Validation architecture: HIGH — mirrors the existing DERIV-04 integration harness exactly.

**Research date:** 2026-07-08
**Valid until:** stable while Phases 77/78 remain merged and no reader cutover has landed (i.e. until Phase 80 starts touching `stage_status` consumers). Re-verify the apply-outcome and PUSHING/PUSHED status mappings against a live restore before the deferred D-02 run.
