"""phaze-ek9zf: the sub-window mood and beat grid cross the agent wire and persist (docs/design/0026-subwindow-mood-and-beat-grid.md).

Real Postgres (the seat's harness), the real router, the real payload builders. Version interoperation:

- OLDER AGENT -> NEWER SERVER: the grid fields are optional, so a payload without them is valid; because the
  grids belong to the run that wrote the windows, a windows-replacing PUT without them DELETES any stored grids.
- NEWER AGENT -> OLDER SERVER: the previous server's ``AnalysisWritePayload`` is ``extra='forbid'`` and would 422
  the two new keys. That direction cannot be made safe from this side, so the rollout order is SERVER FIRST; what is
  tested here is that a newer agent talking to a server that has not been taught about grids is never *silently*
  wrong (the keys are present in the dump only when the child produced them).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from phaze.job_runner import _build_payload
from phaze.models.analysis import AnalysisBeatGrid, AnalysisSubwindow, AnalysisWindow
from phaze.schemas.agent_analysis import AnalysisWritePayload
from phaze.services.beat_grid import to_stored_precision
from phaze.services.set_projection import MOOD_ORDER
from phaze.tasks.functions import _build_analysis_write_payload
from tests.agents.routers.test_agent_analysis import _make_client, _seed_file, _window


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    from phaze.models.agent import Agent


def _sub(window_index: int, sub_index: int, mood: bool = True) -> dict[str, Any]:
    start = float(sub_index * 15)
    return {
        "window_index": window_index,
        "sub_index": sub_index,
        "start_sec": start,
        "end_sec": start + 15.0,
        "mood_scores": [round(0.01 * (i + 1) + sub_index / 1000, 6) for i in range(len(MOOD_ORDER))] if mood else None,
    }


def _grid(offset: float = 0.0) -> dict[str, Any]:
    beats = [to_stored_precision(offset + 0.5 * i) for i in range(1, 41)]
    return {"beats": beats, "gaps": [[to_stored_precision(5.0), to_stored_precision(9.0)]]}


async def _subs(session: _AsyncSession, file_id: uuid.UUID) -> list[AnalysisSubwindow]:
    session.expire_all()
    result = await session.execute(select(AnalysisSubwindow).where(AnalysisSubwindow.file_id == file_id).order_by(AnalysisSubwindow.sub_index))
    return list(result.scalars().all())


async def _grid_row(session: _AsyncSession, file_id: uuid.UUID) -> AnalysisBeatGrid | None:
    session.expire_all()
    return (await session.execute(select(AnalysisBeatGrid).where(AnalysisBeatGrid.file_id == file_id))).scalar_one_or_none()


def _body(*, grids: bool, bpm: float = 120.0, offset: float = 0.0, subs: int = 3) -> dict[str, Any]:
    body: dict[str, Any] = {"bpm": bpm, "windows": [_window("fine", 0, 0.0, 30.0, bpm=bpm)]}
    if grids:
        body["subwindows"] = [_sub(0, i, mood=i != subs - 1) for i in range(subs)]
        body["beat_grid"] = _grid(offset)
    return body


@pytest.mark.asyncio
async def test_a_payload_from_an_agent_without_grids_is_accepted_and_persists_what_it_does_today(
    seed_test_agent: tuple[Agent, str], session: _AsyncSession
) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=False))
    assert r.status_code == 200, r.text
    windows = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all()
    assert [w.bpm for w in windows] == [120.0]
    assert await _subs(session, file_id) == []
    assert await _grid_row(session, file_id) is None


@pytest.mark.asyncio
async def test_a_payload_with_grids_round_trips_exactly(seed_test_agent: tuple[Agent, str], session: _AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    body = _body(grids=True)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=body)
    assert r.status_code == 200, r.text
    subs = await _subs(session, file_id)
    assert [
        {"window_index": s.window_index, "sub_index": s.sub_index, "start_sec": s.start_sec, "end_sec": s.end_sec, "mood_scores": s.mood_scores}
        for s in subs
    ] == body["subwindows"]
    assert subs[-1].mood_scores is None, "a recorded gap stays NULL, never an invented vector"
    grid = await _grid_row(session, file_id)
    assert grid is not None
    assert list(grid.beats) == body["beat_grid"]["beats"], "float32 beats read back bit for bit"
    assert grid.beat_count == len(body["beat_grid"]["beats"])
    assert grid.gaps == body["beat_grid"]["gaps"]


@pytest.mark.asyncio
async def test_reanalysis_replaces_the_grids_rather_than_appending(seed_test_agent: tuple[Agent, str], session: _AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=True, subs=4))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=True, offset=100.0, subs=2))
    assert r.status_code == 200, r.text
    assert [s.sub_index for s in await _subs(session, file_id)] == [0, 1]
    grid = await _grid_row(session, file_id)
    assert grid is not None
    assert list(grid.beats) == _grid(100.0)["beats"]


@pytest.mark.asyncio
async def test_an_older_agent_reanalysing_after_a_newer_one_leaves_zero_grid_rows(seed_test_agent: tuple[Agent, str], session: _AsyncSession) -> None:
    """Grids belong to the run that wrote the windows: new windows without grids must not sit beside a previous run's grids."""
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=True))
        assert await _grid_row(session, file_id) is not None
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=False, bpm=99.0))
    assert r.status_code == 200, r.text
    assert await _subs(session, file_id) == []
    assert await _grid_row(session, file_id) is None


@pytest.mark.asyncio
async def test_an_aggregate_only_put_does_not_touch_the_grids(seed_test_agent: tuple[Agent, str], session: _AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(grids=True))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 130.0})
    assert r.status_code == 200, r.text
    assert len(await _subs(session, file_id)) == 3
    assert await _grid_row(session, file_id) is not None


@pytest.mark.asyncio
async def test_the_grids_are_scoped_to_the_path_file_id(seed_test_agent: tuple[Agent, str], session: _AsyncSession) -> None:
    agent, token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_a}", json=_body(grids=True))
        await ac.put(f"/api/internal/agent/analysis/{file_b}", json=_body(grids=True))
        await ac.put(f"/api/internal/agent/analysis/{file_a}", json=_body(grids=False))
    assert await _grid_row(session, file_a) is None
    assert await _grid_row(session, file_b) is not None
    assert len(await _subs(session, file_b)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"beat_grid": {"beats": [1.0, 1.0]}},
        {"beat_grid": {"beats": [2.0, 1.0]}},
        {"beat_grid": {"beats": [1.0], "gaps": [[3.0, 2.0]]}},
        {"beat_grid": {"beats": [1.0], "beat_count": 1}},
        {"subwindows": [{**_sub(0, 0), "mood_scores": [0.1, 0.2]}]},
        {"subwindows": [{**_sub(0, 0), "end_sec": 0.0}]},
        {"subwindows": [{**_sub(0, 0), "extra": 1}]},
    ],
)
async def test_a_malformed_grid_is_a_422_and_stores_nothing(
    seed_test_agent: tuple[Agent, str], session: _AsyncSession, patch: dict[str, Any]
) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={**_body(grids=False), **patch})
    assert r.status_code == 422
    assert await _subs(session, file_id) == []
    assert await _grid_row(session, file_id) is None


def _child_result(*, grids: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bpm": 128.0,
        "windows": [_window("fine", 0, 0.0, 30.0, bpm=128.0)],
        "fine_windows_analyzed": 1,
        "fine_windows_total": 1,
    }
    if grids:
        result["subwindows"] = [_sub(0, 0), _sub(0, 1, mood=False)]
        result["beat_grid"] = _grid()
    return result


@pytest.mark.parametrize("build", [_build_payload, _build_analysis_write_payload], ids=["pod-lane", "worker-lane"])
def test_both_producers_carry_the_grids_when_the_child_made_them_and_omit_the_keys_when_it_did_not(build: Any) -> None:
    with_grids = build(_child_result(grids=True)).model_dump(mode="json", exclude_unset=True)
    assert with_grids["subwindows"] == _child_result(grids=True)["subwindows"]
    assert with_grids["beat_grid"]["beats"] == _grid()["beats"]
    # An older child image returns neither key: the wire body then carries neither, i.e. exactly today's body.
    without = build(_child_result(grids=False)).model_dump(mode="json", exclude_unset=True)
    assert "subwindows" not in without
    assert "beat_grid" not in without
    assert isinstance(build(_child_result(grids=False)), AnalysisWritePayload)


def test_both_producers_build_the_grid_fields_through_one_helper() -> None:
    # phaze-7hcgn.4 finding 7: the two producers had byte-identical copies of the grid-field block; one helper now.
    from phaze import job_runner
    from phaze.schemas import agent_analysis
    from phaze.tasks import functions

    assert job_runner.grid_payload_fields is agent_analysis.grid_payload_fields
    assert functions.grid_payload_fields is agent_analysis.grid_payload_fields
    # A malformed shape from the child is left out, never sent as an unknown or null key.
    assert agent_analysis.grid_payload_fields({"subwindows": {"not": "a list"}, "beat_grid": [1.0]}) == {}
    fields = agent_analysis.grid_payload_fields(_child_result(grids=True))
    assert set(fields) == {"subwindows", "beat_grid"}


@pytest.mark.parametrize("bad", [[1.0, float("nan")], [1.0, float("inf")], [-0.5, 1.0]], ids=["nan", "inf", "negative"])
def test_a_beat_grid_with_a_non_finite_or_negative_beat_is_refused(bad: list[float]) -> None:
    from pydantic import ValidationError

    from phaze.schemas.agent_analysis import BeatGridPayload

    with pytest.raises(ValidationError, match="finite and non-negative"):
        BeatGridPayload(beats=bad)
