"""phaze-ek9zf, CLAUDE.md rule 3: the REAL consumer chain for the sub-window mood and the beat grid.

``analyze_file(include_grids=True)`` with real essentia and the real graphs over a short seeded synthetic clip
-> the real payload builder -> a real PUT through the real router -> the rows in ``analysis_subwindow`` and
``analysis_beat_grid``, compared with what ``analyze_file`` produced. Builder tests over a dict shape cannot catch
a key or format mismatch between what the analysis really emits and what the server parses; this one can.

SKIPS where the real models are absent (a disclosed gap, not coverage), like the other real-model tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from phaze.job_runner import _build_payload
from phaze.models.analysis import AnalysisBeatGrid, AnalysisSubwindow
from phaze.services.analysis import analyze_file
from tests.agents.routers.test_agent_analysis import _make_client, _seed_file
from tests.analyze.services.pipeline.test_subwindow_mood_real_models import _COARSE, _MODELS_DIR, _write_wav, requires_real_models


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


@requires_real_models
@pytest.mark.integration
async def test_real_analysis_output_reaches_both_tables_unchanged(seed_test_agent: tuple[Agent, str], session: AsyncSession, tmp_path: Path) -> None:
    assert _MODELS_DIR is not None
    audio = tmp_path / "tone_click_124bpm.wav"
    _write_wav(audio)
    result = analyze_file(str(audio), _MODELS_DIR, fine_window_sec=30, coarse_window_sec=_COARSE, fine_min_sec=15, include_grids=True)
    assert result["subwindows"], "the real analysis produced no sub-windows: the chain below would prove nothing"
    assert result["beat_grid"]["beats"], "the real analysis produced no beats: the chain below would prove nothing"

    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    body = _build_payload(result).model_dump(mode="json", exclude_unset=True)
    async with _make_client(session, token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=body)
    assert response.status_code == 200, response.text

    session.expire_all()
    subs = (
        (
            await session.execute(
                select(AnalysisSubwindow)
                .where(AnalysisSubwindow.file_id == file_id)
                .order_by(AnalysisSubwindow.window_index, AnalysisSubwindow.sub_index)
            )
        )
        .scalars()
        .all()
    )
    assert [
        {"window_index": s.window_index, "sub_index": s.sub_index, "start_sec": s.start_sec, "end_sec": s.end_sec, "mood_scores": s.mood_scores}
        for s in subs
    ] == result["subwindows"]
    grid = (await session.execute(select(AnalysisBeatGrid).where(AnalysisBeatGrid.file_id == file_id))).scalar_one()
    assert list(grid.beats) == result["beat_grid"]["beats"]
    assert grid.beat_count == len(result["beat_grid"]["beats"])
    assert grid.gaps == result["beat_grid"]["gaps"]
