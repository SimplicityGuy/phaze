"""The one wall-clock budget a test may put on a child process: a HANG GUARD, never a pass/fail bound (phaze-0vlnp).

THE SHAPE THIS REPLACES
-----------------------

Tests that spawn a real interpreter -- an import-boundary probe (``python -c "import
phaze.tasks.agent_worker"``), a second ``pytest`` against the real ``conftest.py``, the analysis
child -- each carried their own round number: ``timeout=20``, ``30``, ``60``, ``120``. Every one was
chosen on a quiet machine, and none of them was the thing the test asserts on. They exist so that a
WEDGED child fails the test instead of hanging the gate forever. What they actually measured was the
machine:

* ``tests/shared/core/test_task_split.py::test_agent_worker_does_not_import_phaze_database`` --
  ``TimeoutExpired`` at 20 s under ~6 concurrent pytest processes (phaze-37ovq's branch-check run,
  2026-09-28); 4.58 s alone.
* ``tests/shared/test_explicit_test_db_url_fails_closed.py::
  test_explicit_url_with_a_broken_connect_is_refused_before_collection`` -- ``TimeoutExpired`` at
  60 s in phaze-1l03t's merge-main validation and phaze-3sgw0's check-fast (2026-09-27), five
  full-suite gates running; 9-11 s alone (phaze-uzywu).

Both went red as the ONLY failure in a full-suite gate, in a shape indistinguishable from a real
regression. Measured distributions against every former budget are recorded on bead phaze-0vlnp.

WHAT THIS DOES INSTEAD
----------------------

* **One ceiling, set as hang protection.** :data:`CHILD_PROCESS_HANG_GUARD_SEC` is reachable only by a
  child that is neither finishing nor failing -- wedged. It matches the ceiling ``tests/_lock_barrier.py``
  already uses for a wedged lock contender, and the one ``tests/shared/test_test_db_session_exclusivity.py``
  already used for its real second ``pytest``. No green or red outcome depends on its value: every test
  that uses it asserts on the child's exit code and output.
* **Where a child waits on something of its own, that wait carries its own explicit bound** -- the
  fail-closed test's child refuses on ``tests.db_guard``'s ``connect_timeout``, not on this ceiling --
  so a slow machine lengthens the run and never changes its verdict.
* **Enforced, not advised.** ``tests/shared/test_child_process_budgets.py`` fails the build on any
  subprocess wait in ``tests/`` that passes a bare numeric ``timeout=``.

This is the ceiling for a child PROCESS. An in-process wait (``asyncio.wait_for``, a watchdog, a
polling deadline) is a different shape with a different remedy -- put the clock under the test's control
(``tests/analyze/services/pipeline/test_analysis_exec.py``'s ``_BeatClock``, phaze-avw21) or wait on the
condition itself (``tests/_lock_barrier.py``, phaze-lz69g).
"""

from __future__ import annotations


CHILD_PROCESS_HANG_GUARD_SEC = 300.0
