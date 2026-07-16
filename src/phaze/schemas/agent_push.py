"""Pydantic schemas for the internal-API push callbacks (Phase 50).

Two control-plane endpoints mirror the existing `put_analysis` /
`report_analysis_failed` split (RESEARCH §Critical Finding 1 + Open-Q2):

- ``POST /api/internal/agent/push/{file_id}/pushed``   — the fileserver agent
  reports a successful rsync to the compute scratch dir; control terminalizes
  the file's ``cloud_job`` row (``submitted`` -> ``succeeded``) and enqueues
  ``process_file`` (50-05). Phase 90 (D-09) removed the companion
  ``FileState.PUSHED`` dual-write -- the ``cloud_job`` sidecar is now the sole
  derived authority.
- ``POST /api/internal/agent/push/{file_id}/mismatch`` — the compute agent
  reports the rsync'd copy failed sha256 verification; control re-drives the
  push, or caps it to a terminal failure once ``push_max_attempts`` is reached.

These models are consumed by the agent HTTP client (``services/agent_client.py``,
50-03) and the control-plane router (``routers/agent_push.py``, 50-05). They are
deliberately ORM-free (no database / model / ORM-engine imports) so they stay
import-safe across the Postgres-free agent boundary.

AUTH-01 discipline: ``file_id`` always travels on the URL path, never in the
request body — the request models carry only optional diagnostic detail, never
identity. Every model declares ``extra="forbid"`` like the other agent payloads.
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class PushedResponse(BaseModel):
    """Echo confirming control recorded a successful push (file → PUSHED)."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    status: Literal["pushed"] = "pushed"


class PushMismatchRequest(BaseModel):
    """Body the compute agent POSTs when the rsync'd copy fails sha256 verification.

    ``file_id`` is on the path (AUTH-01); the body carries only optional
    diagnostics. ``detail`` is a bounded free-text string (``max_length`` caps the
    DoS-via-huge-string threat) and MUST NOT carry identity.
    """

    model_config = ConfigDict(extra="forbid")

    detail: str | None = Field(default=None, max_length=2000)


class PushMismatchResponse(BaseModel):
    """Echo confirming the mismatch was recorded and the disposition chosen.

    ``cleared`` is True when ``push_max_attempts`` was reached and the file moved
    to a terminal failure (the ``push_file`` ledger entry was cleared); False when
    control will re-drive the push (the PUSHING slot is kept, D-12).
    """

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    status: Literal["mismatch"] = "mismatch"
    cleared: bool
