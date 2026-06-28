"""Tests for the control-plane S3 staging service (Phase 53, Plan 02).

aioboto3/aiobotocore's async response parsing is incompatible with moto's in-process
``mock_aws`` (it expects ``response.content`` to be awaitable), so these tests run
against a wire-compatible ``ThreadedMotoServer`` (real HTTP) with ``ControlSettings``
S3 fields pointed at it. Presigned-URL byte transfers go over httpx -- exactly as the
agent/pod do in production; the control plane never touches file bytes (DIST-01/KSTAGE-01).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse
import uuid

import boto3
from botocore.exceptions import ClientError
import httpx
from moto.server import ThreadedMotoServer
import pytest

from phaze.config import get_settings
from phaze.services import s3_staging


if TYPE_CHECKING:
    from collections.abc import Iterator


_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}


@pytest.fixture
def moto_s3_server() -> Iterator[str]:
    """Start a wire-compatible moto S3 server on a free port; yield its endpoint URL."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_env(moto_s3_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point ControlSettings S3 fields at the moto server and create the staging bucket."""
    monkeypatch.setenv("PHAZE_ROLE", "control")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", moto_s3_server)
    monkeypatch.setenv("PHAZE_S3_BUCKET", _BUCKET)
    monkeypatch.setenv("PHAZE_S3_REGION", "us-east-1")
    monkeypatch.setenv("PHAZE_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("PHAZE_S3_SECRET_ACCESS_KEY", "testing")
    get_settings.cache_clear()
    boto3.client("s3", endpoint_url=moto_s3_server, region_name="us-east-1", **_CREDS).create_bucket(Bucket=_BUCKET)
    yield moto_s3_server
    get_settings.cache_clear()


def test_staged_object_key_is_deterministic_and_file_id_scoped() -> None:
    """The staged key is f'phaze-staging/{file_id}' -- deterministic + file_id-scoped (KSTAGE-04)."""
    fid = uuid.uuid4()
    key = s3_staging.staged_object_key(fid)
    assert key == f"phaze-staging/{fid}"
    assert s3_staging.staged_object_key(fid) == key  # deterministic
    assert s3_staging.staged_object_key(uuid.uuid4()) != key  # distinct per file_id


async def test_create_multipart_upload_returns_upload_id(s3_env: str) -> None:
    """create_multipart_upload returns a non-empty upload id."""
    upload_id = await s3_staging.create_multipart_upload(uuid.uuid4())
    assert isinstance(upload_id, str)
    assert upload_id


async def test_presign_upload_parts_returns_part_count_urls(s3_env: str) -> None:
    """presign_upload_parts returns exactly part_count URLs that carry the key + upload id."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 3)
    assert len(urls) == 3
    for url in urls:
        assert str(fid) in url  # file_id-scoped key in the URL
        assert "uploadid" in url.lower()  # multipart UploadId in the query


async def test_multipart_round_trip_assembles_object(s3_env: str) -> None:
    """create -> presign parts -> PUT bytes -> complete assembles the object; GET returns the bytes."""
    fid = uuid.uuid4()
    payload = b"phaze-staging-roundtrip-payload-bytes"
    upload_id = await s3_staging.create_multipart_upload(fid)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 1)
    async with httpx.AsyncClient() as http:
        put = await http.put(urls[0], content=payload)
        assert put.status_code == 200
        etag = put.headers["ETag"]
        await s3_staging.complete_multipart_upload(fid, upload_id, [(1, etag)])
        get_url = await s3_staging.presign_get(fid)
        got = await http.get(get_url)
        assert got.status_code == 200
        assert got.content == payload


async def test_abort_multipart_upload_removes_in_flight(s3_env: str) -> None:
    """After abort, the in-flight upload is gone (a re-abort is a swallowed idempotent no-op)."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid)
    await s3_staging.abort_multipart_upload(fid, upload_id)
    # The upload id is now invalid; a second abort surfaces NoSuchUpload, which is swallowed.
    await s3_staging.abort_multipart_upload(fid, upload_id)


async def test_abort_multipart_upload_absent_is_idempotent_noop(s3_env: str) -> None:
    """Aborting an absent multipart upload (NoSuchUpload from S3) is an idempotent no-op (CR-02).

    The terminal-cleanup path in ``report_upload_failed`` calls abort unguarded; a missing upload
    is the desired end state, so it must not raise (else a permanent 500-retry loop -- CR-02).
    """
    await s3_staging.abort_multipart_upload(uuid.uuid4(), "nonexistent-upload-id")


async def test_complete_multipart_upload_already_gone_is_idempotent_noop(s3_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Completing an already-gone multipart upload (NoSuchUpload from S3) is an idempotent no-op (WR-01).

    A retry after an S3-success / DB-failure re-calls complete on an invalidated UploadId; it must
    not raise (else a permanent 500-retry loop with the file stuck UPLOADING while the object IS
    already assembled). report_uploaded's status pre-check does NOT cover this case (the flip never
    committed), so the swallow in the service is the real fix.

    moto returns an internal 500 (KeyError) rather than a clean NoSuchUpload for complete-on-missing
    (real S3/AWS returns NoSuchUpload), so the S3 client is stubbed to raise the genuine ClientError.
    """

    class _FakeClientCM:
        async def __aenter__(self) -> AsyncMock:
            client = AsyncMock()
            client.complete_multipart_upload.side_effect = ClientError({"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload")
            return client

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_staging, "_client", lambda _cfg: _FakeClientCM())
    # NoSuchUpload from the S3 client is swallowed -> no raise (idempotent success).
    await s3_staging.complete_multipart_upload(uuid.uuid4(), "gone-upload-id", [(1, '"deadbeef"')])


async def test_complete_multipart_upload_other_clienterror_raises(s3_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-absent ClientError from complete is re-raised as S3StagingError (WR-01 -- fail loud)."""

    class _FakeClientCM:
        async def __aenter__(self) -> AsyncMock:
            client = AsyncMock()
            client.complete_multipart_upload.side_effect = ClientError({"Error": {"Code": "InvalidPart"}}, "CompleteMultipartUpload")
            return client

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_staging, "_client", lambda _cfg: _FakeClientCM())
    with pytest.raises(s3_staging.S3StagingError):
        await s3_staging.complete_multipart_upload(uuid.uuid4(), "upload-id", [(1, '"deadbeef"')])


async def test_presign_get_encodes_short_ttl(s3_env: str) -> None:
    """The presigned GET URL is file_id-scoped and encodes a TTL <= s3_presign_get_ttl_sec (KSTAGE-03)."""
    cfg = get_settings()
    fid = uuid.uuid4()
    url = await s3_staging.presign_get(fid)
    assert str(fid) in url
    query = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in query:  # SigV4 -- direct TTL delta
        ttl = int(query["X-Amz-Expires"][0])
        assert 0 < ttl <= cfg.s3_presign_get_ttl_sec
    else:  # SigV2 (botocore default) -- absolute Expires epoch
        ttl = int(query["Expires"][0]) - int(time.time())
        assert 0 < ttl <= cfg.s3_presign_get_ttl_sec + 5


async def test_delete_staged_object_missing_key_is_idempotent_noop(s3_env: str) -> None:
    """Deleting a never-uploaded file_id is an idempotent no-op (no raise)."""
    await s3_staging.delete_staged_object(uuid.uuid4())


async def test_delete_staged_object_removes_existing(s3_env: str) -> None:
    """delete_staged_object removes a previously-assembled object."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 1)
    async with httpx.AsyncClient() as http:
        put = await http.put(urls[0], content=b"to-be-deleted")
        await s3_staging.complete_multipart_upload(fid, upload_id, [(1, put.headers["ETag"])])
        await s3_staging.delete_staged_object(fid)
        get_url = await s3_staging.presign_get(fid)
        got = await http.get(get_url)
        assert got.status_code in (403, 404)  # object is gone


async def test_ensure_bucket_lifecycle_ttl_sets_expiration_on_prefix(s3_env: str) -> None:
    """ensure_bucket_lifecycle_ttl sets an Expiration of s3_lifecycle_ttl_days on the staging prefix."""
    cfg = get_settings()
    await s3_staging.ensure_bucket_lifecycle_ttl()
    s3 = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=_BUCKET)["Rules"]
    rule = next(r for r in rules if r["Expiration"]["Days"] == cfg.s3_lifecycle_ttl_days)
    prefix = rule.get("Filter", {}).get("Prefix") or rule.get("Prefix")
    assert prefix == "phaze-staging/"


async def test_missing_bucket_config_raises_s3_staging_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no S3 bucket/endpoint configured, the service fails loud rather than building a client."""
    monkeypatch.setenv("PHAZE_ROLE", "control")
    monkeypatch.delenv("PHAZE_S3_BUCKET", raising=False)
    monkeypatch.delenv("PHAZE_S3_ENDPOINT_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(s3_staging.S3StagingError):
            await s3_staging.create_multipart_upload(uuid.uuid4())
    finally:
        get_settings.cache_clear()
