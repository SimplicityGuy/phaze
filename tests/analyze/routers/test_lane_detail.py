"""Phase 88 (88-02, DRILL-01 / D-06 / D-07 / D-00b / D-03): lane-detail helpers + endpoint.

Task 1 (this wave, RED-first) locks the two degrade-safe lane-data helpers in ``services/backends.py``:

* ``get_lane_recent_completions(session, backend_id, kind, limit=20)`` -- the last ``LANE_RECENT_N``
  completions for ANY lane kind, newest-first (D-07); ``[]`` on any query error (D-00b degrade). Extended
  by phaze-2u8v.3 to fix three defects found in the same panel: a ``local`` lane no longer hardcodes
  ``[]`` (Open Question 1's "omit, don't fabricate" masked the real local-completions defect -- local
  writes no ``cloud_job`` row, but genuinely completes work and must show it, read straight off
  ``files``/``analysis``); each row's ``completed_at`` is the TRUE ``AnalysisResult.analysis_completed_at``
  instant, not a lane-side observation timestamp (the kueue lane's old ``CloudJob.updated_at`` only moves
  on the next ``*/5 * * * *`` reconcile tick, clustering every completion on a 5-minute boundary); and
  each row's ``label`` is the file's own name, never the bare ``file_id.hex[:8]`` the panel used to show
  (confirmed a UUID prefix, not the sha256 it was presumed to be).
* ``get_lane_queue_depths(app_state, backend_id)`` -- per-lane-tier queue depth, each source degrading
  to 0 on a missing ``app.state`` attr / broker hiccup (never a 500 into the 5s tick).

Task 2 extends this module with the ``GET /pipeline/lanes/{backend_id}`` endpoint + ``_lane_detail.html``
body assertions (kind-adaptivity, offline empty state, own-tick).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _render_lane_detail(**context: object) -> str:
    """Render the _lane_detail.html body directly (no request global) for template-level assertions."""
    from phaze.routers.pipeline import templates

    return templates.get_template("pipeline/partials/_lane_detail.html").render(**context)


async def _seed_succeeded(session: AsyncSession, make_file, backend_id: str, n: int, base: datetime) -> None:  # type: ignore[no-untyped-def]
    """Seed ``n`` succeeded CloudJob rows for ``backend_id`` with strictly-increasing ``updated_at``.

    ``updated_at`` is a naive TIMESTAMP column (no ``timezone=True``); seed NAIVE datetimes so the
    round-trip stays naive and the newest-first assertion compares like-for-like (naive footgun).
    """
    jobs = []
    for i in range(n):
        file = await make_file(original_filename="done.mp3")
        jobs.append(
            CloudJob(
                id=uuid.uuid4(),
                file_id=file.id,
                s3_key=f"staging/{file.id}",
                status=CloudJobStatus.SUCCEEDED.value,
                backend_id=backend_id,
                created_at=base + timedelta(seconds=i),
                updated_at=base + timedelta(seconds=i),
            )
        )
    session.add_all(jobs)
    await session.commit()


@pytest.mark.asyncio
async def test_recent_completions_bounded_newest_first(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """25 succeeded rows -> exactly LANE_RECENT_N (20) returned, newest-first (D-07)."""
    from phaze.services.backends import LANE_RECENT_N, get_lane_recent_completions

    assert LANE_RECENT_N == 20
    # tz-aware: post-phaze-cz3m cloud_job.updated_at is timestamptz, and a naive seed would be read
    # back shifted by the session's UTC offset, breaking the exact-equality assertion below.
    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
    await _seed_succeeded(session, make_file, "compute-x", 25, base)

    # Noise that MUST be excluded: an in-flight row on the same backend, and a succeeded row on another.
    running_file = await make_file(original_filename="running.mp3")
    other_file = await make_file(original_filename="other.mp3")
    session.add_all(
        [
            CloudJob(id=uuid.uuid4(), file_id=running_file.id, status=CloudJobStatus.RUNNING.value, backend_id="compute-x"),
            CloudJob(id=uuid.uuid4(), file_id=other_file.id, status=CloudJobStatus.SUCCEEDED.value, backend_id="compute-y"),
        ]
    )
    await session.commit()

    rows = await get_lane_recent_completions(session, "compute-x", "compute")
    assert len(rows) == 20
    # No AnalysisResult rows seeded here -> completed_at falls back to CloudJob.updated_at (defensive path).
    ups = [r.completed_at for r in rows]
    assert ups == sorted(ups, reverse=True)  # newest-first
    # The 20 newest of the 25 seeded (i=5..24); the oldest returned is base + 5s.
    assert min(ups) == base + timedelta(seconds=5)
    assert all(r.label == "done.mp3" for r in rows)


@pytest.mark.asyncio
async def test_recent_completions_prefers_true_completion_time_over_reconcile_tick(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """phaze-2u8v.3 (b): a row's completed_at is the TRUE analysis_completed_at, not the lagging cloud_job.updated_at.

    Regression fixture for the confirmed root cause: ``reconcile_cloud_jobs`` is a fixed ``*/5 * * * *``
    CronJob (``tasks/controller.py``), so a kueue completion's ``cloud_job.updated_at`` is stamped on
    whichever 5-minute tick happens to notice it -- always LATER than, and unrelated in spacing to, the
    real completion instant recorded in ``analysis.analysis_completed_at`` by the agent callback. This
    mirrors the live-archive shape confirmed for phaze-2u8v.3: a completion whose true instant was
    ``20:05:21`` read back with ``cloud_job.updated_at`` bucketed to the next tick, ``20:10:00``.
    """
    from phaze.services.backends import get_lane_recent_completions

    file = await make_file(original_filename="live-set.mp3")
    true_completed_at = datetime(2026, 7, 27, 20, 5, 21, tzinfo=UTC)
    tick_observed_at = datetime(2026, 7, 27, 20, 10, 0)  # naive: the next */5 cron tick after the real completion
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            status=CloudJobStatus.SUCCEEDED.value,
            backend_id="vox",
            updated_at=tick_observed_at,
        )
    )
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, analysis_completed_at=true_completed_at))
    await session.commit()

    rows = await get_lane_recent_completions(session, "vox", "kueue")
    assert len(rows) == 1
    assert rows[0].completed_at == true_completed_at
    assert rows[0].completed_at != tick_observed_at


@pytest.mark.asyncio
async def test_recent_completions_label_is_filename_not_bare_hash(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """phaze-2u8v.3 (c): a row's label is the file's own name, never a bare id/hash."""
    from phaze.services.backends import get_lane_recent_completions

    file = await make_file(original_filename="concert-set.mp3")
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUCCEEDED.value, backend_id="vox"))
    await session.commit()

    rows = await get_lane_recent_completions(session, "vox", "kueue")
    assert len(rows) == 1
    assert rows[0].label == "concert-set.mp3"
    assert file.id.hex[:8] not in rows[0].label


@pytest.mark.asyncio
async def test_recent_completions_local_lane_populates(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """phaze-2u8v.3 (a): a local lane surfaces its real completions instead of always [].

    A LocalBackend writes no ``cloud_job`` row (unchanged), so a genuinely-local completion is a
    music/video file with a landed ``analysis_completed_at`` and NO ``cloud_job`` row at all -- exactly
    the discriminator :meth:`LocalBackend.in_flight_count` already uses for its in-flight slice.
    """
    from phaze.services.backends import get_lane_recent_completions

    local_file = await make_file(original_filename="local-done.mp3")
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=local_file.id, analysis_completed_at=datetime(2026, 7, 27, 9, 0, 0, tzinfo=UTC)))
    await session.commit()

    rows = await get_lane_recent_completions(session, "local", "local")
    assert len(rows) == 1
    assert rows[0].label == "local-done.mp3"
    assert rows[0].completed_at == datetime(2026, 7, 27, 9, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_recent_completions_local_lane_excludes_cloud_routed_files(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A file that carries ANY cloud_job row (even succeeded) is NOT double-counted as a local completion."""
    from phaze.services.backends import get_lane_recent_completions

    cloud_file = await make_file(original_filename="cloud-done.mp3")
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=cloud_file.id, analysis_completed_at=datetime(2026, 7, 27, 9, 0, 0, tzinfo=UTC)))
    session.add(CloudJob(id=uuid.uuid4(), file_id=cloud_file.id, status=CloudJobStatus.SUCCEEDED.value, backend_id="vox"))
    await session.commit()

    assert await get_lane_recent_completions(session, "local", "local") == []


@pytest.mark.asyncio
async def test_recent_completions_local_lane_empty_when_none_completed(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Genuinely no local completions yet -> [] (the empty state stays honest, not fabricated)."""
    from phaze.services.backends import get_lane_recent_completions

    await make_file(original_filename="still-analyzing.mp3")  # no AnalysisResult row at all
    assert await get_lane_recent_completions(session, "local", "local") == []


async def _seed_succeeded_tied(session: AsyncSession, make_file, backend_id: str, job_ids: list[uuid.UUID], tied_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """Seed one succeeded CloudJob per id in ``job_ids``, ALL sharing the SAME explicit ``updated_at``.

    ``job_ids`` order is the INSERTION order, deliberately unrelated to id order, so a query
    that fell back to heap/insertion order on the ``updated_at`` tie would produce a different
    sequence than the id-descending tiebreaker and the regression assertion would fail.
    """
    jobs = []
    for job_id in job_ids:
        file = await make_file(original_filename=f"tied-{job_id}.mp3")
        jobs.append(
            CloudJob(
                id=job_id,
                file_id=file.id,
                s3_key=f"staging/{file.id}",
                status=CloudJobStatus.SUCCEEDED.value,
                backend_id=backend_id,
                created_at=tied_at,
                updated_at=tied_at,
            )
        )
    session.add_all(jobs)
    await session.commit()


@pytest.mark.asyncio
async def test_recent_completions_tiebreaker_orders_tied_updated_at_by_id_desc(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Rows with an IDENTICAL updated_at come back ordered by CloudJob.id DESC, not heap order.

    Seeds ``LANE_RECENT_N`` + 1 (21) succeeded rows sharing ONE explicit ``updated_at`` -- so
    ``updated_at`` alone leaves every row tied -- with ids assigned in a SCRAMBLED order
    relative to insertion (the exact flaky-test mistake this regression guards against: no
    clock-raced seeding, no hoping timestamps differ). Only the ``CloudJob.id`` tiebreaker on
    ``services.backends.get_lane_recent_completions`` makes the LIMIT-20 boundary total and
    deterministic.

    Regression guard for phaze-c6j5: reverting the ``, CloudJob.id.desc()`` suffix makes both
    the boundary membership and the in-page order depend on Postgres heap layout (verified:
    this assertion fails without the tiebreaker for the scrambled ids below).
    """
    from phaze.services.backends import LANE_RECENT_N, get_lane_recent_completions

    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive on purpose (naive TIMESTAMP column)
    seed_count = LANE_RECENT_N + 1
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(seed_count)]
    scrambled = ids[::2] + ids[1::2]  # e.g. [0,2,4,...,20,1,3,...,19]

    await _seed_succeeded_tied(session, make_file, "compute-x", scrambled, tied_at)

    rows = await get_lane_recent_completions(session, "compute-x", "compute")
    actual_ids = [row.id for row in rows]

    # LIMIT is LANE_RECENT_N (20 of the 21 seeded); the boundary + in-page order come entirely
    # from the id tiebreaker: the 20 LARGEST ids, strictly descending.
    assert len(actual_ids) == LANE_RECENT_N
    assert actual_ids == sorted(ids, reverse=True)[:LANE_RECENT_N]


@pytest.mark.asyncio
async def test_recent_completions_degrades_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced query error degrades to [] with a guarded rollback -- never raises (D-00b)."""
    from unittest.mock import AsyncMock

    from phaze.services.backends import get_lane_recent_completions

    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    assert await get_lane_recent_completions(session, "compute-x", "compute") == []


@pytest.mark.asyncio
async def test_queue_depths_degrade_to_zero_without_app_state(session: AsyncSession) -> None:
    """A missing ``app.state.task_router`` degrades every lane-tier depth to 0, never raises (D-00b)."""

    from phaze.services.backends import get_lane_queue_depths

    class _BareState:
        pass

    result = await get_lane_queue_depths(session, _BareState(), "compute-x", "compute")
    assert result.depths is not None
    assert set(result.depths) == {"analyze", "meta", "io"}
    assert all(v == 0 for v in result.depths.values())


# ---------------------------------------------------------------------------
# phaze-tbps: get_lane_queue_depths must key an agent-backed COMPUTE lane's queue lookup
# off the backend's agent_ref, not its registry id -- the two are independent fields, and
# real dispatch (routers/agent_push.py) enqueues to queue_for(agent_ref, lane). Passing the
# raw id builds/counts a queue no producer writes and no worker consumes: SAQ.count returns
# 0 (not an error), so a fully saturated lane renders indistinguishable from idle.
# ---------------------------------------------------------------------------

_COMPUTE_ID_NE_AGENT_REF_TOML = """
[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[backends]]
kind = "compute"
id = "oci-a1"
rank = 30
cap = 2
agent_ref = "compute-agent-01"
scratch_dir = "/srv/scratch"
push_host = "oci-a1.push.example"
"""


@pytest.mark.asyncio
async def test_queue_depths_resolve_compute_agent_ref_not_backend_id(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """phaze-tbps acceptance: id != agent_ref, jobs queued on the agent's lanes -> real depths, not zeros.

    Regression fixture for the confirmed defect: a lane-detail read for a compute backend whose
    registry ``id`` ("oci-a1") differs from its bound ``agent_ref`` ("compute-agent-01") must read
    the depths off the agent_ref's queues (where real dispatch actually enqueues), not the id's
    (a queue no producer ever writes to, which silently reads 0 -- "saturated lane looks idle").
    """
    from types import SimpleNamespace

    from phaze.services.backends import get_lane_queue_depths
    from tests._queue_fakes import FakeTaskRouter

    backends_toml_env(_COMPUTE_ID_NE_AGENT_REF_TOML)

    router = FakeTaskRouter()
    # Seed depths on the agent_ref's lanes -- the queues real dispatch actually writes to.
    router.set_counts("compute-agent-01", lane="analyze", queued=3, active=2)
    app_state = SimpleNamespace(task_router=router)

    result = await get_lane_queue_depths(session, app_state, "oci-a1", "compute")

    assert result.depths is not None
    assert result.depths["analyze"] == 5, "a saturated agent_ref lane must not read as idle (all zeros)"
    assert result.depths["meta"] == 0
    assert result.depths["io"] == 0
    assert result.agent_id == "compute-agent-01"
    # The queue lookup must have used agent_ref, never the raw backend id.
    assert "compute-agent-01" in router.queue_for_calls
    assert "oci-a1" not in router.queue_for_calls


@pytest.mark.asyncio
async def test_queue_depths_connects_runtime_registered_agent_lane(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """phaze-en7s7: a lane never touched by an API-side enqueue must be connected before counting.

    Regression (#217's fix, missed here): ``queue_for`` constructs the lane's ``PostgresQueue``
    with its psycopg pool ``open=False``. The sibling reader ``get_queue_activity`` connects each
    queue before counting; this reader replicated the PRE-fix shape (no ``connect()``), so every
    lane's ``PoolClosed`` was silently caught by the per-tier ``except`` and rendered as 0 --
    every poll, not a rare race, since ``resolve_lane_queue_agent`` binds a lane fresh here with
    no other producer having connected it first.
    """
    from types import SimpleNamespace

    from phaze.services.backends import get_lane_queue_depths
    from tests._queue_fakes import FakeTaskRouter

    backends_toml_env(_COMPUTE_ID_NE_AGENT_REF_TOML)

    router = FakeTaskRouter()
    # Models a lane bound fresh at read-time (never touched by an API-side enqueue): count()
    # raises until connect() opens the pool.
    router.queue_for("compute-agent-01", "analyze").require_connect().set_counts(queued=3, active=2)
    app_state = SimpleNamespace(task_router=router)

    result = await get_lane_queue_depths(session, app_state, "oci-a1", "compute")

    assert result.depths is not None
    assert result.depths["analyze"] == 5, "an unconnected lane must not degrade to 0 -- it must be connected first"
    assert result.depths["meta"] == 0
    assert result.depths["io"] == 0


# ---------------------------------------------------------------------------
# phaze-2u8v.1: the SAME class of bug that phaze-tbps closed for COMPUTE lanes was left
# open for the two kinds the operator actually runs. A registry of [local, kueue "vox"]
# built ``phaze-agent-local-*`` / ``phaze-agent-vox-*`` -- queues no producer writes and
# no worker consumes -- so both lane panels rendered "analyze 0 · meta 0 ·
# io 0" while the SAME work read 2075 on the fileserver agent's own detail pane.
#
# A local lane's work is enqueued by ``LocalBackend.dispatch`` to the LIVE FILESERVER
# AGENT's lane queues, so that is what it must count. A kueue lane enqueues onto NO
# ``phaze-agent-*`` queue at all (its work is a k8s Job) -- so it must say so, not print a
# zero row that reads as "idle".
# ---------------------------------------------------------------------------

_LOCAL_AND_KUEUE_TOML = """
[[backends]]
kind = "kueue"
id = "vox"
rank = 10
cap = 3
buckets = ["burst-vox"]

  [backends.kube]
  api_url = "https://kube.example:6443"
  namespace = "phaze"
  local_queue = "phaze-burst"

[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[buckets]]
id = "burst-vox"
scope = "cluster-specific"
bucket = "phaze-burst"
endpoint_url = "https://s3.example"
"""


@pytest.mark.asyncio
async def test_queue_depths_local_lane_reads_the_live_fileserver_agent(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """phaze-2u8v.1 acceptance: a local lane counts the FILESERVER agent's queues, never ``phaze-agent-local-*``.

    ``LocalBackend.dispatch`` resolves ``select_active_agent(kind="fileserver")`` and enqueues
    ``process_file`` to ``queue_for(agent.id, ...)``. The lane panel must read the SAME queues, so it
    reports the same per-tier figures the agent-detail pane does -- they are one queue read twice.
    Keying off the registry id counted a queue that does not exist, and SAQ returns 0 for that (not an
    error): the reported shape where a lane holding 2075 jobs rendered as idle.
    """
    from types import SimpleNamespace

    from phaze.services.backends import get_lane_queue_depths
    from tests._queue_fakes import FakeTaskRouter, seed_active_agent

    backends_toml_env(_LOCAL_AND_KUEUE_TOML)
    await seed_active_agent(session, "nox", kind="fileserver")

    router = FakeTaskRouter()
    # The real queues: 942 waiting + 1133 claimed on the fileserver agent's analyze lane.
    router.set_counts("nox", lane="analyze", queued=942, active=1133)
    router.set_counts("nox", lane="io", queued=7, active=1)
    app_state = SimpleNamespace(task_router=router)
    router.queue_for_calls.clear()  # drop the seeding calls -- only the read under test may be asserted

    result = await get_lane_queue_depths(session, app_state, "local", "local")

    assert result.agent_id == "nox"
    assert result.note is None
    assert result.depths is not None
    assert result.depths["analyze"] == 2075, "the local lane must report the fileserver agent's real analyze depth"
    assert result.depths["io"] == 8
    assert result.depths["meta"] == 0
    assert set(router.queue_for_calls) == {"nox"}, "the registry id must never be used as a queue key"


@pytest.mark.asyncio
async def test_queue_depths_local_lane_without_fileserver_says_so(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """No live fileserver agent -> a NOTE, never a fabricated all-zero row (the most dangerous moment to lie)."""
    from types import SimpleNamespace

    from phaze.services.backends import NO_FILESERVER_AGENT_NOTE, get_lane_queue_depths
    from tests._queue_fakes import FakeTaskRouter

    backends_toml_env(_LOCAL_AND_KUEUE_TOML)

    router = FakeTaskRouter()
    result = await get_lane_queue_depths(session, SimpleNamespace(task_router=router), "local", "local")

    assert result.depths is None
    assert result.agent_id is None
    assert result.note == NO_FILESERVER_AGENT_NOTE
    assert router.queue_for_calls == [], "no phantom queue may be constructed when there is no agent"


@pytest.mark.asyncio
async def test_queue_depths_kueue_lane_reports_not_applicable(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A kueue lane has NO SAQ agent queue -> a note, and it must NOT borrow the fileserver's figures.

    Two wrong answers are ruled out at once. ``phaze-agent-vox-*`` does not exist (the reported zeros),
    and the fileserver's queues are not this cluster's either -- its ``analyze`` backlog is LOCAL work,
    and its ``io`` lane carries the ``s3_upload`` staging jobs of EVERY kueue lane, so attributing them
    to one cluster would double-count across N clusters.
    """
    from types import SimpleNamespace

    from phaze.services.backends import KUEUE_NO_SAQ_QUEUE_NOTE, get_lane_queue_depths
    from tests._queue_fakes import FakeTaskRouter, seed_active_agent

    backends_toml_env(_LOCAL_AND_KUEUE_TOML)
    await seed_active_agent(session, "nox", kind="fileserver")

    router = FakeTaskRouter()
    router.set_counts("nox", lane="analyze", queued=942, active=1133)
    router.queue_for_calls.clear()  # drop the seeding calls -- only the read under test may be asserted
    result = await get_lane_queue_depths(session, SimpleNamespace(task_router=router), "vox", "kueue")

    assert result.depths is None, "a kueue lane must not render a zero row that reads as idle"
    assert result.agent_id is None
    assert result.note == KUEUE_NO_SAQ_QUEUE_NOTE
    assert router.queue_for_calls == [], "a kueue lane must name no queue -- not its own id, not the fileserver's"


@pytest.mark.asyncio
async def test_lane_identity_maps_to_the_real_saq_queue_names(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """phaze-2u8v.1 acceptance: pin lane-identity -> SAQ queue NAME against the REAL router.

    The failure mode this closes is a silent one: rename the queue format in
    ``AgentTaskRouter._queue_for`` (or resolve a lane to the wrong identity) and every per-tier count
    quietly becomes 0, because SAQ's ``count`` succeeds on a queue nobody writes to. The fakes cannot
    catch that -- they mint whatever name they are asked for. So assert against the production router
    that the resolved identity for a local lane composes EXACTLY the queue names the live deployment
    has (``phaze-agent-nox-analyze``, ...), and that a kueue lane resolves to no identity at all.
    """
    from phaze.services.agent_task_router import AgentTaskRouter
    from phaze.services.backends import resolve_lane_queue_agent
    from phaze.services.enqueue_router import LANES
    from tests._queue_fakes import seed_active_agent

    backends_toml_env(_LOCAL_AND_KUEUE_TOML)
    await seed_active_agent(session, "nox", kind="fileserver")

    local = await resolve_lane_queue_agent(session, "local", "local")
    assert local.agent_id == "nox"

    # The real router. ``build_pipeline_queue`` opens no connection at construction (it only needs
    # parseable DSNs), so these hosts deliberately name nothing -- this test reads queue NAMES, never
    # a broker, and must not touch the worktree's own Postgres/Redis.
    router = AgentTaskRouter(queue_url="postgresql://never-connected/never-connected", cache_redis_url="redis://never-connected")
    names = [router.queue_for(local.agent_id, lane).name for lane in LANES]
    assert names == ["phaze-agent-nox-analyze", "phaze-agent-nox-meta", "phaze-agent-nox-io"]

    kueue = await resolve_lane_queue_agent(session, "vox", "kueue")
    assert kueue.agent_id is None, "a kueue lane must resolve to NO agent queue -- there is none to name"


# ---------------------------------------------------------------------------
# Task 2: GET /pipeline/lanes/{backend_id} endpoint + _lane_detail.html body.
# ---------------------------------------------------------------------------


async def _first_lane(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Return the first degrade-safe snapshot lane (default registry -> a 'local' lane)."""
    from phaze.services.backends import get_backend_lane_snapshot

    lanes = await get_backend_lane_snapshot(session)
    if not lanes:
        pytest.skip("no backend lanes resolved in this environment")
    return lanes[0]


@pytest.mark.asyncio
async def test_lane_detail_known_lane_renders_fields(client: AsyncClient, session: AsyncSession) -> None:
    """A known lane -> 200 fragment with the RANK label, in-flight/cap, and the own 5s tick (DRILL-01)."""
    lane = await _first_lane(session)
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200, response.text
    body = response.text

    assert f"· {lane['id']}" in body
    assert f"RANK {lane['rank']}" in body
    assert f"{lane['in_flight']}/{lane['cap']}" in body
    # Queue depths degrade to 0 (test client skips the lifespan -> no task_router) but still render.
    assert "Queue depth" in body
    assert "Recent completions" in body
    # D-03 own-tick: the body self-refreshes on its own bounded 5s tick scoped to this lane.
    assert 'hx-trigger="every 5s"' in body
    assert f'hx-get="/pipeline/lanes/{lane["id"]}"' in body
    assert 'hx-target="#detail-pane"' in body
    # phaze-d80hf: the own-tick element itself carries the failure handlers -- a failed tick swaps
    # nothing, so #detail-pane's after-swap handler (which is what used to be documented as "the
    # wave-2 tick handler") never fires and never sets `refreshError`. These are the ONLY assignment
    # sites for that flag now that _detail_pane.html's own comment used to (falsely) claim the wave-2
    # body set it with no code anywhere doing so.
    assert "hx-on::response-error=" in body
    assert "hx-on::send-error=" in body
    assert "refreshError = true" in body


@pytest.mark.asyncio
async def test_lane_detail_unknown_lane_is_friendly_offline(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown backend_id -> a friendly "Lane offline" fragment (200 HTML), never a 500 / JSON (T-88-03)."""
    await _first_lane(session)  # ensure the registry resolves in this env
    response = await client.get("/pipeline/lanes/__nope__")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Lane offline" in body
    assert "offline" in body
    # Never a JSON error body.
    assert response.headers["content-type"].startswith("text/html")
    assert '{"detail"' not in body


@pytest.mark.asyncio
async def test_lane_detail_local_lane_no_completions_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """A local lane shows the "No completions in the last 20" empty state (OQ1: local writes none)."""
    lane = await _first_lane(session)
    if lane["kind"] != "local":
        pytest.skip("default registry did not resolve a local lane first")
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200, response.text
    assert "No completions in the last 20." in response.text


@pytest.mark.asyncio
async def test_lane_detail_template_kueue_shows_inadmissible() -> None:
    """A kueue lane renders quota-waiting + Inadmissible under the D-06 kueue-only branch."""
    kueue_lane = {"id": "k8s-a", "kind": "kueue", "rank": 3, "cap": 8, "in_flight": 2, "available": True, "quota_wait": 4, "inadmissible": 2}
    body = _render_lane_detail(lane=kueue_lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
    assert "inadmissible" in body
    assert "waiting" in body
    assert 'role="alert"' in body  # inadmissible > 0 -> amber alert


@pytest.mark.asyncio
async def test_lane_detail_template_non_kueue_has_no_inadmissible() -> None:
    """A local/compute lane renders NO quota/Inadmissible row (D-06 -- no fabricated n/a fillers)."""
    for kind in ("local", "compute"):
        lane = {"id": f"{kind}-1", "kind": kind, "rank": 1, "cap": 4, "in_flight": 0, "available": True, "quota_wait": 0, "inadmissible": 0}
        body = _render_lane_detail(lane=lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
        assert "inadmissible" not in body
        assert 'role="alert"' not in body


@pytest.mark.asyncio
async def test_lane_detail_unavailable_lane_renders_true_in_flight_not_zero() -> None:
    """phaze-pc2q: an unavailable lane must render its TRUE in_flight/cap, never a fabricated "0".

    Mirrors _lane_card.html's phaze-xd8k fix, reintroduced here: a lane can be unreachable for
    NEW dispatch (``available=False``) while still draining work it already accepted
    (``in_flight > 0``). A literal "0" reads as "nothing is running" and contradicts the sibling
    lane card, which already renders the true count for the identical offline state.
    """
    lane = {"id": "cloud-a", "kind": "compute", "rank": 2, "cap": 8, "in_flight": 3, "available": False, "quota_wait": 0, "inadmissible": 0}
    body = _render_lane_detail(lane=lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
    assert "3/8" in body
    # The fabricated zero must not appear as the header numerator (a bare "0" span).
    assert ">0</span>" not in body


@pytest.mark.asyncio
async def test_lane_detail_no_unsafe_filter(client: AsyncClient, session: AsyncSession) -> None:
    """Operator-declared lane id/kind stay Jinja-autoescaped -- never |safe (T-88-05)."""
    lane = await _first_lane(session)
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200
    assert "|safe" not in response.text


# ---------------------------------------------------------------------------
# phaze-2u8v.1: the RENDERED panel. The service can resolve perfectly and the panel can still
# lie, because "no SAQ queue" and "queue is empty" render identically as a zero row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_detail_template_renders_real_depths_and_names_the_agent() -> None:
    """Real depths render per tier, captioned with the agent they were counted from."""
    lane = {"id": "local", "kind": "local", "rank": 99, "cap": 1, "in_flight": 4, "available": True, "quota_wait": 0, "inadmissible": 0}
    body = _render_lane_detail(
        lane=lane,
        recent_completions=[],
        queue_depths={"analyze": 2075, "meta": 0, "io": 8},
        queue_depths_agent="nox",
        queue_depths_note=None,
        refreshed_at=None,
        recent_n=20,
    )
    assert "analyze 2075" in body
    assert "io 8" in body
    # Naming the agent is what makes a future zero auditable: the operator can see WHICH queues
    # were counted instead of guessing why a busy lane reads idle.
    assert "nox" in body


@pytest.mark.asyncio
async def test_lane_detail_template_renders_the_note_instead_of_a_zero_row() -> None:
    """``queue_depths is None`` -> the reason, NEVER "analyze 0 · meta 0 · io 0".

    This is the exact string the operator reported on both lane panels. A kueue lane has no SAQ
    agent queue at all, so printing zeros there is a fabricated measurement, not a degraded one.
    """
    from phaze.services.backends import KUEUE_NO_SAQ_QUEUE_NOTE

    lane = {"id": "vox", "kind": "kueue", "rank": 10, "cap": 3, "in_flight": 3, "available": True, "quota_wait": 0, "inadmissible": 0}
    body = _render_lane_detail(
        lane=lane,
        recent_completions=[],
        queue_depths=None,
        queue_depths_agent=None,
        queue_depths_note=KUEUE_NO_SAQ_QUEUE_NOTE,
        refreshed_at=None,
        recent_n=20,
    )
    assert "Queue depth" in body
    assert KUEUE_NO_SAQ_QUEUE_NOTE in body
    assert "analyze 0" not in body
    assert "meta 0" not in body


@pytest.mark.asyncio
async def test_lane_detail_endpoint_local_lane_reports_the_fileserver_depths(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: GET /pipeline/lanes/local renders the FILESERVER agent's real per-tier depths.

    The reported bug in one request: the panel must not answer "analyze 0" while the agent holding
    the very jobs this lane dispatched answers 2075.
    """
    from tests._queue_fakes import FakeTaskRouter, seed_active_agent

    lane = await _first_lane(session)
    if lane["kind"] != "local":
        pytest.skip("default registry did not resolve a local lane first")

    await seed_active_agent(session, "nox", kind="fileserver")
    router = FakeTaskRouter()
    router.set_counts("nox", lane="analyze", queued=942, active=1133)
    # The test client skips the lifespan, so app.state has no task_router -- install the fake.
    monkeypatch.setattr(client._transport.app.state, "task_router", router, raising=False)  # type: ignore[attr-defined]

    response = await client.get(f"/pipeline/lanes/{lane['id']}")

    assert response.status_code == 200, response.text
    assert "analyze 2075" in response.text
    assert "analyze 0" not in response.text
