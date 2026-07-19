"""Tests for the SAQ 'active' breakdown (phaze.services.queue_introspection).

phaze-grx3 REGRESSION: SAQ marks a row ``status='active'`` at dequeue and buffers it in-process, so
under a burst FAR more rows are ``active`` than the lane's ``concurrency`` can run (~3449 observed vs
concurrency 2). A raw ``count(*) WHERE status='active'`` therefore over-reports "running" by three
orders of magnitude. This asserts the operator-facing figure now distinguishes genuinely-running
(``attempts>=1``) from claimed-but-unrun (``attempts=0``) rows.

The reproduction seeds a queue with 2 running + N claimed-but-unrun ``active`` rows and asserts the
breakdown reports ``running == 2`` -- NOT ``total_active``. It FAILS on pre-fix code, which had no
such splitter and would let ``active: N+2`` masquerade as the running count.

ctx/session wiring mirrors tests/fingerprint/tasks/test_aborting_reaper.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import text

from phaze.services.queue_introspection import summarize_active_jobs


if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


_CREATE_SAQ_JOBS = text(
    """
    CREATE TABLE IF NOT EXISTS saq_jobs (
        key TEXT PRIMARY KEY,
        lock_key SERIAL NOT NULL,
        job BYTEA NOT NULL,
        queue TEXT NOT NULL,
        status TEXT NOT NULL,
        priority SMALLINT NOT NULL DEFAULT 0,
        group_key TEXT,
        scheduled BIGINT NOT NULL DEFAULT 0,
        expire_at BIGINT
    )
    """
)

_QUEUE = "phaze-agent-nox-fingerprint"


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def _seed(session: AsyncSession, *, key: str, status: str, blob: dict[str, object]) -> None:
    payload = json.dumps({"function": "fingerprint_file", "status": status, **blob}).encode("utf-8")
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": payload, "queue": _QUEUE, "status": status},
    )


async def test_breakdown_separates_running_from_claimed_unrun(session: AsyncSession) -> None:
    """2 running (attempts present) + 3 claimed-but-unrun (attempts absent) => running == 2, not 5."""
    await session.execute(_CREATE_SAQ_JOBS)
    now = _now_ms()
    # Genuinely running: attempts key present (SAQ omits it only when 0).
    for i in range(2):
        await _seed(session, key=f"fingerprint_file:run-{i}", status="active", blob={"attempts": 1, "started": now, "timeout": 600})
    # Claimed-but-unrun: no attempts key; two of them are past the 600s timeout (sweep-eligible).
    await _seed(session, key="fingerprint_file:buf-fresh", status="active", blob={"started": now, "timeout": 600})
    await _seed(session, key="fingerprint_file:buf-old-1", status="active", blob={"started": now - 900_000, "timeout": 600})
    await _seed(session, key="fingerprint_file:buf-old-2", status="active", blob={"started": now - 900_000, "timeout": 600})
    await session.commit()

    breakdown = await summarize_active_jobs(session, _QUEUE)

    assert breakdown.total_active == 5
    assert breakdown.running == 2  # the TRUE in-flight number == lane concurrency, not 5
    assert breakdown.claimed_unrun == 3
    assert breakdown.stuck_past_timeout == 2  # the two old buffered rows, sweep-eligible
    assert not breakdown.degraded
    # The operator-facing rendering must never present total_active as the running number.
    lines = "\n".join(breakdown.as_lines())
    assert "NOT the number running" in lines
    assert "running (attempts>=1, genuinely executing): 2" in lines


async def test_breakdown_scopes_to_the_named_queue(session: AsyncSession) -> None:
    """Active rows on OTHER queues are not counted -- the split is per-queue."""
    await session.execute(_CREATE_SAQ_JOBS)
    now = _now_ms()
    await _seed(session, key="fingerprint_file:mine", status="active", blob={"attempts": 1, "started": now, "timeout": 600})
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:k, :j, :q, 'active', 0)"),
        {"k": "fingerprint_file:other", "j": json.dumps({"attempts": 1, "started": now}).encode("utf-8"), "q": "phaze-agent-other-fingerprint"},
    )
    await session.commit()

    breakdown = await summarize_active_jobs(session, _QUEUE)

    assert breakdown.total_active == 1
    assert breakdown.running == 1


async def test_breakdown_degrades_when_saq_jobs_unreadable(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable saq_jobs (pre-migration env) -> a degraded breakdown (not a raise), rendered loudly.

    Forced by pointing the read at a nonexistent relation so the SAVEPOINT read raises and the
    except branch returns ``degraded`` (the phaze_test DB already has a real saq_jobs table, so
    "missing table" cannot be reproduced by simply not creating it).
    """
    monkeypatch.setattr(
        "phaze.services.queue_introspection._ACTIVE_BREAKDOWN_SQL",
        text("SELECT 1 FROM saq_jobs_does_not_exist WHERE queue = :queue"),
    )

    breakdown = await summarize_active_jobs(session, _QUEUE)

    assert breakdown.degraded
    assert breakdown.total_active == 0
    assert "unavailable" in "\n".join(breakdown.as_lines())
