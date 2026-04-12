"""SAQ job for batch execution of approved rename proposals."""

from __future__ import annotations

import logging
from typing import Any
import uuid

from phaze.services.execution import execute_single_file, get_approved_proposals


logger = logging.getLogger(__name__)

_REDIS_KEY_TTL_SECONDS = 3600  # 1 hour


async def execute_approved_batch(ctx: dict[str, Any], *, batch_id: str | None = None) -> dict[str, Any]:
    """Execute all approved rename proposals sequentially.

    Processes each approved proposal via copy-verify-delete, tracking progress
    in Redis for SSE endpoint consumption.

    Args:
        ctx: SAQ context dict containing queue with Redis connection.
        batch_id: Optional batch ID. Generated if not provided.

    Returns:
        Dict with batch_id, total, completed, and failed counts.
    """
    if batch_id is None:
        batch_id = uuid.uuid4().hex

    redis = ctx["queue"].redis

    async with ctx["async_session"]() as session:
        proposals = await get_approved_proposals(session)
        total = len(proposals)
        completed = 0
        failed = 0

        # Store initial progress in Redis
        await redis.hset(
            f"exec:{batch_id}",
            mapping={
                "total": total,
                "completed": 0,
                "failed": 0,
                "status": "running",
            },
        )

        # Process each proposal sequentially (safer for single-drive disk I/O)
        for proposal in proposals:
            success = await execute_single_file(session, proposal, proposal.file)
            if success:
                completed += 1
            else:
                failed += 1

            # Update Redis progress after each file
            await redis.hset(
                f"exec:{batch_id}",
                mapping={
                    "total": total,
                    "completed": completed,
                    "failed": failed,
                    "status": "running",
                },
            )

        # Mark batch as complete
        await redis.hset(
            f"exec:{batch_id}",
            mapping={
                "total": total,
                "completed": completed,
                "failed": failed,
                "status": "complete",
            },
        )

        # Set TTL for cleanup
        await redis.expire(f"exec:{batch_id}", _REDIS_KEY_TTL_SECONDS)

        logger.info("Batch %s complete: %d/%d succeeded, %d failed", batch_id, completed, total, failed)

        return {
            "batch_id": batch_id,
            "total": total,
            "completed": completed,
            "failed": failed,
        }
