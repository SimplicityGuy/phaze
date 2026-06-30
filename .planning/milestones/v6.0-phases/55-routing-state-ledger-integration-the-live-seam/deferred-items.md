# Deferred items — Phase 55

## Pre-existing test-harness flakiness (out of scope for 55-04)

`tests/test_routers/test_pipeline.py` intermittently errors at SETUP time with an
`IntegrityError` inserting into `agents` (`pk_agents` / `ck_agents_id_charset`). The failing
test is non-deterministic — it moves between unrelated tests across runs (`test_recover_*`,
`test_enqueue_proposals_background`, `test_extract_metadata_enqueues`) and depends on
pytest-randomly ordering. Each failing test passes in isolation.

Root cause appears to be the function-scoped `async_engine` fixture (`tests/conftest.py:131`)
running `Base.metadata.create_all` + a legacy-agent seed per test against the shared test
Postgres; under churn the drop/create + seed can race or leak. This is unrelated to plan
55-04's ledger-scoped backfill changes (all 10 `backfill` tests pass deterministically, mypy
clean). Logged per the executor scope boundary; not fixed here.
