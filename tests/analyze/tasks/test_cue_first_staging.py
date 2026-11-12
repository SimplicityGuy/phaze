"""phaze-tdton: the cloud staging window fills with CUE-linked held files before older non-CUE ones.

Production shape: nearly every CUE-linked unanalysed file is long, so it is held in AWAITING_CLOUD and only
``stage_cloud_window`` releases it. The window is bounded and its membership rule is unchanged; the drain
walks the awaiting set as two bands (phaze-2al7q.3): the CUE-linked band first, then the rest, each in FIFO
``(created_at, id)`` order from its own keyset cursor.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from phaze.services.pipeline import CLOUD_BANDS, get_cloud_staging_candidates
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.analyze.tasks.test_drain_head_of_line import (
    _BASE_TIME,
    _FREE_CLOUD_SLOTS,
    _SMALL_BUDGET,
    _CloudStub,
    _FullLocalBackend,
    _make_ctx,
    _make_file,
    _patch_backends,
    _seed,
    _seed_lane,
    _small_budget,
)
from tests.shared.services.pipeline.test_cue_first_ordering import _link


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _fresh_resume_cursors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_awaiting_cloud, "_resume_cursors", dict.fromkeys(CLOUD_BANDS))


@pytest.mark.asyncio
async def test_a_cue_linked_long_file_is_staged_before_an_older_non_cue_long_file(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    older_plain = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(4)]
    newer_cue = [_make_file(_BASE_TIME + timedelta(hours=5, seconds=i)) for i in range(2)]
    await _seed(session, [*older_plain, *newer_cue], attempts=0)
    for f in newer_cue:
        await _link(session, f, "cue")
    await session.commit()

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert result["staged"] == _FREE_CLOUD_SLOTS, "the window stays bounded by its free slots"
    assert cloud.dispatched[:2] == [f.id for f in newer_cue], "CUE-linked files fill the window first, FIFO among themselves"
    assert cloud.dispatched[2] == older_plain[0].id, "then the oldest non-CUE file: FIFO is unchanged outside the CUE band"


@pytest.mark.asyncio
async def test_the_two_bands_page_exactly_and_together_are_the_awaiting_set(session: AsyncSession) -> None:
    """Each band pages its own FIFO order with no skip and no repeat, the bands are disjoint, and their union is every candidate."""
    plain = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(5)]
    cue = [_make_file(_BASE_TIME + timedelta(hours=5, seconds=i)) for i in range(4)]
    await _seed(session, [*plain, *cue], attempts=0)
    for f in cue:
        await _link(session, f, "cue")
    await session.commit()

    seen: dict[str, list[uuid.UUID]] = {}
    for band in CLOUD_BANDS:
        seen[band] = []
        cursor = None
        while page := await get_cloud_staging_candidates(session, 3, band=band, after=cursor):
            seen[band].extend(f.id for f, _ in page)
            cursor = (page[-1][0].created_at, page[-1][0].id)

    assert seen == {"cue": [f.id for f in cue], "rest": [f.id for f in plain]}


@pytest.mark.asyncio
async def test_a_cue_row_linked_after_the_cursor_left_the_cue_band_is_staged_on_the_next_tick(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-2al7q.3 finding 7: the CUE band is walked from its own cursor every tick.

    One cursor used to walk CUE rank 0 and then the rest; once it had moved past rank 0, a file that became a
    CUE-linked candidate behind it (linked later, or released from a hold) waited until the walk wrapped --
    up to ceil(awaiting / scan budget) ticks. Here the rest band holds a prefix longer than the budget, so
    tick 1 carries a cursor deep into it. Then an OLD file gets its awaiting row and a CUE link. Tick 2 must
    stage it.
    """
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    _small_budget(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held_ids, _ = await _seed_lane(session, held=3 * _SMALL_BUDGET, routable=0)

    first = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))
    assert first == {"staged": 0, "skipped": _SMALL_BUDGET}
    assert release_awaiting_cloud._resume_cursors["rest"] is not None, "precondition: the walk carries a cursor past the head"

    newly_linked = _make_file(_BASE_TIME - timedelta(days=1))
    await _seed(session, [newly_linked], attempts=0)
    await _link(session, newly_linked, "cue")
    await session.commit()

    second = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert cloud.dispatched == [newly_linked.id], "the newly CUE-linked file is staged on the very next tick"
    assert second["staged"] == 1
    assert second["staged"] + second["skipped"] <= _SMALL_BUDGET, "and the tick stays within its scan budget"
    assert release_awaiting_cloud._resume_cursors["rest"] is not None, "the rest band keeps walking its held prefix"
    assert not set(cloud.dispatched) & set(held_ids)


@pytest.mark.asyncio
async def test_a_cue_band_held_prefix_cannot_starve_the_rest_band(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CUE band spends at most its share of the scan budget, so routable non-CUE work still moves every tick."""
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    _small_budget(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held_cue = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(4 * _SMALL_BUDGET)]
    await _seed(session, held_cue, attempts=3)
    for f in held_cue:
        await _link(session, f, "cue")
    plain = [_make_file(_BASE_TIME + timedelta(hours=1, seconds=i)) for i in range(_FREE_CLOUD_SLOTS)]
    await _seed(session, plain, attempts=0)
    await session.commit()

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert cloud.dispatched == [f.id for f in plain], "the rest band was reached in the same tick"
    assert result["staged"] + result["skipped"] <= _SMALL_BUDGET
    assert release_awaiting_cloud._resume_cursors["cue"] is not None, "the CUE band carries its own position"


@pytest.mark.asyncio
async def test_an_empty_rest_band_after_the_cue_band_clears_its_cursor(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CUE band staged what it had, slots are still free, and the rest band has nothing: its stale cursor resets to the head."""
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    cue = _make_file(_BASE_TIME)
    await _seed(session, [cue], attempts=0)
    await _link(session, cue, "cue")
    await session.commit()
    monkeypatch.setattr(release_awaiting_cloud, "_resume_cursors", {"cue": None, "rest": (_BASE_TIME, cue.id)})

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert result == {"staged": 1, "skipped": 0}
    assert cloud.dispatched == [cue.id]
    assert release_awaiting_cloud._resume_cursors == {"cue": None, "rest": None}


class _VanishOnRest(_CloudStub):
    """Dispatches CUE-linked files; for any other file the fileserver has vanished (``NoActiveAgentError``)."""

    def __init__(self, cue_ids: set[uuid.UUID]) -> None:
        super().__init__()
        self.cue_ids = cue_ids

    async def dispatch(self, file, session, task_router):  # type: ignore[no-untyped-def]
        from phaze.services.enqueue_router import NoActiveAgentError

        if file.id not in self.cue_ids:
            raise NoActiveAgentError("fileserver")
        return await super().dispatch(file, session, task_router)


@pytest.mark.asyncio
async def test_a_fileserver_vanishing_in_the_rest_band_ends_the_tick_and_keeps_the_cue_work(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    cue = _make_file(_BASE_TIME + timedelta(hours=1))
    plain = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(2)]
    await _seed(session, [cue, *plain], attempts=0)
    await _link(session, cue, "cue")
    await session.commit()
    cloud = _VanishOnRest({cue.id})
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert cloud.dispatched == [cue.id], "the CUE band's dispatch is kept"
    assert result == {"staged": 1, "skipped": len(plain)}, "every rest-band row not dispatched is held"
