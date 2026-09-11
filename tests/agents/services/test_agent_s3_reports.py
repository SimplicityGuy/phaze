"""Mutation-sensitive contracts for the extracted agent S3 callback protocols."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.config import ControlSettings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services import agent_s3_reports, cloud_staging, s3_staging
from phaze.services.agent_s3_reports import (
    ProtocolOutcome,
    UnknownUploadFileError,
    UploadedReason,
    UploadFailedReason,
    process_upload_failed,
    process_uploaded,
)
from tests._queue_fakes import FakeTaskRouter
from tests.agents.routers.test_agent_s3 import (
    _COMPUTE_REGISTRY,
    _KUEUE_REGISTRY,
    _cloud_job,
    _seed_cloud_job,
    _seed_file,
    _seed_ledger,
)


if TYPE_CHECKING:
    from phaze.models.agent import Agent


async def test_uploaded_protocol_commits_before_network_and_preserves_part_order(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Removing/reordering the pre-network commit must break this protocol contract."""
    agent, _token = seed_test_agent
    backends_toml_env(_COMPUTE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)

    order: list[str] = []
    seen_parts: list[tuple[int, str]] = []
    real_commit = AsyncSession.commit

    async def _commit(inner: AsyncSession) -> None:
        await real_commit(inner)
        order.append("commit")

    async def _complete(_file_id: Any, _upload_id: Any, parts: list[tuple[int, str]], _bucket: Any) -> None:
        assert not session.in_transaction(), "the read transaction must be released before S3 I/O"
        order.append("network")
        seen_parts.extend(parts)

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", _complete)
    parts = [(2, '"etag-2"'), (1, '"etag-1"')]

    result = await process_uploaded(session, file_id, parts, settings, SimpleNamespace(), AsyncMock())

    assert result.outcome is ProtocolOutcome.COMPLETED
    assert order.index("commit") < order.index("network")
    assert seen_parts == parts


async def test_uploaded_protocol_enqueues_only_after_durable_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """The generation-pinned CAS must commit before queue resolution or enqueue."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)

    order: list[str] = []
    real_commit = AsyncSession.commit

    async def _commit(inner: AsyncSession) -> None:
        await real_commit(inner)
        order.append("commit")

    async def _complete(*_args: Any, **_kwargs: Any) -> None:
        assert not session.in_transaction()
        order.append("network")

    class Queue:
        async def enqueue(self, *_args: Any, **_kwargs: Any) -> None:
            assert not session.in_transaction(), "enqueue must run after the CAS commit"
            order.append("enqueue")

    async def _resolve(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        assert not session.in_transaction(), "queue resolution must run after the CAS commit"
        order.append("resolve")
        return SimpleNamespace(queue=Queue())

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", _complete)

    result = await process_uploaded(session, file_id, [(1, '"etag"')], settings, SimpleNamespace(), _resolve)

    assert result.outcome is ProtocolOutcome.COMPLETED
    assert result.reason is UploadedReason.SUBMIT_ROUTED
    assert order == ["commit", "network", "commit", "resolve", "enqueue"]
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_protocol_enqueue_failure_preserves_durable_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A post-commit queue failure is a typed hold, not a rolled-back upload."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())
    failure = RuntimeError("controller queue unavailable")

    async def _fail_after_commit(*_args: Any, **_kwargs: Any) -> None:
        assert not session.in_transaction()
        raise failure

    result = await process_uploaded(session, file_id, [(1, '"etag"')], settings, SimpleNamespace(), _fail_after_commit)

    assert result.outcome is ProtocolOutcome.HELD
    assert result.reason is UploadedReason.ENQUEUE_FAILED
    assert result.enqueue_error is failure
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_protocol_pins_cas_to_captured_upload_id(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A redrive swapping upload ids between network completion and CAS must force a no-op."""
    agent, _token = seed_test_agent
    backends_toml_env(_COMPUTE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)

    async def _swap_generation(*_args: Any, **_kwargs: Any) -> None:
        await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(upload_id="fresh-upload"))

    monkeypatch.setattr(s3_staging, "complete_multipart_upload", _swap_generation)
    result = await process_uploaded(session, file_id, [(1, '"etag"')], settings, SimpleNamespace(), AsyncMock())

    assert result.outcome is ProtocolOutcome.NOOP
    assert result.reason is UploadedReason.UPLOAD_ID_CAS_MISS
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.upload_id == "fresh-upload"


async def test_failure_protocol_acquires_advisory_lock_before_counter_read(backends_toml_env: Any) -> None:
    """The first database action must remain the transaction-scoped advisory lock."""
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()

    class FirstExecute(RuntimeError):
        pass

    class RecordingSession:
        statement: str | None = None

        async def execute(self, statement: Any) -> None:
            self.statement = str(statement)
            raise FirstExecute

    recording = RecordingSession()
    with pytest.raises(FirstExecute):
        await process_upload_failed(recording, uuid.uuid4(), settings, FakeTaskRouter())  # type: ignore[arg-type]

    assert recording.statement is not None
    assert "pg_advisory_xact_lock" in recording.statement
    assert "scheduling_ledger" not in recording.statement


async def test_failure_protocol_cas_miss_never_cleans_up(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """An over-cap late callback cannot delete an object owned by advanced work."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.RUNNING)
    await _seed_ledger(session, file_id, attempt=settings.push_max_attempts)
    abort = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)

    result = await process_upload_failed(session, file_id, settings, FakeTaskRouter())

    assert result.outcome is ProtocolOutcome.NOOP
    assert result.reason is UploadFailedReason.OVER_CAP_CAS_MISS
    abort.assert_not_awaited()
    delete.assert_not_awaited()


async def test_empty_parts_cas_miss_commits_without_cleanup(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A lost empty-parts spill race remains a committed no-op with no S3 effects."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    hold = AsyncMock(return_value=False)
    clear = AsyncMock()
    abort = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(agent_s3_reports, "hold_awaiting_cloud", hold)
    monkeypatch.setattr(agent_s3_reports, "clear_ledger_entry", clear)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)

    result = await process_uploaded(session, file_id, [], settings, SimpleNamespace(), AsyncMock())

    assert result.outcome is ProtocolOutcome.NOOP
    assert result.reason is UploadedReason.EMPTY_PARTS
    hold.assert_awaited_once()
    clear.assert_not_awaited()
    abort.assert_not_awaited()
    delete.assert_not_awaited()
    assert not session.in_transaction()


async def test_failure_protocol_cleanup_without_upload_id_runs_post_commit(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A bucket-backed spill with no multipart id still deletes only after state commits."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(upload_id=None))
    await _seed_ledger(session, file_id, attempt=settings.push_max_attempts)

    order: list[str] = []
    real_commit = AsyncSession.commit

    async def _commit(inner: AsyncSession) -> None:
        await real_commit(inner)
        order.append("commit")

    async def _delete(*_args: Any, **_kwargs: Any) -> None:
        assert not session.in_transaction(), "cleanup must not hold the spill transaction"
        order.append("delete")

    abort = AsyncMock()
    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", _delete)

    result = await process_upload_failed(session, file_id, settings, FakeTaskRouter())

    assert result.outcome is ProtocolOutcome.SPILLED
    assert result.reason is UploadFailedReason.SPILLED
    assert order == ["commit", "delete"]
    abort.assert_not_awaited()


async def test_failure_protocol_cleanup_failure_is_returned_post_commit(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Cleanup faults stay attached to an already-durable spill result."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    await _seed_ledger(session, file_id, attempt=settings.push_max_attempts)
    failure = s3_staging.S3StagingError("cleanup unavailable")

    async def _abort(*_args: Any, **_kwargs: Any) -> None:
        assert not session.in_transaction()
        raise failure

    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)

    result = await process_upload_failed(session, file_id, settings, FakeTaskRouter())

    assert result.outcome is ProtocolOutcome.SPILLED
    assert result.reason is UploadFailedReason.SPILLED
    assert result.cleanup_error is failure
    delete.assert_not_awaited()
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.AWAITING.value


async def test_failure_protocol_unknown_file_is_rejected_before_redrive(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """An under-cap callback for an unknown file never starts an upload retry."""
    backends_toml_env(_KUEUE_REGISTRY)
    redrive = AsyncMock()
    monkeypatch.setattr(cloud_staging, "redrive_upload", redrive)

    with pytest.raises(UnknownUploadFileError):
        await process_upload_failed(session, uuid.uuid4(), ControlSettings(), FakeTaskRouter())

    redrive.assert_not_awaited()


async def test_uploaded_protocol_late_callback_is_noop_without_network(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A callback for an already-uploaded row stays a typed no-op and never re-completes S3."""
    agent, _token = seed_test_agent
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADED)
    complete = AsyncMock()
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", complete)

    result = await process_uploaded(session, file_id, [(1, '"etag"')], settings, SimpleNamespace(), AsyncMock())

    assert result.outcome is ProtocolOutcome.NOOP
    assert result.reason is UploadedReason.ABSENT_OR_LATE
    complete.assert_not_awaited()
