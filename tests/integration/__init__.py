"""Real-Postgres integration tests for the Phase 36 queue-backend migration.

Every test in this package opens a real :class:`saq.queue.postgres.PostgresQueue`
against the ephemeral integration-test Postgres broker (``just test-db`` /
``just integration-test``, host port 5433). They prove the migration-target
behaviors that are ONLY verifiable against a live Postgres broker:

* ``test_pg_queue_priority`` -- native ``ORDER BY priority, scheduled`` dequeue
  ordering and the ``now >= scheduled`` future-park gate (REQ-36-2);
* ``test_pg_dedup`` -- the ``ON CONFLICT (key)`` in-flight dedup no-op that returns
  ``None`` and re-enqueues only after the job reaches a terminal status (REQ-36-3).

The whole package is auto-marked ``integration`` by the ``tests/conftest.py``
collection hook (path rule), so ``pytest -m 'not integration'`` excludes it when
no broker is running.
"""
