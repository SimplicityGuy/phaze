"""Control-plane S3 object-staging service (Phase 53, Plan 02 -- KSTAGE-01/03/04, DIST-01).

The single home of every S3 SDK call in the system. The control plane presigns a
multipart upload, mints a just-in-time presigned GET, completes/aborts the upload, deletes
the staged object, and configures a bucket-lifecycle TTL backstop -- but it NEVER touches
file bytes. The file-server agent (upload leg) and the one-shot pod (download leg) only ever
receive time-boxed presigned URLs; bucket credentials live here and nowhere else
(KSTAGE-02/DIST-01).

Structure mirrors the stateless-service conventions of ``enqueue_router.py`` (module-level
async functions, ``__future__`` annotations, ``TYPE_CHECKING`` guard, a fail-loud custom
error) and the external-client discipline of ``agent_client.py`` (one operation per function,
secrets never logged). There are NO ORM imports here -- the service is pure aioboto3 keyed by
``file_id`` (reconcile-by-file_id; the ``file_id``-scoped key is the single object identity).

The client is built from the operator-provided ``ControlSettings`` S3 surface, so it works
against ANY S3-compatible backend (MinIO/Backblaze/AWS/...) via an explicit ``endpoint_url``
and addressing style (KSTAGE-05), not just AWS.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from phaze.config import get_settings


if TYPE_CHECKING:
    import uuid

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig


_STAGING_PREFIX = "phaze-staging"
_LIFECYCLE_RULE_ID = "phaze-staging-ttl"
# Error codes a delete may surface for an already-absent object/upload -- swallowed so the
# inline-delete + reconcile paths are idempotent (a missing object is the desired end state).
_DELETE_ABSENT_CODES = frozenset({"NoSuchKey", "NoSuchUpload", "404"})
# Error codes an abort/complete may surface when the multipart upload is already gone (aborted,
# completed, or expired by the bucket lifecycle rule) -- swallowed so the terminal-cleanup and
# control-side completion paths are idempotent (a missing upload is the desired end state).
_ABORT_ABSENT_CODES = frozenset({"NoSuchUpload", "404"})


class S3StagingError(RuntimeError):
    """Raised when the S3 staging substrate is unconfigured or a control-side S3 call fails.

    Fail-loud (cf. ``enqueue_router.NoActiveAgentError``): an unset bucket/endpoint is an
    operator misconfiguration that must surface immediately, never a silent no-op that would
    leave bytes unstaged.
    """


def staged_object_key(file_id: uuid.UUID) -> str:
    """Return the deterministic, ``file_id``-scoped staging key (KSTAGE-04).

    The key is the single object identity for a file across the upload, download, and
    delete legs -- reconcile-by-``file_id`` everywhere (no per-attempt suffixes).
    """
    return f"{_STAGING_PREFIX}/{file_id}"


def pick_bucket(file_id: uuid.UUID, bucket_ids: list[str]) -> str:
    """Deterministically map a file to one of the backend's bound bucket ids (D-06, MKUE-02).

    Stable across process restarts: it hashes the UUID *bytes* with ``sha256`` -- NOT Python's
    per-process salted ``hash()`` -- so cleanup/presign/reconcile can independently re-agree on the
    same choice. ``sorted()`` gives a stable order independent of TOML/registry ordering.

    The returned id is the AUTHORITATIVE value recorded on ``cloud_job.staging_bucket``. Presign and
    cleanup READ that recorded column; they never re-derive here (re-deriving would drift the moment
    the backend's bucket set changes in config or the row's ``backend_id`` is repurposed) (D-01/D-06).

    Raises ``S3StagingError`` when the bound bucket set is empty -- an operator misconfiguration that
    must fail loud rather than silently stage nowhere (the config validator already guards this too).
    """
    ordered = sorted(bucket_ids)
    if not ordered:
        raise S3StagingError("kueue backend resolves to an empty bucket set")
    digest = hashlib.sha256(file_id.bytes).digest()
    index = int.from_bytes(digest, "big") % len(ordered)
    return ordered[index]


def resolve_bucket_config(cfg: ControlSettings, bucket_id: str | None) -> BucketConfig | None:
    """Resolve a recorded ``cloud_job.staging_bucket`` id to its ``BucketConfig`` (MKUE-02, Pitfall 4).

    The AUTHORITATIVE inverse of :func:`pick_bucket`: presign / cleanup call sites read the value
    recorded on ``cloud_job.staging_bucket`` at stage time and resolve it here -- they NEVER re-derive
    via ``pick_bucket`` (a config change to the backend's bucket set would then mis-point the lookup).

    Returns ``None`` when ``bucket_id`` is ``None`` (a compute/all-local row that staged no S3 object,
    or an unstaged file) so the caller can skip the S3 op cleanly, or when the id is absent from the
    resolved registry (an operator removed the bucket). Pure + ORM-free: it reads only ``cfg.buckets``,
    so ``s3_staging`` stays model-free (the router/caller passes ``cfg`` down).
    """
    if bucket_id is None:
        return None
    return {bucket.id: bucket for bucket in cfg.buckets}.get(bucket_id)


def _client(bucket: BucketConfig) -> Any:
    """Build the aioboto3 S3 client context manager from the active bucket's identity/creds.

    Returns an ``async with``-able client. Credentials come from the control-plane-only
    ``SecretStr`` fields and are never logged. The region falls back to ``us-east-1`` so
    SigV4-style presigning has a region even when an S3-compatible backend leaves it unset.

    phaze-1v37: bound the client with an EXPLICIT connect + read timeout (``s3_client_timeout_sec``,
    default 30s). Botocore's minute-scale defaults plus its retry policy let a wedged/blackholed S3
    endpoint hang a control-side S3 call (complete/abort/delete multipart) for minutes; on the control
    plane those calls run from HTTP staging callbacks, so an unbounded hang pins resources far longer
    than necessary. ``retries={"max_attempts": 1}`` keeps the worst-case bound at a single
    connect+read window instead of the default exponential-backoff retry chain. Presigning is a local
    signing op (no network round-trip), so the timeout only ever bounds the real S3 verbs.
    """
    cfg = cast("ControlSettings", get_settings())
    timeout = cfg.s3_client_timeout_sec
    session = aioboto3.Session(
        aws_access_key_id=bucket.access_key_id.get_secret_value() if bucket.access_key_id else None,
        aws_secret_access_key=bucket.secret_access_key.get_secret_value() if bucket.secret_access_key else None,
        region_name=bucket.region or "us-east-1",
    )
    return session.client(
        "s3",
        endpoint_url=bucket.endpoint_url,
        config=AioConfig(
            s3={"addressing_style": bucket.addressing_style},
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"max_attempts": 1},
        ),
    )


async def create_multipart_upload(file_id: uuid.UUID, bucket: BucketConfig) -> str:
    """Initiate a multipart upload for ``file_id`` on ``bucket`` and return its ``UploadId`` (D-01).

    WR-02: wrap a raw ``ClientError`` (bad creds, missing bucket, network failure) in ``S3StagingError``
    so this verb presents the module's single fail-loud error surface, matching its
    ``complete``/``abort``/``delete`` siblings -- a caller's ``except S3StagingError`` sees every S3-SDK
    failure class uniformly, never a leaked ``botocore.exceptions.ClientError``.
    """
    key = staged_object_key(file_id)
    try:
        async with _client(bucket) as client:
            resp = await client.create_multipart_upload(Bucket=bucket.bucket, Key=key)
            upload_id: str = resp["UploadId"]
            return upload_id
    except ClientError as exc:
        raise S3StagingError(f"failed to create multipart upload for {file_id}") from exc


async def presign_upload_parts(file_id: uuid.UUID, upload_id: str, part_count: int, bucket: BucketConfig) -> list[str]:
    """Presign ``part_count`` PUT URLs (PartNumber 1..part_count) on ``bucket`` for the upload leg (D-01).

    Each URL is bounded by ``s3_presign_put_ttl_sec``. The agent PUTs each part's bytes
    to its URL over httpx and reports back the ``(part_number, etag)`` pairs.

    WR-02: wrap a raw ``ClientError`` in ``S3StagingError`` so this verb matches the module's fail-loud
    error surface (see :func:`create_multipart_upload`).
    """
    cfg = cast("ControlSettings", get_settings())  # kept-global tuning knobs (D-15)
    key = staged_object_key(file_id)
    urls: list[str] = []
    try:
        async with _client(bucket) as client:
            for part_number in range(1, part_count + 1):
                url: str = await client.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": bucket.bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
                    ExpiresIn=cfg.s3_presign_put_ttl_sec,  # kept-global tuning knob (D-15)
                )
                urls.append(url)
        return urls
    except ClientError as exc:
        raise S3StagingError(f"failed to presign upload parts for {file_id}") from exc


async def complete_multipart_upload(file_id: uuid.UUID, upload_id: str, parts: list[tuple[int, str]], bucket: BucketConfig) -> None:
    """Assemble the staged object from its uploaded parts (control-side, never the agent).

    ``parts`` is a list of ``(part_number, etag)`` pairs; they are sorted by part number so
    the ``MultipartUpload.Parts`` list is ordered as S3 requires.

    Idempotent: once a multipart upload completes, S3 invalidates the ``UploadId`` and a repeat
    completion returns ``NoSuchUpload``. ``report_uploaded`` has a status pre-check, but it only
    prevents the double-call when the DB flip to UPLOADED committed -- a retry after an
    S3-success / DB-failure still re-reads UPLOADING, passes the pre-check, and re-calls here.
    Swallowing the already-gone-upload codes makes that retry an idempotent no-op (the object is
    already assembled) instead of a permanent 500-retry loop (WR-01). Any other ``ClientError``
    is re-raised as ``S3StagingError``.
    """
    key = staged_object_key(file_id)
    multipart = {"Parts": [{"PartNumber": part_number, "ETag": etag} for part_number, etag in sorted(parts)]}
    async with _client(bucket) as client:
        try:
            await client.complete_multipart_upload(Bucket=bucket.bucket, Key=key, UploadId=upload_id, MultipartUpload=multipart)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _ABORT_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to complete multipart upload for {file_id}") from exc


async def abort_multipart_upload(file_id: uuid.UUID, upload_id: str, bucket: BucketConfig) -> None:
    """Abort an in-flight multipart upload on ``bucket`` (control-side cleanup of a failed upload).

    Idempotent -- a missing upload is the desired end state, so an already-absent upload error
    (``NoSuchUpload``: aborted, completed, or expired by the bucket lifecycle rule) is swallowed.
    This keeps the terminal-cleanup path in ``report_upload_failed`` from entering a permanent
    500-retry loop when a prior partial run already aborted the multipart (CR-02). Mirrors
    ``delete_staged_object``; any other ``ClientError`` is re-raised as ``S3StagingError``.
    """
    key = staged_object_key(file_id)
    async with _client(bucket) as client:
        try:
            await client.abort_multipart_upload(Bucket=bucket.bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _ABORT_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to abort multipart upload for {file_id}") from exc


async def presign_get(file_id: uuid.UUID, bucket: BucketConfig) -> str:
    """Mint a short-TTL presigned GET URL for the staged object on ``bucket``, just-in-time (KSTAGE-03).

    Bounded by ``s3_presign_get_ttl_sec`` (short, minted at pod start so it never expires
    during a Kueue wait). The download leg fetches the bytes from this URL; the control
    plane never reads them.

    WR-02: wrap a raw ``ClientError`` in ``S3StagingError`` so this verb matches the module's fail-loud
    error surface (see :func:`create_multipart_upload`).
    """
    cfg = cast("ControlSettings", get_settings())  # kept-global tuning knobs (D-15)
    key = staged_object_key(file_id)
    try:
        async with _client(bucket) as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket.bucket, "Key": key},
                ExpiresIn=cfg.s3_presign_get_ttl_sec,  # kept-global tuning knob (D-15)
            )
            return url
    except ClientError as exc:
        raise S3StagingError(f"failed to presign GET for {file_id}") from exc


async def delete_staged_object(file_id: uuid.UUID, bucket: BucketConfig) -> None:
    """Delete the staged object for ``file_id`` on ``bucket`` -- idempotent (D-02 inline-delete).

    A missing object/upload is the desired end state, so an absent-object error is swallowed:
    safe to call when a prior delete / lifecycle sweep already removed it. The caller resolves the
    recorded ``staging_bucket`` first and SKIPS this call entirely for a bucketless (compute /
    unstaged) row, so no client is ever built for a file that staged no S3 object.
    """
    key = staged_object_key(file_id)
    async with _client(bucket) as client:
        try:
            await client.delete_object(Bucket=bucket.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _DELETE_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to delete staged object for {file_id}") from exc


async def ensure_bucket_lifecycle_ttl(bucket: BucketConfig) -> None:
    """Configure ``bucket``'s lifecycle so staged objects expire after ``s3_lifecycle_ttl_days``.

    The TTL backstop (KSTAGE-04, D-02) reaps any object the inline delete missed -- e.g. a
    Kueue eviction with no completion callback. Scoped to the ``phaze-staging/`` prefix so it
    never touches unrelated objects in an operator-shared bucket.
    """
    cfg = cast("ControlSettings", get_settings())  # kept-global tuning knobs (D-15)
    async with _client(bucket) as client:
        await client.put_bucket_lifecycle_configuration(
            Bucket=bucket.bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": _LIFECYCLE_RULE_ID,
                        "Filter": {"Prefix": f"{_STAGING_PREFIX}/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": cfg.s3_lifecycle_ttl_days},  # kept-global tuning knob (D-15)
                    }
                ]
            },
        )
