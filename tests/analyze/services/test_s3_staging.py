"""Tests for the control-plane S3 staging service (Phase 53, Plan 02; Phase 70, MKUE-02).

aioboto3/aiobotocore's async response parsing is incompatible with moto's in-process
``mock_aws`` (it expects ``response.content`` to be awaitable), so these tests run
against a wire-compatible ``ThreadedMotoServer`` (real HTTP) with a per-call ``BucketConfig``
pointed at it. Presigned-URL byte transfers go over httpx -- exactly as the agent/pod do in
production; the control plane never touches file bytes (DIST-01/KSTAGE-01).

Phase 70 (MKUE-02): every public verb now takes an explicit ``bucket: BucketConfig`` (the
module-global ``active_bucket`` read via ``_staging_config`` is retired). The recorded bucket is
resolved by the caller via :func:`s3_staging.resolve_bucket_config`; presign/delete act on exactly
the passed bucket (proven by the 2-bucket cases below).
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace
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
from phaze.config_backends import BucketConfig
from phaze.services import s3_staging


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}


def _bucket_config(endpoint_url: str, bucket_name: str, *, bid: str = "staging") -> BucketConfig:
    """Build a BucketConfig pointed at ``endpoint_url``/``bucket_name`` with the moto test creds."""
    return BucketConfig(
        id=bid,
        scope="shared",
        endpoint_url=endpoint_url,
        bucket=bucket_name,
        region="us-east-1",
        addressing_style="path",
        access_key_id="testing",
        secret_access_key="testing",  # noqa: S106 -- moto test credential, not a real secret
    )


@pytest.fixture
def moto_s3_server() -> Iterator[str]:
    """Start a wire-compatible moto S3 server on a free port; yield its endpoint URL."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_env(moto_s3_server: str, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Callable[[str], object]) -> Iterator[str]:
    """Register a one-kueue-backend backends.toml pointed at the moto server + create the bucket.

    The staging verbs no longer read a global bucket; the kept-global TTL / part-size tuning knobs
    (D-15) still come from ``ControlSettings``, so this fixture keeps a valid control registry for the
    verbs that read a knob (``presign_upload_parts`` / ``presign_get`` / ``ensure_bucket_lifecycle_ttl``).
    """
    monkeypatch.setenv("PHAZE_ROLE", "control")
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "cluster-01"
        rank = 10
        cap = 4
        buckets = ["staging"]

        [backends.kube]
        api_url = "https://kube.test"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "staging"
        scope = "shared"
        endpoint_url = "{moto_s3_server}"
        bucket = "{_BUCKET}"
        region = "us-east-1"
        addressing_style = "path"
        access_key_id = "testing"
        secret_access_key = "testing"
        """
    )
    boto3.client("s3", endpoint_url=moto_s3_server, region_name="us-east-1", **_CREDS).create_bucket(Bucket=_BUCKET)
    yield moto_s3_server
    get_settings.cache_clear()


@pytest.fixture
def bucket(s3_env: str) -> BucketConfig:
    """The single staging BucketConfig pointed at the moto server (resolved from the registry env)."""
    return _bucket_config(s3_env, _BUCKET)


# === pick_bucket (pure) ==================================================================


def test_pick_bucket_is_order_independent_via_sorted() -> None:
    """pick_bucket sorts the id list, so registry/TOML ordering never changes the choice (D-06)."""
    fid = uuid.uuid4()
    assert s3_staging.pick_bucket(fid, ["b", "a", "c"]) == s3_staging.pick_bucket(fid, ["c", "b", "a"])


def test_pick_bucket_matches_stable_sha256_formula_not_salted_hash() -> None:
    """pick_bucket is restart-stable: it equals the hand-computed sha256-of-UUID-bytes mod (D-06).

    Computing the expected index by hand proves the selector uses a stable digest of the UUID bytes,
    NOT Python's per-process salted ``hash()`` (which would vary across a simulated restart).
    """
    fid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    bucket_ids = ["bucket-x", "bucket-a", "bucket-m"]
    ordered = sorted(bucket_ids)
    digest = hashlib.sha256(fid.bytes).digest()
    expected = ordered[int.from_bytes(digest, "big") % len(ordered)]
    assert s3_staging.pick_bucket(fid, bucket_ids) == expected
    # Stable across repeated calls (a "restart" cannot change a pure sha256-of-bytes result).
    assert s3_staging.pick_bucket(fid, bucket_ids) == expected


def test_pick_bucket_empty_set_raises_s3_staging_error() -> None:
    """An empty bound bucket set is an operator misconfiguration -- fail loud (D-06)."""
    with pytest.raises(s3_staging.S3StagingError):
        s3_staging.pick_bucket(uuid.uuid4(), [])


def test_pick_bucket_always_returns_a_member_of_the_set() -> None:
    """Over many random UUIDs, every chosen id is a member of the bound bucket set (D-06)."""
    bucket_ids = ["b-1", "b-2", "b-3"]
    members = set(bucket_ids)
    for _ in range(500):
        assert s3_staging.pick_bucket(uuid.uuid4(), bucket_ids) in members


# === resolve_bucket_config (pure inverse of pick_bucket; MKUE-02, Pitfall 4) =============


def test_resolve_bucket_config_none_id_returns_none() -> None:
    """A None recorded ``staging_bucket`` (compute / unstaged row) resolves to None -> caller skips S3."""
    cfg = SimpleNamespace(buckets=[])
    assert s3_staging.resolve_bucket_config(cfg, None) is None  # type: ignore[arg-type]


def test_resolve_bucket_config_unknown_id_returns_none() -> None:
    """A recorded id absent from the resolved registry (operator removed the bucket) resolves to None."""
    known = _bucket_config("http://minio.test:9000", "n", bid="known")
    cfg = SimpleNamespace(buckets=[known])
    assert s3_staging.resolve_bucket_config(cfg, "absent") is None  # type: ignore[arg-type]


def test_resolve_bucket_config_resolves_recorded_id() -> None:
    """The recorded id resolves to exactly its registered BucketConfig (authoritative, never re-derived)."""
    b = _bucket_config("http://minio.test:9000", "n", bid="staging-a")
    cfg = SimpleNamespace(buckets=[b])
    assert s3_staging.resolve_bucket_config(cfg, "staging-a") is b  # type: ignore[arg-type]


# === staged_object_key (pure, bucket-agnostic) ==========================================


def test_staged_object_key_is_deterministic_and_file_id_scoped() -> None:
    """The staged key is f'phaze-staging/{file_id}' -- deterministic + file_id-scoped (KSTAGE-04)."""
    fid = uuid.uuid4()
    key = s3_staging.staged_object_key(fid)
    assert key == f"phaze-staging/{fid}"
    assert s3_staging.staged_object_key(fid) == key  # deterministic
    assert s3_staging.staged_object_key(uuid.uuid4()) != key  # distinct per file_id


# === bucket-parameterized verbs =========================================================


async def test_create_multipart_upload_returns_upload_id(bucket: BucketConfig) -> None:
    """create_multipart_upload returns a non-empty upload id."""
    upload_id = await s3_staging.create_multipart_upload(uuid.uuid4(), bucket)
    assert isinstance(upload_id, str)
    assert upload_id


async def test_presign_upload_parts_returns_part_count_urls(bucket: BucketConfig) -> None:
    """presign_upload_parts returns exactly part_count URLs that carry the key + upload id."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid, bucket)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 3, bucket)
    assert len(urls) == 3
    for url in urls:
        assert str(fid) in url  # file_id-scoped key in the URL
        assert "uploadid" in url.lower()  # multipart UploadId in the query


async def test_multipart_round_trip_assembles_object(bucket: BucketConfig) -> None:
    """create -> presign parts -> PUT bytes -> complete assembles the object; GET returns the bytes."""
    fid = uuid.uuid4()
    payload = b"phaze-staging-roundtrip-payload-bytes"
    upload_id = await s3_staging.create_multipart_upload(fid, bucket)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 1, bucket)
    async with httpx.AsyncClient() as http:
        put = await http.put(urls[0], content=payload)
        assert put.status_code == 200
        etag = put.headers["ETag"]
        await s3_staging.complete_multipart_upload(fid, upload_id, [(1, etag)], bucket)
        get_url = await s3_staging.presign_get(fid, bucket)
        got = await http.get(get_url)
        assert got.status_code == 200
        assert got.content == payload


async def test_abort_multipart_upload_removes_in_flight(bucket: BucketConfig) -> None:
    """After abort, the in-flight upload is gone (a re-abort is a swallowed idempotent no-op)."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid, bucket)
    await s3_staging.abort_multipart_upload(fid, upload_id, bucket)
    # The upload id is now invalid; a second abort surfaces NoSuchUpload, which is swallowed.
    await s3_staging.abort_multipart_upload(fid, upload_id, bucket)


async def test_abort_multipart_upload_absent_is_idempotent_noop(bucket: BucketConfig) -> None:
    """Aborting an absent multipart upload (NoSuchUpload from S3) is an idempotent no-op (CR-02).

    The terminal-cleanup path in ``report_upload_failed`` calls abort unguarded; a missing upload
    is the desired end state, so it must not raise (else a permanent 500-retry loop -- CR-02).
    """
    await s3_staging.abort_multipart_upload(uuid.uuid4(), "nonexistent-upload-id", bucket)


async def test_complete_multipart_upload_already_gone_is_idempotent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: _FakeClientCM())
    stub_bucket = _bucket_config("http://minio.test:9000", _BUCKET)
    # NoSuchUpload from the S3 client is swallowed -> no raise (idempotent success).
    await s3_staging.complete_multipart_upload(uuid.uuid4(), "gone-upload-id", [(1, '"deadbeef"')], stub_bucket)


async def test_complete_multipart_upload_other_clienterror_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-absent ClientError from complete is re-raised as S3StagingError (WR-01 -- fail loud)."""

    class _FakeClientCM:
        async def __aenter__(self) -> AsyncMock:
            client = AsyncMock()
            client.complete_multipart_upload.side_effect = ClientError({"Error": {"Code": "InvalidPart"}}, "CompleteMultipartUpload")
            return client

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: _FakeClientCM())
    stub_bucket = _bucket_config("http://minio.test:9000", _BUCKET)
    with pytest.raises(s3_staging.S3StagingError):
        await s3_staging.complete_multipart_upload(uuid.uuid4(), "upload-id", [(1, '"deadbeef"')], stub_bucket)


def _stub_client_method_raising(monkeypatch: pytest.MonkeyPatch, method: str, exc: Exception) -> None:
    """Patch ``s3_staging._client`` so ``client.<method>`` raises ``exc`` (WR-02 fail-loud error paths)."""

    class _FakeClientCM:
        async def __aenter__(self) -> AsyncMock:
            client = AsyncMock()
            getattr(client, method).side_effect = exc
            return client

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: _FakeClientCM())


async def test_create_multipart_upload_clienterror_raises_s3_staging_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw ClientError from create_multipart_upload surfaces as S3StagingError (WR-02)."""
    _stub_client_method_raising(monkeypatch, "create_multipart_upload", ClientError({"Error": {"Code": "NoSuchBucket"}}, "CreateMultipartUpload"))
    with pytest.raises(s3_staging.S3StagingError, match="create multipart upload"):
        await s3_staging.create_multipart_upload(uuid.uuid4(), _bucket_config("http://minio.test:9000", _BUCKET))


async def test_presign_upload_parts_clienterror_raises_s3_staging_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw ClientError while presigning part URLs surfaces as S3StagingError (WR-02)."""
    _stub_client_method_raising(monkeypatch, "generate_presigned_url", ClientError({"Error": {"Code": "AccessDenied"}}, "GeneratePresignedUrl"))
    with pytest.raises(s3_staging.S3StagingError, match="presign upload parts"):
        await s3_staging.presign_upload_parts(uuid.uuid4(), "upload-id", 3, _bucket_config("http://minio.test:9000", _BUCKET))


async def test_presign_get_clienterror_raises_s3_staging_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw ClientError while presigning a GET URL surfaces as S3StagingError (WR-02)."""
    _stub_client_method_raising(monkeypatch, "generate_presigned_url", ClientError({"Error": {"Code": "AccessDenied"}}, "GeneratePresignedUrl"))
    with pytest.raises(s3_staging.S3StagingError, match="presign GET"):
        await s3_staging.presign_get(uuid.uuid4(), _bucket_config("http://minio.test:9000", _BUCKET))


async def test_abort_multipart_upload_other_clienterror_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-absent ClientError from abort is re-raised as S3StagingError (CR-02 -- fail loud)."""
    _stub_client_method_raising(monkeypatch, "abort_multipart_upload", ClientError({"Error": {"Code": "AccessDenied"}}, "AbortMultipartUpload"))
    with pytest.raises(s3_staging.S3StagingError, match="abort multipart upload"):
        await s3_staging.abort_multipart_upload(uuid.uuid4(), "upload-id", _bucket_config("http://minio.test:9000", _BUCKET))


async def test_delete_staged_object_other_clienterror_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-absent ClientError from delete is re-raised as S3StagingError (D-02 -- fail loud)."""
    _stub_client_method_raising(monkeypatch, "delete_object", ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject"))
    with pytest.raises(s3_staging.S3StagingError, match="delete staged object"):
        await s3_staging.delete_staged_object(uuid.uuid4(), _bucket_config("http://minio.test:9000", _BUCKET))


async def test_delete_staged_object_absent_code_clienterror_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent-object ClientError (NoSuchKey) is swallowed -> idempotent no-op (S3 itself never raises this, but a strict backend can)."""
    _stub_client_method_raising(monkeypatch, "delete_object", ClientError({"Error": {"Code": "NoSuchKey"}}, "DeleteObject"))
    await s3_staging.delete_staged_object(uuid.uuid4(), _bucket_config("http://minio.test:9000", _BUCKET))  # no raise


async def test_presign_get_encodes_short_ttl(bucket: BucketConfig) -> None:
    """The presigned GET URL is file_id-scoped and encodes a TTL <= s3_presign_get_ttl_sec (KSTAGE-03)."""
    cfg = get_settings()
    fid = uuid.uuid4()
    url = await s3_staging.presign_get(fid, bucket)
    assert str(fid) in url
    query = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in query:  # SigV4 -- direct TTL delta
        ttl = int(query["X-Amz-Expires"][0])
        assert 0 < ttl <= cfg.s3_presign_get_ttl_sec
    else:  # SigV2 (botocore default) -- absolute Expires epoch
        ttl = int(query["Expires"][0]) - int(time.time())
        assert 0 < ttl <= cfg.s3_presign_get_ttl_sec + 5


async def test_delete_staged_object_missing_key_is_idempotent_noop(bucket: BucketConfig) -> None:
    """Deleting a never-uploaded file_id is an idempotent no-op (no raise)."""
    await s3_staging.delete_staged_object(uuid.uuid4(), bucket)


async def test_delete_staged_object_removes_existing(bucket: BucketConfig) -> None:
    """delete_staged_object removes a previously-assembled object."""
    fid = uuid.uuid4()
    upload_id = await s3_staging.create_multipart_upload(fid, bucket)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 1, bucket)
    async with httpx.AsyncClient() as http:
        put = await http.put(urls[0], content=b"to-be-deleted")
        await s3_staging.complete_multipart_upload(fid, upload_id, [(1, put.headers["ETag"])], bucket)
        await s3_staging.delete_staged_object(fid, bucket)
        get_url = await s3_staging.presign_get(fid, bucket)
        got = await http.get(get_url)
        assert got.status_code in (403, 404)  # object is gone


async def test_ensure_bucket_lifecycle_ttl_sets_expiration_on_prefix(s3_env: str, bucket: BucketConfig) -> None:
    """ensure_bucket_lifecycle_ttl sets an Expiration of s3_lifecycle_ttl_days on the staging prefix."""
    cfg = get_settings()
    await s3_staging.ensure_bucket_lifecycle_ttl(bucket)
    s3 = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=_BUCKET)["Rules"]
    rule = next(r for r in rules if r["Expiration"]["Days"] == cfg.s3_lifecycle_ttl_days)
    prefix = rule.get("Filter", {}).get("Prefix") or rule.get("Prefix")
    assert prefix == "phaze-staging/"


async def test_ensure_bucket_lifecycle_ttl_also_aborts_incomplete_multipart_uploads(s3_env: str, bucket: BucketConfig) -> None:
    """The same rule reaps incomplete multipart uploads -- Expiration alone never touches them (phaze-sqpv)."""
    cfg = get_settings()
    await s3_staging.ensure_bucket_lifecycle_ttl(bucket)
    s3 = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=_BUCKET)["Rules"]
    rule = next(r for r in rules if r["ID"] == s3_staging._LIFECYCLE_RULE_ID)
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == cfg.s3_lifecycle_ttl_days


async def test_ensure_bucket_lifecycle_ttl_preserves_foreign_operator_rules(s3_env: str, bucket: BucketConfig) -> None:
    """A pre-existing, non-phaze-owned rule on a shared bucket survives the call untouched (phaze-fu3w).

    PutBucketLifecycleConfiguration is a full-replace API; a naive single-rule PUT would silently
    delete every operator-defined rule on a shared bucket. This proves the read-modify-write merge
    keeps the operator's own rule intact.
    """
    s3 = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    s3.put_bucket_lifecycle_configuration(
        Bucket=_BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "operator-backups-expiry",
                    "Filter": {"Prefix": "backups/"},
                    "Status": "Enabled",
                    "Expiration": {"Days": 30},
                }
            ]
        },
    )

    await s3_staging.ensure_bucket_lifecycle_ttl(bucket)

    rules = s3.get_bucket_lifecycle_configuration(Bucket=_BUCKET)["Rules"]
    rule_ids = {r["ID"] for r in rules}
    assert rule_ids == {"operator-backups-expiry", s3_staging._LIFECYCLE_RULE_ID}
    operator_rule = next(r for r in rules if r["ID"] == "operator-backups-expiry")
    assert operator_rule["Expiration"]["Days"] == 30


async def test_ensure_bucket_lifecycle_ttl_is_idempotent_and_upserts_by_rule_id(s3_env: str, bucket: BucketConfig) -> None:
    """Calling twice does not duplicate the phaze rule -- it upserts by ID (phaze-fu3w)."""
    await s3_staging.ensure_bucket_lifecycle_ttl(bucket)
    await s3_staging.ensure_bucket_lifecycle_ttl(bucket)

    s3 = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=_BUCKET)["Rules"]
    phaze_rules = [r for r in rules if r["ID"] == s3_staging._LIFECYCLE_RULE_ID]
    assert len(phaze_rules) == 1


async def test_ensure_bucket_lifecycle_ttl_wraps_client_error(bucket: BucketConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw ClientError from the S3 SDK surfaces as S3StagingError, matching the module's WR-02 contract."""

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get_bucket_lifecycle_configuration(self, **_kwargs: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration")

        async def put_bucket_lifecycle_configuration(self, **_kwargs: object) -> None:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutBucketLifecycleConfiguration")

    monkeypatch.setattr(s3_staging, "_client", lambda _bucket: _BoomClient())
    with pytest.raises(s3_staging.S3StagingError):
        await s3_staging.ensure_bucket_lifecycle_ttl(bucket)


# === per-bucket determinism: the CALLED bucket is the one acted on (MKUE-02, 2-bucket set) =====


@pytest.fixture
def two_buckets(
    moto_s3_server: str, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Callable[[str], object]
) -> Iterator[tuple[BucketConfig, BucketConfig]]:
    """Create bucket-a + bucket-b on one moto server; yield a (bucket_a, bucket_b) BucketConfig pair.

    The registry env keeps the kept-global tuning knobs resolvable (presign_get reads a knob); the two
    BucketConfigs point at DISTINCT bucket names so a verb called on one provably never touches the other.
    """
    monkeypatch.setenv("PHAZE_ROLE", "control")
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "cluster-01"
        rank = 10
        cap = 4
        buckets = ["staging-a", "staging-b"]

        [backends.kube]
        api_url = "https://kube.test"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "staging-a"
        scope = "shared"
        endpoint_url = "{moto_s3_server}"
        bucket = "phaze-bucket-a"
        region = "us-east-1"
        addressing_style = "path"
        access_key_id = "testing"
        secret_access_key = "testing"

        [[buckets]]
        id = "staging-b"
        scope = "shared"
        endpoint_url = "{moto_s3_server}"
        bucket = "phaze-bucket-b"
        region = "us-east-1"
        addressing_style = "path"
        access_key_id = "testing"
        secret_access_key = "testing"
        """
    )
    client = boto3.client("s3", endpoint_url=moto_s3_server, region_name="us-east-1", **_CREDS)
    client.create_bucket(Bucket="phaze-bucket-a")
    client.create_bucket(Bucket="phaze-bucket-b")
    bucket_a = _bucket_config(moto_s3_server, "phaze-bucket-a", bid="staging-a")
    bucket_b = _bucket_config(moto_s3_server, "phaze-bucket-b", bid="staging-b")
    yield bucket_a, bucket_b
    get_settings.cache_clear()


async def _stage_into(bucket: BucketConfig, fid: uuid.UUID, payload: bytes) -> None:
    """Stage a single-part object for ``fid`` into ``bucket`` end-to-end."""
    upload_id = await s3_staging.create_multipart_upload(fid, bucket)
    urls = await s3_staging.presign_upload_parts(fid, upload_id, 1, bucket)
    async with httpx.AsyncClient() as http:
        put = await http.put(urls[0], content=payload)
        await s3_staging.complete_multipart_upload(fid, upload_id, [(1, put.headers["ETag"])], bucket)


async def test_presign_get_acts_on_the_called_bucket(two_buckets: tuple[BucketConfig, BucketConfig]) -> None:
    """presign_get on a 2-bucket set targets EXACTLY the passed bucket -- never the sibling (MKUE-02)."""
    bucket_a, bucket_b = two_buckets
    fid = uuid.uuid4()
    await _stage_into(bucket_a, fid, b"lives-in-bucket-a")
    async with httpx.AsyncClient() as http:
        # The object exists in bucket-a: a presign on bucket-a returns it.
        got_a = await http.get(await s3_staging.presign_get(fid, bucket_a))
        assert got_a.status_code == 200
        assert got_a.content == b"lives-in-bucket-a"
        # The same file_id-scoped key does NOT exist in bucket-b: a presign on bucket-b 404s.
        got_b = await http.get(await s3_staging.presign_get(fid, bucket_b))
        assert got_b.status_code in (403, 404)


async def test_delete_staged_object_acts_on_the_called_bucket(two_buckets: tuple[BucketConfig, BucketConfig]) -> None:
    """delete_staged_object on a 2-bucket set removes ONLY the object in the passed bucket (MKUE-02)."""
    bucket_a, bucket_b = two_buckets
    fid = uuid.uuid4()
    await _stage_into(bucket_a, fid, b"lives-in-bucket-a")
    # Deleting against bucket-b is a swallowed no-op (the object is not there) -- bucket-a is untouched.
    await s3_staging.delete_staged_object(fid, bucket_b)
    async with httpx.AsyncClient() as http:
        still_there = await http.get(await s3_staging.presign_get(fid, bucket_a))
        assert still_there.status_code == 200
        # Deleting against the CALLED bucket-a removes it.
        await s3_staging.delete_staged_object(fid, bucket_a)
        gone = await http.get(await s3_staging.presign_get(fid, bucket_a))
        assert gone.status_code in (403, 404)


def test_client_is_bounded_with_explicit_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-1v37: _client bounds every S3 call with explicit connect/read timeouts + no retries.

    Botocore's minute-scale defaults plus its retry chain let a wedged S3 endpoint pin the calling
    connection for minutes; the control-side staging callbacks run these S3 verbs, so an unbounded hang
    drains the small DB pool. The AioConfig must carry connect_timeout == read_timeout ==
    s3_client_timeout_sec and cap retries at a single attempt.
    """
    captured: dict[str, object] = {}

    class _CapSession:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, _service: str, **kwargs: object) -> object:
            captured["config"] = kwargs.get("config")
            return SimpleNamespace()

    monkeypatch.setattr(s3_staging.aioboto3, "Session", _CapSession)
    monkeypatch.setattr(s3_staging, "get_settings", lambda: SimpleNamespace(s3_client_timeout_sec=17))

    s3_staging._client(_bucket_config("http://s3.test", "b"))

    cfg = captured["config"]
    assert cfg.connect_timeout == 17
    assert cfg.read_timeout == 17
    assert cfg.retries == {"max_attempts": 1}
