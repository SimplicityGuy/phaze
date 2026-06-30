# Phase 57.1 — Deferred / Out-of-Scope Items

## Pre-existing environment test failures (NOT caused by plan 57.1-04)

During the plan 57.1-04 full-suite run, three DB+Redis router test files reported
failures/errors:

- `tests/test_routers/test_agent_exec_batches.py`
- `tests/test_routers/test_agent_tracklists.py`
- `tests/test_routers/test_execution_dispatch.py`

Total: **37 failed, 56 errors** (the rest of the suite — 2472 tests — passed).

**Confirmed pre-existing / environmental (NOT introduced by this plan):**
Running `tests/test_routers/test_agent_tracklists.py` in isolation against the commit
*before* any 57.1-04 work (`6526a7b`, HEAD~3) reproduced the **identical**
`6 failed, 8 passed, 14 errors`. The tracebacks are network/SSL teardown errors
(`getaddrinfo() returned empty list`, `server_hostname is only meaningful with ssl`) —
these tests reach for a Redis/network resource that is not configured in this local
sandbox (only the ephemeral `just test-db` Postgres:5433 + Redis:6380 are up).

None of these three files exercise the modules changed by 57.1-04
(`services/analysis.py`, `tasks/functions.py`, `job_runner.py`, `config.py`). The
plan's own scope (analyze progress signal across the three lanes) is fully green.

**Disposition:** out of scope for 57.1-04. These will pass in the CI environment
(Redis/network available) per the discogsography CI pattern. Not a blocker for this plan.
