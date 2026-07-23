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
identity. Every REQUEST model declares ``extra="forbid"`` like the other agent
payloads; RESPONSE models stay loose (``extra="ignore"``) per the Phase 25
convention (``schemas/agent_identity.py``) — see ``PushedResponse`` /
``PushMismatchResponse`` below.
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class PushedResponse(BaseModel):
    """Echo confirming control recorded a successful push (file → PUSHED).

    RESPONSE-only model the agent TRUSTS from the control plane -- not an
    attacker-facing request body -- so it stays loose (Phase 25 convention,
    ``schemas/agent_identity.py``: only REQUEST schemas are strict). A
    control-plane-first rolling deploy adding one additive field here must not
    hard-fail ``model_validate`` on an older agent AFTER the server has already
    committed the ``cloud_job`` state transition (mirrors the forward-compat
    rationale on ``PresignDownloadResponse``, ``schemas/agent_analysis.py``).
    """

    model_config = ConfigDict(extra="ignore")  # forward-compat: tolerate additive fields from a newer control plane (rollout skew)

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

    RESPONSE-only model the agent TRUSTS from the control plane -- stays loose
    (Phase 25 convention) for the same forward-compat reason as ``PushedResponse``.
    """

    model_config = ConfigDict(extra="ignore")  # forward-compat: tolerate additive fields from a newer control plane (rollout skew)

    file_id: uuid.UUID
    status: Literal["mismatch"] = "mismatch"
    cleared: bool
