"""phaze-qijoh, CLAUDE.md rule 3: the REAL consumer chain for the per-window embeddings.

``analyze_file(include_grids=True, include_embeddings=True)`` with real essentia and the real graphs over a short seeded
synthetic clip -> the real payload builder -> a real PUT through the real router -> the rows in ``analysis_embedding``,
compared with what ``analyze_file`` produced. This mirrors ``test_agent_analysis_grids_real_chain.py`` (phaze-ek9zf): real
essentia and real model weights, the same fixture and the same skip.

SKIPS where the real models are absent (``PHAZE_TEST_MODELS_DIR`` unset: CI today). That is a disclosed gap, not coverage:
the router/schema/store half of the chain is exercised without essentia in ``test_agent_analysis_embeddings.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from phaze.job_runner import _build_payload
from phaze.services.analysis import analyze_file
from phaze.services.analysis_embedding_store import load_embeddings
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, decode_embedding
from tests.agents.routers.test_agent_analysis import _make_client, _seed_file
from tests.analyze.services.pipeline.test_subwindow_mood_real_models import _COARSE, _MODELS_DIR, _write_wav, requires_real_models


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


@requires_real_models
@pytest.mark.integration
async def test_real_analysis_embeddings_reach_the_table_unchanged(seed_test_agent: tuple[Agent, str], session: AsyncSession, tmp_path: Path) -> None:
    assert _MODELS_DIR is not None
    audio = tmp_path / "tone_click_124bpm.wav"
    _write_wav(audio)
    result = analyze_file(
        str(audio), _MODELS_DIR, fine_window_sec=30, coarse_window_sec=_COARSE, fine_min_sec=15, include_grids=True, include_embeddings=True
    )
    produced = result["embeddings"]["windows"]
    assert produced, "the real analysis produced no embeddings: the chain below would prove nothing"

    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    body = _build_payload(result).model_dump(mode="json", exclude_unset=True)
    assert [w["window_index"] for w in body["embeddings"]["windows"]] == [w["window_index"] for w in produced], (
        "the builder dropped or reordered windows"
    )
    async with _make_client(session, token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=body)
    assert response.status_code == 200, response.text

    session.expire_all()
    stored = await load_embeddings(session, file_id, EMBEDDING_MODEL)
    assert sorted(stored) == [w["window_index"] for w in produced]
    for window in produced:
        assert stored[window["window_index"]].shape == (EMBEDDING_DIM,)
        assert np.array_equal(stored[window["window_index"]], decode_embedding(window["vector"])), (
            "stored bytes differ from what analyze_file produced"
        )
