"""The automatic-association unit and its agent lock (phaze-spd83).

The triggers, the task and the real producers are covered end to end in
``tests/discovery/test_companion_autolink.py``; coalescing on a real broker in
``tests/integration/test_companion_association_coalescing.py``. This module pins the two pieces those
cannot reach: the lock on a pooled engine (where it must live on a connection of its own), and the
dry-run unit that ``phaze backfill companion-links`` runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from saq import Job
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.companion_autolink import (
    ASSOCIATION_TASK,
    ASSOCIATION_WINDOW_SECONDS,
    _lock_key,
    agent_association_lock,
    association_window,
    run_agent_association,
)
from phaze.services.enqueue_router import CONTROLLER_TASKS
from phaze.tasks._shared.deterministic_key import apply_deterministic_key


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


async def _held_elsewhere(engine: AsyncEngine, agent_id: str) -> bool:
    """Whether ANOTHER connection is refused the agent's lock (it is then held)."""
    async with engine.connect() as other:
        taken = bool((await other.execute(select(func.pg_try_advisory_lock(_lock_key(agent_id))))).scalar())
        if taken:
            await other.execute(select(func.pg_advisory_unlock(_lock_key(agent_id))))
        await other.commit()
    return not taken


async def test_on_a_pooled_engine_the_lock_survives_the_runs_commits_and_is_released(async_engine: AsyncEngine) -> None:
    """The phaze-yhhy shape: association commits per page, which hands a pooled session's connection back.

    The lock must sit on its own connection, held across those commits, and be gone afterwards --
    a lock taken on the session's own connection would leak onto whichever pooled connection it was.
    """
    agent_id = f"lock-{uuid.uuid4().hex[:8]}"
    async with AsyncSession(bind=async_engine) as session:
        async with agent_association_lock(session, agent_id) as acquired:
            assert acquired
            await session.execute(select(1))
            await session.commit()  # what every association page does
            assert await _held_elsewhere(async_engine, agent_id)
            async with agent_association_lock(session, agent_id) as second:
                assert not second  # a second run for the agent is refused, not queued behind it
        assert not await _held_elsewhere(async_engine, agent_id)


async def test_a_dry_run_unit_writes_nothing_and_skips_the_junk_review(session: AsyncSession) -> None:
    media = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path="/m/rel/set.mp3",
        original_filename="set.mp3",
        current_path="/m/rel/set.mp3",
        file_type="mp3",
        file_size=1,
    )
    cue = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash="b" * 64,
        original_path="/m/rel/set.cue",
        original_filename="set.cue",
        current_path="/m/rel/set.cue",
        file_type="cue",
        file_size=1,
    )
    session.add_all([media, cue])
    await session.flush()
    session.add(
        CompanionContentFeatures(
            file_id=cue.id,
            agent_id="test-fileserver",
            fingerprint="b" * 64,
            encoding="ascii",
            byte_size=1,
            media_references=[{"name": "set.mp3", "source": "cue_file"}],
            reference_count=1,
            is_tracklist=False,
            extractor_version=1,
        )
    )
    await session.flush()

    association, detection = await run_agent_association(session, "test-fileserver", apply=False)

    assert (association.links_created, detection) == (1, None)
    assert (await session.execute(select(func.count()).select_from(FileCompanion))).scalar_one() == 0


def test_a_window_is_whole_and_half_open() -> None:
    start = 1_000 * ASSOCIATION_WINDOW_SECONDS
    assert association_window(start) == association_window(start + ASSOCIATION_WINDOW_SECONDS - 0.001) == 1_000
    assert association_window(start + ASSOCIATION_WINDOW_SECONDS) == 1_001


async def test_the_key_is_per_agent_and_window() -> None:
    """One key per (agent, window): the coalescing unit. Any other kwarg cannot split or merge runs."""
    jobs = [
        Job(function=ASSOCIATION_TASK, kwargs={"agent_id": "nox", "window": 7}),
        Job(function=ASSOCIATION_TASK, kwargs={"agent_id": "nox", "window": 7}, key="caller-supplied"),
        Job(function=ASSOCIATION_TASK, kwargs={"agent_id": "nox", "window": 8}),
        Job(function=ASSOCIATION_TASK, kwargs={"agent_id": "lux", "window": 7}),
    ]
    for job in jobs:
        await apply_deterministic_key(job)

    assert [job.key for job in jobs] == [
        f"{ASSOCIATION_TASK}:nox:7",
        f"{ASSOCIATION_TASK}:nox:7",
        f"{ASSOCIATION_TASK}:nox:8",
        f"{ASSOCIATION_TASK}:lux:7",
    ]


def test_registered_on_the_controller_as_a_routable_function_with_no_cron() -> None:
    """Event-driven, never a timer: the api routes it, the controller worker runs it."""
    from phaze.tasks import controller

    assert ASSOCIATION_TASK in CONTROLLER_TASKS
    assert ASSOCIATION_TASK in {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    assert ASSOCIATION_TASK not in {getattr(cron.function, "__name__", "") for cron in controller.settings["cron_jobs"]}
