"""Unit tests for the FastAPI-free shared ``process_file`` producer.

``services/analysis_enqueue.py`` owns the single source of truth for the
deterministic SAQ job key (``process_file:<file_id>``), the complete 5-field
``ProcessFilePayload``, and the job policy (``timeout=14400`` / ``retries=2``).
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

from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis_enqueue import enqueue_process_file, process_file_job_key
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
    """The enqueue carries the 5-field payload plus ``timeout=14400`` / ``retries=2``."""
    queue = FakeQueue("phaze-agent-nox")
    fid = uuid.uuid4()
    file = _fake_file(fid)

    await enqueue_process_file(queue, file, "nox", "/models/pb")

    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    # Exactly the five ProcessFilePayload fields, nothing else (extra="forbid" contract).
    assert set(payload) == {"file_id", "original_path", "file_type", "agent_id", "models_path"}
    assert payload["file_id"] == str(fid)
    assert payload["original_path"] == file.original_path
    assert payload["file_type"] == "mp3"
    assert payload["agent_id"] == "nox"
    assert payload["models_path"] == "/models/pb"
    # The exact kwargs the worker receives validate cleanly against the schema.
    assert str(ProcessFilePayload.model_validate(payload).file_id) == str(fid)

    policy = queue.captured_policy[0]
    assert policy["timeout"] == 14400
    assert policy["retries"] == 2
    # retries explicitly NOT 1 -- apply_project_job_defaults would clobber 1 -> 4.
    assert policy["retries"] != 1
