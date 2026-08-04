"""Tests for the SAQ 'active' breakdown (phaze.services.queue_introspection).

phaze-grx3 REGRESSION: SAQ marks a row ``status='active'`` at dequeue and buffers it in-process, so
under a burst FAR more rows are ``active`` than the lane's ``concurrency`` can run (~3449 observed vs
concurrency 2). A raw ``count(*) WHERE status='active'`` therefore over-reports "running" by three
orders of magnitude. This asserts the operator-facing figure now distinguishes genuinely-running
(``attempts>=1``) from claimed-but-unrun (``attempts=0``) rows.

The reproduction seeds a queue with 2 running + N claimed-but-unrun ``active`` rows and asserts the
breakdown reports ``running == 2`` -- NOT ``total_active``. It FAILS on pre-fix code, which had no
such splitter and would let ``active: N+2`` masquerade as the running count.

phaze-o0n6 EXTENSION: knowing the split was not enough. The claimed-but-unrun population does not
drain on its own (the sweep processes aborts serially and fell 2,413 rows behind on the live
deployment), and while such a row exists it holds ``process_file:<file_id>`` un-overwritable, so its
file is un-requeueable by every path. The breakdown therefore also reports ``stranded`` -- the rows
``reap_stranded_active_jobs`` would delete -- and raises :attr:`ActiveJobBreakdown.exceeds_concurrency`
when that count passes what the lane could ever have been running. ``phaze queue status`` exits 1 on
it, which is the GUARD acceptance item 5 asks for. Parity between ``stranded`` and the reaper's actual
delete set is asserted in ``tests/analyze/tasks/test_active_reaper.py``.

ctx/session wiring mirrors tests/analyze/tasks/test_aborting_reaper.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import text

from phaze import cli
from phaze.services.enqueue_router import LANE_CONCURRENCY_SETTING, LANES
from phaze.services.queue_introspection import ActiveJobBreakdown, concurrency_for_queue, summarize_active_jobs


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

_QUEUE = "phaze-agent-nox-analyze"


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def _seed(session: AsyncSession, *, key: str, status: str, blob: dict[str, object]) -> None:
    payload = json.dumps({"function": "process_file", "status": status, **blob}).encode("utf-8")
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
        await _seed(session, key=f"process_file:run-{i}", status="active", blob={"attempts": 1, "started": now, "timeout": 600})
    # Claimed-but-unrun: no attempts key; two of them are past the 600s timeout (sweep-eligible).
    await _seed(session, key="process_file:buf-fresh", status="active", blob={"started": now, "timeout": 600})
    await _seed(session, key="process_file:buf-old-1", status="active", blob={"started": now - 900_000, "timeout": 600})
    await _seed(session, key="process_file:buf-old-2", status="active", blob={"started": now - 900_000, "timeout": 600})
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
    await _seed(session, key="process_file:mine", status="active", blob={"attempts": 1, "started": now, "timeout": 600})
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:k, :j, :q, 'active', 0)"),
        {"k": "process_file:other", "j": json.dumps({"attempts": 1, "started": now}).encode("utf-8"), "q": "phaze-agent-other-analyze"},
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
    # phaze-o0n6: a MISSING measurement must never present as a detected incident. `stranded` is 0
    # because nothing was read, not because nothing is stranded, so the guard stays silent.
    assert not breakdown.exceeds_concurrency


# --- phaze-o0n6: the stranded-rows guard ---------------------------------------------------------


def test_lane_concurrency_setting_covers_every_lane() -> None:
    """The lane -> concurrency-knob map is TOTAL over LANES, so a new lane cannot silently go unguarded.

    ``concurrency_for_queue`` falls back to ``worker_max_jobs`` for an unrecognised suffix, which is
    right for an all-mode worker and WRONG (silently too permissive) for a real lane someone added
    without a knob. This is the assertion that turns that into a build failure instead.
    """
    assert set(LANE_CONCURRENCY_SETTING) == set(LANES)


def test_concurrency_for_queue_reads_the_lane_suffix() -> None:
    """``phaze-agent-<id>-<lane>`` resolves to that lane's knob, clamped by worker_max_jobs."""
    assert concurrency_for_queue("phaze-agent-nox-analyze") == 4  # lane_analyze_concurrency default
    assert concurrency_for_queue("phaze-agent-nox-meta") == 2  # lane_meta_concurrency default


def test_concurrency_for_queue_falls_back_for_an_unlaned_queue() -> None:
    """An all-mode / unrecognised queue name gets ``worker_max_jobs`` -- what such a worker really runs at."""
    from phaze.config import get_settings

    assert concurrency_for_queue("phaze-agent-nox") == get_settings().worker_max_jobs


def test_guard_fires_only_above_concurrency() -> None:
    """``exceeds_concurrency`` is strictly ``stranded > concurrency``, and is inert without information.

    Three properties in one place because they are one decision: fire above the bound, stay silent AT
    it (a lane can legitimately have ``concurrency`` jobs whose native work has outrun cancellation),
    and never fire on a degraded read or an unresolved bound -- an alarm that cries wolf on missing
    information is an alarm an operator turns off.
    """

    def _b(**kw: object) -> ActiveJobBreakdown:
        base = {"queue": _QUEUE, "total_active": 10, "running": 0, "claimed_unrun": 10, "stuck_past_timeout": 0}
        return ActiveJobBreakdown(**{**base, **kw})  # type: ignore[arg-type]

    assert _b(stranded=5, concurrency=4).exceeds_concurrency
    assert not _b(stranded=4, concurrency=4).exceeds_concurrency
    assert not _b(stranded=0, concurrency=4).exceeds_concurrency
    assert not _b(stranded=5, concurrency=4, degraded=True).exceeds_concurrency
    assert not _b(stranded=5, concurrency=0).exceeds_concurrency


def test_alarm_is_rendered_for_the_operator() -> None:
    """The alarm line names the count, the bound and the consequence -- not just a number."""
    lines = "\n".join(
        ActiveJobBreakdown(
            queue=_QUEUE, total_active=2413, running=0, claimed_unrun=2413, stuck_past_timeout=2413, stranded=2413, concurrency=4
        ).as_lines()
    )
    assert "ALARM: 2413 stranded > concurrency 4" in lines
    assert "cannot be re-enqueued" in lines


def test_queue_status_cli_exits_1_on_the_alarm(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """THE GUARD (acceptance item 5): ``phaze queue status`` exits non-zero when stranded > concurrency.

    A non-zero exit is what makes this DETECTED rather than discovered incidentally: it is the only
    part of the reporting a monitor can act on without parsing prose. The breakdown is still printed in
    full, so the operator sees the numbers alongside the failure.
    """

    async def _fake(queue_name: str, concurrency: int | None = None) -> ActiveJobBreakdown:
        return ActiveJobBreakdown(
            queue=queue_name, total_active=2413, running=0, claimed_unrun=2413, stuck_past_timeout=2413, stranded=2413, concurrency=4
        )

    monkeypatch.setattr(cli, "_run_queue_status", _fake)

    assert cli.main(["queue", "status", "--queue", _QUEUE]) == 1
    assert "ALARM" in capsys.readouterr().out


def test_queue_status_cli_exits_0_when_healthy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A busy-but-healthy queue still exits 0 -- the guard must not make the diagnostic unusable."""

    async def _fake(queue_name: str, concurrency: int | None = None) -> ActiveJobBreakdown:
        return ActiveJobBreakdown(
            queue=queue_name, total_active=3449, running=4, claimed_unrun=3445, stuck_past_timeout=100, stranded=2, concurrency=4
        )

    monkeypatch.setattr(cli, "_run_queue_status", _fake)

    assert cli.main(["queue", "status", "--queue", _QUEUE]) == 0
    assert "ALARM" not in capsys.readouterr().out


def test_queue_status_cli_passes_the_concurrency_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--concurrency`` reaches the reader, so the bound can be set when the lane knob is not local."""
    seen: dict[str, object] = {}

    async def _fake(queue_name: str, concurrency: int | None = None) -> ActiveJobBreakdown:
        seen["queue"] = queue_name
        seen["concurrency"] = concurrency
        return ActiveJobBreakdown(queue=queue_name, total_active=0, running=0, claimed_unrun=0, stuck_past_timeout=0)

    monkeypatch.setattr(cli, "_run_queue_status", _fake)

    assert cli.main(["queue", "status", "--queue", _QUEUE, "--concurrency", "16"]) == 0
    assert seen == {"queue": _QUEUE, "concurrency": 16}
