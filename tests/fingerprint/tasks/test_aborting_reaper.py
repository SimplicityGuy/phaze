"""Tests for the control-side zombie-'aborting' reaper (phaze.tasks.aborting_reaper).

phaze-e57w REGRESSION: a swept job that exhausts retries (or a never-run buffered row) can stick in
``status='aborting'`` forever. SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose
status is in ``('aborted','complete','failed')`` -- ``'aborting'`` is NOT in that allowlist -- so the
surviving deterministic key ``fingerprint_file:<file_id>`` permanently blocks re-enqueue of the file.

The reaper DELETEs such rows (releasing the key) once they are older than ``aborting_reap_seconds``,
measured against the FROZEN ``started`` timestamp (NOT ``touched`` -- SAQ's sweeper bumps ``touched``
on every abort pass, so a touched-based bound would never fire; spike phaze-qmc2.1). It must:

  - reap a genuinely-stuck 'aborting' zombie (row deleted -> key freed -> file re-queueable),
  - leave a FRESH 'aborting' row untouched (age guard: a genuinely mid-abort job is not stolen),
  - never touch a non-'aborting' row (a live 'active' row past the bound is grx3's problem, not this).

These reproduce the stale-state transition e57w fixes and FAIL on pre-fix code (which has no reaper,
so the zombie row -- and thus the block -- persists forever).

ctx["async_session"] is sourced from ``phaze.database.async_session`` -- monkeypatched by the
``session`` fixture's fanout to a factory bound to the per-test connection, exactly as the production
controller wires it (mirrors tests/discovery/tasks/test_scan_reaper.py).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from phaze.tasks.aborting_reaper import reap_stuck_aborting_jobs


if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


# SAQ 0.26.4 saq_jobs schema (saq/queue/postgres_migrations.py) -- created locally because SAQ owns
# this table at runtime and Base.metadata.create_all does not know about it.
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


class _StubCfg:
    """Minimal stand-in for the settings object the reaper reads."""

    def __init__(self, aborting_reap_seconds: int) -> None:
        self.aborting_reap_seconds = aborting_reap_seconds


def _patch_bound(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    monkeypatch.setattr("phaze.tasks.aborting_reaper.get_settings", lambda: _StubCfg(seconds))


def _make_ctx() -> dict[str, Any]:
    from phaze.database import async_session

    return {"async_session": async_session}


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def _seed_job(session: AsyncSession, *, key: str, status: str, started_ms: int, queue: str = "phaze-agent-nox-fingerprint") -> None:
    """Insert a saq_jobs row whose JSON blob carries the given started (ms) timestamp."""
    blob = json.dumps({"function": "fingerprint_file", "status": status, "started": started_ms, "touched": _now_ms()}).encode("utf-8")
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": blob, "queue": queue, "status": status},
    )


async def _count(session: AsyncSession, key: str) -> int:
    row = await session.execute(text("SELECT count(*) FROM saq_jobs WHERE key = :key"), {"key": key})
    return int(row.scalar_one())


async def test_reaper_frees_stuck_aborting_zombie(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zombie 'aborting' row older than the bound is deleted, releasing its deterministic key."""
    await session.execute(_CREATE_SAQ_JOBS)
    bound = 900
    zombie_key = "fingerprint_file:11111111-1111-1111-1111-111111111111"
    # started well past the bound -> stuck.
    await _seed_job(session, key=zombie_key, status="aborting", started_ms=_now_ms() - (bound + 300) * 1000)
    await session.commit()
    assert await _count(session, zombie_key) == 1  # the block exists (re-enqueue would collapse to None)

    _patch_bound(monkeypatch, bound)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 1}
    assert await _count(session, zombie_key) == 0  # key released -> file is re-queueable again


async def test_reaper_leaves_fresh_aborting_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that only just entered 'aborting' is NOT reaped -- a genuine mid-abort must not be stolen."""
    await session.execute(_CREATE_SAQ_JOBS)
    bound = 900
    fresh_key = "fingerprint_file:22222222-2222-2222-2222-222222222222"
    await _seed_job(session, key=fresh_key, status="aborting", started_ms=_now_ms() - 5 * 1000)  # 5s old
    await session.commit()

    _patch_bound(monkeypatch, bound)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, fresh_key) == 1


async def test_reaper_ignores_non_aborting_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old 'active' row (grx3's buffered claim) is NOT this reaper's job -- only 'aborting' is reaped."""
    await session.execute(_CREATE_SAQ_JOBS)
    bound = 900
    active_key = "fingerprint_file:33333333-3333-3333-3333-333333333333"
    await _seed_job(session, key=active_key, status="active", started_ms=_now_ms() - (bound + 300) * 1000)
    await session.commit()

    _patch_bound(monkeypatch, bound)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, active_key) == 1


async def test_reaper_degrades_when_saq_jobs_unreadable(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable saq_jobs (pre-migration env) -> the reaper returns reaped=0 instead of raising.

    Forced by pointing the DELETE at a nonexistent relation so the SAVEPOINT statement raises (the
    phaze_test DB already has a real saq_jobs table, so a truly-missing table cannot be reproduced
    by simply not creating it).
    """
    monkeypatch.setattr("phaze.tasks.aborting_reaper._REAP_ABORTING_SQL", text("DELETE FROM saq_jobs_does_not_exist RETURNING key"))
    _patch_bound(monkeypatch, 900)

    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
