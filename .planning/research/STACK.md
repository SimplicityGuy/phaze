# Stack Research — Derived Per-Stage Status Primitives

**Domain:** Internal refactor — retire linear `FileState`, derive per-entity/per-stage status via Postgres anti-joins at 200K-row scale
**Researched:** 2026-07-08
**Confidence:** HIGH (planner claims measured on a live PostgreSQL **18.4** container with a 200K-file synthetic corpus mirroring the real schema; SQLAlchemy/Alembic APIs verified via Context7 against the installed versions)

> **ZERO NEW DEPENDENCIES.** This document proposes **no** libraries. It answers: *which capabilities of the existing stack (PostgreSQL 18, SQLAlchemy 2.0.51, Alembic 1.18.4, SAQ 0.26.4) to use, and which to avoid.* Every "you could also use X" carries an explicit adopt/YAGNI verdict.

> **Version correction (verified, load-bearing):** the milestone brief and design say "Postgres 16". The deployed database is **`postgres:18-alpine`** (`docker-compose.yml:58`, `justfile:147/153`, and the running `phaze-test-db` = *PostgreSQL 18.4*). Every planner behavior below was measured on 18.4, not 16. This matters for §1 (the "Right Anti Join" the design's framing attributes to PG16 is present and used in 18) and for the EXPLAIN idioms in §2 (BUFFERS is now on by default in 18).

---

## Installed Versions (verified)

| Component | Installed | Source |
|-----------|-----------|--------|
| PostgreSQL | **18.4** (aarch64 alpine) | `docker-compose.yml`, live `SELECT version()` |
| SQLAlchemy | **2.0.51** | `uv.lock` |
| Alembic | **1.18.4** | `uv.lock` |
| asyncpg | 0.31.0 | `uv.lock` |
| SAQ | 0.26.4 (`saq[postgres]`) | `uv.lock` / `pyproject.toml:60` |
| greenlet | 3.5.2 | `uv.lock` (async ORM bridge) |

Alembic head is **031** (`031_add_route_control.py`); next migration is **032**. Migrations are **sync** (`def upgrade()`, plain `op.*`), 3-digit zero-padded string revisions, mirrored `downgrade()`.

---

## The six questions, answered

### 1. `NOT EXISTS` vs `LEFT JOIN … IS NULL` vs `EXCEPT` vs `NOT IN` — which does PG18 plan best?

**Recommendation: use `NOT EXISTS` (correlated) exclusively.** In SQLAlchemy: `~exists().where(...)` / `not_(exists()...)`.

**Measured on PG18.4, 200K files, `analysis` (1:1), no partial index yet:**

| Form | Plan node | Exec time | Correct? |
|------|-----------|-----------|----------|
| `NOT EXISTS (SELECT 1 …)` | `Hash Anti Join` | ~34 ms | ✅ |
| `LEFT JOIN … ON pred WHERE right.pk IS NULL` | `Hash Anti Join` (**byte-identical plan**) | ~30 ms | ✅ |
| `EXCEPT` | `HashSetOp Except` | ~45 ms | ⚠️ dedups left side |
| `NOT IN (subquery)` | `Seq Scan` + hashed SubPlan (1:1) / **`Materialize` rescan (1:N)** | 30 ms → **>170 s** | ❌ NULL-unsafe |

**Findings that decide the recommendation:**

1. **`NOT EXISTS` and the correctly-written `LEFT JOIN … IS NULL` produce the *identical* `Hash Anti Join` plan.** The PG16 "Right Anti Join" improvement (planner may hash the *smaller* side) is active in 18 — on the 1:N fingerprint table the planner chose `Parallel Hash Right Anti Join`, hashing the 200K `files` rows rather than the 300K `fingerprint_results` rows. So performance is a wash between these two forms; **the tiebreaker is correctness and readability.**

2. **`LEFT JOIN … IS NULL` has a silent-wrong-answer trap that `NOT EXISTS` does not.** The predicate must go in the **`ON` clause**, not the `WHERE`. Measured on the per-engine `fingerprint_results` table (2 rows/file):
   - `NOT EXISTS (… status IN ('success','completed'))` → **51 507** pending (correct)
   - `LEFT JOIN … ON file_id=id AND status IN (…) WHERE r.file_id IS NULL` → 51 507 (correct)
   - `LEFT JOIN … ON file_id=id WHERE r.status IS NULL` → **50 000 (WRONG)** — the pre-filter join emitted 350 000 rows and the `WHERE` dropped every file that had *any* engine row, silently under-counting by 1 507.
   Because three of this milestone's stages are 1:N (`fingerprint_results` per-engine, `tracklists`, `proposals`), the LEFT JOIN form is an active footgun here. `NOT EXISTS` cannot express it wrong.

3. **`EXCEPT` deduplicates its left input** (it is a set operation). `SELECT f.id FROM files EXCEPT …` happens to be safe only because `id` is unique; the moment anyone writes `SELECT f.file_type …` it collapses 200K rows to 3. It also cannot carry extra projected columns. Avoid.

4. **`NOT IN` is disqualified.** On the 1:N table its cost estimate was **418 million**; the hashed SubPlan spilled `work_mem` (270K candidate rows > 4 MB) and degraded to `Materialize` + linear rescan, which we had to cancel after ~3 minutes. Independently, `NOT IN (subquery)` returns **zero rows** if the subquery yields a single NULL — and while these `file_id` columns are non-null today, nothing structurally guarantees a future nullable projected column. **Never use `NOT IN` for these anti-joins.**

**Does it matter at 200K rows?** For the boolean *set* predicate: only in that `NOT IN` is catastrophic and `EXCEPT`/LEFT-JOIN-in-WHERE are wrong. `NOT EXISTS` and LEFT-JOIN-in-ON tie on speed. **But the shape of the *enclosing* query matters far more than the anti-join operator — see §3 and §6.** The single worst plan measured in this study was not an operator choice; it was putting `EXISTS` in a `GROUP BY`/`SELECT` list (792 ms, §6).

**Planner-behavior citation:** PG16 added RIGHT/OUTER anti-join support so the optimizer can hash the smaller relation ([PG16 release notes](https://www.postgresql.org/about/news/postgresql-16-released-2715/); [pganalyze](https://pganalyze.com/blog/5mins-postgres-16-faster-query-plans)); this is why the anti-join is symmetric under PG18 and both correct forms collapse to one plan.

---

### 2. Partial index sizing (`postgresql_where`), covering/INCLUDE, and EXPLAIN verification

**Recommendation:** add partial indexes **only for the sparse/selective predicates**, keyed on `file_id`, mirrored into ORM `__table_args__` per house style (`019`). Do **not** add a partial index for a dense predicate purely for the batch count — the planner won't use it there and it costs write bandwidth.

**Measured index usage and sizes (200K corpus, after `VACUUM ANALYZE`):**

| Partial index | Predicate | Selectivity | Size | Planner used it? |
|---------------|-----------|-------------|------|------------------|
| `ix_analysis_failed` | `failed_at IS NOT NULL` | ~4% (4K rows) | **144 kB** | ✅ `Index Only Scan` (830→18 buffers) |
| `ix_analysis_completed` | `analysis_completed_at IS NOT NULL` | ~60% (60K rows) | 1.8 MB | ❌ batch count chose `Seq Scan`; ✅ single-file/page probes |
| `ix_fprint_failed` | `status = 'failed'` | ~10% | 912 kB | ✅ (single-file lookups) |
| `ix_fprint_success` | `status IN ('success','completed')` | ~90% | 6.7 MB | ❌ for batch anti-join; ✅ single-file/page `EXISTS` |
| `ix_metadata_failed` | `failed_at IS NOT NULL` | sparse | 200 kB | ✅ |
| `ix_metadata_done` | `failed_at IS NULL` | ~95% (dense) | 3.5 MB | ❌ mostly |

**The rule the data teaches:** a partial index pays off **when its predicate is selective** (the failure markers: 4–10%, tiny 144–912 kB indexes the planner reaches for) and is **ignored for the whole-corpus count when its predicate matches most rows** (the "done" predicates on 1:1 tables at 60–95% — a hash-join of two seq scans is cheaper). So:

- **DO add** the sparse failure-marker partial indexes: `ix_analysis_failed WHERE failed_at IS NOT NULL`, `ix_metadata_failed WHERE failed_at IS NOT NULL`, and a `WHERE status='failed'` partial index on `fingerprint_results` (912 kB) for the failed-engine retry scan. These are cheap and demonstrably used.
- **Add the "done" partial indexes too, but justify them by the READ paths, not the stats count.** `ix_analysis_completed(file_id)` and `ix_fprint_success(file_id)` are ignored by the whole-corpus count (seq scan wins) but are used for **single-file** and **small-page** `EXISTS` reads — measured turning an 830-buffer seq scan into a 3-buffer `Index Only Scan` (`Heap Fetches: 0`). Net: they earn keep on the detail/list UI, not the poll.

**Covering / INCLUDE:** **YAGNI.** The anti-join predicate needs only `file_id` (the correlation key), and a partial index `(file_id) WHERE <marker>` already produces `Index Only Scan` with `Heap Fetches: 0` (measured — the partial `WHERE` plus `file_id` fully answers the `EXISTS`). `INCLUDE (...)` would only help if you projected non-key columns through the same index, which the derivation never does (it only needs existence). Do not add `INCLUDE`.

**How to verify an index is actually used (PG18 EXPLAIN patterns):**

```sql
-- PG18: BUFFERS is ON by default with ANALYZE (verified). No need for (ANALYZE, BUFFERS).
EXPLAIN (ANALYZE) SELECT f.id FROM files f
WHERE NOT EXISTS (SELECT 1 FROM analysis a
                  WHERE a.file_id = f.id AND a.failed_at IS NOT NULL);
```
Read the plan for, in order of what "used" means:
1. `Index Only Scan using ix_analysis_failed` with **`Heap Fetches: 0`** — index fully answered the probe (best case).
2. `Bitmap Index Scan on <idx>` feeding a `Bitmap Heap Scan` — used, with heap recheck.
3. A bare `Seq Scan … Filter: (<predicate>)` with high `Rows Removed by Filter` — index **not** used (fine for dense predicates; a red flag for sparse ones).
4. To prove the index *can* be used and quantify the win, wrap with `SET enable_seqscan = off;` and re-EXPLAIN: the counterfactual on `ix_analysis_completed` switched to `Index Only Scan` and shaved buffers/time — confirming it's a cost decision, not an unusable index.

Reference precedent in-tree: `018` (`ix_analysis_window_bpm_fine WHERE tier='fine'`), `019` (`uq_proposals_file_id_pending WHERE status='pending'`), `012` (`WHERE status='live'`), `014` (`WHERE revoked_at IS NULL`). House style mirrors every partial index into ORM `__table_args__` so `autogenerate` stays in sync — replicate that (design §5 already commits to it).

---

### 3. SQLAlchemy 2.x idioms for reusable predicates usable BOTH in `.where()` and as a per-row value

**Recommendation: plain module-level functions returning `ColumnElement[bool]`.** Not `column_property`, not `hybrid_property`, not `query_expression`. This is the most consequential architecture choice in the milestone and the evidence is decisive.

**Why not `column_property`:** a `column_property` with a correlated scalar subquery (verified via Context7 — the canonical `address_count` recipe) is emitted into **every SELECT of the mapped entity, whether or not you want it**. The derivation is 4 stages × (done/failed/in-flight) correlated subqueries; baking any onto `FileRecord` means every `select(FileRecord)` in the 40-reader codebase silently pays for subqueries it doesn't need, and projecting `EXISTS` into the select list plans **catastrophically** at corpus scale (§6). Rejected.

**Why not `hybrid_property.expression`:** it works and is type-clean in 2.0 via `@x.inplace.expression` + `@classmethod` returning `ColumnElement[bool]` (verified via Context7). But it couples the predicate to the `FileRecord` class, forces `cls`-based correlation, and makes the cross-table `saq_jobs`/`scheduling_ledger` predicates (which don't belong to `FileRecord`) awkward. A Python-side `stage_status(file, stage)` would still be a separate function, so the hybrid buys no single-source-of-truth. Rejected as unnecessary coupling.

**Why not `query_expression()` / `with_expression()`:** these load an ad-hoc expression onto a mapped attribute *only when requested* (verified via Context7 — `default None`, "does not populate objects already loaded"). Closer to right than `column_property`, but it still binds to the entity, doesn't compose into `.where()`, and adds a mapped attribute for no gain over an explicit function call. YAGNI.

**The winning idiom (verified: passes `uv run mypy` strict + `uv run ruff check` at line-length 150 against the real models):**

```python
"""Reusable stage predicates — one SQL source of truth, composable into where() and case()."""

from __future__ import annotations

from sqlalchemy import ColumnElement, String, cast, column, exists, literal, table
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult

_FINGERPRINT_OK: tuple[str, ...] = ("success", "completed")

# SAQ owns saq_jobs; scheduling_ledger is Alembic-managed. Both referenced as UNREGISTERED Core
# TableClauses — deliberately NOT mapped models and NOT in Base.metadata, so autogenerate can
# never emit DDL for saq_jobs (honours the 020/030 "NEVER reference saq_jobs" banner in ORM space).
_saq_jobs = table("saq_jobs", column("key", String), column("status", String))
_scheduling_ledger = table("scheduling_ledger", column("key", String), column("function", String))


def analyze_done() -> ColumnElement[bool]:
    return exists().where(AnalysisResult.file_id == FileRecord.id, AnalysisResult.analysis_completed_at.is_not(None))


def fingerprint_done() -> ColumnElement[bool]:
    return exists().where(FingerprintResult.file_id == FileRecord.id, FingerprintResult.status.in_(_FINGERPRINT_OK))
```

Compiled output (verified via `.compile(dialect=postgresql.dialect())`): each function renders a correctly **correlated** `NOT (EXISTS (SELECT * FROM analysis WHERE analysis.file_id = files.id AND …))`. The same function object drops into `select(FileRecord.id).where(not_(analyze_done()))` **and** into a `case()` (§4) with zero duplication.

**Key correctness detail:** use the argument-less `exists().where(...)` (correlating form), **not** `select(...).exists()` unless you add `.correlate()`. The bare `exists().where(<inner>.col == <outer>.col)` auto-correlates to the enclosing `files` — verified in the compiled SQL. For the SAQ key predicate, build the key with `literal(f"{fn}:") + cast(FileRecord.id, String)` (renders `%(param)s || CAST(files.id AS VARCHAR)`), matching the `<function>:<file_id>` key the `before_enqueue` chokepoint stamps (`deterministic_key.py`).

**mypy-strict note:** annotate every function `-> ColumnElement[bool]` (import from `sqlalchemy`). `and_`/`or_`/`not_` and the `|` operator on `ColumnElement[bool]` all stay typed; no `# type: ignore` needed. The probe compiled clean under the project's exact strict config (only errors were for the not-yet-added `failed_at` columns — expected, this milestone adds them).

---

### 4. Modelling the 4-valued status with a single source of truth

**Recommendation:** a Python `StrEnum` for the *values*, and `sqlalchemy.case()` composed from the §3 predicate functions for the *SQL-side* precedence — with the **precedence order encoded once** as data both sides share. Verified `case()` API via Context7 (positional `(when_expr, value)` tuples, `else_=`).

```python
import enum
from sqlalchemy import ColumnElement, case, literal

class StageStatus(enum.StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"

# precedence: in_flight ≻ done ≻ failed ≻ not_started  (design §2.3, load-bearing)
def analyze_status_sql() -> ColumnElement[str]:
    return case(
        (in_flight(Stage.ANALYZE), literal(StageStatus.IN_FLIGHT.value)),
        (analyze_done(),           literal(StageStatus.DONE.value)),
        (analyze_failed(),         literal(StageStatus.FAILED.value)),
        else_=literal(StageStatus.NOT_STARTED.value),
    )
```

**How to stop Python-side and SQL-side drifting (single source of truth):** the *predicate functions* (§3) are the shared truth — the SQL `case()` and any Python `stage_status(file, stage)` both consume the **same** `analyze_done()`/`analyze_failed()`/`in_flight()` definitions. Encode the precedence order **once** as an ordered list `[(IN_FLIGHT, in_flight), (DONE, done), (FAILED, failed)]` and build **both** the `case()` (iterate → `when` tuples) and the Python evaluator (iterate → first-truthy) from that one list. Then a code path cannot apply a different precedence than the SQL. A single parametrized test asserting the `case()` result equals the Python evaluator across a fixture matrix locks them together (put it in `tests/shared/` per the bucket rules).

**Do NOT** store the derived status; it is computed, never persisted (design §5). And do **not** `GROUP BY` this `case()` across the corpus — §6, it is ~30× slower than per-stage counts.

---

### 5. Alembic two-step additive-then-destructive migration for dropping a NOT NULL column on a live corpus

The design mandates the shape (`032` additive + shadow-compare gate + `033` destructive). Stack-specific guidance:

**`032` (additive, reversible) — `upgrade()`:**
1. `op.add_column("analysis", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))` and `metadata.failed_at` (+ `error_message` Text) — all **nullable, no default** (D-02). Nullable-add is metadata-only in PG, **no table rewrite** on 200K rows.
2. Backfill markers from `files.state` with **static, bound-param SQL** (the `019`/`031` no-f-string discipline): e.g. `UPDATE analysis SET failed_at = now() FROM files WHERE analysis.file_id = files.id AND files.state = 'analysis_failed' AND analysis.failed_at IS NULL`. Create dedup/cloud sidecar rows similarly.
3. `op.create_index(..., postgresql_where=sa.text("failed_at IS NOT NULL"))` for the sparse markers (§2).
4. **Never touch `files.state`. Never reference `saq_jobs`** (020/030 banner — the migration is DDL/data only; the `saq_jobs`/`scheduling_ledger` reads live in application code).

**`032` `downgrade()`** is cleanly reversible: `drop_index`, `drop_column` — the markers are re-derivable, so dropping them loses nothing the enum didn't already hold.

**`033` (destructive) — the reversibility problem you asked about:** dropping `files.state` destroys the source enum, so a *faithful* `downgrade()` is impossible. Handle it the way `019` did with its non-reversible dedupe DELETE — make `downgrade()` re-create the column and **reconstruct** the enum from the now-authoritative derived sources, documenting the one lossy case:

```python
def upgrade() -> None:
    op.drop_index("ix_files_state", table_name="files")
    op.drop_column("files", "state")

def downgrade() -> None:
    # Best-effort inverse: completion/decision/apply states are faithfully rebuilt from output
    # tables; the collapsed routing/dedup members from the 032 sidecars. NOT byte-identical —
    # derivation is strictly MORE informative than the scalar (a file can be metadata-done AND
    # analyze-done, which no single value encodes), so downgrade picks the highest-precedence
    # legacy member. Documented + accepted, mirroring 019's "cannot resurrect collapsed rows".
    op.add_column("files", sa.Column("state", sa.String(length=30), nullable=True))
    op.execute(sa.text("UPDATE files SET state = 'discovered' WHERE state IS NULL"))
    op.execute(sa.text(_REBUILD_STATE_SQL))  # static CASE over output tables + sidecars
    op.alter_column("files", "state", nullable=False)
    op.create_index("ix_files_state", "files", ["state"])
```

`_REBUILD_STATE_SQL` is a static `CASE`/`UPDATE … FROM` reconstruction — the shadow-compare implication logic (design §6.2) run in reverse. This is the honest pattern: **reversible-with-documented-lossiness**, matching the `019` precedent. Do not pretend byte-fidelity.

**Ordering / safety on the live corpus:** the two-migration split with the **shadow-compare gate between** is the safety mechanism (`032` deploys and runs for real; the gate proves derivation ⇒ legacy state on the actual corpus; only then `033`). Add the **quiesce step** — drain cloud-push lanes (`--profile drain`) before `033` so `PUSHING`/`uploading` rows aren't snapshotted mid-flight. Each migration gets an integration test in `tests/integration/test_migrations/` (constraint §8).

---

### 6. PG18 / SQLAlchemy features that make this easier — with explicit YAGNI verdicts

| Feature | Verdict | Rationale (measured where relevant) |
|---------|---------|-------------------------------------|
| **`GENERATED ALWAYS AS … STORED` column** for a denormalized status | **YAGNI (hard no)** | A stored, denormalized column by another name — exactly what design §5 forbids. Worse, a generated column can only reference *the same row's* columns, so it **cannot** express cross-table `EXISTS` over `analysis`/`saq_jobs`. Structurally impossible here, not just unwanted. |
| **Materialized view** of per-file status | **YAGNI** | Adds a refresh lifecycle (staleness, `REFRESH … CONCURRENTLY`, a unique-index requirement) and a second source of truth. The whole-corpus stats run in ~26 ms (below); per-file/page reads in <1 ms with §2 indexes. No refresh machinery justified. Revisit only if a *measured* poll regresses (design §5 gate). |
| **Statement-level trigger** to maintain a status cache | **YAGNI** | Same denormalization objection + it reintroduces the write-side coupling the enum deletion removes (the "writers clobber each other" problem). The derivation is the point. |
| **`count(*) FILTER (WHERE …)`** one-pass aggregate for the stats poll | **ADOPT (non-obvious win)** | The `/pipeline/stats` poll needs done/failed counts for several stages. One `LEFT JOIN` with `count(*) FILTER (WHERE marker)` per marker computes them in a **single scan of `files`, one round-trip** — measured ~95 ms for 3 markers across `files ⋈ analysis ⋈ metadata`. Pairs with the existing `get_stage_progress` shape. Standard SQL, zero new deps. |
| **Per-stage anti-join `count(*)`** (current `get_stage_progress` style) | **ADOPT / keep** | Whole-corpus `count(*) … NOT EXISTS` for one stage measured **~26 ms** (parallel `Hash Anti Join` → `Partial Aggregate`). This is the fast path; it *replaces* `get_pipeline_stats`' `GROUP BY state`. |
| **`GROUP BY (CASE WHEN EXISTS …)`** to mimic the old `GROUP BY state` in one query | **AVOID (measured trap)** | Tempting as a drop-in for the retiring `GROUP BY state`, but measured **792 ms** — forced a `Seq Scan` (est. cost 2.7M), triggered **JIT (49 functions)**, sorted 160K rows through the multi-branch CASE. **~30× slower** than per-stage anti-join counts. Do not collapse stats into one `GROUP BY case()`. |
| **`EXISTS` in the `SELECT`/projection list** for a page of file rows | **AVOID unless the page is materialized first** | Measured trap: `SELECT id, EXISTS(...), … FROM files ORDER BY id LIMIT 50` planned the `EXISTS` subplans as **hashed whole-table scans** (1875 buffers, 30 ms) — the planner can't push LIMIT through correlated subplans. **Fix (measured, adopt):** `WITH page AS MATERIALIZED (SELECT id FROM files ORDER BY id LIMIT 50) SELECT p.id, CASE …EXISTS… FROM page p` ran in **0.9 ms** (each `EXISTS` became a per-row index probe). For the single-row **detail** page the naive projection is already fine (0.27 ms). This governs the "file-row State display" reader (design §7): compute the status **per already-selected page**, never as a correlated projection over the full `files` scan. |
| **`scheduling_ledger` as the durable `in_flight` source** (vs the fragile `saq_jobs` read) | **Evaluate in planning — likely ADOPT for pending/eligibility** | Verified from SAQ source: `retry()` sets status→QUEUED and calls `_retry` — it does **not** re-run `before_enqueue`; the ledger row is written in that same chokepoint (`apply_deterministic_key`) and cleared **only on terminal**. So for the three stage functions the ledger is a **durable superset** of `saq_jobs(queued|active)`: a paused/parked job has both; a crashed-mid-job file has only the ledger (the exact window the ledger closes, per its docstring + the 44.5K over-enqueue incident). Reading `scheduling_ledger` (Alembic-managed, indexed PK) for `in_flight` lets the *pending/eligibility* queries **avoid the SAVEPOINT-wrapped `saq_jobs` coupling entirely**. Design D-01 recommends the **union** (`saq_jobs ∪ ledger`); the measurement shows the ledger is the load-bearing half. **Caveat to confirm:** holds only if every stage enqueue routes through the chokepoint hook (verify all three stage queues register it). Keep the `saq_jobs` read for the live **busy-pill** display (`get_stage_busy_counts`), where "active right now" is the actual question. |
| **CTE `MATERIALIZED` / `NOT MATERIALIZED` hints** | **ADOPT selectively** | Used above to fence the page before correlating. PG12+ honours the explicit `MATERIALIZED` keyword; verified it produced `CTE Scan` + per-row index probes. Use precisely for the "paginate then decorate" pattern; don't sprinkle it. |
| **PG18 `EXPLAIN` BUFFERS-by-default** | **Use in verification docs** | PG18 enables BUFFERS with `ANALYZE` automatically ([PG18 release notes](https://www.postgresql.org/docs/current/release-18.html); [depesz](https://www.depesz.com/2025/01/15/waiting-for-postgresql-18-enable-buffers-with-explain-analyze-by-default/)). When recording the mandated poll-latency measurement (design §5 Risks), a bare `EXPLAIN (ANALYZE)` shows buffers — cite `shared hit/read` as I/O evidence. |

---

## Alternatives Considered

| Recommended | Alternative | When the alternative would win |
|-------------|-------------|-------------------------------|
| `NOT EXISTS` (`~exists().where`) | `LEFT JOIN … ON pred WHERE pk IS NULL` | Never here — ties on plan/speed, loses on the 1:N silent-wrong-answer trap and readability. Only if you *also* need the joined row's columns in the output (you don't). |
| Module-level `ColumnElement[bool]` functions | `hybrid_property.expression` | If the predicate were purely single-table, genuinely a `FileRecord` domain attribute, *and* you wanted instance-level Python access for free. Cross-table `saq_jobs`/ledger predicates rule it out as the primary idiom. |
| Module-level functions | `column_property(scalar_subquery)` | If you wanted the value on *every* `select(FileRecord)` unconditionally and the table were small. At 200K rows with 40 readers, unconditional emission is a liability. |
| Per-stage anti-join counts + `FILTER` aggregate | `GROUP BY case()` | Never (measured ~30× slower). |
| Sparse-only failure-marker partial indexes | Index every predicate | If the "done" predicates were selective (they're 60–95% dense) — they aren't for the batch count, so add "done" indexes only for the read paths (§2). |

## What NOT to Use

| Avoid | Why | Use instead |
|-------|-----|-------------|
| `NOT IN (subquery)` for anti-joins | NULL-unsafe (one NULL → zero rows); measured 418M cost / >170 s with `work_mem` spill on the 1:N table | `NOT EXISTS` |
| `EXCEPT` for "pending" sets | Deduplicates the left input; can't carry extra columns; slowest correct form | `NOT EXISTS` |
| `LEFT JOIN` with the stage predicate in `WHERE` (not `ON`) | Silently under-counted the 1:N fingerprint set by 1 507 rows | `NOT EXISTS` (or predicate strictly in `ON`) |
| `GROUP BY (CASE WHEN EXISTS…)` over the corpus | 792 ms, JIT-triggered, ~30× slower than per-stage counts | Per-stage anti-join `count(*)` or `count(*) FILTER (…)` |
| `EXISTS` in the SELECT list over an un-materialized `files` scan | Planner can't push LIMIT through correlated subplans → whole-table hashed subplans (1875 buffers for 50 rows) | `WITH page AS MATERIALIZED (… LIMIT n)` then correlate (0.9 ms) |
| A mapped model / `Base.metadata` entry for `saq_jobs` | `autogenerate` would try to emit DDL for the SAQ-owned table (020/030 banner) | Unregistered Core `table("saq_jobs", column(...))`, or static `text()` SQL in a SAVEPOINT |
| Denormalized status column / generated column / matview / trigger | Reintroduces the write-side coupling the milestone deletes; §5 explicit non-goal; generated columns can't cross tables | Derive via the §3 predicates |
| `INCLUDE`-covering indexes on the markers | `EXISTS` needs only `file_id`; partial `(file_id) WHERE marker` already gives `Heap Fetches: 0` | Plain partial index on `file_id` |

## Version Compatibility

| A | Compatible with | Notes |
|---|-----------------|-------|
| PostgreSQL **18.4** | SQLAlchemy 2.0.51 / asyncpg 0.31 | RIGHT anti-join (PG16+) active → `NOT EXISTS`/LEFT-JOIN symmetric; `EXPLAIN ANALYZE` BUFFERS default-on |
| SQLAlchemy 2.0.51 | `case()`, `exists().where()`, `not_`, `ColumnElement[bool]` | All verified emitting correlated `NOT EXISTS`; strict-mypy clean with `-> ColumnElement[bool]` annotations |
| Alembic 1.18.4 | SQLAlchemy 2.0 | Sync migrations, `op.create_index(postgresql_where=sa.text(...))` (019 precedent); mirror into ORM `__table_args__` for autogenerate parity |
| SAQ 0.26.4 | `saq_jobs` table | `retry()` does not re-run `before_enqueue`; ledger written once at enqueue, cleared at terminal → ledger ⊇ live saq_jobs for stage functions |

---

## Sources

- **Live measurement** — PostgreSQL 18.4 (`phaze-test-db`, `postgres:18-alpine`), 200K-file synthetic corpus mirroring `files`/`metadata`/`fingerprint_results`/`analysis`/`saq_jobs`(SAQ's real DDL)/`scheduling_ledger`; all EXPLAIN ANALYZE plans, row-count equivalence, index-usage/size numbers above — **HIGH confidence** (empirical, this schema, this PG version). DB dropped after measurement; no residue left in the repo.
- **Code verification** — candidate `predicates.py` type-checked with `uv run mypy` (strict, project config) and `uv run ruff check` (line-length 150) against the real models: clean. Compiled SQL inspected via `.compile(postgresql.dialect())`. SAQ `retry()`/`before_enqueue` read from installed `saq/queue/base.py`; SAQ DDL from `saq/queue/postgres_migrations.py`. **HIGH.**
- **Context7 `/websites/sqlalchemy_en_20_orm` & `_core`** — `hybrid_property.inplace.expression` typing, `column_property` correlated-subquery emission, `query_expression`/`with_expression` semantics, `case()` signature, `exists()` correlation. **HIGH.**
- [PostgreSQL 16 release notes](https://www.postgresql.org/about/news/postgresql-16-released-2715/) + [pganalyze: PG16 anti-joins](https://pganalyze.com/blog/5mins-postgres-16-faster-query-plans) — RIGHT/OUTER anti-join planner improvement. **HIGH.**
- [PostgreSQL 18 release notes](https://www.postgresql.org/docs/current/release-18.html) + [depesz: BUFFERS default](https://www.depesz.com/2025/01/15/waiting-for-postgresql-18-enable-buffers-with-explain-analyze-by-default/) — EXPLAIN BUFFERS default-on. **HIGH.**
- In-tree precedent — migrations `012`/`014`/`018`/`019`/`031`, models `file.py`/`analysis.py`/`fingerprint.py`/`metadata.py`/`proposal.py`/`cloud_job.py`/`scheduling_ledger.py`, `services/pipeline.py:299/466`, `tasks/_shared/deterministic_key.py`. **HIGH.**

---
*Stack research for: derived per-stage status primitives (Parallel Enrich DAG milestone)*
*Researched: 2026-07-08 · PostgreSQL 18.4 · SQLAlchemy 2.0.51 · Alembic 1.18.4 · SAQ 0.26.4*
