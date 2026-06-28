"""Pydantic schemas for the S3 object-staging upload leg (Phase 53, KSTAGE-02).

The file-server agent uploads file bytes by PUTting multipart parts to presigned
URLs (the control plane initiates the multipart upload, presigns the part URLs,
and completes it -- the agent holds NO S3 SDK or bucket credentials, D-01). Two
control-plane callbacks bracket the byte transfer, mirroring the Phase 50 push
split (``agent_push.py``):

- ``POST /api/internal/agent/s3/{file_id}/uploaded`` -- the agent reports the
  ordered ``(part_number, etag)`` list it collected from each part PUT response;
  control completes the multipart upload and flips the file forward.
- ``POST /api/internal/agent/s3/{file_id}/failed``   -- the agent reports a
  terminal/transfer failure with optional bounded diagnostics.

These models are ORM-free and S3-SDK-free (no database, ORM-engine, or object-store
client imports) so they stay import-safe across the Postgres-free *and* SDK-free
agent boundary (tests/test_task_split.py). Every model declares ``extra="forbid"``.

AUTH-01 discipline: ``file_id`` rides the URL path on the callbacks, never the
request body. ``UploadFileS3Payload`` is the exception that carries ``file_id``
because it is a SAQ-job payload whose deterministic-key builder reads it (mirrors
``PushFilePayload``).

D-04: the agent collects each part's ETag and reports the ordered list -- there
are NO S3-side per-part checksums (Content-MD5 / x-amz-checksum); the pod's
end-to-end sha256 is the single integrity gate.
"""

from typing import Literal
from urllib.parse import urlparse
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UploadFileS3Payload(BaseModel):
    """SAQ job: httpx multipart-PUT upload of a single media file to presigned part URLs.

    Phase 53 (KSTAGE-02): enqueued by the control plane (which initiates+presigns
    the multipart upload) and run on the file-server agent (which owns the media
    mount). The deterministic-key builder reads ``file_id``, so it must be present.
    ``original_path`` is the media-mount source the agent reads; ``part_urls`` is
    the ordered list of presigned PUT URLs (part N PUTs ``part_urls[N-1]``).
    """

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    part_urls: list[str] = Field(min_length=1)
    part_size_bytes: int = Field(gt=0)
    agent_id: str

    @field_validator("original_path")
    @classmethod
    def _original_path_absolute(cls, v: str) -> str:
        """Reject a relative source path (mirrors PushFilePayload; the agent reads this path)."""
        if not v.startswith("/"):
            raise ValueError("original_path must be an absolute path")
        return v

    @field_validator("part_urls")
    @classmethod
    def _part_urls_http(cls, v: list[str]) -> list[str]:
        """Reject any non-http(s) presigned URL (SSRF / scheme-confusion defense, T-53-13)."""
        for url in v:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"part URL must be a well-formed http(s) URL with a host, got {url!r}")
        return v


class UploadedPart(BaseModel):
    """One completed multipart part: its 1-based number and the S3-returned ETag (D-04)."""

    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1)
    # min_length=1: a missing S3 ETag header strips to "" (s3_upload.py), which would otherwise
    # pass validation and then fail CompleteMultipartUpload with a 400 (WR-04). S3 always returns
    # a non-empty ETag, so an empty one is a malformed callback to reject at the wire boundary.
    etag: str = Field(min_length=1)


class UploadedRequest(BaseModel):
    """Body the agent POSTs on a successful upload: the ordered completed-part list.

    Upload metadata only -- NOT identity. ``file_id`` rides the URL path (AUTH-01);
    ``extra="forbid"`` rejects any attempt to smuggle identity into the body.
    """

    model_config = ConfigDict(extra="forbid")

    parts: list[UploadedPart]


class UploadedResponse(BaseModel):
    """Echo confirming control completed the multipart upload (file moved forward)."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    status: Literal["uploaded"] = "uploaded"


class UploadFailedRequest(BaseModel):
    """Body the agent POSTs on an upload failure: only an optional bounded diagnostic.

    ``file_id`` is on the path (AUTH-01); ``detail`` is bounded to cap the
    huge-string DoS surface (T-53-12) and MUST NOT carry identity.
    """

    model_config = ConfigDict(extra="forbid")

    detail: str | None = Field(default=None, max_length=2000)


class UploadFailedResponse(BaseModel):
    """Echo confirming control recorded the upload failure and the disposition chosen.

    ``cleared`` is True when the staging row/upload was torn down (terminal); False
    when control will re-drive the upload.
    """

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    status: Literal["failed"] = "failed"
    cleared: bool
