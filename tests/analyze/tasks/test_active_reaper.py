"""Tests for the control-side stranded-'active' reaper (phaze.tasks.active_reaper).

phaze-o0n6 REGRESSION. SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose status is in
``('aborted','complete','failed')``. ``'aborting'`` is not in that allowlist (phaze-e57w) -- and
neither is ``'active'``. ``PostgresQueue._dequeue`` marks rows ``active`` in bulk and buffers them
in-process, so any process death abandons every buffered row as ``active`` with nothing alive to
finalize it, and each abandoned row holds ``process_file:<file_id>`` hostage FOREVER. Measured on the
live deployment: 2,413 such rows against worker concurrency 4, of which 2,411 keyed files that had
never been analyzed and could not be re-enqueued by ANY path, including the recovery CLI.

Two groups of assertions:

1. **The reaper itself** -- mirrors ``test_aborting_reaper.py`` one-for-one, because the three guards
   are literally the same statement (``phaze.tasks._saq_reap.REAP_STRANDED_SQL``): the FROZEN
   ``started`` bound (never ``touched``, which the sweeper bumps), the PER-ROW bound derived from the
   row's own serialized ``timeout`` (``process_file`` runs 7200s, so any fixed constant is wrong for
   it), and the status CAS in the DELETE's ``WHERE``. Plus the one asymmetry with the ``'aborting'``
   population: a stranded row may carry ``attempts >= 1`` (picked up, then abandoned mid-flight by a
   dying process) and MUST still be reaped -- it blocks its key exactly as hard as a never-run claim.

2. **The bead's OPEN QUESTION**, resolved against a real database rather than guessed: is releasing
   the SAQ key SUFFICIENT to get the file re-analyzed, or must the file's orphaned
   ``scheduling_ledger`` row be cleared too? ``test_reaped_active_row_makes_its_ledger_row_recoverable``
   drives the REAL ``recover_orphaned_work`` across the reap and shows the answer is: releasing the key
   is sufficient, AND the ledger row must SURVIVE, because that row IS the recovery source
   (``orphaned = ledger MINUS live-saq_jobs-keys MINUS domain-completed``). The counterfactual test
   next to it shows that deleting the ledger row instead makes recovery re-enqueue NOTHING -- the
   file would be lost, silently and permanently. Getting this backwards was the stated risk.

ctx["async_session"] is sourced from ``phaze.database.async_session`` -- monkeypatched by the
``session`` fixture's fanout to a factory bound to the per-test connection, exactly as the production
controller wires it (mirrors tests/analyze/tasks/test_aborting_reaper.py).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select, text

from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.queue_introspection import summarize_active_jobs
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks.active_reaper import reap_stranded_active_jobs
from phaze.tasks.reenqueue import recover_orphaned_work
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


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

_QUEUE = "phaze-agent-nox-analyze"
# The real production bound for the stranded population: services/analysis_enqueue.py enqueues
# process_file with a 7200s job timeout, which is why the reaper's bound must be per-row.
_PROCESS_FILE_TIMEOUT = 7200


class _StubCfg:
    """Minimal stand-in for the settings object the reaper reads."""

    def __init__(self, active_reap_slack_seconds: int) -> None:
        self.active_reap_slack_seconds = active_reap_slack_seconds


def _patch_slack(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    monkeypatch.setattr("phaze.tasks.active_reaper.get_settings", lambda: _StubCfg(seconds))


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
    attempts: int | None = None,
    queue: str = _QUEUE,
) -> None:
    """Insert a saq_jobs row whose JSON blob carries the given FROZEN ``started`` (ms) timestamp.

    ``touched`` is deliberately stamped at NOW on every row: that is what SAQ's sweeper does on each
    pass (773 of the live stranded population carried ``error: "swept"``), and it is exactly the
    condition under which a ``touched``-based age bound would never fire. Every reap assertion below
    therefore also proves the bound reads ``started``.

    ``timeout`` (seconds) mirrors what ``Job.to_dict`` serializes once a job's timeout differs from
    the SAQ dataclass default (10s); ``None`` models a legacy/bare row, exercising the COALESCE
    fallback. ``attempts`` is omitted when None, as SAQ omits it at 0.
    """
    blob: dict[str, Any] = {"function": "process_file", "status": status, "started": started_ms, "touched": _now_ms()}
    if timeout is not None:
        blob["timeout"] = timeout
    if attempts is not None:
        blob["attempts"] = attempts
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": json.dumps(blob).encode("utf-8"), "queue": queue, "status": status},
    )


async def _count(session: AsyncSession, key: str) -> int:
    row = await session.execute(text("SELECT count(*) FROM saq_jobs WHERE key = :key"), {"key": key})
    return int(row.scalar_one())


async def _ledger_count(session: AsyncSession, key: str) -> int:
    row = await session.execute(text("SELECT count(*) FROM scheduling_ledger WHERE key = :key"), {"key": key})
    return int(row.scalar_one())


# --- the reaper's three guards ------------------------------------------------------------------


async def test_reaper_frees_stranded_active_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row stranded 'active' past its OWN timeout + slack is deleted, releasing its key."""
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    stranded_key = f"process_file:{uuid.uuid4()}"
    await _seed_job(
        session,
        key=stranded_key,
        status="active",
        started_ms=_now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000,
        timeout=_PROCESS_FILE_TIMEOUT,
    )
    await session.commit()
    assert await _count(session, stranded_key) == 1  # the block exists (re-enqueue would collapse to None)

    _patch_slack(monkeypatch, slack)
    outcome = await reap_stranded_active_jobs(_make_ctx())

    assert outcome == {"reaped": 1}
    assert await _count(session, stranded_key) == 0  # key released -> file is re-queueable again


async def test_reaper_leaves_fresh_active_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that only just went 'active' is NOT reaped -- a genuinely-running job must not be stolen."""
    await session.execute(_CREATE_SAQ_JOBS)
    fresh_key = f"process_file:{uuid.uuid4()}"
    await _seed_job(session, key=fresh_key, status="active", started_ms=_now_ms() - 5 * 1000, timeout=600)  # 5s old
    await session.commit()

    _patch_slack(monkeypatch, 900)
    outcome = await reap_stranded_active_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, fresh_key) == 1


async def test_reaper_bound_is_per_row_not_a_fixed_constant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 7200s ``process_file`` gets a 7200s+slack window, not the window a 600s-default job gets.

    The phaze-lqkz guard, which applies here UNCHANGED and matters MORE than it did for 'aborting':
    'active' is a live status, so a bound that fires early would delete the broker row of a job a
    worker is still executing. 1800s is far past any fixed constant a reaper might have picked, and
    far INSIDE this row's own 8100s window.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    key = f"process_file:{uuid.uuid4()}"
    await _seed_job(session, key=key, status="active", started_ms=_now_ms() - 1800 * 1000, timeout=_PROCESS_FILE_TIMEOUT)
    await session.commit()

    _patch_slack(monkeypatch, slack)
    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 0}
    assert await _count(session, key) == 1

    # Age it past its OWN 8100s window -> now it is genuinely stranded and gets reaped.
    await session.execute(text("DELETE FROM saq_jobs WHERE key = :k"), {"k": key})
    await _seed_job(
        session, key=key, status="active", started_ms=_now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 60) * 1000, timeout=_PROCESS_FILE_TIMEOUT
    )
    await session.commit()

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 1}
    assert await _count(session, key) == 0


async def test_reaper_falls_back_to_saq_default_timeout_when_blob_has_none(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose blob carries no explicit ``timeout`` (legacy/bare) falls back to the SAQ default (10s) + slack."""
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    key = f"process_file:{uuid.uuid4()}"
    # No `timeout` kwarg -> blob omits it. 10s (SAQ default) + 900s slack = 910s window.
    await _seed_job(session, key=key, status="active", started_ms=_now_ms() - 1000 * 1000)
    await session.commit()

    _patch_slack(monkeypatch, slack)

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 1}
    assert await _count(session, key) == 0


async def test_reaper_reaps_a_row_that_was_picked_up_then_abandoned(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """``attempts >= 1`` does NOT exempt a row -- it was picked up and then abandoned mid-flight.

    The one asymmetry with the 'aborting' population. ``queue_introspection`` uses ``attempts`` to
    tell RUNNING from claimed-but-buffered, and the live stranded rows carried "attempts absent or 1",
    i.e. BOTH kinds. A worker that dies mid-``process()`` leaves an ``attempts=1`` row holding its key
    just as permanently as a never-drained buffer entry, so ``attempts`` must not be a reap filter --
    the age-past-own-timeout bound is what separates abandoned from executing.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    key = f"process_file:{uuid.uuid4()}"
    await _seed_job(
        session,
        key=key,
        status="active",
        started_ms=_now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000,
        timeout=_PROCESS_FILE_TIMEOUT,
        attempts=1,
    )
    await session.commit()

    _patch_slack(monkeypatch, slack)

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 1}
    assert await _count(session, key) == 0


async def test_reaper_ignores_non_active_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The status CAS holds: an old 'aborting'/'queued' row belongs to another owner, not this reaper.

    The mirror image of ``test_aborting_reaper.py::test_reaper_ignores_non_aborting_rows``. Keeping
    the two reapers disjoint by status is what makes the CAS meaningful -- a row mid-transition is
    claimed by exactly one of them, never both.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    aborting_key = f"process_file:{uuid.uuid4()}"
    queued_key = f"process_file:{uuid.uuid4()}"
    old = _now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000
    await _seed_job(session, key=aborting_key, status="aborting", started_ms=old, timeout=_PROCESS_FILE_TIMEOUT)
    await _seed_job(session, key=queued_key, status="queued", started_ms=old, timeout=_PROCESS_FILE_TIMEOUT)
    await session.commit()

    _patch_slack(monkeypatch, slack)
    outcome = await reap_stranded_active_jobs(_make_ctx())

    assert outcome == {"reaped": 0}
    assert await _count(session, aborting_key) == 1
    assert await _count(session, queued_key) == 1


async def test_reaper_skips_a_row_with_no_started_timestamp(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blob without ``started`` is excluded, not reaped on incomplete data (age is unknowable)."""
    await session.execute(_CREATE_SAQ_JOBS)
    key = f"process_file:{uuid.uuid4()}"
    blob = {"function": "process_file", "status": "active", "touched": _now_ms(), "timeout": 600}
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, 'active', 0)"),
        {"key": key, "job": json.dumps(blob).encode("utf-8"), "queue": _QUEUE},
    )
    await session.commit()

    _patch_slack(monkeypatch, 900)

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 0}
    assert await _count(session, key) == 1


async def test_reaper_degrades_when_saq_jobs_unreadable(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable saq_jobs (pre-migration env) -> the reaper returns reaped=0 instead of raising.

    Forced by pointing the DELETE at a nonexistent relation so the SAVEPOINT statement raises (the
    test DB already has a real saq_jobs table, so a truly-missing table cannot be reproduced by simply
    not creating it). A reaper hiccup must never abort a controller cron tick.
    """
    monkeypatch.setattr("phaze.tasks.active_reaper._REAP_ACTIVE_SQL", text("DELETE FROM saq_jobs_does_not_exist RETURNING key"))
    _patch_slack(monkeypatch, 900)

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 0}


# --- the phaze-o0n6 GUARD: stranded > concurrency ------------------------------------------------


async def test_stranded_count_equals_what_the_reaper_deletes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard's ``stranded`` figure IS the reaper's delete set -- pinned behaviourally, not textually.

    ``queue_introspection._ACTIVE_BREAKDOWN_SQL`` spells the reap predicate a second time (SQLAlchemy
    ``text()`` does not compose, and splicing a shared fragment into an aggregate would trade a
    readable statement for an unreadable one). This is the assertion that keeps the two honest: an
    alarm that counted a different population than the remedy acts on would be worse than no alarm.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    monkeypatch.setattr("phaze.services.queue_introspection.get_settings", lambda: _StubCfg(slack))
    _patch_slack(monkeypatch, slack)

    now = _now_ms()
    stranded_ms = now - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000
    for _ in range(6):
        await _seed_job(session, key=f"process_file:{uuid.uuid4()}", status="active", started_ms=stranded_ms, timeout=_PROCESS_FILE_TIMEOUT)
    # Decoys that must NOT be counted or reaped: inside their window, no `started`, wrong status.
    await _seed_job(session, key=f"process_file:{uuid.uuid4()}", status="active", started_ms=now - 60_000, timeout=_PROCESS_FILE_TIMEOUT)
    await _seed_job(session, key=f"process_file:{uuid.uuid4()}", status="aborting", started_ms=stranded_ms, timeout=_PROCESS_FILE_TIMEOUT)
    await session.commit()

    breakdown = await summarize_active_jobs(session, _QUEUE, concurrency=4)
    assert breakdown.stranded == 6
    assert breakdown.exceeds_concurrency  # 6 > 4: the alarm fires

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": breakdown.stranded}

    after = await summarize_active_jobs(session, _QUEUE, concurrency=4)
    assert after.stranded == 0
    assert not after.exceeds_concurrency


async def test_guard_is_silent_at_or_below_concurrency(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly ``concurrency`` stranded rows is the largest count healthy operation can produce -> no alarm.

    The lane runs at most ``concurrency`` jobs at once, so at any instant at most that many rows can
    legitimately be ``active`` past their own timeout + slack (a job whose native essentia work has
    outrun asyncio cancellation). The guard fires strictly ABOVE that, so a busy-but-healthy lane
    never cries wolf -- the property that decides whether an operator keeps the monitor enabled.
    """
    await session.execute(_CREATE_SAQ_JOBS)
    slack = 900
    monkeypatch.setattr("phaze.services.queue_introspection.get_settings", lambda: _StubCfg(slack))
    stranded_ms = _now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000
    for _ in range(4):
        await _seed_job(session, key=f"process_file:{uuid.uuid4()}", status="active", started_ms=stranded_ms, timeout=_PROCESS_FILE_TIMEOUT)
    await session.commit()

    breakdown = await summarize_active_jobs(session, _QUEUE, concurrency=4)

    assert breakdown.stranded == 4
    assert not breakdown.exceeds_concurrency


# --- the bead's OPEN QUESTION, resolved against a real database ----------------------------------


def _analyze_payload(file_id: uuid.UUID, agent_id: str) -> dict[str, Any]:
    return {"file_id": str(file_id), "original_path": f"/music/{file_id}.mp3", "file_type": "mp3", "agent_id": agent_id}


_AGENT_ID = "nox"
_AGENT_LANE_QUEUE = f"{_AGENT_ID}-analyze"


async def _seed_blocked_file(session: AsyncSession, *, slack: int, agent_id: str = _AGENT_ID) -> tuple[FileRecord, str]:
    """Reproduce ONE live phaze-o0n6 row: an un-analyzed file + its ledger row + a stranded active job.

    Returns ``(file, key)``. The owning agent must already be seeded (see :func:`_seed_owner`) -- it is
    per-TEST, not per-file, because a cohort of stranded files shares one fileserver. No ``analysis``
    row is created, so the file is NOT domain-complete for ANALYZE -- matching the 1,644 live rows with
    no analysis row at all, the population that must recover.
    """
    file = FileRecord(
        agent_id=agent_id,
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}.mp3",
        original_filename="track.mp3",
        current_path=f"/music/{uuid.uuid4().hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )
    session.add(file)
    await session.commit()

    key = f"process_file:{file.id}"
    # The ledger row every stranded file carries: written at the before_enqueue chokepoint, never
    # cleared because the job died before any terminal callback fired. Timeout captured so a replay
    # keeps the real 7200s bound rather than falling back to the 600s role default.
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs=_analyze_payload(file.id, agent_id), timeout=_PROCESS_FILE_TIMEOUT)
    await session.execute(_CREATE_SAQ_JOBS)
    await _seed_job(
        session,
        key=key,
        status="active",
        started_ms=_now_ms() - (_PROCESS_FILE_TIMEOUT + slack + 300) * 1000,
        timeout=_PROCESS_FILE_TIMEOUT,
    )
    await session.commit()
    return file, key


async def _seed_owner(session: AsyncSession) -> None:
    """Seed the live fileserver that owns every blocked file in a test (recovery routes per owner)."""
    await seed_active_agent(session, _AGENT_ID)


async def _recover(session: AsyncSession) -> tuple[dict[str, Any], DedupFakeTaskRouter]:
    """Run the REAL ``recover_orphaned_work`` (force=True) and return ``(result, router)``.

    ``force=True`` bypasses ONLY the no-op DETECT gate -- the ledger/live-key/domain-completed
    classification and the deterministic-key dedup are the production ones, unstubbed. In particular
    ``get_live_job_keys`` really reads ``saq_jobs``, which is the whole point: it is what makes the
    stranded row's key count as LIVE.
    """
    router = DedupFakeTaskRouter()
    from phaze.database import async_session

    ctx = {"async_session": async_session, "queue": DedupFakeQueue("controller"), "task_router": router}
    result = await recover_orphaned_work(ctx, force=True)
    return result, router


async def test_reaped_active_row_makes_its_ledger_row_recoverable(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """OPEN QUESTION, ANSWERED: releasing the SAQ key IS sufficient -- and the ledger row must SURVIVE.

    ``recover_orphaned_work`` re-enqueues ``ledger MINUS live-saq_jobs-keys MINUS domain-completed``.
    So the two halves of the answer are one mechanism:

    - BEFORE the reap, the stranded ``active`` row makes ``process_file:<file_id>`` a LIVE key, so the
      ledger row is subtracted out and recovery re-enqueues NOTHING. That is the blocked state, and it
      is why the reaper is needed at all -- no amount of re-driving fixes it.
    - AFTER the reap, the key is gone, the SAME ledger row is now genuinely orphaned, and recovery
      replays its stored payload (with the stored 7200s timeout) onto its owning fileserver. Nothing
      further is required: no ledger mutation, no explicit re-drive, no operator step.

    The ledger row is therefore the RECOVERY SOURCE, not a second block, which is why
    ``reap_stranded_active_jobs`` deletes the ``saq_jobs`` row and NOTHING ELSE (asserted below).
    """
    slack = 900
    _patch_slack(monkeypatch, slack)
    await _seed_owner(session)
    file, key = await _seed_blocked_file(session, slack=slack)

    # BEFORE: the key is live, so the ledger row is invisible to recovery -- the file is stuck.
    before, before_router = await _recover(session)
    # ``unreplayable`` is phaze-71nz's fourth counter (a payload refused as carrying time-limited
    # material). It is 0 here and stays 0 throughout this test: ``process_file`` is time-invariant,
    # so it is classified replay-safe and never routed to a regenerator.
    assert before["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert before_router.captures == []

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 1}

    # The reaper touched the broker row ONLY: the ledger row (the recovery source) is untouched.
    assert await _count(session, key) == 0
    assert await _ledger_count(session, key) == 1
    row = (await session.execute(text("SELECT function, timeout FROM scheduling_ledger WHERE key = :k"), {"k": key})).one()
    assert row.function == "process_file"
    assert row.timeout == _PROCESS_FILE_TIMEOUT  # the real bound survives for the replay

    # AFTER: the same ledger row is now an orphan and recovery re-drives the file. No extra step.
    after, after_router = await _recover(session)
    # The load-bearing assertion of this test: freeing the key alone (ledger row untouched) is what
    # makes recovery re-drive the file. ``unreplayable`` is 0 -- the row was replayed on its merits,
    # not refused and not regenerated.
    assert after["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    # Routed to the file's OWNING fileserver's analyze lane, replaying the STORED payload and the
    # STORED 7200s bound (not the 600s role default a bare re-enqueue would stamp).
    assert after.get("forced") is True
    assert after_router.queue_for_calls == [_AGENT_ID]
    assert after_router.captures == [(f"phaze-agent-{_AGENT_ID}-analyze", "process_file", _analyze_payload(file.id, _AGENT_ID))]
    policy = after_router.queues[_AGENT_LANE_QUEUE].captured_policy
    assert [p["key"] for p in policy] == [key]
    assert [p["timeout"] for p in policy] == [_PROCESS_FILE_TIMEOUT]


async def test_deleting_the_ledger_row_too_would_destroy_recoverability(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE COUNTERFACTUAL: clearing the ledger row as well makes the file permanently unrecoverable.

    This is the version of the fix the bead warned against implementing. With the ledger row gone
    there is nothing left that records the file was ever scheduled, and recovery is DEFINED over the
    ledger, so it re-enqueues nothing -- for this file, forever. Nor does any other path pick it up:
    the ledger is the durable "was scheduled" marker the 2026-06-18 over-enqueue incident introduced
    precisely so recovery could not fall back to sweeping the complement-of-done.

    ``ledger_reaper``'s docstring states the same rule from the other side ("an ORPHANED row is
    genuinely owed work that the ledger is RIGHT to hold"). This test is that rule made executable, so
    a future edit to ``active_reaper`` that "tidies up" the ledger row fails here instead of in
    production.
    """
    slack = 900
    _patch_slack(monkeypatch, slack)
    await _seed_owner(session)
    _file, key = await _seed_blocked_file(session, slack=slack)

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 1}
    # The mistake: clear the "orphaned" ledger row too.
    await session.execute(text("DELETE FROM scheduling_ledger WHERE key = :k"), {"k": key})
    await session.commit()

    result, router = await _recover(session)

    # All four counters are 0 because there is no ledger row left to consider at all -- NOT because
    # the row was refused. ``unreplayable`` (phaze-71nz) staying 0 is what distinguishes "deleted,
    # therefore invisible" from "present, but declined as carrying time-limited material".
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert router.captures == []  # the file is now invisible to every recovery path


async def test_reaper_leaves_ledger_rows_of_other_files_alone(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whole stranded cohort recovers together, and every ledger row survives the reap.

    The live population was 2,411 files, not one. Reaping is a single set-based DELETE, so the
    per-file assertion above does not by itself prove the set-based behaviour; this seeds three and
    asserts all three ledger rows survive and all three re-drive.
    """
    slack = 900
    _patch_slack(monkeypatch, slack)
    await _seed_owner(session)
    keys = [(await _seed_blocked_file(session, slack=slack))[1] for _ in range(3)]

    assert await reap_stranded_active_jobs(_make_ctx()) == {"reaped": 3}

    surviving = set((await session.scalars(select(SchedulingLedger.key).where(SchedulingLedger.key.in_(keys)))).all())
    assert surviving == set(keys)

    result, router = await _recover(session)
    assert result["stages"]["process_file"]["reenqueued"] == 3
    assert {p["key"] for p in router.queues[_AGENT_LANE_QUEUE].captured_policy} == set(keys)
