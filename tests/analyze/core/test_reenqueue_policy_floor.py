"""Recovery replay of a legacy NULL-bounds ``process_file`` ledger row (phaze-plpnf).

Live logs on 2026-08-11 showed a steady stream of ``process_file`` jobs dying at exactly 600s
with a SAQ ``TimeoutError``, carrying ``timeout=600`` / ``retries=4`` -- the ROLE defaults --
while ``process_file``'s sole producer (``services.analysis_enqueue.enqueue_process_file``)
always pins ``timeout=7200`` / ``retries=2``. The observed jobs shared a queued-at cluster
matching ADR-0006's recorded operator recovery flood: ``scheduling_ledger`` rows written before
the Phase-45 ``timeout``/``retries`` capture columns existed carry NULL bounds, so
``reenqueue._replay_row`` omits both kwargs from its replay enqueue, and the queue's
``apply_project_job_defaults`` before_enqueue hook filled the ROLE default (600s/4 retries)
instead of ``process_file``'s own policy.

These tests drive ``_replay_row`` against a fake queue whose ``enqueue()`` builds a REAL
``saq.Job`` and runs the REAL ``apply_project_job_defaults`` hook (the actual production
before_enqueue chokepoint, not a mock) -- so a regression here is caught at the same layer the
production hook chain runs, not just at the hook's own unit-test layer
(``tests/analyze/tasks/test_queue_defaults.py``).

MUTATION: revert ``queue_defaults._FUNCTION_POLICY_FLOOR`` (or its enforcement) and
``test_replay_of_null_bounds_process_file_row_lands_on_policy_not_role_default`` goes RED --
the replayed job comes out at 600s/4 retries, reproducing the 2026-08-08 incident.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
import uuid

import pytest
from saq import Job

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks.reenqueue import _replay_row, _zero


# Mirrors tests/_queue_fakes.py's ``_JOB_CONTROL_FIELDS`` split: SAQ's real
# ``Queue.enqueue`` routes dataclass-field kwargs (timeout/retries/key/...) onto the Job
# itself and everything else into ``job.kwargs`` (the task payload).
_JOB_CONTROL_FIELDS = frozenset(Job.__dataclass_fields__)


class _RealHookQueue:
    """A fake SAQ queue whose ``enqueue()`` runs the REAL ``apply_project_job_defaults``
    before_enqueue hook against a REAL ``saq.Job``, so a test can assert the job's FINAL
    effective policy -- not merely what ``_replay_row`` chose to pass as kwargs.

    Production's ``PostgresQueue`` (built by ``tasks._shared.queue_factory``) registers this
    exact hook first in its ``before_enqueue`` chain; this fake reproduces only that one hook
    (the one under test here) rather than the full chain, mirroring how
    ``tests/analyze/services/pipeline/test_analysis_enqueue.py::test_enqueue_policy_survives_apply_project_job_defaults``
    already drives the hook directly against a real ``Job``.
    """

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def enqueue(self, function: str, **kwargs: Any) -> Job:
        control = {k: v for k, v in kwargs.items() if k in _JOB_CONTROL_FIELDS}
        payload = {k: v for k, v in kwargs.items() if k not in _JOB_CONTROL_FIELDS}
        job = Job(function=function, kwargs=payload, **control)
        await apply_project_job_defaults(job)
        self.jobs.append(job)
        return job


@pytest.mark.asyncio
async def test_replay_of_null_bounds_process_file_row_lands_on_policy_not_role_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``process_file`` ledger row with NULL captured ``timeout``/``retries`` (the legacy /
    pre-Phase-45 shape ``recover_orphaned_work`` replays after an operator recovery) must come
    out of the replay at exactly ``timeout=7200`` / ``retries=2`` -- never the role default.

    The role default is deliberately configured here to match the LIVE production values
    (600s / 4 retries) that produced the 2026-08-08 incident, so a regression reproduces the
    exact observed failure mode (jobs dying at 600s) rather than a synthetic one.
    """
    fake_cfg = MagicMock(worker_job_timeout=600, worker_max_retries=4, worker_keep_result=3600)
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    file_id = uuid.uuid4()
    row = SchedulingLedger(
        key=f"process_file:{file_id}",
        function="process_file",
        routing="agent",
        payload={"file_id": str(file_id)},
        # NULL bounds -- the pre-Phase-45 legacy shape (columns added later; existing rows were
        # never backfilled). timeout/retries default to None (nullable columns).
    )
    assert row.timeout is None
    assert row.retries is None

    queue = _RealHookQueue()
    tally = _zero()

    await _replay_row(queue, row, tally)

    assert tally["reenqueued"] == 1, f"replay did not land a fresh job: {tally}"
    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job.timeout == 7200, f"replayed process_file job must never run under its 7200s floor: got {job.timeout}"
    assert job.retries == 2, f"replayed process_file job must never run over its retries=2 policy: got {job.retries}"


@pytest.mark.asyncio
async def test_replay_of_captured_bounds_process_file_row_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ledger row that DID capture its bounds (the post-Phase-45 common case) replays with
    those bounds untouched -- the floor is a no-op when the producer's own policy already
    satisfies it, so this stays byte-identical to pre-phaze-plpnf behavior.
    """
    fake_cfg = MagicMock(worker_job_timeout=600, worker_max_retries=4, worker_keep_result=3600)
    monkeypatch.setattr("phaze.config.get_settings", lambda: fake_cfg)

    file_id = uuid.uuid4()
    row = SchedulingLedger(
        key=f"process_file:{file_id}",
        function="process_file",
        routing="agent",
        payload={"file_id": str(file_id)},
        timeout=7200,
        retries=2,
    )

    queue = _RealHookQueue()
    tally = _zero()

    await _replay_row(queue, row, tally)

    assert tally["reenqueued"] == 1
    job = queue.jobs[0]
    assert job.timeout == 7200
    assert job.retries == 2
