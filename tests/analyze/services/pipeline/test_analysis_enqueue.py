"""Unit tests for the FastAPI-free shared ``process_file`` producer.

``services/analysis_enqueue.py`` owns the single source of truth for the
deterministic SAQ job key (``process_file:<file_id>``), the complete 5-field
``ProcessFilePayload``, and the job policy (``timeout=0`` + a progress ``heartbeat`` /
``retries=2``).
Both producers -- the dashboard "Run Analysis" path and the Wave-2 reboot
re-enqueue path -- funnel through it so SAQ's per-queue deterministic-key dedup
can collapse a repeat enqueue of an in-flight file to a no-op (32-RESEARCH §Q4).

These tests drive the helper directly against :class:`tests._queue_fakes.FakeQueue`,
whose ``captured_policy`` holds the split-out job-control kwargs (so ``key`` /
``timeout`` / ``retries`` land there) and whose ``captured`` holds the task payload
only -- mirroring ``saq.Queue.enqueue``'s dataclass-field split.
"""

from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from phaze.config import get_settings
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis_enqueue import classify_process_file_collision, enqueue_process_file, process_file_job_key
from tests._queue_fakes import FakeQueue


def _fake_file(file_id: uuid.UUID) -> SimpleNamespace:
    """A FileRecord stand-in exposing only the fields the helper reads."""
    return SimpleNamespace(id=file_id, original_path=f"/music/{file_id.hex}.mp3", file_type="mp3")


def test_process_file_job_key_format() -> None:
    """The deterministic key is exactly ``process_file:<uuid-string>``."""
    assert process_file_job_key(uuid.UUID(int=1)) == "process_file:00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_enqueue_process_file_captures_deterministic_key() -> None:
    """``enqueue_process_file`` sets ``key=process_file:<file.id>`` on the enqueue."""
    queue = FakeQueue("phaze-agent-nox")
    file = _fake_file(uuid.uuid4())

    job = await enqueue_process_file(queue, file, "nox", "/models/pb")

    assert job is not None  # FakeQueue (non-dedup) always returns a job
    assert queue.captured_policy[0]["key"] == process_file_job_key(file.id)
    assert queue.captured_policy[0]["key"] == f"process_file:{file.id}"


@pytest.mark.asyncio
async def test_enqueue_process_file_complete_payload_and_policy() -> None:
    """The enqueue carries the 5-field payload plus the progress-based job policy."""
    queue = FakeQueue("phaze-agent-nox")
    fid = uuid.uuid4()
    file = _fake_file(fid)

    await enqueue_process_file(queue, file, "nox", "/models/pb")

    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    # The five required ProcessFilePayload fields plus the two Phase-50 optional cloud-push
    # fields (serialized as None when not overridden), nothing else (extra="forbid" contract).
    # phaze-w55w1 removed the two Phase-44 cap fields from this set entirely.
    assert set(payload) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "expected_sha256",
        "scratch_path",
    }
    assert payload["expected_sha256"] is None
    assert payload["scratch_path"] is None
    assert payload["file_id"] == str(fid)
    assert payload["original_path"] == file.original_path
    assert payload["file_type"] == "mp3"
    assert payload["agent_id"] == "nox"
    assert payload["models_path"] == "/models/pb"
    # The exact kwargs the worker receives validate cleanly against the schema.
    assert str(ProcessFilePayload.model_validate(payload).file_id) == str(fid)

    policy = queue.captured_policy[0]
    # phaze-w55w1: NO wall-clock net (SAQ reads 0 as "disabled"), and a progress `heartbeat` in
    # its place. Both halves are asserted: a heartbeat without timeout=0 would still let the
    # elapsed-time bound kill a healthy multi-hour analysis, and timeout=0 without a heartbeat
    # would leave the job with no liveness bound at all -- the burst lane's old failure.
    assert policy["timeout"] == 0
    # DERIVED from the stall threshold with slack, not equal to it: the outer net watches a
    # narrower signal than the child's own watchdog, so equal deadlines let SAQ sweep a job the
    # watchdog is correctly holding healthy (phaze-w55w1).
    assert policy["heartbeat"] == get_settings().analysis_job_heartbeat_sec
    assert policy["heartbeat"] > get_settings().analysis_stall_timeout_sec
    assert policy["retries"] == 2
    # retries explicitly NOT 1 -- apply_project_job_defaults would clobber 1 -> 4.
    assert policy["retries"] != 1


@pytest.mark.asyncio
async def test_enqueue_process_file_rejects_the_removed_cap_kwargs() -> None:
    """phaze-w55w1: `fine_cap` / `coarse_cap` are gone from the producer's signature.

    A TypeError at the call site is the point: a caller still trying to request a per-file
    window budget is asking for a lever that no longer exists, and a silently-swallowed kwarg
    would let the (removed) deepen semantics look like they still worked.
    """
    queue = FakeQueue("phaze-agent-nox")
    file = _fake_file(uuid.uuid4())

    with pytest.raises(TypeError):
        await enqueue_process_file(queue, file, "nox", "/models/pb", fine_cap=0)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_enqueue_policy_survives_apply_project_job_defaults() -> None:
    """A Job built with the enqueue policy (timeout=0/retries=2) is NOT clobbered.

    ``apply_project_job_defaults`` fills a Job attribute still sitting at its SAQ default
    (timeout==10, retries==1, ttl==600) and then pins the per-function policy. phaze-w55w1
    makes the timeout half of this sharper than it was: ``timeout=0`` IS the policy, but 0 is
    also exactly the kind of value a "raise it to a sane default" hook would helpfully
    overwrite -- which would silently restore the wall-clock kill this bead removed. retries=1
    remains the other trap (RESEARCH Pitfall 1: it gets clobbered to worker_max_retries==4).
    """
    from saq import Job

    from phaze.tasks._shared.queue_defaults import apply_project_job_defaults

    job = Job(function="process_file", timeout=0, retries=2)
    await apply_project_job_defaults(job)

    assert job.timeout == 0
    assert job.retries == 2


# ---------------------------------------------------------------------------
# phaze-ewen: classify_process_file_collision -- distinguishing a genuinely in-flight
# deterministic-key collision from one held by a dead/stuck job.
# ---------------------------------------------------------------------------


def _stub_job(status: str, *, stuck: bool = False) -> SimpleNamespace:
    """A saq.Job stand-in exposing only the two attributes the classifier reads."""
    return SimpleNamespace(status=status, stuck=stuck)


def test_classify_collision_none_job_is_in_flight() -> None:
    """An unlookupable row (queue.job returned None) degrades to in_flight -- never cries wolf."""
    assert classify_process_file_collision(None) == "in_flight"


@pytest.mark.parametrize("status", ["aborting", "failed", "aborted"])
def test_classify_collision_dead_status_is_blocked(status: str) -> None:
    """A dead/dying status is BLOCKED regardless of the (irrelevant) stuck flag."""
    assert classify_process_file_collision(_stub_job(status)) == "blocked"


@pytest.mark.parametrize("status", ["queued", "active", "new"])
def test_classify_collision_live_status_not_stuck_is_in_flight(status: str) -> None:
    """A live, non-stuck status is genuinely in_flight."""
    assert classify_process_file_collision(_stub_job(status, stuck=False)) == "in_flight"


def test_classify_collision_stuck_active_is_blocked() -> None:
    """An 'active' row past its timeout/heartbeat (stuck) is BLOCKED -- a claimed corpse, not live work."""
    assert classify_process_file_collision(_stub_job("active", stuck=True)) == "blocked"


def test_classify_collision_unknown_status_degrades_to_in_flight() -> None:
    """An unrecognized status value is not in the blocked allowlist -- degrade to in_flight, not crash."""
    assert classify_process_file_collision(_stub_job("complete")) == "in_flight"


def test_classify_collision_reads_enum_value_not_the_enum_object() -> None:
    """A real saq.job.Status enum member (not a bare string) classifies identically via .value."""
    from saq.job import Status

    assert classify_process_file_collision(_stub_job(Status.ABORTING)) == "blocked"
    assert classify_process_file_collision(_stub_job(Status.QUEUED)) == "in_flight"
