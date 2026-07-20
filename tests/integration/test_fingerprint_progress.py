"""Real-PG replacement for the toothless ``get_fingerprint_progress`` mock stub (Phase 84, D-15).

The former unit test (``tests/fingerprint/services/test_fingerprint.py``) stubbed ``session.execute``
with a ``side_effect`` list and asserted the very dict it fed in, so it stayed green through ANY rewrite
of the query -- including a wrong one. This seeds a real Postgres corpus and calls the real function,
pinning:

* **D-10** -- ``total`` is ``file_type IN MUSIC_VIDEO_TYPES`` AND ``~dedup_resolved_clause()``; a
  non-audio file and a marker-present music file are BOTH excluded, while a ``state='duplicate_resolved'``
  music file with NO marker is INCLUDED (proving the derivation keys on the marker, not ``FileRecord.state``).
* **D-11 / DERIV-05** -- ``completed`` and ``failed`` are FILE counts: a file with one engine ``success``
  + one engine ``failed`` counts toward ``completed`` (one success wins), NOT ``failed``; a file with two
  ``failed`` engine rows counts ``1`` toward ``failed`` (a FILE, not a ROW count).
* **D-17** -- ``completed ⊆ total`` and ``failed ⊆ total`` (shared denominator).

Real-PG harness idiom mirrors ``tests/integration/test_shadow_compare.py`` (DSN derivation +
connectivity-probe ``pytest.skip`` so a bare ``uv run pytest`` skips rather than errors when Postgres is
down; ``Base.metadata.create_all`` + a seeded ``legacy-application-server`` Agent for the
``files.agent_id`` RESTRICT FK; per-test rollback). Run with real PG via ``just test-bucket integration``
(ephemeral PG ``:5433``).

MUTATION EVIDENCE (recorded in 84-04-SUMMARY.md): reverting ``completed`` to
a scalar ``state == 'fingerprinted'`` read drops ``completed`` from 2 to 0 (RED); reverting ``failed`` to a
``fingerprint_results`` ROW count over ``status='failed'`` lifts ``failed`` from 1 to 2 (RED).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.base import Base
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.services.fingerprint import get_fingerprint_progress
from tests.db_guard import integration_dsns, require_test_database


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration


# Raw libpq broker DSN + SQLAlchemy async DSN, derived exactly as tests/integration/test_shadow_compare.py.
# DSN pair + destructive-DB guard, shared with every other integration module via `tests.db_guard`.
# This test only reads (per-test rollback), so it is not destructive; still refuse a non-`_test` DB so a
# bare run against a dev stack can never seed rows into it. `just test-db` points at `phaze_test`.
BROKER_DSN, SA_DSN = integration_dsns()
_TARGET_DB = require_test_database(SA_DSN, context="integration tests")

_LEGACY_AGENT_ID = "test-fileserver"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables present and the FK agent seeded.

    Probes broker connectivity first and ``pytest.skip``s when Postgres is down. ``create_all`` makes
    the harness independent of Alembic. The test runs in one rolled-back transaction.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # Seed the FK-parent IDEMPOTENTLY: a committed ``test-fileserver`` may already exist (the
        # ``committed_db`` fixture re-seeds one, and the session-scoped ``async_engine`` seeds one), so a
        # blind INSERT collides on ``pk_agents`` under the full-bucket ordering (92-05, DI-92-04-02).
        # Get-or-insert satisfies the FK either way and keeps this hermetic fixture order-independent.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _new_file(session: AsyncSession, *, file_type: str) -> uuid.UUID:
    """Seed a bare FileRecord of ``file_type`` at ``state`` with NO backing rows; return its id."""
    fid = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=fid,
            sha256_hash=uuid.uuid4().hex,
            original_path=f"/media/{fid}.{file_type}",
            original_filename=f"{fid}.{file_type}",
            current_path=f"/media/{fid}.{file_type}",
            file_type=file_type,
            file_size=1234,
        )
    )
    await session.flush()
    return fid


async def _add_fp(session: AsyncSession, file_id: uuid.UUID, engine: str, status: str) -> None:
    """Seed one per-engine fingerprint_results row for ``file_id``."""
    session.add(FingerprintResult(file_id=file_id, engine=engine, status=status))
    await session.flush()


async def test_get_fingerprint_progress_derives_from_output_tables(db_session: AsyncSession) -> None:
    """The 3-key contract, derived from file_type + dedup marker + done/failed_clause (D-09..D-17)."""
    # (1) music file with an engine success -> total + completed.
    f_success = await _new_file(db_session, file_type="mp3")
    await _add_fp(db_session, f_success, "audfprint", "success")

    # (2) video file, no fingerprint rows -> total only.
    await _new_file(db_session, file_type="mp4")

    # (3) non-audio file -> excluded from total entirely (D-10 denominator).
    f_txt = await _new_file(db_session, file_type="txt")
    await _add_fp(db_session, f_txt, "audfprint", "success")  # even a success cannot pull a non-MV file into total.

    # (4) dedup-resolved music file (marker present) -> excluded from total (D-10 marker exclusion).
    f_marked = await _new_file(db_session, file_type="mp3")
    db_session.add(DedupResolution(file_id=f_marked))
    await db_session.flush()

    # (5) a music file with NO dedup marker -> INCLUDED in total (D-10: only a marker excludes; post-MIG-04
    #     there is no scalar state that could ever exclude it -- membership derives from the marker alone).
    await _new_file(db_session, file_type="mp3")

    # (6) one engine success + one engine failure -> completed, NOT failed (DERIV-05: one success wins).
    f_mixed = await _new_file(db_session, file_type="mp3")
    await _add_fp(db_session, f_mixed, "audfprint", "success")
    await _add_fp(db_session, f_mixed, "panako", "failed")

    # (7) all engines failed (two failed rows) -> failed, counted ONCE (a FILE, not a ROW count).
    f_failed = await _new_file(db_session, file_type="mp3")
    await _add_fp(db_session, f_failed, "audfprint", "failed")
    await _add_fp(db_session, f_failed, "panako", "failed")

    progress = await get_fingerprint_progress(db_session)

    # total = {1 music-success, 2 video, 5 dup-resolved-no-marker, 6 mixed, 7 all-failed} = 5.
    # Excluded: 3 (non-audio, D-10), 4 (marker present, D-10).
    assert progress == {"total": 5, "completed": 2, "failed": 1}

    # D-17: completed and failed are strict subsets of total (progress bar can never exceed 100%).
    assert progress["completed"] <= progress["total"]
    assert progress["failed"] <= progress["total"]
