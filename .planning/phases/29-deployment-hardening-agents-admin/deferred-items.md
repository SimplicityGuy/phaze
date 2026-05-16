# Phase 29 Deferred Items

## From Plan 29-05 (2026-05-16)

- `tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue`
  fails because Plan 29-03 removed the `agent-worker` block from root `docker-compose.yml` (app-server-only invariant).
  The agent-worker now lives in `docker-compose.agent.yml` (created by Plan 29-04, parallel wave).
  This test should be updated by Plan 29-04 (or a follow-on plan) to scan `docker-compose.agent.yml` as well.
  Out of scope for Plan 29-05 (D-21 / model bootstrap).
