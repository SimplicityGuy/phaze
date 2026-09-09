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
from phaze.schemas.agent_analysis import AnalysisWritePayload
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.pipeline.files import get_files_page
from phaze.services.search_queries import search
from phaze.services.set_projection import CAMELOT_TABLE
from phaze.services.set_projection_backfill import run_backfill, select_files_needing_projection
from phaze.services.set_projection_writer import CURRENT_PROJECTION_VERSION
from phaze.services.set_similarity import find_similar_sets
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


async def _seed_file(session: AsyncSession, agent_id: str, *, stem: str | None = None) -> uuid.UUID:
    """Seed the FileRecord that ``AnalysisResult.file_id`` and every window row FK to.

    ``stem`` prefixes the (invented) filename with a distinctive word so the file is reachable
    through ``services.search_queries.search`` -- that branch's tsvector is built over the display
    filename, and a bare uuid stem is not a word ``plainto_tsquery`` can be relied on to match.
    """
    file_id = uuid.uuid4()
    filename = f"{stem} {file_id}.mp3" if stem else f"{file_id}.mp3"
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="1" * 64,
            original_path=f"/test/music/{filename}",
            original_filename=filename,
            current_path=f"/test/music/{filename}",
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


async def test_a_build_profile_failure_never_fails_the_analysis(
    monkeypatch: pytest.MonkeyPatch,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The SECOND, separate try/except this bead's split introduced (review finding 1): a failure
    in ``build_profile``/building the upsert statement -- as opposed to ``annotate_window_rows``,
    covered above -- is its own guarded step. Here the per-window fields DO get computed and
    persisted; only the profile aggregation fails."""

    def _boom(_windows: list[object]) -> object:
        msg = "synthetic build_profile failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(agent_analysis, "build_profile", _boom)

    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    with structlog.testing.capture_logs() as logs:
        async with _real_client(session, raw_token) as client:
            response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()

    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert analysis.analysis_completed_at is not None
    assert analysis.failed_at is None

    # The per-window projection DID compute (this is the OTHER try/except) -- only the aggregate
    # profile failed.
    coarse = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id, AnalysisWindow.tier == "coarse"))).scalars().all()
    assert coarse
    assert all(w.energy is not None for w in coarse)

    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one_or_none()
    assert profile is None

    warnings = [entry for entry in logs if entry["event"] == "set_profile_upsert_failed"]
    assert len(warnings) == 1
    assert warnings[0]["file_id"] == str(file_id)
    assert warnings[0]["log_level"] == "warning"


async def test_a_real_database_level_projection_failure_never_fails_the_analysis(
    monkeypatch: pytest.MonkeyPatch,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Review finding 1: the prior version of this guard only proved a Python-level exception
    raised BEFORE any SQL ran was survivable -- a proxy that cannot exhibit a DB-level failure
    poisoning the transaction. This test forces a REAL ``ForeignKeyViolation`` (an ``IntegrityError``
    from Postgres itself, not a monkeypatched Python raise) by building the `set_profile` upsert
    against a `file_id` with no `files` row at all, then asserts the SAME thing a vanished-file
    race would need: the analysis still commits (``execute_guarding_vanished_file``'s SAVEPOINT
    unwinds only the failed statement, per request_guards.py rule 5), the windows themselves ARE
    fully projected (only the `set_profile` insert failed), and no profile row exists.
    """
    real_builder = agent_analysis.build_set_profile_upsert_statement

    def _target_a_vanished_file(_file_id: uuid.UUID, projection: object) -> object:
        # Build the IDENTICAL statement shape, but against a file_id with NO `files` row --
        # a genuine FK violation on execution, indistinguishable at the SQL layer from the real
        # phaze-wn1l race (a file deleted between this transaction starting and this INSERT).
        return real_builder(uuid.uuid4(), projection)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_analysis, "build_set_profile_upsert_statement", _target_a_vanished_file)

    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    with structlog.testing.capture_logs() as logs:
        async with _real_client(session, raw_token) as client:
            response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()

    # The analysis itself completed -- the transaction was NOT poisoned by the failed INSERT.
    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert analysis.analysis_completed_at is not None
    assert analysis.failed_at is None

    # The windows themselves ARE fully projected -- only the set_profile insert failed.
    windows = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all()
    coarse = [w for w in windows if w.tier == "coarse"]
    assert coarse
    assert all(w.energy is not None for w in coarse)

    # No profile was written for the REAL file_id (it went to the fabricated one instead, which
    # itself never landed either -- the whole point of the guard).
    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one_or_none()
    assert profile is None

    holds = [entry for entry in logs if "vanished mid-write" in str(entry.get("event", ""))]
    assert len(holds) == 1
    assert holds[0]["file_id"] == str(file_id)
    assert holds[0]["log_level"] == "warning"


async def test_a_featureless_coarse_window_gets_a_fully_none_projection_not_a_manufactured_one(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Review finding 3: a coarse window with no `features` at all must read as a gap (``energy``
    and ``mood_scores`` both ``None``), never a manufactured ``energy=0.0`` / all-null
    `mood_scores` dict computed from nothing."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    result = real_analysis_result()
    for window in result["windows"]:
        if window["tier"] == "coarse":
            window["features"] = None
    payload = _build_analysis_write_payload(result)

    async with _real_client(session, raw_token) as client:
        response = await client.put_analysis(file_id, payload)

    assert response.file_id == file_id
    session.expire_all()
    coarse = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id, AnalysisWindow.tier == "coarse"))).scalars().all()
    assert coarse
    assert all(w.energy is None for w in coarse)
    assert all(w.mood_scores is None for w in coarse)

    # No coarse window contributed any real mood data -> the file-level mean_vector is the honest
    # gap (None), never an 11-NaN vector (the compounding _mean_vector truthiness bug this same
    # review finding traced through to set_projection.py).
    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one()
    assert profile.mean_vector is None


_CLEAR_PROBE_STEM = "setclearprobe"
"""An invented, distinctive filename word the ⌘K palette branch's tsvector can match on."""


async def _profile_row(session: AsyncSession, file_id: uuid.UUID) -> SetProfile | None:
    """``file_id``'s ``set_profile`` row, read with a fresh SELECT.

    Deliberately NOT ``session.get`` (the shape ``routers/record.py`` uses): under this suite's
    single-connection session an ORM instance loaded earlier in the same test is still in the
    identity map after the router's Core DELETE, and ``get`` would try to refresh it and raise
    ``ObjectDeletedError`` -- a test artifact of the shared session, not product behaviour. The
    record-page consumer is exercised through ``session.get`` on an expunged session below, where
    that hazard does not exist and the call is the real one.
    """
    return (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one_or_none()


async def test_a_windows_clear_removes_the_profile_from_every_surface_that_reads_it(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-qj926 case 1: ``PUT {"windows": []}`` must take the file's ``set_profile`` row with it.

    Asserted through the FOUR real consumers of that row rather than the row alone (ADR-0012 rule
    3), each with its OWN query shape: ``routers/record.py``'s ``session.get`` (the record page),
    ``services/pipeline/files.get_files_page``'s ``selectinload`` (the Files table),
    ``services/search_queries.search``'s outer join (the ⌘K palette) and
    ``services/set_similarity.find_similar_sets``'s candidate scan (Similar sets). The last is the
    discriminating one: it selects FROM ``set_profile``, so a stale row does not merely linger, it
    is actively RETURNED as a recommendation describing windows that no longer exist.

    Every assertion is made twice -- once before the clear, to prove the surface really did show
    the file, and once after. Without the baseline half an empty result proves nothing, because
    every one of these consumers renders a profile-less file silently.
    """
    agent, raw_token = seed_test_agent
    cleared = await _seed_file(session, agent.id, stem=_CLEAR_PROBE_STEM)
    peer = await _seed_file(session, agent.id, stem=_CLEAR_PROBE_STEM)
    payload = _build_analysis_write_payload(real_analysis_result())

    async with _real_client(session, raw_token) as client:
        assert (await client.put_analysis(cleared, payload)).file_id == cleared
        assert (await client.put_analysis(peer, payload)).file_id == peer

    session.expire_all()
    peer_profile = await _profile_row(session, peer)
    assert peer_profile is not None, "the peer file needs a profile of its own to rank candidates against"

    # --- baseline: all four surfaces DO show the file while its windows exist -------------------
    assert await _profile_row(session, cleared) is not None
    before_page = await get_files_page(session)
    assert {row.file.id for row in before_page.rows} >= {cleared, peer}, "both seeded files must be on the first Files page"
    assert next(row for row in before_page.rows if row.file.id == cleared).file.set_profile is not None
    before_results, _ = await search(session, _CLEAR_PROBE_STEM)
    assert next(r for r in before_results if r.id == str(cleared)).glyph is not None
    before_similar = await find_similar_sets(session, peer, peer_profile, None, None, None)
    assert cleared in {similar.file_id for similar in before_similar}

    # --- the clear -------------------------------------------------------------------------------
    async with _real_client(session, raw_token) as client:
        assert (await client.put_analysis(cleared, AnalysisWritePayload(windows=[]))).file_id == cleared

    # A fresh identity map, matching the per-request session every consumer below really runs on.
    session.expunge_all()

    window_count = (await session.execute(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == cleared))).scalar_one()
    assert window_count == 0, "windows=[] must still delete every window row"

    # 1. The record page: the exact `session.get` shape `routers/record.py` uses.
    assert await session.get(SetProfile, cleared) is None

    # 2. The Files table: `get_files_page`'s selectinload of `FileRecord.set_profile`.
    after_page = await get_files_page(session)
    cleared_row = next(row for row in after_page.rows if row.file.id == cleared)
    assert cleared_row.file.set_profile is None

    # 3. The ⌘K palette: the file still MATCHES the query (the join is outer, by design) but
    #    carries no glyph -- the palette's own "absent renders nothing" contract.
    after_results, _ = await search(session, _CLEAR_PROBE_STEM)
    cleared_result = next(r for r in after_results if r.id == str(cleared))
    assert cleared_result.glyph is None

    # 4. Similar sets: the cleared file is no longer a candidate at all.
    peer_profile_after = await _profile_row(session, peer)
    assert peer_profile_after is not None, "the clear must not touch the peer's profile"
    after_similar = await find_similar_sets(session, peer, peer_profile_after, None, None, None)
    assert cleared not in {similar.file_id for similar in after_similar}


async def test_a_projection_failure_leaves_a_row_the_backfill_actually_repairs(
    monkeypatch: pytest.MonkeyPatch,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-qj926 case 2: after a failed RE-projection over an already-projected file, the next
    ``phaze backfill set-projection`` run must repair the file.

    The failure path itself is already covered above; what this test adds is the PRIOR profile.
    Before this bead the old row survived at ``CURRENT_PROJECTION_VERSION`` over freshly-written
    windows whose projection columns were NULL, and
    ``select_files_needing_projection`` matches only a MISSING or VERSION-BEHIND row -- so the
    backfill named 0 files and exited 0, and the stale row was unreachable forever.

    Verified against the REAL selection predicate and the REAL ``run_backfill``, not against the
    row's absence: absence is this fix's mechanism, and repairability is the property the bead is
    actually about (ADR-0012 rule 3).
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = _build_analysis_write_payload(real_analysis_result())

    async with _real_client(session, raw_token) as client:
        assert (await client.put_analysis(file_id, payload)).file_id == file_id

    session.expire_all()
    first = await _profile_row(session, file_id)
    assert first is not None, "the first analysis must leave a profile for the re-analysis to make stale"
    assert first.projection_version == CURRENT_PROJECTION_VERSION
    assert first.camelot_modal is not None

    def _boom(_rows: list[dict[str, object]]) -> None:
        msg = "synthetic projection failure on re-analysis"
        raise RuntimeError(msg)

    monkeypatch.setattr(agent_analysis, "annotate_window_rows", _boom)

    async with _real_client(session, raw_token) as client:
        assert (await client.put_analysis(file_id, payload)).file_id == file_id

    session.expunge_all()

    # The windows were rewritten and carry no projection -- the state that made the old row a lie.
    windows = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id))).scalars().all()
    assert windows, "the re-analysis must still have persisted its windows"
    assert all(w.energy is None and w.camelot is None and w.mood_scores is None for w in windows)

    # THE BEAD: the file is selectable by the real predicate, and the real backfill repairs it.
    assert await _profile_row(session, file_id) is None
    assert file_id in set(await select_files_needing_projection(session))

    monkeypatch.undo()
    report = await run_backfill(session)
    assert report.files_projected >= 1
    assert report.files_failed == 0

    session.expunge_all()
    repaired = await _profile_row(session, file_id)
    assert repaired is not None, "the backfill must rewrite the profile it was made able to see"
    assert repaired.projection_version == CURRENT_PROJECTION_VERSION
    assert repaired.camelot_modal == first.camelot_modal
    coarse = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id, AnalysisWindow.tier == "coarse"))).scalars().all()
    assert coarse
    assert all(w.energy is not None for w in coarse)

    # Second run is a no-op for this file -- the repair restored the resumability contract too.
    assert file_id not in set(await select_files_needing_projection(session))
