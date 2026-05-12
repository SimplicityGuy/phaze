- **Test isolation regression in full-suite runs** (observed during 26-07 execution, 2026-05-12).
  Running `uv run pytest tests/test_routers/` produces ~131 errors with
  `UniqueViolationError: Key (id)=(legacy-application-server) already exists.`
  The same tests pass in small groups and individually. Root cause is the
  shared test Postgres DB + the legacy-agent seed in `async_engine` fixture
  colliding under parallel/sequential test reuse. Not introduced by Plan 26-07;
  affects unrelated routers. Possible fixes: per-test transaction rollback,
  per-test DB schema, or seed the legacy agent with `INSERT ... ON CONFLICT DO NOTHING`.
