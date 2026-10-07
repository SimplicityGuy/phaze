"""SAQ task: extract_companion_features -- the backfill's per-chunk read on the owning agent (phaze-osy6j).

``phaze backfill companion-features --apply`` enqueues one job per page of companion rows that have
no features, features of older bytes, or features from an older extractor. The job reads them off the
agent's own mount and reports them through the same ``POST /api/internal/agent/companion-features``
the scan and the watcher use, so the backfill and ingest can never disagree about a rule.

The paths come from the control plane (agent-reported originally, but re-pointable), so containment
is re-checked HERE against the agent's own ``scan_roots`` -- the ``read_companion_files`` precedent:
only the machine holding the mount can resolve a symlink honestly. A path that escapes is skipped.

Idempotent: a replay re-reads and rewrites the same rows. This module MUST NOT import phaze.database,
phaze.models.*, or sqlalchemy (``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_companion_features import CompanionFeaturesChunk
from phaze.schemas.agent_tasks import ExtractCompanionFeaturesPayload
from phaze.services.companion_features_report import read_companion_records
from phaze.services.containment import resolve_and_check_containment


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


def _contained(paths: list[str], scan_roots: list[str]) -> list[str]:
    """The paths that resolve inside one of ``scan_roots``; an escaping path is logged and dropped."""
    kept: list[str] = []
    for path in paths:
        try:
            resolve_and_check_containment(path, scan_roots)
        except ValueError:
            logger.warning("companion features: containment escape skipped", path=path)
            continue
        kept.append(path)
    return kept


async def extract_companion_features(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Read one backfill page of companions and report their features; returns the tally."""
    payload = ExtractCompanionFeaturesPayload.model_validate(kwargs)
    cfg = get_settings()
    # An agent with no configured scan_roots reads nothing (services/containment.py: an empty root
    # list matches nothing) -- never everything.
    scan_roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    api: PhazeAgentClient = ctx["api_client"]

    paths = await asyncio.to_thread(_contained, [target.original_path for target in payload.targets], scan_roots)
    records, unreadable = await asyncio.to_thread(read_companion_records, paths)
    stored = 0
    if records:
        # Unlike ingest, a failed POST here is the job's failure: SAQ retries it, and the rows stay
        # selected by the next backfill run either way.
        stored = (await api.post_companion_features(CompanionFeaturesChunk(features=records))).stored
    result = {
        "requested": len(payload.targets),
        "escaped": len(payload.targets) - len(paths),
        "unreadable": unreadable,
        "reported": len(records),
        "stored": stored,
    }
    logger.info("companion features extracted", agent_id=payload.agent_id, **result)
    return result
