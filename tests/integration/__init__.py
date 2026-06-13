"""Real-Postgres integration tests for the Phase 36 queue-backend migration.

Every test in this package opens a real :class:`saq.queue.postgres.PostgresQueue`
against the ephemeral integration-test Postgres broker (``just test-db`` /
``just integration-test``, host port 5433). They prove the migration-target
behaviors that are ONLY verifiable against a live Postgres broker:

* ``test_pg_queue_priority`` -- native ``ORDER BY priority, scheduled`` dequeue
  ordering and the ``now >= scheduled`` future-park gate (REQ-36-2);
* ``test_pg_dedup`` -- the ``ON CONFLICT (key)`` in-flight dedup no-op that returns
  ``None`` and re-enqueues only after the job reaches a terminal status (REQ-36-3).

The Phase 37 per-stage control-plane suite shares the ``stage_env`` fixture in
``conftest.py`` (a real ``build_pipeline_queue`` queue + a SQLAlchemy session on the same
DB + a seeded ``pipeline_stage_control``) and proves the service-helper semantics against
the live ``saq_jobs`` dequeue/count contract:

* ``test_stage_pause`` -- drain-style pause parks the queued backlog at SENTINEL while an
  active job drains, and the Pitfall-1 ``count("queued")`` -> 0 semantic (REQ-37-1);
* ``test_stage_priority`` -- live backlog reprioritization + the priority lower bound
  (below 0 is un-dequeueable; 0 is the floor) (REQ-37-2);
* ``test_stage_resume`` -- sentinel-guarded resume un-parks only SENTINEL rows and leaves a
  retry-backoff (``scheduled = now + delay``) row untouched (REQ-37-3);
* ``test_stage_concurrency`` -- a concurrent admin UPDATE vs worker dequeue produces no
  double-pickup and no deadlock (REQ-37-4).

The whole package is auto-marked ``integration`` by the ``tests/conftest.py``
collection hook (path rule), so ``pytest -m 'not integration'`` excludes it when
no broker is running.
"""
