# Phase 29 Deferred Items

## From Plan 29-05 (2026-05-16) — RESOLVED by Plan 29-04 (2026-05-16)

- ~~`tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue`~~
  Resolved by Plan 29-04: the test now scans BOTH `docker-compose.yml` and
  `docker-compose.agent.yml`. The agent-worker now lives in
  `docker-compose.agent.yml::worker` (PHAZE_ROLE=agent, command
  `uv run saq phaze.tasks.agent_worker.settings`), so the Phase 27 UAT
  gap-13 invariant is again codified.
