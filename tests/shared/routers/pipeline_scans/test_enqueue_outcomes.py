"""Controller-side tests for `pipeline_scans._enqueue_scan_or_mark_failed`'s reconciliation phase
(split from test_pipeline_scans.py, phaze-1i0h6.6).

Two failure shapes, reconciled differently: a DEFINITE enqueue failure marks the batch FAILED
(WR-06) and renders the swappable alert envelope; an AMBIGUOUS one (phaze-0dfj4 -- the broker
connection was already live when `enqueue_for_agent` raised) leaves the batch RUNNING for the
progress-based stall reaper to resolve, exactly as it would resolve an agent that silently died
mid-scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline_scans._shared import (
    RENDERABLE_ALERT_STATUS,
    AmbiguousEnqueueError,
    ScanBatch,
    ScanStatus,
    _assert_swappable_alert,
    pytest,
    select,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_post_scans_enqueue_failure_marks_batch_failed(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-06: enqueue failure flips batch to FAILED + returns a swappable alert (no DELETE).

    Previously the failure path tried to DELETE the just-created batch and
    commit; if that secondary commit also raised, the original 500-via-
    unhandled-exception bubble obscured the failure cause AND left an orphan
    RUNNING row that no agent would ever PATCH. The new failure path marks
    the batch FAILED instead, surfacing the attempt in Recent Scans for the
    operator to triage.
    """
    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = RuntimeError("redis down")

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS, response.text
    assert "could not enqueue the scan" in response.text

    # The batch row survives but is FAILED with the documented error_message
    # so the operator sees what happened in Recent Scans.
    rows = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == ScanStatus.FAILED.value
    assert rows[0].error_message == "controller could not enqueue scan to agent worker"


# ---------------------------------------------------------------------------
# phaze-0dfj4: an AMBIGUOUS enqueue failure (the broker connection was already live when
# ``enqueue_for_agent`` raised -- the ``saq_jobs`` INSERT may have already committed) must NOT be
# treated the same as a definite one. Marking the batch FAILED here is a lie the operator acts on:
# they re-trigger (the uq constraint only covers RUNNING rows), creating a second batch + job while
# the phantom first job may still dequeue and walk the archive tree concurrently with the real scan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_scans_ambiguous_enqueue_leaves_batch_running(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``AmbiguousEnqueueError`` leaves the batch RUNNING instead of marking it FAILED (phaze-0dfj4).

    The batch was already committed RUNNING before the enqueue attempt; on an ambiguous outcome
    nothing further is written -- the stall reaper (config.scan_stall_seconds) is left to resolve a
    genuinely-lost enqueue exactly as it would resolve an agent that silently died mid-scan.
    """
    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = AmbiguousEnqueueError("enqueue raised after the broker connection was live")

    with caplog.at_level("ERROR", logger="phaze.routers.pipeline_scans"):
        response = await ac.post(
            "/pipeline/scans",
            data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
        )

    # The handler renders the normal RUNNING progress card, not the enqueue-failure alert.
    assert response.status_code == 200, response.text
    assert "could not enqueue the scan" not in response.text

    rows = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == ScanStatus.RUNNING.value
    assert rows[0].error_message is None
    assert any("enqueue ambiguous" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_post_scans_enqueue_failure_with_secondary_commit_also_failing(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WR-06 inner-except: when enqueue fails AND the secondary commit also
    raises, the handler MUST still return the swappable alert envelope (no 500 escape).

    Covers pipeline_scans.py:255-260 — the defensive ``try/except`` around
    ``await session.commit()`` after marking the batch FAILED. A Postgres-down
    scenario can plausibly knock out the secondary commit too; the operator's
    alert envelope is more important than the orphan-row cleanup.
    """
    import logging as _logging

    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = RuntimeError("redis down")

    # Force the SECOND commit (the one that flips batch -> FAILED) to raise.
    # The first commit happens earlier (saves the initial RUNNING batch); we
    # want that to succeed so we reach the inner try/except.
    original_commit = session.commit
    call_state = {"n": 0}

    async def _commit_fails_on_nth_call() -> None:
        call_state["n"] += 1
        if call_state["n"] >= 2:
            raise RuntimeError("postgres down")
        await original_commit()

    monkeypatch.setattr(session, "commit", _commit_fails_on_nth_call)
    rollback_calls = {"n": 0}
    original_rollback = session.rollback

    async def _record_rollback() -> None:
        rollback_calls["n"] += 1
        await original_rollback()

    monkeypatch.setattr(session, "rollback", _record_rollback)

    with caplog.at_level(_logging.ERROR, logger="phaze.routers.pipeline_scans"):
        response = await ac.post(
            "/pipeline/scans",
            data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
        )

    # Swappable alert envelope still surfaces; no 500 leak.
    assert response.status_code == RENDERABLE_ALERT_STATUS, response.text
    assert "could not enqueue the scan" in response.text
    # The secondary-commit failure was logged for triage.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secondary commit failed" in text, f"missing secondary-commit log: {text!r}"
    # Rollback executed at least once (the handler explicitly issues it on failure).
    assert rollback_calls["n"] >= 1


@pytest.mark.asyncio
async def test_post_scans_enqueue_failure_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Branch 5/6 -- enqueue failure.

    The only branch where a 5xx is even arguable, since the failure IS server-side. It is
    still a 200: the operator triggered a scan, phaze understood the request completely,
    and the answer it owes them is "I could not hand this to the worker, retry" -- rendered
    into the target they are staring at. The FAILED ``ScanBatch`` row in Recent Scans is a
    secondary surface the operator only sees if they scroll; it does not substitute for the
    inline alert.
    """
    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = RuntimeError("worker unreachable")

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    _assert_swappable_alert(response, "could not enqueue the scan")
