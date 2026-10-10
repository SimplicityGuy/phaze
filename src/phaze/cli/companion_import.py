"""Thin supported operator companion import/backfill invocation."""

import json
import sys
import uuid

from sqlalchemy import select, text

from phaze.config import get_settings
from phaze.database import async_session
from phaze.models.file import FileRecord
from phaze.services.companion_import_backfill import count_backfill, create_run, list_run_items, run_status
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.tasks.companion_import import enqueue_import_run


def _json(value: object) -> None:
    print(json.dumps(value, default=str))


async def run_companion_import(*, apply: bool, agent_id: str | None, run_id: uuid.UUID | None, page_size: int, after: uuid.UUID | None) -> int:
    """Default dry-run is read-only; --apply creates/resumes durable controller jobs."""
    if not 1 <= page_size <= 50:
        print("error: --page-size must be between 1 and 50", file=sys.stderr)
        return 1
    if not apply:
        async with async_session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            if run_id is None:
                _json(await count_backfill(session, agent_id=agent_id, page_size=page_size))
            else:
                _json(await run_status(session, run_id))
                _json(await list_run_items(session, run_id, after=after, limit=page_size))
        return 0
    cfg = get_settings()
    queue = build_pipeline_queue("controller", cfg.queue_url, cache_redis_url=cfg.redis_url, ledger_sessionmaker=async_session)
    failed = False
    try:
        await queue.connect()
        if run_id is not None:
            async with async_session() as session:
                status = await run_status(session, run_id)
            await enqueue_import_run(queue, run_id, step=uuid.UUID(status["continuation"]))
            _json({"run_id": str(run_id), "status": "resume_requested"})
            return 0
        after_agent = None
        while True:
            async with async_session() as session:
                query = select(FileRecord.agent_id).distinct().order_by(FileRecord.agent_id).limit(page_size)
                if agent_id is not None:
                    query = query.where(FileRecord.agent_id == agent_id)
                if after_agent is not None:
                    query = query.where(FileRecord.agent_id > after_agent)
                agents = list(await session.scalars(query))
            if not agents:
                break
            for owner in agents:
                async with async_session.begin() as session:
                    run = await create_run(session, owner)
                    identity, continuation = run.id, run.continuation
                try:
                    await enqueue_import_run(queue, identity, step=continuation)
                    _json({"run_id": str(identity), "agent_id": owner, "status": "requested"})
                except Exception:
                    failed = True
                    _json({"run_id": str(identity), "agent_id": owner, "status": "enqueue_unconfirmed", "resume": "--apply --run"})
            after_agent = agents[-1]
        return int(failed)
    finally:
        await queue.disconnect()
        await queue.cache_redis.aclose()  # type: ignore[attr-defined]
