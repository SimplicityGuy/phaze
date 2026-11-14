"""phaze-qijoh: per-window embeddings cross the agent wire and persist (server first).

Real Postgres (the seat's harness), the real router, the real payload builder. Version interoperation:

- OLDER AGENT -> NEWER SERVER: ``embeddings`` is optional, so a payload without it is valid; because the embeddings belong
  to the run that wrote the windows, a windows-replacing PUT without them DELETES that file's stored rows for the model.
  (Same rule phaze-ek9zf set for the grids, tested in ``test_agent_analysis_grids.py``.)
- NEWER AGENT -> NEWER SERVER: the field is stored, replaced on re-analysis, and read back byte for byte.
- NEWER AGENT -> OLDER SERVER: cannot be made safe from the server side (the previous schema is ``extra='forbid'``); the
  agent client's capability check and the server-first rollout order (docs/deployment.md) cover it, tested in
  ``tests/agents/services/test_agent_client.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from sqlalchemy import select

from phaze.job_runner import _build_payload
from phaze.models.analysis import AnalysisEmbedding, AnalysisWindow
from phaze.schemas.agent_analysis import MAX_EMBEDDING_WINDOWS, AnalysisWritePayload, embedding_payload_fields
from phaze.services.analysis_embedding_store import load_embeddings, replace_embeddings
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, embeddings_payload, encode_embedding
from phaze.tasks.functions import _build_analysis_write_payload
from tests.agents.routers.test_agent_analysis import _make_client, _seed_file, _window


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _vector(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(EMBEDDING_DIM).astype(np.float16)


def _embeddings(indexes: list[int], *, salt: int = 0, model: str = EMBEDDING_MODEL) -> dict[str, Any]:
    payload = embeddings_payload({i: encode_embedding(_vector(1000 * salt + i)) for i in indexes})
    payload["model"] = model
    return payload


def _body(*, embeddings: dict[str, Any] | None, bpm: float = 120.0) -> dict[str, Any]:
    body: dict[str, Any] = {"bpm": bpm, "windows": [_window("coarse", 0, 0.0, 60.0, bpm=bpm)]}
    if embeddings is not None:
        body["embeddings"] = embeddings
    return body


async def _rows(session: AsyncSession, file_id: uuid.UUID, model: str = EMBEDDING_MODEL) -> dict[int, np.ndarray]:
    session.expire_all()
    return await load_embeddings(session, file_id, model)


@pytest.mark.asyncio
async def test_an_older_agent_payload_without_embeddings_is_accepted_and_persists_what_it_does_today(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=None))
    assert r.status_code == 200, r.text
    assert [w.bpm for w in (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all()] == [120.0]
    assert await _rows(session, file_id) == {}


@pytest.mark.asyncio
async def test_a_newer_agent_payload_stores_its_embeddings_byte_for_byte(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0, 2, 5])))
    assert r.status_code == 200, r.text
    stored = await _rows(session, file_id)
    assert sorted(stored) == [0, 2, 5], "a window with no embedding is absent, never a zero vector"
    for index, vector in stored.items():
        assert vector.dtype == np.float16
        assert np.array_equal(vector, _vector(index))


@pytest.mark.asyncio
async def test_reanalysis_replaces_the_embeddings_rather_than_appending(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0, 1, 2])))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([1, 7], salt=1)))
    assert r.status_code == 200, r.text
    stored = await _rows(session, file_id)
    assert sorted(stored) == [1, 7]
    assert np.array_equal(stored[1], _vector(1001))


@pytest.mark.asyncio
async def test_an_older_agent_reanalysing_after_a_newer_one_leaves_zero_embedding_rows(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """Embeddings belong to the run that wrote the windows: new windows without them must not sit beside a previous run's."""
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0, 1])))
        assert len(await _rows(session, file_id)) == 2
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=None, bpm=99.0))
    assert r.status_code == 200, r.text
    assert await _rows(session, file_id) == {}


@pytest.mark.asyncio
async def test_omitting_embeddings_deletes_only_this_models_rows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    other_file = await _seed_file(session, agent.id)
    await replace_embeddings(session, file_id, _embeddings([0], model="some-other-graph"))
    await replace_embeddings(session, other_file, _embeddings([0, 1]))
    await session.commit()
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0])))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=None))
    assert r.status_code == 200, r.text
    assert await _rows(session, file_id) == {}
    assert sorted(await _rows(session, file_id, "some-other-graph")) == [0], "another model's rows are not this PUT's to delete"
    assert sorted(await _rows(session, other_file)) == [0, 1], "another file's rows are never touched (AUTH-01)"


@pytest.mark.asyncio
async def test_an_aggregate_only_put_does_not_touch_the_embeddings(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0, 1])))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 130.0})
    assert r.status_code == 200, r.text
    assert sorted(await _rows(session, file_id)) == [0, 1]


@pytest.mark.asyncio
async def test_a_present_but_empty_embeddings_clears_the_models_rows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([0])))
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=_embeddings([])))
    assert r.status_code == 200, r.text
    assert await _rows(session, file_id) == {}


def _corrupt(payload: dict[str, Any], **window_fields: Any) -> dict[str, Any]:
    payload["windows"][0].update(window_fields)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("embeddings", "what"),
    [
        (_corrupt(_embeddings([0]), vector="!!not base64!!"), "bad base64"),
        (_corrupt(_embeddings([0]), vector=encode_embedding(_vector(1))[:-8]), "wrong length"),
        (_corrupt(_embeddings([0]), window_index=-1), "negative index"),
        ({**_embeddings([0]), "dim": 768}, "wrong dim"),
        ({**_embeddings([0]), "format": "float32"}, "wrong format"),
        ({**_embeddings([0]), "model": ""}, "empty model"),
        ({**_embeddings([0]), "model": "m" * 65}, "model longer than the column"),
        ({**_embeddings([0]), "windows": _embeddings([0])["windows"] * 2}, "duplicate index"),
        ({**_embeddings([0]), "stowaway": 1}, "unknown field"),
    ],
)
async def test_a_malformed_embeddings_field_is_a_422_and_writes_nothing(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, embeddings: dict[str, Any], what: str
) -> None:
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=embeddings))
    assert r.status_code == 422, f"{what}: {r.text}"
    assert await _rows(session, file_id) == {}
    assert (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all() == [], (
        "nothing else was written either"
    )


@pytest.mark.asyncio
async def test_a_twelve_hour_file_s_embeddings_fit_the_route_and_the_body_is_the_measured_size(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """12 h at 180 s coarse windows = 240 windows. No body limit is configured on this route (see docs/deployment.md)."""
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    body = _body(embeddings=_embeddings(list(range(240))))
    size = len(json.dumps(body).encode())
    assert 0.8e6 < size < 0.9e6, size  # ~3.4 kB a window: 240 x 3,416 base64 chars + framing
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=body)
    assert r.status_code == 200, r.text
    assert len(await _rows(session, file_id)) == 240


@pytest.mark.asyncio
async def test_the_wire_cap_is_storable_because_the_insert_is_chunked(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The cap must not lie (phaze-syxv): one multi-row VALUES would exceed PostgreSQL's int16 bind-parameter count first."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    one = encode_embedding(_vector(3))
    payload = embeddings_payload(dict.fromkeys(range(MAX_EMBEDDING_WINDOWS), one))
    assert await replace_embeddings(session, file_id, payload) == MAX_EMBEDDING_WINDOWS
    await session.commit()
    count = len((await session.execute(select(AnalysisEmbedding.window_index).where(AnalysisEmbedding.file_id == file_id))).all())
    assert count == MAX_EMBEDDING_WINDOWS


@pytest.mark.asyncio
async def test_both_payload_builders_carry_the_embeddings_and_neither_sends_it_when_absent() -> None:
    result: dict[str, Any] = {
        "bpm": 124.0,
        "musical_key": "A minor",
        "windows": [{"tier": "coarse", "window_index": 0, "start_sec": 0.0, "end_sec": 60.0}],
    }
    for build in (_build_payload, _build_analysis_write_payload):
        without = build(result)
        assert "embeddings" not in without.model_fields_set
        assert "embeddings" not in without.model_dump(mode="json", exclude_unset=True), "an older server must see exactly the body it always did"
        with_ = build({**result, "embeddings": _embeddings([0, 3])})
        assert with_.embeddings is not None
        assert [w.window_index for w in with_.embeddings.windows] == [0, 3]


def test_an_embeddings_value_that_fails_validation_is_dropped_not_fatal() -> None:
    """It runs after a finished multi-hour analysis: the windows and aggregates must still go out."""
    bad = _corrupt(_embeddings([0]), vector="!!")
    assert embedding_payload_fields({"embeddings": bad}) == {}
    assert embedding_payload_fields({}) == {}
    assert embedding_payload_fields({"embeddings": "not a dict"}) == {}
    payload = _build_payload({"bpm": 1.0, "windows": [], "embeddings": bad})
    assert payload.bpm == 1.0
    assert "embeddings" not in payload.model_fields_set


def test_embedding_wire_literals_match_the_codec() -> None:
    from phaze.services.analysis_embeddings import EMBEDDING_WIRE_FORMAT

    fields = AnalysisWritePayload.model_fields["embeddings"].annotation
    assert fields is not None
    assert EMBEDDING_WIRE_FORMAT == "float16le-base64"
    assert EMBEDDING_DIM == 1280
