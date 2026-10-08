"""SAQ task: quarantine_companion -- move one approved junk companion into its root's quarantine directory (phaze-lwuf6).

The control plane enqueues one job per approved ``companion_junk_review`` row onto the OWNING agent's
``meta`` lane (``services.junk_quarantine.enqueue_quarantine``). Every check and the move itself live
in ``services.quarantine_move``, against THIS agent's configured scan roots -- only the machine holding
the mount can resolve a symlink honestly. The outcome is PATCHed back, and that report is what marks
the row and retires the ``files`` row.

A refusal is a terminal ``failed`` report, never a raise: a path that escapes, bytes that differ from
the approved ones, or an existing destination will not change on a retry. Only the CALLBACK may fail
the job, so SAQ retries it; the retry finds the source gone and the destination holding the approved
bytes, and reports the move as done (``replayed``).

Requires the media mount to be READ-WRITE on the meta lane (docker-compose.agent.yml), as
``write_file_tags`` does. This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy
(``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_junk_quarantine import JunkQuarantineResultPayload
from phaze.schemas.agent_tasks import QuarantineCompanionPayload
from phaze.services.quarantine_move import QuarantineRefused, quarantine_file


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)

_ERROR_MESSAGE_MAX = 2000


async def quarantine_companion(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Move one approved junk companion, or refuse to, and report the outcome against its review row."""
    payload = QuarantineCompanionPayload.model_validate(kwargs)
    cfg = get_settings()
    # An agent with no configured scan_roots moves nothing (an empty root list matches nothing).
    scan_roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    api: PhazeAgentClient = ctx["api_client"]

    try:
        outcome = await asyncio.to_thread(quarantine_file, payload.source_path, scan_roots, sha256=payload.sha256, size=payload.size)
    except QuarantineRefused as exc:
        logger.warning("junk quarantine refused", review_id=str(payload.review_id), agent_id=payload.agent_id, error=str(exc))
        result = JunkQuarantineResultPayload(status="failed", error_message=str(exc)[:_ERROR_MESSAGE_MAX])
    else:
        logger.info("junk quarantine moved", review_id=str(payload.review_id), agent_id=payload.agent_id, replayed=outcome.replayed)
        result = JunkQuarantineResultPayload(status="quarantined", destination_path=str(outcome.destination), replayed=outcome.replayed)

    await api.report_junk_quarantine(payload.review_id, result)
    return {"review_id": str(payload.review_id), "status": result.status, "replayed": result.replayed}
