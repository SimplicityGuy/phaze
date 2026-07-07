"""Recovery classification + fileserver re-drive for ``push_file`` (Phase 50 Plan 02).

``recover_orphaned_work`` must classify the new Phase-50 ``push_file`` stage so a crash
between staging and the push callback leaves the file re-drivable, while a file that has
already landed on compute scratch (``PUSHED``) -- or advanced past it (``ANALYZED`` /
``ANALYSIS_FAILED``) -- is treated as DONE and never re-pushed. The re-drive of a still-pushing
file must route to a FILESERVER agent (the media-mount owner that initiates the rsync), never
the compute agent, and must skip (not raise) when no fileserver is online (D-10).

The analyze done-set is deliberately UNCHANGED: ``PUSHED`` is NOT analyze-done, so a pushed
file still drives analysis (a ``process_file`` row for a ``PUSHED`` file stays orphaned).

The queue-loss detector ``count_inflight_jobs`` and the live-key set ``get_live_job_keys`` are
stubbed per unit test (the unit DB has no ``saq_jobs`` table). ``ctx`` mirrors the controller
worker shape: ``async_session`` (a sessionmaker bound to the test engine), ``queue`` (a
controller-queue stand-in), ``task_router`` (a ``DedupFakeTaskRouter`` modeling SAQ dedup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks.reenqueue import (
    _DOMAIN_COMPLETED_STAGES,
    _PUSH_DONE,
    _build_done_sets,
    is_domain_completed,
    recover_orphaned_work,
)
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the control settings recover_orphaned_work reads."""

    def __init__(self, *, models_path: str = _MODELS_PATH, llm_batch_size: int = 10) -> None:
        self.models_path = models_path
        self.llm_batch_size = llm_batch_size


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("phaze.tasks.reenqueue.get_settings", lambda: _StubCfg())


def _patch_inflight(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    async def _fake(_session: AsyncSession) -> int:
        return value

    monkeypatch.setattr("phaze.tasks.reenqueue.count_inflight_jobs", _fake)


def _patch_live_keys(monkeypatch: pytest.MonkeyPatch, keys: set[str]) -> None:
    async def _fake(_session: AsyncSession) -> set[str]:
        return set(keys)

    monkeypatch.setattr("phaze.tasks.reenqueue.get_live_job_keys", _fake)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3", state: str = FileState.DISCOVERED) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


def _push_payload(file_id: uuid.UUID) -> dict[str, Any]:
    return {
        "file_id": str(file_id),
        "original_path": f"/music/{file_id}.mp3",
        "file_type": "mp3",
        "agent_id": "nox",
    }


async def _seed_push_ledger(session: AsyncSession, *, file_id: uuid.UUID) -> str:
    """Upsert one ``push_file:<file_id>`` ledger row and return its deterministic key."""
    payload = _push_payload(file_id)
    key = f"push_file:{_KEY_BUILDERS['push_file'](payload)}"
    await upsert_ledger_entry(session, key=key, function="push_file", kwargs=payload)
    await session.commit()
    return key


# --- PUSHING -> re-drive to a fileserver ------------------------------------------------


@pytest.mark.asyncio
async def test_pushing_orphan_redrives_to_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUSHING file's push_file ledger row is orphaned -> re-driven on the fileserver queue."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file(state=FileState.PUSHING)
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["push_file"] == {"reenqueued": 1, "skipped": 0}
    assert "nox-io" in router.queues
    assert [t for t, _ in router.queues["nox-io"].captured] == ["push_file"]
    assert [str(f.id)] == [payload["file_id"] for _name, payload in router.queues["nox-io"].captured]


@pytest.mark.asyncio
async def test_pushing_redrive_routes_to_fileserver_not_compute(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BOTH a fileserver and a compute agent online, push_file re-drives to the FILESERVER."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    f = _make_file(state=FileState.PUSHING)
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "nox-io" in router.queues  # the fileserver got the push (io lane)
    assert not any(k.startswith("cloud") for k in router.queues)  # never the compute agent
    assert result["stages"]["push_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_pushing_redrive_skips_when_no_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fileserver online (only a compute agent) -> push_file row skips with a WARNING, never raises."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # only a compute agent
    f = _make_file(state=FileState.PUSHING)
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The push must NOT land on the compute queue -- it is left for the next staging tick.
    assert "cloud" not in router.queues
    assert result["stages"]["push_file"] == {"reenqueued": 0, "skipped": 0}
    assert "fileserver" in caplog.text.lower()


# --- PUSHED / ANALYZED -> domain-completed (not re-driven) ------------------------------


@pytest.mark.asyncio
async def test_pushing_pushed_state_is_domain_completed(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUSHED file's push_file row is domain-completed (landed on scratch) -> NOT re-pushed."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file(state=FileState.PUSHED)
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["push_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_pushing_analyzed_state_is_domain_completed(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push_file row whose file is ANALYZED / ANALYSIS_FAILED is past pushing -> domain-completed."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f_done = _make_file(state=FileState.ANALYZED)
    f_failed = _make_file(state=FileState.ANALYSIS_FAILED)
    session.add_all([f_done, f_failed])
    await session.commit()
    await _seed_push_ledger(session, file_id=f_done.id)
    await _seed_push_ledger(session, file_id=f_failed.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["push_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


# --- process_file done-set is UNCHANGED: PUSHED is not analyze-done ---------------------


@pytest.mark.asyncio
async def test_pushed_file_is_not_analyze_done_for_process_file(
    async_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """D-10: PUSHED is NOT in the analyze done-set, so a process_file row for it stays orphaned.

    A pushed file has landed on compute scratch but is not yet analyzed; the analyze done-set
    must remain {ANALYZED, ANALYSIS_FAILED} only, so recovery keeps driving its analysis.
    """
    f = _make_file(state=FileState.PUSHED)
    session.add(f)
    await session.commit()

    done_sets = await _build_done_sets(session)

    # PUSHED IS in the push done-set...
    assert str(f.id) in done_sets[_PUSH_DONE]

    # ...but a process_file row for the same PUSHED file is NOT domain-completed (analyze still pending).
    pf_row = SchedulingLedger(key=f"process_file:{f.id}", function="process_file", routing="agent", payload={"file_id": str(f.id)})
    assert is_domain_completed(pf_row, done_sets) is False

    # And a push_file row for the same file IS domain-completed (push landed).
    push_row = SchedulingLedger(key=f"push_file:{f.id}", function="push_file", routing="agent", payload={"file_id": str(f.id)})
    assert is_domain_completed(push_row, done_sets) is True


# --- predicate totality includes push_file ---------------------------------------------


def test_push_file_is_predicate_covered() -> None:
    """push_file joins the domain-predicate-covered set (it is keyed and FileState-classifiable)."""
    assert "push_file" in _DOMAIN_COMPLETED_STAGES
    assert "push_file" in _KEY_BUILDERS
