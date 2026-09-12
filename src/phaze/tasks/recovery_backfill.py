"""Degrade-safe SAQ-to-ledger startup backfill adapter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
import structlog

from phaze.services.scheduling_ledger import insert_ledger_rows_if_absent
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger("phaze.tasks.reenqueue")

_BACKFILL_SAQ_JOBS_SQL = text("SELECT job, key FROM saq_jobs WHERE status IN ('queued', 'active')")


def _parse_job_blob(blob: object) -> dict[str, Any] | None:
    """Deserialize a SAQ job blob to a dict, tolerating malformed individual rows."""
    try:
        data = json.loads(blob) if isinstance(blob, (str, bytes, bytearray)) else blob
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _serialized_job_bound(data: dict[str, Any], field: str) -> int | None:
    """Return a top-level integer SAQ timeout/retry bound when one was serialized."""
    value = data.get(field)
    return value if isinstance(value, int) else None


def _keyed_function_from_job_blob(data: dict[str, Any], key: str) -> str | None:
    """Identify a keyed pipeline function from the blob, falling back to its key prefix."""
    blob_function = data.get("function")
    function = blob_function if isinstance(blob_function, str) else key.split(":", 1)[0]
    return function if function in _KEY_BUILDERS else None


def _classify_saq_job_row(row: Any) -> dict[str, Any] | None:
    """Convert one readable keyed SAQ row into a pending ledger insert."""
    blob, key = row[0], row[1]
    data = _parse_job_blob(blob)
    if data is None or not isinstance(key, str):
        return None
    function = _keyed_function_from_job_blob(data, key)
    if function is None:
        return None
    kwargs = data.get("kwargs")
    return {
        "key": key,
        "function": function,
        "kwargs": dict(kwargs) if isinstance(kwargs, dict) else {},
        "timeout": _serialized_job_bound(data, "timeout"),
        "retries": _serialized_job_bound(data, "retries"),
    }


async def backfill_ledger_from_saq_jobs(session: AsyncSession) -> dict[str, int]:
    """Seed keyed live SAQ jobs without clobbering rows or aborting controller startup."""
    tally = {"inserted": 0, "skipped": 0}
    try:
        async with session.begin_nested():
            rows = (await session.execute(_BACKFILL_SAQ_JOBS_SQL)).all()
    except Exception:
        logger.warning("ledger_backfill_degraded: saq_jobs read failed (pre-migration env?)", exc_info=True)
        return tally

    pending_inserts: list[dict[str, Any]] = []
    for row in rows:
        classified = _classify_saq_job_row(row)
        if classified is None:
            tally["skipped"] += 1
            continue
        pending_inserts.append(classified)

    await insert_ledger_rows_if_absent(session, pending_inserts)
    tally["inserted"] = len(pending_inserts)
    return tally
