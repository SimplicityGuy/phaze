"""Guarded expiration preserves active generations and unknown/foreign ownership."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import func, select, text

from phaze.models.cloud_job import CloudJob
from phaze.services import s3_staging
from phaze.tasks import staging_cleanup
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


NOW = datetime.now(UTC)
CUTOFF = NOW - timedelta(days=2)
OLD = NOW - timedelta(days=3)
BUCKET = SimpleNamespace(id="bucket-test", bucket="staging-test")


@pytest.mark.parametrize("status", ["uploading", "uploaded", "submitted", "running", "awaiting", "failed", "succeeded"])
@pytest.mark.parametrize("upload_id", [None, "abandoned-upload"])
async def test_cleanup_preserves_live_rows_and_cleans_aged_terminal_rows(session, make_file, monkeypatch, status, upload_id):
    file = await make_file()
    row = CloudJob(
        file_id=file.id, staging_bucket=BUCKET.id, status=status, s3_key=s3_staging.staged_object_key(file.id), created_at=OLD, updated_at=OLD
    )
    session.add(row)
    await session.flush()
    cleanup = AsyncMock(return_value=True)
    monkeypatch.setattr(s3_staging, "cleanup_expired_staging", cleanup)
    candidate = s3_staging.StagingCleanupCandidate(file.id, OLD, upload_id)

    cleaned = await staging_cleanup._cleanup_one(session, candidate, BUCKET, CUTOFF)

    safe = status in {"awaiting", "failed", "succeeded"}
    assert cleaned is safe
    assert cleanup.await_count == int(safe)
    assert row.status == status


@pytest.mark.parametrize("ownership", ["missing", "other-bucket", "recent-row", "foreign-key"])
async def test_unknown_foreign_and_recent_rows_fail_closed(session, make_file, monkeypatch, ownership):
    file = await make_file()
    if ownership != "missing":
        session.add(
            CloudJob(
                file_id=file.id,
                staging_bucket="other" if ownership == "other-bucket" else BUCKET.id,
                status="succeeded",
                s3_key="foreign/key" if ownership == "foreign-key" else s3_staging.staged_object_key(file.id),
                created_at=OLD,
                updated_at=NOW if ownership == "recent-row" else OLD,
            )
        )
        await session.flush()
    cleanup = AsyncMock()
    monkeypatch.setattr(s3_staging, "cleanup_expired_staging", cleanup)
    assert not await staging_cleanup._cleanup_one(session, s3_staging.StagingCleanupCandidate(file.id, OLD), BUCKET, CUTOFF)
    cleanup.assert_not_awaited()


@pytest.mark.parametrize("lock_kind", ["admission", "upload-redrive"])
async def test_concurrent_admission_or_redrive_defers_cleanup_without_waiting(session, async_engine, monkeypatch, lock_kind):
    file_id = uuid.uuid4()
    cleanup = AsyncMock()
    monkeypatch.setattr(s3_staging, "cleanup_expired_staging", cleanup)
    async with async_engine.connect() as connection, connection.begin():
        if lock_kind == "admission":
            await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
        else:
            await connection.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"s3_upload:{file_id}"))))
        assert not await staging_cleanup._cleanup_one(session, s3_staging.StagingCleanupCandidate(file_id, OLD), BUCKET, CUTOFF)
    cleanup.assert_not_awaited()


class Client:
    def __init__(self, *, pages=None, head=None):
        self.pages = pages or {}
        self.head_object = AsyncMock(return_value=head or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get_paginator(self, operation):
        pages = self.pages.get(operation, [])

        class Paginator:
            async def paginate(self, **kwargs):
                assert kwargs["Prefix"] == "phaze-staging/"
                for page in pages:
                    yield page

        return Paginator()


async def test_paginated_scan_filters_foreign_keys_and_young_objects_and_uploads(monkeypatch):
    ids = [uuid.uuid4() for _ in range(4)]
    client = Client(
        pages={
            "list_objects_v2": [
                {
                    "Contents": [
                        {"Key": s3_staging.staged_object_key(ids[0]), "LastModified": OLD},
                        {"Key": "foreign/key", "LastModified": OLD},
                        {"Key": "phaze-staging/not-a-uuid", "LastModified": OLD},
                    ]
                },
                {"Contents": [{"Key": s3_staging.staged_object_key(ids[1]), "LastModified": NOW}]},
            ],
            "list_multipart_uploads": [
                {
                    "Uploads": [
                        {"Key": s3_staging.staged_object_key(ids[2]), "Initiated": OLD, "UploadId": "old-upload"},
                        {"Key": s3_staging.staged_object_key(ids[3]), "Initiated": NOW, "UploadId": "live-upload"},
                    ]
                }
            ],
        }
    )
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: client)
    candidates = [item async for item in s3_staging.expired_staging_candidates(BUCKET, CUTOFF)]
    assert candidates == [s3_staging.StagingCleanupCandidate(ids[0], OLD), s3_staging.StagingCleanupCandidate(ids[2], OLD, "old-upload")]


async def test_object_replaced_after_listing_is_preserved(monkeypatch):
    file_id = uuid.uuid4()
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: Client(head={"LastModified": NOW}))
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    assert not await s3_staging.cleanup_expired_staging(s3_staging.StagingCleanupCandidate(file_id, OLD), BUCKET, CUTOFF)
    delete.assert_not_awaited()


async def test_aged_object_and_specific_abandoned_upload_are_cleaned(monkeypatch):
    file_id = uuid.uuid4()
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: Client(head={"LastModified": OLD}))
    delete, abort = AsyncMock(), AsyncMock()
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    assert await s3_staging.cleanup_expired_staging(s3_staging.StagingCleanupCandidate(file_id, OLD), BUCKET, CUTOFF)
    assert await s3_staging.cleanup_expired_staging(s3_staging.StagingCleanupCandidate(file_id, OLD, "captured-upload"), BUCKET, CUTOFF)
    delete.assert_awaited_once_with(file_id, BUCKET)
    abort.assert_awaited_once_with(file_id, "captured-upload", BUCKET)


async def test_unknown_gateway_head_error_preserves_object(monkeypatch):
    client = Client()
    client.head_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: client)
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    with pytest.raises(ClientError):
        await s3_staging.cleanup_expired_staging(s3_staging.StagingCleanupCandidate(uuid.uuid4(), OLD), BUCKET, CUTOFF)
    delete.assert_not_awaited()


async def test_sweep_only_runs_on_positively_identified_unsupported_buckets(session, monkeypatch):
    bucket2 = SimpleNamespace(id="native-bucket")
    monkeypatch.setattr(staging_cleanup, "get_settings", lambda: SimpleNamespace(buckets=[BUCKET, bucket2], s3_lifecycle_ttl_days=2))
    calls = []

    async def candidates(bucket, cutoff):
        calls.append(bucket.id)
        if False:
            yield None

    monkeypatch.setattr(s3_staging, "expired_staging_candidates", candidates)

    @contextlib.asynccontextmanager
    async def factory():
        yield session

    ctx = {"async_session": factory, "staging_lifecycle_fallback_buckets": {BUCKET.id}}
    assert await staging_cleanup.reap_expired_staging(ctx) == {"cleaned": 0, "preserved": 0, "failed_buckets": 0}
    assert calls == [BUCKET.id]


async def test_sweep_runs_fresh_locked_guard_and_isolates_a_failed_bucket(session, make_file, monkeypatch):
    file = await make_file()
    session.add(CloudJob(file_id=file.id, staging_bucket=BUCKET.id, status="failed", created_at=OLD, updated_at=OLD))
    await session.commit()
    bad_bucket = SimpleNamespace(id="unreachable")
    monkeypatch.setattr(staging_cleanup, "get_settings", lambda: SimpleNamespace(buckets=[bad_bucket, BUCKET], s3_lifecycle_ttl_days=2))

    async def candidates(bucket, cutoff):
        if bucket.id == bad_bucket.id:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        yield s3_staging.StagingCleanupCandidate(file.id, OLD)
        yield s3_staging.StagingCleanupCandidate(uuid.uuid4(), OLD)

    monkeypatch.setattr(s3_staging, "expired_staging_candidates", candidates)
    cleanup = AsyncMock(return_value=True)
    monkeypatch.setattr(s3_staging, "cleanup_expired_staging", cleanup)

    @contextlib.asynccontextmanager
    async def factory():
        yield session

    ctx = {"async_session": factory, "staging_lifecycle_fallback_buckets": {BUCKET.id, bad_bucket.id}}
    assert await staging_cleanup.reap_expired_staging(ctx) == {"cleaned": 1, "preserved": 1, "failed_buckets": 1}
    cleanup.assert_awaited_once()


async def test_already_absent_object_is_an_idempotent_noop(monkeypatch):
    client = Client()
    client.head_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: client)
    assert not await s3_staging.cleanup_expired_staging(s3_staging.StagingCleanupCandidate(uuid.uuid4(), OLD), BUCKET, CUTOFF)


def test_foreign_nested_or_noncanonical_keys_and_missing_age_are_preserved():
    fid = uuid.UUID("abcdef12-abcd-abcd-abcd-abcdef123456")
    for key, modified in (
        (f"phaze-staging/{str(fid).upper()}", OLD),
        (f"phaze-staging/{fid}/child", OLD),
        (s3_staging.staged_object_key(fid), None),
        ("foreign/key", OLD),
    ):
        assert s3_staging._cleanup_candidate(key, modified, CUTOFF) is None


async def test_multipart_entry_without_upload_identity_is_not_treated_as_an_object(monkeypatch):
    client = Client(pages={"list_multipart_uploads": [{"Uploads": [{"Key": s3_staging.staged_object_key(uuid.uuid4()), "Initiated": OLD}]}]})
    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: client)
    assert [item async for item in s3_staging.expired_staging_candidates(BUCKET, CUTOFF)] == []
