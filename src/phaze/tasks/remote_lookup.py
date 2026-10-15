"""Controller SAQ delivery for durable operator-created remote lookup intent."""

from typing import Any
import uuid

from phaze.services.remote_lookup_batches import dispatch_pending, execute_work, expand_batch


async def expand_remote_lookup_batch(ctx: dict[str, Any], *, batch_id: str) -> None:
    await expand_batch(ctx["async_session"], uuid.UUID(batch_id))
    await dispatch_pending(ctx["async_session"], ctx["remote_lookup"], ctx["queue"])


async def lookup_remote_tracklist(ctx: dict[str, Any], *, work_id: str) -> str:
    return await execute_work(ctx["async_session"], ctx["remote_lookup"], uuid.UUID(work_id))


async def resume_remote_lookup_batches(ctx: dict[str, Any]) -> None:
    await dispatch_pending(ctx["async_session"], ctx["remote_lookup"], ctx["queue"])
