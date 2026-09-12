"""Owner-specific fileserver, compute, and controller routing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks.reenqueue import (
    recover_orphaned_work,
)
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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


def _make_ctx(router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    # 92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by
    # the ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    # (create_savepoint), exactly as the production controller wires ``ctx["async_session"]`` -- so the task
    # SEES seeded rows and its commits are visible to sibling reads under create_savepoint isolation.
    from phaze.database import async_session

    return {"async_session": async_session, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
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


@pytest.mark.asyncio
async def test_pushing_orphan_redrives_to_fileserver(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUSHING file's push_file ledger row is orphaned -> re-driven on the fileserver queue."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    assert result["stages"]["push_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert "nox-io" in router.queues
    assert [t for t, _ in router.queues["nox-io"].captured] == ["push_file"]
    assert [str(f.id)] == [payload["file_id"] for _name, payload in router.queues["nox-io"].captured]


@pytest.mark.asyncio
async def test_pushing_redrive_routes_to_fileserver_not_compute(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BOTH a fileserver and a compute agent online, push_file re-drives to the FILESERVER."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    assert "nox-io" in router.queues  # the fileserver got the push (io lane)
    assert not any(k.startswith("cloud") for k in router.queues)  # never the compute agent
    assert result["stages"]["push_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_pushing_redrive_skips_when_no_fileserver(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fileserver online (only a compute agent) -> push_file row skips with a WARNING, never raises."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # only a compute agent
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_push_ledger(session, file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    # The push must NOT land on the compute queue -- it is left for the next staging tick.
    assert "cloud" not in router.queues
    assert result["stages"]["push_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert "fileserver" in caplog.text.lower()


def _process_payload(file_id: uuid.UUID, *, agent_id: str) -> dict[str, Any]:
    return {
        "file_id": str(file_id),
        "original_path": f"/music/{file_id}.mp3",
        "file_type": "mp3",
        "agent_id": agent_id,
        "models_path": _MODELS_PATH,
    }


async def _seed_process_ledger(session: AsyncSession, *, file_id: uuid.UUID, agent_id: str) -> str:
    """Upsert one ``process_file:<file_id>`` ledger row owned by ``agent_id`` and return its key."""
    payload = _process_payload(file_id, agent_id=agent_id)
    key = f"process_file:{_KEY_BUILDERS['process_file'](payload)}"
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs=payload)
    await session.commit()
    return key


@pytest.mark.asyncio
async def test_process_file_orphan_owned_by_compute_agent_redrives_to_compute(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-5dkgp: a compute-owned, orphaned process_file row re-drives onto ITS compute owner.

    Before the fix ``_replay_agent_rows_by_owner`` pinned ``kind="fileserver"`` for EVERY agent-routed
    row, so ``select_agent_by_id(owner_id, kind="fileserver")`` raised ``NoActiveAgentError`` for a
    compute-owned ``process_file`` row on every pass, even with the owning compute agent live and
    healthy -- permanently stranding the row (the /pushed callback's own comment: "the durable recovery
    handle recover_orphaned_work re-drives if the job is later lost"). The fix resolves the owner by id
    with no kind pin for every agent function except ``push_file``, so a live compute owner is accepted.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_process_ledger(session, file_id=f.id, agent_id="cloud")
    # No analysis row -> not analyze-done -> orphaned and re-drivable.

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert "cloud-analyze" in router.queues
    assert [t for t, _ in router.queues["cloud-analyze"].captured] == ["process_file"]
    assert [str(f.id)] == [payload["file_id"] for _name, payload in router.queues["cloud-analyze"].captured]


@pytest.mark.asyncio
async def test_process_file_orphan_owned_by_offline_compute_agent_skips(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A compute-owned process_file row whose owner is offline/unregistered skips with a WARNING, never raises."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    # No agent seeded at all -- "cloud" is unregistered.
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_process_ledger(session, file_id=f.id, agent_id="cloud")

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    assert not router.queues
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert "owning agent offline" in caplog.text.lower()


@pytest.mark.asyncio
async def test_push_file_orphan_still_requires_fileserver_kind_when_owner_is_compute(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """phaze-5dkgp regression guard: push_file keeps its D-10 fileserver pin -- a compute-only owner still skips.

    The fix must NOT weaken push_file's Phase-50/D-10 guarantee (a re-driven push reads the media mount
    and must never land on a compute agent) while relaxing process_file's owner-kind requirement.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    f = _make_file()
    session.add(f)
    await session.commit()
    # The push_file row is (unrealistically) owned by a compute agent id -- must still be refused.
    payload = _push_payload(f.id)
    payload["agent_id"] = "cloud"
    key = f"push_file:{_KEY_BUILDERS['push_file'](payload)}"
    await upsert_ledger_entry(session, key=key, function="push_file", kwargs=payload)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(router, controller_queue))

    assert not router.queues
    assert result["stages"]["push_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
