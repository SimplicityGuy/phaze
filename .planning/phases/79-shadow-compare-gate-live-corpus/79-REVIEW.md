---
phase: 79-shadow-compare-gate-live-corpus
reviewed: 2026-07-08T00:00:00Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - src/phaze/services/shadow_compare.py
  - src/phaze/cli/shadow_compare.py
  - tests/integration/test_shadow_compare.py
  - justfile
findings:
  critical: 1
  warning: 2
  info: 1
  total: 4
resolved: 4
status: resolved
resolution_commit: ba489b62
---

> **Resolution (2026-07-08, commit `ba489b62`):** All 4 findings fixed inline during phase
> execution. CR-01 — module-level `_test`-DB guard in `test_shadow_compare.py` refuses any
> non-`_test` target (data-loss footgun closed). WR-01 — `_parse_dsn_or_exit` redacts DSN parse
> failures and the password-masking `URL` object is threaded to the engine. WR-02 —
> `--sample-cap` uses a `_non_negative_int` argparse type. IN-01 — sample query adds `ORDER BY id`.
> Integration bucket re-run green (130 passed); ruff/mypy/bandit clean.
---

# Phase 79: Code Review Report

**Reviewed:** 2026-07-08T00:00:00Z
**Depth:** standard
**Files Reviewed:** 4
**Status:** issues_found

## Summary

Reviewed the state↔derived shadow-compare gate: the shared assertion core
(`services/shadow_compare.py`), the thin argparse CLI (`cli/shadow_compare.py`), the
hermetic fixture-corpus integration gate (`tests/integration/test_shadow_compare.py`), and
the `justfile` recipe.

The core assertion logic is sound. The `INVARIANTS` registry is complete and correctly
scoped (16 entries = every non-`DISCOVERED` `FileState`, verified against
`src/phaze/models/file.py`), the anti-join predicate `and_(state == X, ~predicate())`
correctly encodes implication-not-equality, the soft allowlist is properly excluded from
`hard_fail_total` (verified `Report.hard_fail_total` sums only `not r.soft`), all derived
predicates use correlated `exists(...)` with bound parameters — no `text()` interpolation, no
LEFT-JOIN-null anti-pattern (B608 / SQLi hygiene clean), and the reused Phase-78
`done_clause`/`failed_clause` field references match the model definitions. The CLI honors
the read-only + file_id-only-output contract, and `main()` correctly returns `1` iff a HARD
invariant diverged.

The blocking concern is in the test harness, not the assertion logic: the new CLI-test fixture
issues a committed `TRUNCATE ... CASCADE` against a DSN that **defaults to the developer's dev
database**, creating a real data-loss hazard when the file is run outside `just
integration-test`. Two CLI robustness/secret-hygiene warnings follow.

## Critical Issues

### CR-01: Destructive `TRUNCATE agents CASCADE` runs against a DSN that defaults to the dev database

**File:** `tests/integration/test_shadow_compare.py:57-60, 306-318, 321-343`
**Issue:**
`SA_DSN` is derived exactly like the sibling harness and falls back to the **dev** database when
no env var is set:

```python
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL",
    "postgresql://phaze:phaze@localhost:5432/phaze")).replace("postgresql+asyncpg://", "postgresql://")
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")
```

Per the `justfile` (lines 4-9) and `CLAUDE.md`, port **5432** is the dev DB and **5433** is the
ephemeral test DB. Unlike every sibling integration test (which is rollback-isolated via the
`db_session` fixture), this file's `cli_corpus` fixture **commits** rows (`_cli_commit_file`) and,
on teardown, **commits a destructive truncate**:

```python
async def _cli_truncate_corpus() -> None:
    ...
    await conn.execute(text("TRUNCATE agents CASCADE"))   # wipes agents + files + proposals + cloud_job + ...
```

The connectivity probe only `pytest.skip`s when Postgres is **down** — it does not check *which*
database it hit. So running the sanctioned `just integration-test` is safe (it exports
`TEST_DATABASE_URL=...localhost:5433/phaze_test`), but the command `CLAUDE.md` explicitly documents
for single-file/single-test runs — `uv run pytest tests/integration/test_shadow_compare.py` — with a
dev stack up (`just up` → Postgres on 5432) and `TEST_DATABASE_URL` unset will connect to the live
dev database and `TRUNCATE agents CASCADE`, destroying the entire dev corpus (files, proposals,
cloud_job rows, dedup markers, agents). This is a data-loss defect newly introduced by this file;
the rollback-based siblings never truncate.

**Fix:** Refuse to run the committing/truncating CLI cells unless the target is unambiguously a
test database. Guard in `cli_corpus` before any commit/truncate:

```python
from sqlalchemy.engine import make_url

def _assert_test_db() -> None:
    db = (make_url(SA_DSN).database or "")
    port = make_url(SA_DSN).port
    if not (db.endswith("_test") or port == 5433):
        pytest.skip(f"refusing destructive TRUNCATE against non-test database {db!r}:{port} "
                    "(set TEST_DATABASE_URL to the ephemeral :5433/phaze_test DB or run `just integration-test`)")
```

Call `_assert_test_db()` at the top of the `cli_corpus` fixture (before `_cli_prepare_schema_and_seed_agent`).
Alternatively, scope the TRUNCATE to only the rows this fixture created rather than `CASCADE`-wiping
the whole corpus.

## Warnings

### WR-01: `--database-url` password leaks into stderr on any URL/engine error

**File:** `src/phaze/cli/shadow_compare.py:59-65, 79, 95-99`
**Issue:**
The module docstring and `_safe_target` promise the DSN password is "NEVER passed to `print()` or a
logger" (T-79-04). That holds on the happy path, but the DSN reaches two error surfaces uncaught:

- `main()` calls `_safe_target(args.database_url)` → `make_url(database_url)` (line 64/97). A malformed
  DSN makes `make_url` raise `sqlalchemy.exc.ArgumentError`, whose message embeds the **full URL string
  including the password** (`Could not parse SQLAlchemy URL from string '...'`). The uncaught exception
  prints a traceback with the password to stderr.
- `create_async_engine(database_url)` / first connection (line 79, 82-83) can likewise raise with the
  DSN in the message.

So a fat-fingered or wrong-scheme DSN defeats the stated secret discipline.
**Fix:** Wrap URL parsing/engine construction and re-raise a redacted error:

```python
try:
    url = make_url(database_url)
except Exception as exc:  # never surface the raw DSN
    raise SystemExit("shadow-compare: could not parse --database-url (check scheme/credentials)") from None
```

Apply the same redaction around `create_async_engine` / session open in `_run`, and never let the raw
`database_url` reach an uncaught traceback.

### WR-02: `--sample-cap` accepts negative values → Postgres `LIMIT` error

**File:** `src/phaze/cli/shadow_compare.py:45-48`; `src/phaze/services/shadow_compare.py:216-217`
**Issue:**
The docstring advertises "`--sample-cap` is parsed as `int` … V5 input validation," but `type=int`
only rejects non-integers, not negatives. A negative cap flows into
`sample_query = sample_query.limit(sample_cap)`; Postgres rejects a negative `LIMIT`
("LIMIT must not be negative"), so `--sample-cap -1` crashes the run with a raw DB error rather than a
clean operator message. The advertised validation is incomplete.
**Fix:** Validate non-negativity at parse time:

```python
def _nonneg_int(v: str) -> int:
    n = int(v)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n

parser.add_argument("--sample-cap", dest="sample_cap", type=_nonneg_int, default=20, ...)
```

## Info

### IN-01: Unordered `LIMIT` makes the capped sample non-deterministic

**File:** `src/phaze/services/shadow_compare.py:215-218`
**Issue:**
`select(FileRecord.id).where(condition).limit(sample_cap)` has no `ORDER BY`, so which `file_id`s land
in a capped sample is at Postgres' discretion and may vary run to run. This is harmless for the counts
(which use `func.count`) and for the current tests (rollback isolation guarantees a single divergent
row per case), but on the live corpus two consecutive operator runs can print different sample UUIDs
for the same divergence, which is confusing when triaging. Consider `.order_by(FileRecord.id)` before
`.limit(...)` for stable, reproducible samples.

---

_Reviewed: 2026-07-08T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
