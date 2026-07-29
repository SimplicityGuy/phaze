"""SAQ task: read_companion_files -- bounded sidecar read on the agent's media mount (phaze-6bkk).

The ONE request/response agent task. Every other agent job reports back through the HTTP callback
API; this one returns its value through ``saq.Queue.apply`` because its caller
(``services.proposal.load_companion_contents``, inside the controller's ``generate_proposals``)
needs the text INLINE to build the LLM context -- there is nothing to persist and nothing to
resume from.

Why it exists at all: DIST-01 makes the controller fileless, so its ``open()`` on an agent-reported
companion path could never succeed in the documented production topology. The failure was invisible
because the read sat behind ``except OSError: continue`` -- proposals were generated with an empty
companion context and looked fine.

Containment is re-checked HERE against the agent's OWN ``scan_roots`` rather than trusted from the
control plane: the paths are agent-supplied strings (``POST /agent/files`` upserts them verbatim and
``PATCH /agent/proposals`` can re-point them), and only the machine holding the mount can resolve
symlinks honestly -- ``Path.resolve()`` on the fileless controller normalizes lexically and proves
nothing about the real filesystem.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10 / D-25).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_tasks import ReadCompanionFilesPayload
from phaze.services.companion_read import read_companion_bounded_sync
from phaze.services.containment import resolve_and_check_containment


logger = structlog.get_logger(__name__)


def _read_all_sync(items: list[tuple[str, str]], max_chars: int, scan_roots: list[str]) -> list[dict[str, str]]:
    """Containment-check and bounded-read every companion, skipping the ones that refuse.

    One thread offload for the whole list rather than one per file: the caller is a batch proposal
    job, and a per-file offload would multiply thread-pool churn for reads that are a few KB each.
    A companion that escapes containment, or that cannot be opened, is SKIPPED with a warning --
    never allowed through and never fatal to the batch (parity with the pre-move behavior, minus
    the silence).
    """
    contents: list[dict[str, str]] = []
    for filename, path in items:
        try:
            resolved, _owning_root = resolve_and_check_containment(path, scan_roots)
        except ValueError:
            logger.warning("companion_containment_escape", companion_path=path)
            continue
        try:
            contents.append({"filename": filename, "content": read_companion_bounded_sync(str(resolved), max_chars)})
        except OSError:
            logger.warning("companion_read_failed", companion_path=path, exc_info=True)
            continue
    return contents


async def read_companion_files(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001  -- ctx is SAQ's required first arg; this task needs nothing from it
    """Read the requested companion sidecars off the agent's mount and RETURN their raw text.

    Returns ``{"contents": [{"filename": ..., "content": ...}, ...]}``. The text is deliberately
    raw: ``clean_companion_content`` (ASCII-art stripping + truncation) and ``sanitize_pg_text``
    (NUL / lone-surrogate stripping, phaze-qj9e) are pure functions with no filesystem dependency,
    so they stay on the control plane where the value is persisted.
    """
    payload = ReadCompanionFilesPayload.model_validate(kwargs)
    cfg = get_settings()
    # An agent with no configured scan_roots gets EVERY companion skipped, never allowed through
    # (services/containment.py: an empty root list matches nothing).
    scan_roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []

    items = [(c.filename, c.path) for c in payload.companions]
    contents = await asyncio.to_thread(_read_all_sync, items, payload.max_chars, scan_roots)

    logger.info("companion read completed", requested=len(items), returned=len(contents))
    return {"contents": contents}
