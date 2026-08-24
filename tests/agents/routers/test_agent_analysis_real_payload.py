"""A7 (phaze-qiwdk): the analysis payload's REAL bytes meet the REAL router.

**The gap this closes.** Both halves of this seam were stood in for. The client half is respx
(``tests/agents/services/test_agent_client_endpoints.py``) — a mock that asserts what the client
sent and answers with a canned 200, so the router never sees it. The server half is ~20
hand-written ``json={...}`` literals (``tests/agents/routers/test_agent_analysis.py``) — a body
a person typed, so the producer never wrote it. Agreement between two hand-written fixtures
proves only that the two agree with each other; it says nothing about whether what
``analyze_file`` actually produces is a body this router accepts. That is ADR-0012 rule 3
(verify with the artifact's real consumer) applied to an HTTP seam.

**Why it matters more here than at most seams.** ``AnalysisWindowPayload`` is
``extra='forbid'`` with real bounds, and — this is the sharp part — it is constructed
CLIENT-side, in ``tasks/functions.py::_build_analysis_write_payload`` and
``job_runner.py::_build_payload``. A real essentia value outside any bound therefore raises
``ValidationError`` **in the worker**, not as a 4xx an operator can see, and
``FAILURE_IS_TERMINAL[ANALYZE]`` means the completed multi-hour analysis is simply gone.

Every test below wires REAL components on both halves and nothing in between:

    tests/analyze/_real_result.py  (real essentia + real 68-file model set, real archive track)
      -> phaze.tasks.functions._build_analysis_write_payload   REAL producer (SAQ worker lane)
      -> phaze.job_runner._build_payload                       REAL producer (one-shot pod lane)
      -> phaze.services.agent_client.PhazeAgentClient.put_analysis   REAL client, real dump
      -> httpx ASGITransport                                   real serialization, no respx
      -> phaze.routers.agent_analysis.router                   REAL FastAPI route + pydantic parse
      -> real Postgres                                         real jsonb / varchar acceptance

D-07, D-08 and D-09 are untouched by this module, and it adds no wall clock to any lane.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
import httpx
from httpx import ASGITransport
import pytest
from sqlalchemy import func, select

from phaze.database import get_session
from phaze.job_runner import _build_payload
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.schemas.agent_analysis import AnalysisWindowPayload, AnalysisWritePayload
from phaze.services.agent_client import PhazeAgentClient
from phaze.tasks.functions import _build_analysis_write_payload
from tests.analyze._real_result import real_analysis_result


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Pass-through transport that keeps the LAST request's raw body bytes.

    The point of this module is the producer's ACTUAL BYTES, so one test asserts on the bytes
    themselves rather than only on what the router made of them. Wrapping the ASGI transport
    (instead of reconstructing what httpx "would" send) keeps that assertion on the real object:
    these are the bytes the route then parsed.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.last_body: bytes | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_body = request.content
        return await self._inner.handle_async_request(request)


def _smoke_app(session: AsyncSession) -> FastAPI:
    """The REAL agent-analysis router on a minimal app, matching the sibling module's pattern."""
    app = FastAPI(title="a7-smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed the FileRecord that ``AnalysisResult.file_id`` and every window row FK to."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


@contextlib.asynccontextmanager
async def _real_client(session: AsyncSession, token: str) -> AsyncIterator[PhazeAgentClient]:
    """The REAL ``PhazeAgentClient``, transported onto the REAL router instead of onto respx.

    ``_client`` is the constructor's documented test-injection seam. Injecting an
    ``ASGITransport`` rather than a respx mock is the entire point: the client's own
    ``model_dump(mode='json', exclude_unset=True)``, httpx's own JSON encoding, the route's own
    pydantic parse and the client's own response validation all run for real, in that order.
    """
    transport = _RecordingTransport(ASGITransport(app=_smoke_app(session)))
    inner = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"})
    client = PhazeAgentClient(base_url="http://test", token=token, _client=inner)
    client.recorded_transport = transport  # type: ignore[attr-defined]  # test-only handle on the real bytes
    try:
        yield client
    finally:
        await client.close()


async def test_the_worker_lanes_real_payload_bytes_are_accepted_and_persisted_by_the_real_router(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """SAQ worker lane, end to end: real result -> real producer -> real client -> real router.

    The assertions deliberately reach past the 200. A 200 alone would prove only that pydantic
    parsed the body; what this seam owes is that the values essentia actually produced are
    STORABLE — real ``varchar`` widths, real ``jsonb`` acceptance of every float in every
    window's ``features`` — because the failure this closes is a completed analysis lost at the
    last hop, and the last hop includes the commit.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    result = real_analysis_result()

    payload = _build_analysis_write_payload(result)

    async with _real_client(session, raw_token) as client:
        response = await client.put_analysis(file_id, payload)

    assert response.agent_id == agent.id
    assert response.file_id == file_id

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.bpm == pytest.approx(result["bpm"])
    assert row.musical_key == result["musical_key"]
    assert row.fine_windows_total == result["fine_windows_total"]
    assert row.coarse_windows_total == result["coarse_windows_total"]
    # The aggregate `features` JSONB is the representative coarse window's whole feature dict —
    # every model set, every variant, every float. Postgres accepting it is the real assertion.
    assert row.features is not None
    assert row.features["danceability"] == pytest.approx(result["danceability"])

    window_count = (await session.execute(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalar_one()
    assert window_count == len(result["windows"])


async def test_the_pod_lanes_real_payload_bytes_are_accepted_by_the_same_real_router(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """One-shot pod lane (``job_runner._build_payload``) against the same real router.

    Both lanes are exercised because both build the payload themselves, from the same result
    dict, in two separate functions that are documented as mirroring each other. "Mirroring" is
    a claim about two pieces of code, and the composed path is its own claim (the CLAUDE.md
    wrapper/delegate lesson) — so the pod producer's bytes get their own trip to the router
    rather than inheriting the worker producer's verdict.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    result = real_analysis_result()

    payload = _build_payload(result)

    async with _real_client(session, raw_token) as client:
        response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.musical_key == result["musical_key"]
    window_count = (await session.execute(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalar_one()
    assert window_count == len(result["windows"])


def test_both_lanes_build_the_same_bytes_from_the_same_real_result() -> None:
    """The two producers' wire bytes are IDENTICAL on the real artifact.

    ``job_runner._build_payload``'s docstring claims it "mirrors ``process_file`` exactly". This
    is that claim measured on real input rather than trusted: same dump mode, same
    ``exclude_unset`` set, same key order, same float repr — byte equality, not field-by-field
    approximation, because a divergence in either direction means one lane can store an analysis
    the other cannot.
    """
    result = real_analysis_result()

    worker = _build_analysis_write_payload(result).model_dump(mode="json", exclude_unset=True)
    pod = _build_payload(result).model_dump(mode="json", exclude_unset=True)

    assert json.dumps(worker) == json.dumps(pod)


def test_every_real_window_satisfies_the_wire_bounds_it_is_validated_against() -> None:
    """Every bound on ``AnalysisWindowPayload``, judged against the real artifact.

    ``AnalysisWindowPayload`` is built CLIENT-side, so each bound is a place a real essentia
    value could destroy a completed analysis inside the worker rather than surface as a 4xx.
    The full enumeration, and the verdict for each — the measurement behind the middle column is
    five real archive tracks (201.4 s to 546.0 s, four artists, rock / pop / electronic) plus
    four deliberately degenerate real decodes (200 s and 2 s of digital silence, a 3 s tone below
    the fine-window minimum, and the synthetic parity clip):

        bound                                   real values seen        verdict
        extra='forbid'                          exactly the 8 declared  SAFE by construction --
                                                keys, both tiers        `as_payload_dict` emits
                                                                        only declared names
        tier: Literal['fine','coarse']          both, nothing else      SAFE by construction
        window_index: ge=0, le=INT32_MAX        0 .. 17                 SAFE -- INT32_MAX at 30 s
                                                                        windows is ~1 940 years
        start_sec: ge=0.0                       0.0 .. 540.0            SAFE -- `_iter_windows`
        end_sec:   ge=0.0                       30.0 .. 545.96          starts at 0.0 and climbs
        bpm: ge=0.0                             85.5 .. 172.3; None;    SAFE -- never negative,
                                                738.3 on pure silence   never NaN (see below)
        musical_key: max_length=10              max 8 ("F# minor")      SAFE, 2 chars of margin --
                                                                        key<=2 + ' ' + scale<=5
        mood: max_length=50                     max 10 ("electronic")   SAFE -- `derive_mood`
                                                                        returns one of 7 fixed
                                                                        names, longest 10
        style: max_length=50                    max 29 ("Electronic/    SAFE, 9 chars of margin --
                                                Progressive Breaks")    see the population check
                                                                        in the note below
        danceability: ge=0.0, le=1.0            0.3235 .. 0.9895        SAFE -- a mean of softmax
                                                                        outputs cannot leave 0..1
        features: dict + reject NaN/Inf/NUL     3 031 leaves across the SAFE -- zero non-finite,
                                                five tracks, all        zero NUL, in every real
                                                builtins.float/str/int  AND degenerate decode

    **No bound was found that a real essentia value can violate**, and that negative is the
    result of trying to falsify it rather than of not looking: digital silence is the input most
    likely to drive a classifier to a non-finite output, and 200 s of it produced a complete,
    finite, in-bounds result.

    Two of those verdicts rest on POPULATION checks rather than on the sample, because a sample
    could not settle them. ``style`` is the top ``discogs-effnet-bs64-1`` label with ``---``
    replaced by ``/``; reading that model's own 400-class metadata gives a longest label of 43
    raw characters, 41 after the substitution, and 41 encoded bytes — the longest label in ALL 68
    model metadata files. ``mood`` is drawn from ``_MOOD_SET_NAMES``, seven fixed strings.
    Swapping in the newer discogs519 taxonomy would invalidate the first of those and is the
    change that should send someone back to this test.

    Out of scope here but worth naming, because the measurement surfaced it: silence yields
    ``bpm=738.3`` and a 2 s clip yields the literal ``style="unknown"``. Both are in-bounds and
    both are stored as though they were findings. That is a data-quality question about what
    ``analyze_file`` should return for degenerate audio, not a wire-contract one, so it is not
    changed here.
    """
    result = real_analysis_result()
    windows = [AnalysisWindowPayload(**w) for w in result["windows"]]
    assert len(windows) == len(result["windows"])

    for w in windows:
        assert w.tier in ("fine", "coarse")
        assert w.window_index >= 0
        assert w.start_sec >= 0.0
        assert w.end_sec >= w.start_sec
        if w.bpm is not None:
            assert w.bpm >= 0.0
        if w.musical_key is not None:
            assert len(w.musical_key) <= 10, f"musical_key {w.musical_key!r} would be refused by max_length=10"
        if w.mood is not None:
            assert len(w.mood) <= 50
        if w.style is not None:
            assert len(w.style) <= 50, f"style {w.style!r} would be refused by max_length=50"
        if w.danceability is not None:
            assert 0.0 <= w.danceability <= 1.0, f"danceability {w.danceability!r} is outside the wire's 0..1"

    aggregate = AnalysisWritePayload(**{k: result[k] for k in ("bpm", "musical_key", "danceability")})
    assert aggregate.musical_key is None or len(aggregate.musical_key) <= 10


def _reject_json_constant(token: str) -> float:
    """``json.loads(parse_constant=...)`` hook: refuse the three non-standard tokens.

    ``NaN`` / ``Infinity`` / ``-Infinity`` are what Python's encoder emits for non-finite floats
    and what its decoder silently accepts back — which is exactly why they can travel a whole
    Python-to-Python path unnoticed and die at PostgreSQL's ``jsonb`` parser instead.
    """
    msg = f"body carries the non-standard JSON token {token!r}"
    raise ValueError(msg)


async def test_the_bytes_the_producer_actually_puts_on_the_wire_are_standard_json(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The recorded request body is parseable as STRICT JSON — no ``NaN``, no ``Infinity``.

    Python's ``json`` is non-standard at both ends by default: the encoder emits bare ``NaN`` and
    the decoder accepts it, so a non-finite essentia value can cross producer, httpx, Starlette
    and pydantic without anything objecting, and first fail at the ``jsonb`` parser inside a
    transaction — see ``services/pg_text.py``. Asserting strict-parseability on the real bytes is
    the cheapest place to see that, and it is an assertion the two hand-written fixtures this
    module replaces could not make: neither of them was produced by the producer.
    """
    _agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, _agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    async with _real_client(session, raw_token) as client:
        await client.put_analysis(file_id, payload)
        body = client.recorded_transport.last_body  # type: ignore[attr-defined]

    assert body is not None
    json.loads(body, parse_constant=_reject_json_constant)
