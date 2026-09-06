"""phaze-x1qr3.3: the persist path writes the set projection, and a projection failure never
fails the analysis it rides along with.

Mirrors ``tests/agents/routers/test_agent_analysis_real_payload.py``'s real-producer/real-consumer
wiring (ADR-0012 rule 3): the REAL ``analyze_file`` artifact -> the REAL wire payload builder -> the
REAL ``PhazeAgentClient`` -> the REAL router, so the projection is exercised against genuine
essentia output shapes (real ``musical_key`` strings, real ``features`` JSONB), never a hand-built
stub that cannot exhibit the alphabetical-ordering trap ``positive_class_vector`` exists to avoid.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
import httpx
from httpx import ASGITransport
from sqlalchemy import func, select
import structlog

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile
from phaze.routers import agent_analysis
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.set_projection import CAMELOT_TABLE
from phaze.services.set_projection_writer import CURRENT_PROJECTION_VERSION
from phaze.tasks.functions import _build_analysis_write_payload
from tests.analyze._real_result import real_analysis_result


_VALID_CAMELOT_CODES = frozenset(CAMELOT_TABLE.values())
"""The only 24 strings `_wheel_adjacent` (set_projection.py) can parse without raising -- a stored
`camelot` value outside this set (or NULL) would blow up `harmonic_discipline`/glyph rendering on
read, so the writer must never persist anything else there (reviewer finding on phaze-x1qr3.2)."""


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _smoke_app(session: AsyncSession) -> FastAPI:
    """The REAL agent-analysis router on a minimal app (mirrors the A7 seam test's helper)."""
    app = FastAPI(title="x1qr3-3-smoke", version="test")
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
            sha256_hash="1" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=2048,
        )
    )
    await session.commit()
    return file_id


@contextlib.asynccontextmanager
async def _real_client(session: AsyncSession, token: str) -> AsyncIterator[PhazeAgentClient]:
    """The REAL ``PhazeAgentClient``, transported onto the REAL router instead of a mock."""
    transport = ASGITransport(app=_smoke_app(session))
    inner = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"})
    client = PhazeAgentClient(base_url="http://test", token=token, _client=inner)
    try:
        yield client
    finally:
        await client.close()


async def test_persisted_analysis_writes_the_full_projection(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """After a real analysis PUT: every coarse window has ``energy``+``mood_scores``, every fine
    window carrying a key has ``camelot``, and exactly one ``set_profile`` row exists."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    async with _real_client(session, raw_token) as client:
        response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()
    windows = (
        (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.window_index))).scalars().all()
    )
    assert windows, "no windows persisted"
    coarse = [w for w in windows if w.tier == "coarse"]
    fine = [w for w in windows if w.tier == "fine"]
    assert coarse, "fixture has no coarse windows to assert over"
    assert fine, "fixture has no fine windows to assert over"

    for window in coarse:
        assert window.energy is not None
        assert 0.0 <= window.energy <= 1.0
        assert window.mood_scores is not None
        # camelot lives on FINE rows only -- a coarse row must never carry one.
        assert window.camelot is None
    for window in fine:
        if window.musical_key is not None:
            assert window.camelot is not None, f"fine window with musical_key {window.musical_key!r} has no camelot"
        # Reviewer finding (phaze-x1qr3.2): `_wheel_adjacent` bare-parses `camelot` with
        # `int(a[:-1])` and raises on anything not shaped like a table value. The writer must
        # never persist a raw `musical_key` string or any value outside the 24-entry table.
        if window.camelot is not None:
            assert window.camelot in _VALID_CAMELOT_CODES, f"camelot {window.camelot!r} is not a table value"

    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one()
    assert profile.projection_version == CURRENT_PROJECTION_VERSION
    # The real fixture's fine tier carries a real key throughout, so the file-level modal Camelot
    # position is knowable; likewise its coarse tier carries real features throughout.
    assert profile.camelot_modal is not None
    assert profile.mean_vector is not None
    assert profile.arc is not None


async def test_a_projection_failure_never_fails_the_analysis(
    monkeypatch: pytest.MonkeyPatch,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A broken per-window annotate step is caught, logged, and the analysis still completes with
    the profile left NULL -- the acceptance bar's exact wording."""

    def _boom(_rows: list[dict[str, object]]) -> None:
        msg = "synthetic projection failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(agent_analysis, "annotate_window_rows", _boom)

    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    with structlog.testing.capture_logs() as logs:
        async with _real_client(session, raw_token) as client:
            response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()

    # The analysis itself completed -- the whole point of the guard.
    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert analysis.analysis_completed_at is not None
    assert analysis.failed_at is None

    # Windows are still there (base fields) -- just without the projection columns.
    window_count = (await session.execute(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalar_one()
    assert window_count == len(payload.windows or [])
    windows = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all()
    assert all(w.energy is None and w.camelot is None and w.mood_scores is None for w in windows)

    # No profile was written -- left NULL/absent, never a garbage all-NULL row.
    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one_or_none()
    assert profile is None

    warnings = [entry for entry in logs if entry["event"] == "set_projection_window_annotate_failed"]
    assert len(warnings) == 1
    assert warnings[0]["file_id"] == str(file_id)
    assert warnings[0]["log_level"] == "warning"
