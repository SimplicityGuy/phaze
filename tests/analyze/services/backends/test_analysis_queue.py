"""The Analyze queue view: running now, waiting, and the honest alarm (phaze-lwz8n).

The operator saw a red "UNSAFE · LOCAL EXCEEDS CAPACITY: 695 in-flight jobs exceed scheduler cap 1" over
a perfectly healthy backlog, and asked for "a view of a queue, showing what's running and what's left".
These tests read a REAL-shaped ``saq_jobs`` table (``tests/_saq_jobs_seed.py``: SAQ's own columns and
``Job.to_dict`` blob, where ``attempts`` is absent until the worker starts the job) -- never a mocked
count -- because the defect being fixed was a count that meant something other than it said.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.backends import get_running_analyses, get_waiting_page, read_local_analyze_queue
from phaze.services.backends.lane_detail import claim_overdue_seconds, resolve_local_analyze_queue_name
from tests._queue_fakes import FakeTaskRouter, seed_active_agent, wire_fakes
from tests._saq_jobs_seed import create_saq_jobs, drop_saq_jobs, now_ms, seed_bulk_queued, seed_job


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_HEARTBEAT = 3600


async def _file(session: AsyncSession, name: str) -> FileRecord:
    record = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/test/music/{name}",
        original_filename=name,
        current_path=f"/test/music/{name}",
        file_type="mp3",
        file_size=1024,
    )
    session.add(record)
    await session.flush()
    return record


async def _queue(session: AsyncSession) -> tuple[SimpleNamespace, str]:
    """A live fileserver agent + a router, and the analyze queue name the production resolver picks."""
    await seed_active_agent(session, "nox", kind="fileserver")
    app_state = SimpleNamespace(task_router=FakeTaskRouter())
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    assert resolved is not None
    await create_saq_jobs(session)
    return app_state, resolved[1]


def _running_blob(*, started_ago_s: int = 60, touched_ago_s: int = 5, heartbeat: int = _HEARTBEAT) -> dict[str, int]:
    now = now_ms()
    return {"attempts": 1, "started": now - started_ago_s * 1000, "touched": now - touched_ago_s * 1000, "timeout": 0, "heartbeat": heartbeat}


def _claimed_blob(*, claimed_ago_s: int = 1) -> dict[str, int]:
    now = now_ms()
    return {"started": now - claimed_ago_s * 1000, "touched": now - claimed_ago_s * 1000, "timeout": 0, "heartbeat": _HEARTBEAT}


@pytest.mark.asyncio
async def test_queue_split_separates_running_waiting_and_the_three_real_problems(session: AsyncSession) -> None:
    """running = started rows; waiting = queued + claimed-unrun; stuck = stranded + overdue claims + lost heartbeats."""
    app_state, queue = await _queue(session)
    overdue = claim_overdue_seconds() + 60
    for i in range(3):
        await seed_job(session, queue, key=f"process_file:q{i}", status="queued")
    await seed_job(session, queue, key="process_file:run", status="active", blob=_running_blob())
    await seed_job(session, queue, key="process_file:silent", status="active", blob=_running_blob(touched_ago_s=_HEARTBEAT + 60))
    await seed_job(session, queue, key="process_file:claim-a", status="active", blob=_claimed_blob())
    await seed_job(session, queue, key="process_file:claim-b", status="active", blob=_claimed_blob())
    await seed_job(session, queue, key="process_file:claim-old", status="active", blob=_claimed_blob(claimed_ago_s=overdue))
    # A bounded (non-analysis) job abandoned mid-flight long ago: the active reaper's own "stranded" set.
    await seed_job(
        session, queue, key="extract_file_metadata:x", status="active", blob={"attempts": 1, "started": now_ms() - 7_200_000, "timeout": 10}
    )

    split = await read_local_analyze_queue(session, app_state)

    assert split is not None
    assert split.queue == queue
    assert (split.queued, split.running, split.claimed_unrun) == (3, 3, 3)
    assert split.waiting == 6
    assert (split.stranded, split.claimed_overdue, split.heartbeat_lost) == (1, 1, 1)
    assert split.stuck == 3


@pytest.mark.asyncio
async def test_a_fresh_claim_and_a_long_queue_are_not_stuck(session: AsyncSession) -> None:
    """A backlog is not a problem: queued rows and just-claimed rows never count as stuck."""
    app_state, queue = await _queue(session)
    await seed_bulk_queued(session, queue, 679)
    await seed_job(session, queue, key="process_file:run", status="active", blob=_running_blob(started_ago_s=4 * 3600))
    for i in range(15):
        await seed_job(session, queue, key=f"process_file:claim-{i}", status="active", blob=_claimed_blob(claimed_ago_s=30))

    split = await read_local_analyze_queue(session, app_state)

    assert split is not None
    assert (split.queued, split.running, split.claimed_unrun, split.stuck) == (679, 1, 15, 0)


@pytest.mark.asyncio
async def test_queue_split_is_unknown_without_a_readable_broker(session: AsyncSession) -> None:
    """No agent, no router, or no saq_jobs -> None (unknown), never a split full of zeros."""
    assert await read_local_analyze_queue(session, None) is None
    assert await read_local_analyze_queue(session, SimpleNamespace(task_router=FakeTaskRouter())) is None  # no live agent
    await seed_active_agent(session, "nox", kind="fileserver")
    await drop_saq_jobs(session)  # a sibling module may have left one behind; this test needs none
    assert await read_local_analyze_queue(session, SimpleNamespace(task_router=FakeTaskRouter())) is None  # no saq_jobs table


@pytest.mark.asyncio
async def test_running_list_shows_file_lane_start_windows_and_heartbeat(session: AsyncSession) -> None:
    """phaze-lwz8n acceptance 2 -- each STARTED job with its file, lane, start, windows done/total and heartbeat."""
    app_state, queue = await _queue(session)
    running_file = await _file(session, "set-01.mp3")
    claimed_file = await _file(session, "set-02.mp3")
    cloud_file = await _file(session, "set-03.mp3")
    session.add(
        AnalysisResult(file_id=running_file.id, fine_windows_analyzed=12, fine_windows_total=40, coarse_windows_analyzed=3, coarse_windows_total=10)
    )
    session.add(CloudJob(file_id=cloud_file.id, s3_key="staging/x", status=CloudJobStatus.RUNNING.value, backend_id="k8s"))
    await seed_job(session, queue, key=f"process_file:{running_file.id}", status="active", blob=_running_blob(started_ago_s=600, touched_ago_s=30))
    await seed_job(session, queue, key=f"process_file:{claimed_file.id}", status="active", blob=_claimed_blob())
    await session.flush()

    lanes = [{"id": "local", "kind": "local"}, {"id": "k8s", "kind": "kueue"}]
    running = await get_running_analyses(session, app_state, lanes)

    assert running is not None
    by_label = {run.label: run for run in running}
    assert set(by_label) == {"set-01.mp3", "set-03.mp3"}, "a claimed-but-unrun job is waiting, not running"
    local = by_label["set-01.mp3"]
    assert (local.file_id, local.lane, local.lane_kind) == (running_file.id, "local", "local")
    assert (local.windows_done, local.windows_total, local.percent) == (15, 50, 30)
    assert local.started_at is not None and 590 <= (datetime.now(UTC) - local.started_at).total_seconds() <= 620
    assert local.heartbeat_at is not None and 20 <= (datetime.now(UTC) - local.heartbeat_at).total_seconds() <= 60
    assert local.heartbeat_lost is False
    cloud = by_label["set-03.mp3"]
    assert (cloud.lane, cloud.lane_kind, cloud.started_at, cloud.heartbeat_at, cloud.windows_total) == ("k8s", "kueue", None, None, None)


@pytest.mark.asyncio
async def test_running_list_marks_a_lost_heartbeat(session: AsyncSession) -> None:
    """A started job silent past its OWN serialized heartbeat is SAQ's ``Job.stuck`` -- flagged per row."""
    app_state, queue = await _queue(session)
    silent = await _file(session, "set-04.mp3")
    await seed_job(session, queue, key=f"process_file:{silent.id}", status="active", blob=_running_blob(touched_ago_s=_HEARTBEAT + 120))

    running = await get_running_analyses(session, app_state, [{"id": "local", "kind": "local"}])

    assert running is not None and len(running) == 1
    assert running[0].heartbeat_lost is True
    assert running[0].windows_total is None


@pytest.mark.asyncio
async def test_running_list_is_none_when_the_broker_is_unreadable(session: AsyncSession) -> None:
    """An unreadable saq_jobs is an unknown list, rendered as such -- not "nothing running"."""
    await seed_active_agent(session, "nox", kind="fileserver")
    await drop_saq_jobs(session)
    app_state = SimpleNamespace(task_router=FakeTaskRouter())
    assert await get_running_analyses(session, app_state, [{"id": "local", "kind": "local"}]) is None


@pytest.mark.asyncio
async def test_waiting_page_lists_claimed_first_then_queued_and_pages(session: AsyncSession) -> None:
    """The waiting list: claimed-but-unrun first (overdue marked), then queued in SAQ order, paged by sentinel."""
    app_state, queue = await _queue(session)
    names = [f"set-{i:02d}.mp3" for i in range(10, 14)]
    files = [await _file(session, name) for name in names]
    await seed_job(session, queue, key=f"process_file:{files[0].id}", status="queued", scheduled=2)
    await seed_job(session, queue, key=f"process_file:{files[1].id}", status="queued", scheduled=1)
    await seed_job(session, queue, key=f"process_file:{files[2].id}", status="active", blob=_claimed_blob(claimed_ago_s=claim_overdue_seconds() + 60))
    await seed_job(session, queue, key=f"process_file:{files[3].id}", status="active", blob=_running_blob())
    enqueued = datetime.now(UTC) - timedelta(hours=2)
    session.add(SchedulingLedger(key=f"process_file:{files[1].id}", function="process_file", routing="local", payload={}, enqueued_at=enqueued))
    await session.flush()

    first = await get_waiting_page(session, app_state, page=1, page_size=2)
    assert [row.label for row in first.rows] == ["set-12.mp3", "set-11.mp3"]
    assert first.rows[0].claimed and first.rows[0].overdue
    assert not first.rows[1].claimed and first.rows[1].enqueued_at == enqueued
    assert first.has_next and first.note is None

    second = await get_waiting_page(session, app_state, page=2, page_size=2)
    assert [row.label for row in second.rows] == ["set-10.mp3"]
    assert not second.has_next


@pytest.mark.asyncio
async def test_waiting_page_explains_an_unreadable_queue(session: AsyncSession) -> None:
    """No live agent / unreadable broker -> a note, never an empty list presented as "nothing waiting"."""
    none_page = await get_waiting_page(session, None)
    assert none_page.rows == [] and none_page.note
    await seed_active_agent(session, "nox", kind="fileserver")
    await drop_saq_jobs(session)
    broken = await get_waiting_page(session, SimpleNamespace(task_router=FakeTaskRouter()))
    assert broken.rows == [] and broken.note


# ------------------------------------------------------------------ the page, end to end


@pytest.mark.asyncio
async def test_page_renders_the_operators_backlog_as_a_queue_not_an_alarm(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-lwz8n acceptance 1 + 5, end to end over a real-shaped broker.

    679 queued and 16 claimed-but-only-1-running on the local lane, with the default registry's local
    cap of 1. No UNSAFE / OVER LIMIT / red banner, the Running-now list shows the one started job, and
    the header reads running + waiting (not the ledger in-flight count).
    """
    wire_fakes(client)
    app_state = client._transport.app.state  # type: ignore[attr-defined]
    await seed_active_agent(session, "nox", kind="fileserver")
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    assert resolved is not None
    queue = resolved[1]
    await create_saq_jobs(session)
    running_file = await _file(session, "set-20.mp3")
    session.add(AnalysisResult(file_id=running_file.id, fine_windows_analyzed=5, fine_windows_total=20))
    # A running local job has its scheduling-ledger row (written at enqueue), so it is routed, not "unrouted".
    session.add(SchedulingLedger(key=f"process_file:{running_file.id}", function="process_file", routing="local", payload={}))
    await seed_bulk_queued(session, queue, 679)
    await seed_job(session, queue, key=f"process_file:{running_file.id}", status="active", blob=_running_blob(started_ago_s=900))
    for i in range(15):
        await seed_job(session, queue, key=f"process_file:claim-{i}", status="active", blob=_claimed_blob(claimed_ago_s=20))
    await session.commit()

    body = (await client.get("/s/analyze", headers={"HX-Request": "true"})).text
    for alarm in ("UNSAFE", "OVER LIMIT", "AT CAPACITY", "CONGESTED", "STUCK", "exceeds scheduler cap"):
        assert alarm not in body, alarm
    health = body[body.index('id="analysis-health-card"') :]
    assert "bg-red-50" not in health[: health.index("</section>")]

    queue_view = body[body.index('id="analyze-queue"') :]
    queue_view = queue_view[: queue_view.index("</section>")]
    assert "set-20.mp3" in queue_view
    assert "5/20 windows" in queue_view
    assert "15m ago" in queue_view
    assert re.search(r">1</span> running · <span[^>]*>694</span> waiting", queue_view)
    # The header: running + waiting, labelled, seeded on the initial render for the store-bound subtitle.
    assert "$store.pipeline.analyzeRunning = 1" in queue_view and "$store.pipeline.analyzeWaiting = 694" in queue_view
    assert "running ·" in body and "files in the analyze stage" not in body

    poll = (await client.get("/pipeline/stats")).text
    assert "UNSAFE" not in poll and "OVER LIMIT" not in poll
    assert 'id="dag-seed-analyzeRunning" hx-swap-oob="true"' in poll and "$store.pipeline.analyzeRunning = 1" in poll
    assert "$store.pipeline.analyzeWaiting = 694" in poll
    oob_queue = poll[poll.index('id="analyze-queue"') :]
    assert 'hx-swap-oob="true"' in oob_queue[:200]
    assert "set-20.mp3" in oob_queue


@pytest.mark.asyncio
async def test_page_turns_red_for_claims_never_started(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-lwz8n acceptance 3 end to end -- the phaze-tch4d signature (claimed, never run) is red."""
    wire_fakes(client)
    app_state = client._transport.app.state  # type: ignore[attr-defined]
    await seed_active_agent(session, "nox", kind="fileserver")
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    assert resolved is not None
    await create_saq_jobs(session)
    await _file(session, "set-21.mp3")  # a corpus exists, so the workspace renders (not the first-run empty state)
    for i in range(4):
        await seed_job(
            session, resolved[1], key=f"process_file:claim-{i}", status="active", blob=_claimed_blob(claimed_ago_s=claim_overdue_seconds() + 120)
        )
    await session.commit()

    body = (await client.get("/s/analyze", headers={"HX-Request": "true"})).text
    health = body[body.index('id="analysis-health-card"') :]
    health = health[: health.index("</section>")]
    assert 'role="alert"' in health and "bg-red-50" in health
    assert "STUCK" in health and "4 claimed but not started past the stall bound" in health

    waiting = (await client.get("/pipeline/analyze-queue/waiting")).text
    assert waiting.count("CLAIMED, NOT STARTED</span>") == 4


@pytest.mark.asyncio
async def test_lane_detail_shows_the_lanes_running_list(client: AsyncClient, session: AsyncSession) -> None:
    """The lane detail pane gets the same running list, and the local lane shows running · waiting, not in_flight/cap."""
    wire_fakes(client)
    app_state = client._transport.app.state  # type: ignore[attr-defined]
    await seed_active_agent(session, "nox", kind="fileserver")
    resolved = await resolve_local_analyze_queue_name(session, app_state)
    assert resolved is not None
    await create_saq_jobs(session)
    running_file = await _file(session, "set-30.mp3")
    await seed_job(session, resolved[1], key=f"process_file:{running_file.id}", status="active", blob=_running_blob())
    await seed_bulk_queued(session, resolved[1], 3)
    await session.commit()

    body = (await client.get("/pipeline/lanes/local")).text
    assert "1 running · 3 waiting" in body
    assert "Running now" in body and "set-30.mp3" in body


# ------------------------------------------------------------------ pure helpers (branch coverage)


def test_running_row_progress_is_unknown_before_the_file_is_sized() -> None:
    """No window totals yet -> done/total/percent are unknown (rendered "sizing…"), never 0/0."""
    from phaze.services.backends import RunningAnalysis

    run = RunningAnalysis(
        file_id=None,
        label="job process_file:x",
        lane="local",
        lane_kind="local",
        started_at=None,
        heartbeat_at=None,
        heartbeat_lost=False,
        fine_done=None,
        fine_total=None,
        coarse_done=None,
        coarse_total=None,
    )
    assert (run.windows_done, run.windows_total, run.percent) == (None, None, None)


def test_broker_field_and_key_parsing_rejects_what_it_cannot_name() -> None:
    """An absent SAQ timestamp is unknown; a key that is not ``process_file:<uuid>`` names no file."""
    from phaze.services.backends.lane_detail import _completion_label, _file_id_from_key, _ms_to_datetime

    assert _ms_to_datetime(0) is None
    assert _ms_to_datetime(None) is None
    assert _ms_to_datetime(1_000) == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert _file_id_from_key("extract_file_metadata:abc") is None
    assert _file_id_from_key("process_file:not-a-uuid") is None
    file_id = uuid.uuid4()
    assert _file_id_from_key(f"process_file:{file_id}") == file_id
    assert _completion_label("", file_id) == f"file {file_id.hex[:8]} (id, not a hash)"
