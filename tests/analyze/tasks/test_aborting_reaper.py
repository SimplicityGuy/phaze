"""Tests for the control-side zombie-'aborting' reaper (phaze.tasks.aborting_reaper).

phaze-e57w REGRESSION: a swept job that exhausts retries (or a never-run buffered row) can stick in
``status='aborting'`` forever. SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose
status is in ``('aborted','complete','failed')`` -- ``'aborting'`` is NOT in that allowlist -- so the
surviving deterministic key ``process_file:<file_id>`` permanently blocks re-enqueue of the file.

The reaper DELETEs such rows (releasing the key) once they are older than that ROW's OWN job
timeout plus ``aborting_reap_slack_seconds`` (phaze-lqkz), measured against the FROZEN ``started``
timestamp (NOT ``touched`` -- SAQ's sweeper bumps ``touched`` on every abort pass, so a
touched-based bound would never fire; spike phaze-qmc2.1). It must:

  - reap a genuinely-stuck 'aborting' zombie whose age exceeds ITS OWN timeout + slack (row deleted
    -> key freed -> file re-queueable),
  - leave a FRESH 'aborting' row untouched (age guard: a genuinely mid-abort job is not stolen),
  - leave a row whose age exceeds the OLD fixed bound but not ITS OWN (longer) timeout + slack
    untouched -- the phaze-lqkz regression: a 7200s ``process_file`` job must get a 7200s+slack
    window, not the 900s window a 600s-default job gets,
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

    def __init__(self, aborting_reap_slack_seconds: int) -> None:
        self.aborting_reap_slack_seconds = aborting_reap_slack_seconds


def _patch_slack(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    monkeypatch.setattr("phaze.tasks.aborting_reaper.get_settings", lambda: _StubCfg(seconds))


def _make_ctx() -> dict[str, Any]:
    from phaze.database import async_session

    return {"async_session": async_session}


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def _seed_job(
    session: AsyncSession,
    *,
    key: str,
    status: str,
    started_ms: int,
    timeout: int | None = None,
    queue: str = "phaze-agent-nox-analyze",
) -> None:
    """Insert a saq_jobs row whose JSON blob carries the given started (ms) timestamp.

    ``timeout`` (seconds) mirrors what SAQ's ``Job.to_dict`` serializes once a job's timeout
    differs from the SAQ dataclass default (10s) -- omitted (``None``) models a legacy/bare row
    with no explicit timeout in its blob, exercising the reaper's COALESCE fallback.
    """
    blob: dict[str, Any] = {"function": "process_file", "status": status, "started": started_ms, "touched": _now_ms()}
    if timeout is not None:
        blob["timeout"] = timeout
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": json.dumps(blob).encode("utf-8"), "queue": queue, "status": status},
    )


async def _count(session: AsyncSession, key: str) -> int:
    row = await session.execute(text("SELECT count(*) FROM saq_jobs WHERE key = :key"), {"key": key})
    return int(row.scalar_one())


async def test_reaper_frees_stuck_aborting_zombie(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zombie 'aborting' row older than its OWN timeout + slack is deleted, releasing its key."""
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 300
    timeout = 600
    zombie_key = "process_file:11111111-1111-1111-1111-111111111111"
    # started well past timeout + slack -> stuck.
    await _seed_job(session, key=zombie_key, status="aborting", started_ms=_now_ms() - (timeout + slack + 300) * 1000, timeout=timeout)
    await session.commit()
    assert await _count(session, zombie_key) == 1  # the block exists (re-enqueue would collapse to None)

    _patch_slack(monkeypatch, slack)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 1}
    assert await _count(session, zombie_key) == 0  # key released -> file is re-queueable again


async def test_reaper_leaves_fresh_aborting_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that only just entered 'aborting' is NOT reaped -- a genuine mid-abort must not be stolen."""
    await session.execute(_CREATE_SAQ_JOBS)
    fresh_key = "process_file:22222222-2222-2222-2222-222222222222"
    await _seed_job(session, key=fresh_key, status="aborting", started_ms=_now_ms() - 5 * 1000, timeout=600)  # 5s old
    await session.commit()

    _patch_slack(monkeypatch, 300)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, fresh_key) == 1


async def test_reaper_respects_a_longer_job_timeout(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-lqkz regression: a 7200s process_file job gets a 7200s+slack window, not the ~900s a
    600s-default job gets.

    Pre-fix the reaper compared every row's age against ONE fixed absolute bound (900s), so a
    timed-out ``process_file`` (7200s timeout) entered 'aborting' at age 7200s -- already ~8x past
    that bound -- making it INSTANTLY reap-eligible and racing the owning worker's finalization
    window. The fixed bound derives the window from THIS row's own serialized timeout instead.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 300
    long_timeout = 7200
    key = "process_file:44444444-4444-4444-4444-444444444444"
    # 1200s old: instantly reap-eligible under the OLD 900s fixed bound, but well inside this row's
    # OWN 7200s+300s=7500s window -- must be left alone.
    await _seed_job(session, key=key, status="aborting", started_ms=_now_ms() - 1200 * 1000, timeout=long_timeout)
    await session.commit()

    _patch_slack(monkeypatch, slack)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, key) == 1

    # Age it past its OWN 7500s window -> now it is genuinely stuck and gets reaped.
    await session.execute(text("DELETE FROM saq_jobs WHERE key = :k"), {"k": key})
    await _seed_job(session, key=key, status="aborting", started_ms=_now_ms() - (long_timeout + slack + 60) * 1000, timeout=long_timeout)
    await session.commit()

    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 1}
    assert await _count(session, key) == 0


async def test_reaper_falls_back_to_saq_default_timeout_when_blob_has_none(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose blob carries no explicit ``timeout`` (legacy/bare) falls back to the SAQ default (10s) + slack."""
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 300
    key = "process_file:55555555-5555-5555-5555-555555555555"
    # No `timeout` kwarg -> blob omits it. 10s (SAQ default) + 300s slack = 310s window.
    await _seed_job(session, key=key, status="aborting", started_ms=_now_ms() - 400 * 1000)
    await session.commit()

    _patch_slack(monkeypatch, slack)
    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 1}
    assert await _count(session, key) == 0


async def test_reaper_ignores_non_aborting_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old 'active' row (grx3's buffered claim) is NOT this reaper's job -- only 'aborting' is reaped."""
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 300
    active_key = "process_file:33333333-3333-3333-3333-333333333333"
    await _seed_job(session, key=active_key, status="active", started_ms=_now_ms() - (600 + slack + 300) * 1000, timeout=600)
    await session.commit()

    _patch_slack(monkeypatch, slack)
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
    _patch_slack(monkeypatch, 300)

    outcome = await reap_stuck_aborting_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
