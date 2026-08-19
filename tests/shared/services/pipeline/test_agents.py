"""Tests for `services/pipeline/agents.py` (split from test_pipeline.py, phaze-7l8jh).

get_queue_activity, get_agent_lane_depths, get_agent_recent_scans, count_active_agents, queue_progress_percent -- `services/pipeline/agents.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.services.pipeline._shared import (
    FakeQueue,
    FakeTaskRouter,
    SimpleNamespace,
    _NullSavepoint,
    _scan_batch,
    count_active_agents,
    datetime,
    get_agent_lane_depths,
    get_agent_recent_scans,
    get_queue_activity,
    pytest,
    seed_active_agent,
    timedelta,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_queue_activity_sums_across_agents(session: AsyncSession):
    """agent_* fields sum queued+active over ALL non-revoked agents; controller is separate."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.set_counts("lux", queued=4, active=1)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert activity["agent_queued"] == 7
    assert activity["agent_active"] == 3
    assert activity["agent_busy"] == 10
    assert activity["controller_queued"] == 5
    assert activity["controller_active"] == 0
    assert activity["controller_busy"] == 5


@pytest.mark.asyncio
async def test_get_queue_activity_excludes_scheduled(session: AsyncSession):
    """A large 'incomplete' (scheduled-inclusive) depth never changes the busy totals."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    # Seed the cached per-agent queue directly so we can also set a huge incomplete depth.
    router.queue_for("nox").set_counts(queued=3, active=2, incomplete=999)
    controller = FakeQueue("controller")
    controller.set_counts(queued=1, active=0, incomplete=999)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # Only queued+active are read; the 999 scheduled-inclusive depth is ignored.
    assert activity["agent_busy"] == 5
    assert activity["controller_busy"] == 1


@pytest.mark.asyncio
async def test_get_queue_activity_degrades_on_redis_error(session: AsyncSession):
    """A Redis error on every source degrades all six values to 0 without raising."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.queue_for("nox").fail_count()
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    controller.fail_count()
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert all(v == 0 for v in activity.values())


@pytest.mark.asyncio
async def test_get_queue_activity_degrades_on_missing_app_state(session: AsyncSession):
    """A SimpleNamespace lacking task_router/controller_queue degrades to all-zero."""
    await seed_active_agent(session, "nox")

    activity = await get_queue_activity(SimpleNamespace(), session)

    assert all(v == 0 for v in activity.values())


@pytest.mark.asyncio
async def test_get_queue_activity_controller_independent_of_agents(session: AsyncSession):
    """A controller-queue outage zeroes only the controller; agent depth stays intact."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    controller.fail_count()
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert activity["agent_queued"] == 3
    assert activity["agent_active"] == 2
    assert activity["agent_busy"] == 5
    assert activity["controller_queued"] == 0
    assert activity["controller_active"] == 0
    assert activity["controller_busy"] == 0


@pytest.mark.asyncio
async def test_get_queue_activity_degrades_when_agents_query_raises():
    """The ``Agent`` select itself failing (DB hiccup, missing table) degrades the agent source to 0.

    Distinct from the redis/missing-app-state degrades above: here the DB READ that discovers which
    agents exist is what fails, before any per-agent queue is ever touched. The controller source is
    untouched by this session and keeps reporting normally (source isolation).
    """

    class _ExplodingAgentsSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            msg = 'relation "agents" does not exist'
            raise RuntimeError(msg)

    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=FakeTaskRouter(), controller_queue=controller)

    activity = await get_queue_activity(app_state, _ExplodingAgentsSession())  # type: ignore[arg-type]

    assert activity["agent_queued"] == 0
    assert activity["agent_active"] == 0
    assert activity["agent_busy"] == 0
    assert activity["controller_queued"] == 5
    assert activity["controller_busy"] == 5


@pytest.mark.asyncio
async def test_get_queue_activity_connects_runtime_registered_agent(session: AsyncSession):
    """A per-agent queue not pre-connected at startup is connected before counting.

    Regression (#217): ``main.py`` only opens pools for agents present at boot. A compute
    agent registered at runtime (``phaze agents add --kind compute``) has an unopened psycopg
    pool, so ``count`` raised ``PoolClosed`` and the whole agent source degraded to 0 (and
    logged ``queue_activity_degraded`` every 5s) until the api restarted. The reader must
    ``connect()`` (idempotent) before ``count`` -- mirroring ``enqueue_for_agent``.
    """
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "k8s-vox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    # k8s-vox models the runtime agent: its base-queue count raises until connect() opens the pool.
    router.queue_for("k8s-vox").require_connect().set_counts(queued=4, active=1)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # Both agents counted (7 queued, 3 active) -- NOT degraded to 0 by the unopened pool.
    assert activity["agent_queued"] == 7
    assert activity["agent_active"] == 3
    assert activity["agent_busy"] == 10
    assert activity["controller_busy"] == 5


@pytest.mark.asyncio
async def test_get_queue_activity_isolates_one_failing_agent(session: AsyncSession):
    """One agent's count failure zeroes only that agent, not the whole agent source.

    A single dead/unconnectable agent queue must not wipe every other agent's live depth
    from the 5s dashboard poll (the pathology that made a single runtime agent degrade the
    entire metric). Per-agent failure isolation, alongside the existing per-source split. (#217)
    """
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "k8s-vox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.queue_for("k8s-vox").set_counts(queued=99, active=99).fail_count()
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # nox's real depth survives; only the failing k8s-vox contributes 0.
    assert activity["agent_queued"] == 3
    assert activity["agent_active"] == 2
    assert activity["agent_busy"] == 5
    assert activity["controller_busy"] == 5


# ---------------------------------------------------------------------------
# get_agent_lane_depths (phaze-en7s7) — per-lane agent-activity-pane depths,
# connect-before-count regression (the #217 fix, missed by this sibling reader)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_lane_depths_connects_runtime_registered_agent(session: AsyncSession):
    """A runtime-registered agent's lanes are connected before counting, not degraded to 0.

    Regression (phaze-en7s7): unlike ``get_queue_activity`` (fixed for #217), this reader called
    ``q.count(...)`` directly with no preceding ``q.connect()``. ``main.py``'s lifespan only opens
    pools for agents present at boot, so an agent registered at runtime (``phaze agents add``) has
    an unopened psycopg pool and every lane's ``count()`` raised ``PoolClosed`` -- silently caught
    by the per-lane ``except`` and rendered as 0 on EVERY poll, not a rare race. ``connect()`` is
    idempotent, mirroring the fixed sibling.
    """
    await seed_active_agent(session, "k8s-vox")
    router = FakeTaskRouter()
    for lane in ("analyze", "meta", "io"):
        router.queue_for("k8s-vox", lane).require_connect().set_counts(queued=2, active=1)
    app_state = SimpleNamespace(task_router=router)

    depths = await get_agent_lane_depths(app_state, "k8s-vox")

    assert depths == {"analyze": 3, "meta": 3, "io": 3}


@pytest.mark.asyncio
async def test_get_agent_lane_depths_isolates_one_failing_lane(session: AsyncSession):
    """One lane's count failure zeroes only that lane, not the whole agent's depth dict."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    router.queue_for("nox", "analyze").set_counts(queued=5, active=1)
    router.queue_for("nox", "meta").set_counts(queued=9, active=9).fail_count()
    router.queue_for("nox", "io").set_counts(queued=1, active=0)
    app_state = SimpleNamespace(task_router=router)

    depths = await get_agent_lane_depths(app_state, "nox")

    assert depths == {"analyze": 6, "meta": 0, "io": 1}


# phaze-2akf: the get_search_busy_count / get_scrape_busy_count sections that used to sit here are
# gone with those functions. They counted saq_jobs rows for search_tracklist /
# scrape_and_store_tracklist, two tasks the legacy scrape path took with it, so both were
# structurally pinned at 0. get_match_busy_count -- the surviving sibling, same static
# _STAGE_BUSY_SQL scan and the same SAVEPOINT degrade -- is still covered below, which is what keeps
# that shared shape under test.


# ---------------------------------------------------------------------------
# count_active_agents (Phase 40, REQ-40-2) — online-agent liveness count, degrade-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_active_agents_excludes_revoked_and_never_seen(session: AsyncSession) -> None:
    """Counts ONLY agents matching select_active_agent's liveness (revoked + never-seen excluded).

    Seeds one online agent (recently seen, not revoked), one revoked agent (``revoked_at`` set), and
    one never-seen agent (``last_seen_at`` None) — the count must be exactly 1, reusing the EXACT
    enqueue_router liveness rule (CONTEXT decision 2).
    """
    from datetime import UTC, datetime

    from phaze.models.agent import Agent

    await seed_active_agent(session, "nox")  # online: revoked_at NULL, last_seen_at set
    session.add(Agent(id="revoked", name="revoked", token_hash=None, scan_roots=[], last_seen_at=datetime.now(UTC), revoked_at=datetime.now(UTC)))
    session.add(Agent(id="never-seen", name="never-seen", token_hash=None, scan_roots=[], last_seen_at=None, revoked_at=None))
    await session.flush()

    assert await count_active_agents(session) == 1


@pytest.mark.asyncio
async def test_count_active_agents_zero_when_none_online(session: AsyncSession) -> None:
    """With no online agents the count is 0 (fail-safe default leaves the node blocked 'Needs agent')."""
    assert await count_active_agents(session) == 0


@pytest.mark.asyncio
async def test_count_active_agents_degrades_on_db_error() -> None:
    """count_active_agents returns 0 and never raises when the agents read fails (T-40-05).

    The degrade default 0 is FAIL-SAFE: ``agentOnline == 0`` leaves the new node blocked 'Needs
    agent', so a liveness-read failure can never let a scan launch with no agent online. The read
    runs inside a SAVEPOINT (``begin_nested``); the exception is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("agents table unavailable")

    assert await count_active_agents(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_agent_recent_scans_tiebreaker_orders_tied_created_at_by_id_desc(session: AsyncSession) -> None:
    """Rows with an IDENTICAL created_at come back ordered by ScanBatch.id DESC, not heap order.

    Seeds ``_AGENT_RECENT_SCANS_N`` + 1 (11) rows sharing ONE explicit ``created_at`` -- so
    ``created_at`` alone leaves every row tied -- with ids assigned in a SCRAMBLED order
    relative to insertion. Only the ``ScanBatch.id`` tiebreaker on
    ``services.pipeline.get_agent_recent_scans`` makes the LIMIT-10 boundary total and
    deterministic: the 10 returned rows must be exactly the 10 largest ids (id DESC to match
    the primary ``created_at DESC``), and their order must be strictly descending by id.

    Regression guard for phaze-c6j5: reverting the ``, ScanBatch.id.desc()`` suffix makes
    both the boundary membership and the in-page order depend on Postgres heap layout
    (verified: this assertion fails without the tiebreaker -- heap/insertion order does not
    match descending-id order for the scrambled ids below).
    """
    from phaze.services.pipeline import _AGENT_RECENT_SCANS_N

    await seed_active_agent(session, "nox")

    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive: test schema's created_at is TIMESTAMP WITHOUT TZ
    # 11 fixed, distinct ids -- deliberately NOT inserted in id order.
    seed_count = _AGENT_RECENT_SCANS_N + 1
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(seed_count)]
    scrambled = ids[::2] + ids[1::2]  # e.g. [0,2,4,6,8,10,1,3,5,7,9]

    for bid in scrambled:
        session.add(_scan_batch("nox", batch_id=bid, created_at=tied_at))
    await session.commit()

    rows = await get_agent_recent_scans(session, "nox")
    actual_ids = [row.id for row in rows]

    # LIMIT is _AGENT_RECENT_SCANS_N (10 of the 11 seeded); the boundary + in-page order come
    # entirely from the id tiebreaker: the 10 LARGEST ids, strictly descending.
    assert len(actual_ids) == _AGENT_RECENT_SCANS_N
    assert actual_ids == sorted(ids, reverse=True)[:_AGENT_RECENT_SCANS_N]


@pytest.mark.asyncio
async def test_get_agent_recent_scans_orders_by_created_at_then_id(session: AsyncSession) -> None:
    """Distinct created_at values dominate; the id tiebreaker only breaks exact ties.

    Seeds explicit, strictly-increasing ``created_at`` values with ids in the OPPOSITE
    order, and asserts the result follows ``created_at`` DESC (newest first) -- confirming
    the primary sort key still wins when timestamps differ.
    """
    await seed_active_agent(session, "nox")
    base = datetime(2026, 7, 20, 9, 0, 0)
    # created_at increases with i; id decreases with i -> the two keys disagree.
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{(90 - i * 10):02d}") for i in range(5)]
    for i, bid in enumerate(ids):
        session.add(_scan_batch("nox", batch_id=bid, created_at=base + timedelta(seconds=i)))
    await session.commit()

    rows = await get_agent_recent_scans(session, "nox")

    # Newest created_at first: i=4 (last inserted, largest timestamp) down to i=0.
    assert [row.id for row in rows] == list(reversed(ids))


@pytest.mark.asyncio
async def test_get_agent_recent_scans_degrades_to_empty_list_on_db_error() -> None:
    """A DB error on the ``ScanBatch`` read degrades to ``[]`` -- never raises into the agent-pane poll.

    The read runs inside a SAVEPOINT; the exception propagates out of the nested scope and is caught
    by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            msg = 'relation "scan_batches" does not exist'
            raise RuntimeError(msg)

    rows = await get_agent_recent_scans(_ExplodingSession(), "nox")  # type: ignore[arg-type]

    assert rows == []
