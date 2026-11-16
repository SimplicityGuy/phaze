"""phaze-qijoh: per-window embeddings cross the agent wire and persist (server first).

Real Postgres (the seat's harness), the real router, the real payload builder. Version interoperation:

- OLDER AGENT -> NEWER SERVER: ``embeddings`` is optional, so a payload without it is valid; because the embeddings belong
  to the run that wrote the windows, a windows-replacing PUT without them DELETES that file's stored rows, every model's
  (phaze-t04h6.4). (Same rule phaze-ek9zf set for the grids, tested in ``test_agent_analysis_grids.py``.)
- NEWER AGENT -> NEWER SERVER: the field is stored, replaced on re-analysis, and read back byte for byte.
- NEWER AGENT -> OLDER SERVER: cannot be made safe from the server side (the previous schema is ``extra='forbid'``); the
  agent client's capability check and the server-first rollout order (docs/deployment.md) cover it, tested in
  ``tests/agents/services/test_agent_client.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np
import pytest
from sqlalchemy import select

from phaze.job_runner import _build_payload
from phaze.models.analysis import AnalysisEmbedding, AnalysisResult, AnalysisWindow
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
    """A windows-replacing PUT whose coarse windows are window 0 plus every window an embedding names (phaze-t04h6.4)."""
    named = {w["window_index"] for w in embeddings["windows"] if isinstance(w.get("window_index"), int)} if embeddings else set()
    indexes = sorted({0} | {i for i in named if i >= 0})
    body: dict[str, Any] = {"bpm": bpm, "windows": [_window("coarse", i, i * 60.0, (i + 1) * 60.0, bpm=bpm) for i in indexes]}
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
@pytest.mark.parametrize("sent", ["omitted", "this_model", "the_other_model"])
async def test_another_models_rows_do_not_survive_a_windows_replacing_put(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, sent: str
) -> None:
    """phaze-t04h6.4 finding 2: every model's rows were computed over the windows the PUT replaces, so none may outlive them.

    The wire accepts any model name, so the stored set is replaced as a whole. The other file's rows stay (AUTH-01).
    """
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    other_file = await _seed_file(session, agent.id)
    await replace_embeddings(session, file_id, _embeddings([0, 1], model="some-other-graph"))
    await replace_embeddings(session, file_id, _embeddings([0, 1]))
    await replace_embeddings(session, other_file, _embeddings([0, 1], model="some-other-graph"))
    await session.commit()
    embeddings = {"omitted": None, "this_model": _embeddings([2], salt=3), "the_other_model": _embeddings([2], salt=3, model="some-other-graph")}[
        sent
    ]
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_body(embeddings=embeddings))
    assert r.status_code == 200, r.text
    expected = {"omitted": ({}, {}), "this_model": ([2], {}), "the_other_model": ({}, [2])}[sent]
    this_model, other_model = await _rows(session, file_id), await _rows(session, file_id, "some-other-graph")
    assert (sorted(this_model) or {}, sorted(other_model) or {}) == expected, "a previous run's rows outlived the windows they came from"
    assert sorted(await _rows(session, other_file, "some-other-graph")) == [0, 1], "another file's rows are never touched (AUTH-01)"


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


async def _nothing_written(session: AsyncSession, file_id: uuid.UUID) -> None:
    assert await _rows(session, file_id) == {}
    assert (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all() == [], (
        "nothing else was written either"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("windows", "what"),
    [
        (None, "embeddings without windows"),
        ([_window("coarse", 0, 0.0, 60.0)], "an embedding for a coarse window the PUT does not carry"),
        ([_window("coarse", 0, 0.0, 60.0), _window("fine", 1, 0.0, 30.0)], "an index the PUT carries only as a FINE window"),
    ],
)
async def test_mismatched_or_windowless_embeddings_are_refused_before_any_write(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, windows: list[dict[str, Any]] | None, what: str
) -> None:
    """phaze-t04h6.4 finding 6, the server's backstop. The agent cannot send these bodies; the matching agent-side test is
    ``test_the_agent_drops_embeddings_whose_coarse_window_it_does_not_send``."""
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    body: dict[str, Any] = {"bpm": 120.0, "embeddings": _embeddings([0, 1])}
    if windows is not None:
        body["windows"] = windows
    async with _make_client(session, token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=body)
    assert r.status_code == 422, f"{what}: {r.text}"
    assert "embeddings" in r.text
    await _nothing_written(session, file_id)
    assert (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalars().all() == [], (
        "not even the aggregate row"
    )


@pytest.mark.parametrize("builder", [_build_payload, _build_analysis_write_payload], ids=["burst_lane", "worker_lane"])
def test_the_agent_drops_embeddings_whose_coarse_window_it_does_not_send(builder: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-t04h6.4 finding 6, the agent half: both payload builders keep only the embeddings of coarse windows they send,
    so a healthy agent can never draw the terminal 422 above. The drop is counted (``assembly``, one per window)."""
    from phaze.schemas import agent_analysis as schema_mod

    drops: list[tuple[str, int]] = []
    monkeypatch.setattr(schema_mod, "record_embedding_drop", lambda stage, _what, *, count=1, **_kw: drops.append((stage, count)))
    result: dict[str, Any] = {
        "bpm": 124.0,
        "windows": [_window("coarse", 0, 0.0, 180.0), _window("fine", 1, 0.0, 30.0), _window("coarse", 2, 180.0, 360.0)],
        "embeddings": _embeddings([0, 1, 2, 9]),
    }
    payload = builder(result)
    assert payload.embeddings is not None
    assert [w.window_index for w in payload.embeddings.windows] == [0, 2]
    assert drops == [("assembly", 2)]
    AnalysisWritePayload.model_validate(payload.model_dump(mode="json", exclude_unset=True))  # what the server parses: accepted

    no_windows = builder({"bpm": 1.0, "windows": [], "embeddings": _embeddings([0])})
    assert no_windows.embeddings is not None
    assert no_windows.embeddings.windows == [], "no coarse window sent, so no embedding sent: an empty set, never a mismatch"


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
    await _nothing_written(session, file_id)


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
        "windows": [{"tier": "coarse", "window_index": i, "start_sec": i * 60.0, "end_sec": (i + 1) * 60.0} for i in (0, 3)],
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


# --- a whoami failure before the completion PUT (phaze-t04h6.4 finding 1) -------------------------------------------


class _WhoamiUnreachable(httpx.AsyncBaseTransport):
    """The real app behind ASGI, except that the first ``failures`` whoami requests die at connect time."""

    def __init__(self, inner: httpx.AsyncBaseTransport, failures: int) -> None:
        self._inner, self._failures = inner, failures
        self.puts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/whoami") and self._failures > 0:
            self._failures -= 1
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "PUT":
            self.puts += 1
        return await self._inner.handle_async_request(request)


@pytest.mark.asyncio
async def test_a_whoami_failure_before_the_completion_keeps_the_stored_rows_and_the_retry_stores_the_runs_grids_and_embeddings(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The real client, the real routers (whoami + analysis), real Postgres; whoami's transport fails for one whole attempt.

    Before the fix the client read the failure as "no capabilities", sent a windows-replacing PUT without grids or
    embeddings, and the server deleted the previous run's rows while this run's were never sent. Now the completion raises
    the class a failed PUT raises (``AgentApiServerError``), sends nothing, and the lane's retry stores the run in full.
    """
    from httpx import ASGITransport

    from phaze.routers import agent_identity
    from phaze.services.agent_client import AgentApiServerError, PhazeAgentClient
    from tests.agents.routers.test_agent_analysis import _make_smoke_app
    from tests.agents.routers.test_agent_analysis_grids import _grid, _grid_row, _sub, _subs

    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    url = f"/api/internal/agent/analysis/{file_id}"

    # The previous run: embeddings for coarse windows 0-1, a beat grid, three sub-windows.
    previous = {**_body(embeddings=_embeddings([0, 1])), "subwindows": [_sub(0, i) for i in range(3)], "beat_grid": _grid(0.0)}
    async with _make_client(session, token) as ac:
        assert (await ac.put(url, json=previous)).status_code == 200

    # This run: a different set, built by the real payload builder from an analyze_file-shaped result.
    windows = [_window("coarse", i, i * 180.0, (i + 1) * 180.0) for i in (0, 1, 2)]
    result = {
        "bpm": 128.0,
        "windows": windows,
        "embeddings": _embeddings([0, 2], salt=5),
        "subwindows": [_sub(2, i) for i in range(2)],
        "beat_grid": _grid(0.25),
    }
    payload = _build_payload(result)

    app = _make_smoke_app(session)
    app.include_router(agent_identity.router)
    transport = _WhoamiUnreachable(ASGITransport(app=app), failures=3)  # 3 = the client's whole bounded retry
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as http:

        async def _no_wait(_delay: float) -> None:
            return None

        client = PhazeAgentClient("http://test", token, _client=http, _retry_sleep=_no_wait)
        with pytest.raises(AgentApiServerError):
            await client.put_analysis(file_id, payload)
        assert transport.puts == 0, "no PUT went out: a stripped one would have deleted the stored rows"
        stored = await _rows(session, file_id)
        assert sorted(stored) == [0, 1] and np.array_equal(stored[1], _vector(1)), "the previous run's embeddings are intact"
        assert len(await _subs(session, file_id)) == 3
        grid = await _grid_row(session, file_id)
        assert grid is not None and grid.beats[0] == _grid(0.0)["beats"][0]

        await client.put_analysis(file_id, payload)  # the lane's retry: the SAQ job re-run or the burst re-drive

    assert transport.puts == 1
    stored = await _rows(session, file_id)
    assert sorted(stored) == [0, 2], "this run's embeddings were stored, not the previous run's"
    assert np.array_equal(stored[2], _vector(5002))
    assert [s.window_index for s in await _subs(session, file_id)] == [2, 2]
    grid = await _grid_row(session, file_id)
    assert grid is not None and grid.beats[0] == _grid(0.25)["beats"][0]
