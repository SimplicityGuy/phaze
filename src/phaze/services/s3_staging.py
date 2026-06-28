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

from typing import TYPE_CHECKING, Any, cast

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from phaze.config import get_settings


if TYPE_CHECKING:
    import uuid

    from phaze.config import ControlSettings


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


def _staging_config() -> ControlSettings:
    """Return ControlSettings with the S3 staging substrate validated as present.

    Raises ``S3StagingError`` if the bucket or endpoint is unset so a presign/upload never
    proceeds against a half-configured backend.
    """
    cfg = cast("ControlSettings", get_settings())
    if not cfg.s3_bucket or not cfg.s3_endpoint_url:
        raise S3StagingError("S3 staging requires both s3_bucket and s3_endpoint_url to be configured (set PHAZE_S3_BUCKET / PHAZE_S3_ENDPOINT_URL)")
    return cfg


def _client(cfg: ControlSettings) -> Any:
    """Build the aioboto3 S3 client context manager from the ControlSettings S3 surface.

    Returns an ``async with``-able client. Credentials come from the control-plane-only
    ``SecretStr`` fields and are never logged. The region falls back to ``us-east-1`` so
    SigV4-style presigning has a region even when an S3-compatible backend leaves it unset.
    """
    session = aioboto3.Session(
        aws_access_key_id=cfg.s3_access_key_id.get_secret_value() if cfg.s3_access_key_id else None,
        aws_secret_access_key=cfg.s3_secret_access_key.get_secret_value() if cfg.s3_secret_access_key else None,
        region_name=cfg.s3_region or "us-east-1",
    )
    return session.client(
        "s3",
        endpoint_url=cfg.s3_endpoint_url,
        config=AioConfig(s3={"addressing_style": cfg.s3_addressing_style}),
    )


async def create_multipart_upload(file_id: uuid.UUID) -> str:
    """Initiate a multipart upload for ``file_id`` and return its ``UploadId`` (D-01)."""
    cfg = _staging_config()
    key = staged_object_key(file_id)
    async with _client(cfg) as client:
        resp = await client.create_multipart_upload(Bucket=cfg.s3_bucket, Key=key)
    upload_id: str = resp["UploadId"]
    return upload_id


async def presign_upload_parts(file_id: uuid.UUID, upload_id: str, part_count: int) -> list[str]:
    """Presign ``part_count`` PUT URLs (PartNumber 1..part_count) for the upload leg (D-01).

    Each URL is bounded by ``s3_presign_put_ttl_sec``. The agent PUTs each part's bytes
    to its URL over httpx and reports back the ``(part_number, etag)`` pairs.
    """
    cfg = _staging_config()
    key = staged_object_key(file_id)
    urls: list[str] = []
    async with _client(cfg) as client:
        for part_number in range(1, part_count + 1):
            url: str = await client.generate_presigned_url(
                "upload_part",
                Params={"Bucket": cfg.s3_bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
                ExpiresIn=cfg.s3_presign_put_ttl_sec,
            )
            urls.append(url)
    return urls


async def complete_multipart_upload(file_id: uuid.UUID, upload_id: str, parts: list[tuple[int, str]]) -> None:
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
    cfg = _staging_config()
    key = staged_object_key(file_id)
    multipart = {"Parts": [{"PartNumber": part_number, "ETag": etag} for part_number, etag in sorted(parts)]}
    async with _client(cfg) as client:
        try:
            await client.complete_multipart_upload(Bucket=cfg.s3_bucket, Key=key, UploadId=upload_id, MultipartUpload=multipart)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _ABORT_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to complete multipart upload for {file_id}") from exc


async def abort_multipart_upload(file_id: uuid.UUID, upload_id: str) -> None:
    """Abort an in-flight multipart upload (control-side cleanup of a failed/abandoned upload).

    Idempotent -- a missing upload is the desired end state, so an already-absent upload error
    (``NoSuchUpload``: aborted, completed, or expired by the bucket lifecycle rule) is swallowed.
    This keeps the terminal-cleanup path in ``report_upload_failed`` from entering a permanent
    500-retry loop when a prior partial run already aborted the multipart (CR-02). Mirrors
    ``delete_staged_object``; any other ``ClientError`` is re-raised as ``S3StagingError``.
    """
    cfg = _staging_config()
    key = staged_object_key(file_id)
    async with _client(cfg) as client:
        try:
            await client.abort_multipart_upload(Bucket=cfg.s3_bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _ABORT_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to abort multipart upload for {file_id}") from exc


async def presign_get(file_id: uuid.UUID) -> str:
    """Mint a short-TTL presigned GET URL for the staged object, just-in-time (KSTAGE-03).

    Bounded by ``s3_presign_get_ttl_sec`` (short, minted at pod start so it never expires
    during a Kueue wait). The download leg fetches the bytes from this URL; the control
    plane never reads them.
    """
    cfg = _staging_config()
    key = staged_object_key(file_id)
    async with _client(cfg) as client:
        url: str = await client.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.s3_bucket, "Key": key},
            ExpiresIn=cfg.s3_presign_get_ttl_sec,
        )
    return url


async def delete_staged_object(file_id: uuid.UUID) -> None:
    """Delete the staged object for ``file_id`` -- idempotent (D-02 inline-delete capability).

    A missing object/upload is the desired end state, so an absent-object error is swallowed:
    safe to call when no object was ever staged (the all-local path) or when a prior delete /
    lifecycle sweep already removed it.
    """
    cfg = _staging_config()
    key = staged_object_key(file_id)
    async with _client(cfg) as client:
        try:
            await client.delete_object(Bucket=cfg.s3_bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _DELETE_ABSENT_CODES:
                return
            raise S3StagingError(f"failed to delete staged object for {file_id}") from exc


async def ensure_bucket_lifecycle_ttl() -> None:
    """Configure the bucket lifecycle so staged objects expire after ``s3_lifecycle_ttl_days``.

    The TTL backstop (KSTAGE-04, D-02) reaps any object the inline delete missed -- e.g. a
    Kueue eviction with no completion callback. Scoped to the ``phaze-staging/`` prefix so it
    never touches unrelated objects in an operator-shared bucket.
    """
    cfg = _staging_config()
    async with _client(cfg) as client:
        await client.put_bucket_lifecycle_configuration(
            Bucket=cfg.s3_bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": _LIFECYCLE_RULE_ID,
                        "Filter": {"Prefix": f"{_STAGING_PREFIX}/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": cfg.s3_lifecycle_ttl_days},
                    }
                ]
            },
        )
