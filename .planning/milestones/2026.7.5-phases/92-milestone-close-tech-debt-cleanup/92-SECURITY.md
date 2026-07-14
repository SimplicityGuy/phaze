---
phase: 92
slug: milestone-close-tech-debt-cleanup
status: verified
threats_open: 0
asvs_level: 1
register_authored_at_plan_time: true
created: 2026-07-13
---

# Phase 92 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time (all 5 PLANs carry `<threat_model>` blocks); this audit verifies each
> mitigation exists in the shipped implementation. This is a milestone-close tech-debt cleanup phase —
> behavior-preserving except the CLEAN-01 latency win; almost all changes are test-infra hermeticity.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| `get_stage_progress` fan-out → asyncpg pool | N extra concurrent session checkouts every 5s dashboard poll contend for the deliberately-lean per-worker pool (post-PgBouncer-incident cap: pool_size=5/max_overflow=5=10) | Read-only aggregate counts; no PII |
| test fixture ↔ shared DB connection | Test-isolation boundary; a leak here is a hermeticity failure, not a runtime security surface (test-infra only) | Test rows only, rolled back per test |
| production fan-out ↔ per-test connection (under fixture) | The `_route_stats_fanout` monkeypatch reroutes `get_stage_progress`'s production sessions onto the per-test connection; test-infra only, reverted per test | Test rows only |
| full test suite ↔ shared test DB | Verification-only; the D-08 gate confirms no cross-test state leak across the whole suite | Test rows only |
| source comments | Comment-only change; no code path, input, or data flow crosses any boundary | None |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-92-01-CMT | Tampering | source comments (`backends.py`, `agent_files.py`) | accept | Comment-only edit, byte-verifiable via git diff; anti-drift guards re-run (verifier confirmed KubeConfig comment count=1, ruff/mypy clean) | closed |
| T-92-01-SC | Tampering | npm/pip/cargo installs | accept | Zero package installs in Phase 92; verified no `pyproject.toml`/`uv.lock` diff since phase base — no supply-chain surface | closed |
| T-92-02-DoS | Denial of Service | `get_stage_progress` concurrent session fan-out vs lean pool | mitigate | Fan-out bounded by a fresh-per-poll `asyncio.Semaphore(4)` (`_stats_fanout()`, `pipeline.py`); `_read_in_own_session` wraps session acquisition so a `pool_timeout` degrades the node to its safe default (`logger.warning("stage_progress_degraded")`) rather than 500ing the poll; ≥6 pool slots stay free for request traffic. D-05 200K measurement validated the cap (poll p50 861ms direct / 1072ms endpoint). | closed |
| T-92-02-SKEW | Information Disclosure (stale read) | independent-session MVCC snapshots | accept | Sub-second cross-node snapshot skew under live writes is invisible on a 5s dashboard poll; documented in `92-VERIFICATION.md`, not claimed as strict identity (code-review WR-02 accepted) | closed |
| T-92-02-SC | Tampering | npm/pip/cargo installs | accept | Zero package installs (see T-92-01-SC evidence) | closed |
| T-92-03-ISO | Tampering (test-state leak) | conftest transactional fixture | mitigate | `AsyncSession(join_transaction_mode="create_savepoint")` on a per-test outer transaction; teardown `await outer.rollback()` discards ALL in-test commits (`tests/conftest.py`). Mutation-safe contract test `tests/shared/test_conftest_hermeticity.py` proves rollback isolation (break recipe documented + exercised); production code untouched. | closed |
| T-92-03-VIS | Information Disclosure (false-zero read) | production fan-out reading a different connection than the seed | mitigate | `_route_stats_fanout` monkeypatches the deferred-import seam `phaze.database.async_session` onto the per-test `_db_connection` create_savepoint factory + sets `pipeline._STATS_FANOUT = asyncio.Semaphore(1)` (serialize onto the one shared conn). Contract-test assertion 3 + the 8 previously-RED `test_stage_progress.py` cells now green prove non-zero reads; monkeypatch is per-test and reverted; production default stays `None` (fresh Semaphore(4)). | closed |
| T-92-03-SC | Tampering | npm/pip/cargo installs | accept | Zero package installs (see T-92-01-SC evidence) | closed |
| T-92-04-VIS | Tampering (false-green test) | migrated verify reads (21 sites / 13 files) | mitigate | Verify reads rebind to the shared `verify` fixture (outer-transaction connection) so they still assert real commits; assertion bodies unchanged; 8 genuine cross-connection concurrency tests relocated to `tests/integration/` on the `committed_db` real-engine fixture rather than weakened. Per-bucket green in isolation confirms. | closed |
| T-92-04-SC | Tampering | npm/pip/cargo installs | accept | Zero package installs (see T-92-01-SC evidence) | closed |
| T-92-05-GATE | Tampering (undetected leak) | per-bucket isolation gate | mitigate | All 9 buckets run cold in isolation (D-08); port footgun (5433) + colima flake + zero/stale-fan-out-read explicitly distinguished so a real hermeticity leak cannot hide behind an env error. Verifier independently re-ran the gate live: 3,411 tests, 0 failed across all buckets. Root fix = idempotent get-or-insert `test-fileserver` seeding (order-independent by construction). | closed |
| T-92-05-SC | Tampering | npm/pip/cargo installs | accept | Zero package installs (see T-92-01-SC evidence) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-92-01 | T-92-01-CMT | Comment-only edits are byte-verifiable via git diff and re-run anti-drift guards; no executable surface | Phase plan (D-09/D-10) | 2026-07-13 |
| AR-92-02 | T-92-0{1..5}-SC | Zero package installs across the whole phase; no `pyproject.toml`/`uv.lock` diff → no supply-chain attack surface introduced | Phase plan (RESEARCH §Standard Stack) | 2026-07-13 |
| AR-92-03 | T-92-02-SKEW | Sub-second independent-session MVCC snapshot skew is invisible on a 5s dashboard poll; explicitly not claimed as strict identity (documented in 92-VERIFICATION.md; code-review WR-02) | Phase plan + code review | 2026-07-13 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-13 | 12 | 12 | 0 | /gsd:secure-phase (orchestrator, code-grounded evidence + prior verifier live re-run) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-13 — 12/12 threats closed; 5 mitigations code-grounded, 7 accepted-risk dispositions documented. No implementation files modified by this audit.
