---
phase: 78
slug: derivation-layer-eligibility-anti-drift-test-harness
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-08
---

# Phase 78 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| agent process → shared code | `enums/stage.py` is imported inside the Postgres-free compute/file-server agent worker; it must not transitively pull in `phaze.database` / `sqlalchemy` (Phase 26 D-03 boundary). | Plain scalars only (no DB handle) |
| control-plane query → Postgres | `stage_status.py` `ColumnElement` builders compose into control-side SELECTs; all operands are ORM columns / bound params. | Derived per-stage status (no untrusted input) |
| SAQ-owned `saq_jobs` → derivation read | `saq_jobs` is read (never written) as a corroborating detail only, SAVEPOINT-isolated, static SQL; Alembic never references it. | Read-only queued/active counts |

No new network endpoint, no external/untrusted input, no data write in this phase — it is a pure DB-free transform plus additive read-only `ColumnElement` builders.

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-78-01 | Elevation / boundary violation | `enums/stage.py` import graph on the agent path | mitigate | DB-free module: no `phaze.models`/`phaze.database`/`sqlalchemy` import; subprocess banned-import guard test enforces it | closed |
| T-78-02 | Tampering | resolver scalar inputs | accept | Pure function over plain scalars owned by caller; no interpolation, no SQL, no side effects, no persisted state | closed |
| T-78-03 | Tampering (SQL injection) | derived predicate builders | mitigate | `ColumnElement`/`exists()`/bound params only; sole raw `text()` is `saq_detail` with static status allowlist, no interpolated operand | closed |
| T-78-04 | Denial of Service | `/pipeline/stats` hot poll on a `saq_jobs` read hiccup | mitigate | `saq_detail` `begin_nested()` SAVEPOINT-wrapped, degrades to safe default on ANY error; `in_flight` from durable ledger, not `saq_jobs` | closed |
| T-78-05 | Tampering / queue-state repudiation | migration/derivation touching SAQ-owned `saq_jobs` | mitigate | `saq_jobs` read-only, detail-only; no new Alembic migration added; phase touched zero alembic files | closed |
| T-78-06 | Repudiation | `in_flight` source ambiguity (crashed-mid-run falsely not_started) | mitigate | Written D-01 decision record fixes `scheduling_ledger` as authoritative + durable; `inflight_clause` = ledger-row-exists | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

### Verification Evidence

- **T-78-01** — `grep -nE "import (phaze\.models|phaze\.database|sqlalchemy)" src/phaze/enums/stage.py` returns nothing. Module top imports only `enum` and `typing` (`Mapping` under `TYPE_CHECKING`). Subprocess banned-import guard present at `tests/shared/test_stage_resolver.py:128` (`test_stage_module_stays_db_free`) — runs `import phaze.enums.stage` in a fresh subprocess and asserts `phaze.models`/`phaze.database`/`sqlalchemy` are absent from `sys.modules` (mirrors `tests/shared/core/test_task_split.py`). CLOSED.
- **T-78-02** — `resolve_status`/`eligible` (`src/phaze/enums/stage.py:135,167`) and their `_*_status` twins perform no I/O, no DB access, no writes, no string interpolation into any sink; they read plain scalars and return an enum. Acceptance still valid — logged below. CLOSED.
- **T-78-03** — All `done_clause`/`failed_clause`/`inflight_clause` predicates built via the SQLAlchemy expression API (`exists`, `select`, `.in_`, `func.concat`, `cast`, bound comparisons). The only raw `text()` is `_SAQ_DETAIL_SQL` (`src/phaze/services/stage_status.py:195`): a static literal with the status allowlist `('queued','active')` and NO f-string/`.format`/`%`/`+` interpolation of any operand. The two f-strings at lines 117/147 are `ValueError` messages, not SQL. CLOSED.
- **T-78-04** — `saq_detail` (`src/phaze/services/stage_status.py:198`) wraps its read in `async with session.begin_nested():` and has `except Exception:` that logs and returns the zeroed safe default `{queued:0, active:0}` with no re-raise. `inflight_clause` reads `SchedulingLedger`, never `saq_jobs`, for the boolean. Proven by `test_inflight_savepoint_degrade` (`tests/integration/test_stage_status_equivalence.py:431`) — drops `saq_jobs` mid-test, asserts safe default with no raise and `in_flight` still `True` from the ledger. CLOSED.
- **T-78-05** — `stage_status.py` issues SELECT-only against `saq_jobs`. `git log main..HEAD --name-only` shows zero alembic files touched in phase 78 (only the 4 impl/test files + eligibility DAG test changed). Migration 032's `saq_jobs` mentions are docstring/comment banners (negative "must never reference" assertions), not DDL. CLOSED.
- **T-78-06** — D-01 decision record present verbatim in the `stage_status.py` module docstring (`src/phaze/services/stage_status.py:33-55`): `scheduling_ledger` authoritative, `saq_jobs` corroborating-only, durability rationale (guards the 44.5K over-enqueue class), union/ledger-alone alternatives rejected. `inflight_clause` (line 150) implements ledger-row-exists via `exists(select(SchedulingLedger.key).where(...))`. CLOSED.

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-78-01 | T-78-02 | `resolve_status`/`eligible` are pure functions over plain scalars owned by the caller — no interpolation, no SQL, no side effects, no persisted state. Low-value, no attack surface: a caller supplying malformed scalars only mis-derives its own status. No trust boundary is crossed by the scalar inputs themselves. | robert@simplicityguy.com | 2026-07-08 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-08 | 6 | 6 | 0 | gsd-security-auditor |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-08
