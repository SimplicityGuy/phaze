"""Behavioral derived-wins divergence guard for the three enrich pending-set cutovers (READ-01, D-14).

On a *consistent* corpus (``output rows ≡ FileRecord.state``) no test can tell "reads the derived
layer" from "reads ``FileRecord.state``" -- both return identical rows, so a green guard proves nothing.
This file seeds a deliberately **inconsistent** corpus (an output row present while ``state`` disagrees,
and vice-versa) and asserts the DERIVED reader wins at every one of the three pending helpers:

* **File A** -- a ``metadata`` row present (stage done) but ``state='discovered'`` -> **EXCLUDED** from the
  metadata pending set (the derived ``done`` wins over the stale ``discovered`` state).
* **File B** -- **no** ``fingerprint`` row but ``state='analyzed'`` -> **INCLUDED** in the fingerprint set
  (a state advanced past ``METADATA_EXTRACTED`` must NOT hide an un-fingerprinted file -- the deadlock).
* **File C** -- **no** ``analysis`` row but ``state='fingerprinted'`` -> **INCLUDED** in the analyze set
  (a state advanced past ``DISCOVERED`` must NOT hide an un-analyzed file).
* **File D** -- a ``fingerprint`` row with ``status='failed'`` only (state whatever) -> **INCLUDED** in the
  fingerprint set: the derived ``eligible_clause(FINGERPRINT)`` (``ELIGIBLE_AFTER_FAILURE True``) subsumes
  the old failed-retry UNION, so the collapse loses no coverage.

Every assertion carries a ``MUTATION:`` comment naming the exact ``FileRecord.state`` revert that inverts
it -- so reverting a helper to its pre-cutover state filter turns the matching cell RED (the guard has
teeth, ``feedback_mutation_test_guard_tests``).

Real-PG ``db_session`` fixture + ``_file`` seed helper + the destructive ``*_test`` DB guard are copied
from ``tests/integration/test_dedup_divergence.py``. Run with real PG via ``just test-bucket integration``
on port 5433 (export ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.base import Base
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.services.pipeline import (
    get_discovered_files_with_duration,
    get_fingerprint_pending_files,
    get_metadata_pending_files,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

# DSN derivation + destructive-DB guard, identical to test_dedup_divergence.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run pending-set divergence integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "legacy-application-server"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent (copied from test_dedup_divergence)."""
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
        # Idempotent FK-agent seed: the shared ``*_test`` DB may already carry a committed
        # ``legacy-application-server`` agent (a sibling bucket's committing test) -- re-adding it would
        # raise UniqueViolationError at flush (82-01 SUMMARY environmental note). Only add if absent.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, state: str) -> FileRecord:
    """Seed a bare music FileRecord at ``state``; return the ORM object (id is set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{fid}.mp3",
        original_filename=f"{fid}.mp3",
        current_path=f"/media/{fid}.mp3",
        file_type="mp3",
        file_size=1234,
        state=state,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _metadata_ids(session: AsyncSession) -> set[uuid.UUID]:
    return {f.id for f in await get_metadata_pending_files(session)}


async def _fingerprint_ids(session: AsyncSession) -> set[uuid.UUID]:
    return {f.id for f in await get_fingerprint_pending_files(session)}


async def _analyze_ids(session: AsyncSession) -> set[uuid.UUID]:
    return {r.id for r, _duration in await get_discovered_files_with_duration(session)}


# --------------------------------------------------------------------------------------------------
# File A -- metadata row present (done) but state='discovered' -> EXCLUDED from the metadata set.
# MUTATION (get_metadata_pending_files -> select(FileRecord).where(file_type.in_(MUSIC_VIDEO_TYPES))
# i.e. the pre-cutover all-music state-agnostic set): File A rejoins the set -- this assertion inverts.
# --------------------------------------------------------------------------------------------------
async def test_metadata_done_with_stale_discovered_state_is_excluded(db_session: AsyncSession) -> None:
    file_a = await _file(db_session, state=FileState.DISCOVERED.value)
    db_session.add(FileMetadata(file_id=file_a.id, failed_at=None))  # stage done despite state='discovered'
    await db_session.flush()

    assert file_a.id not in await _metadata_ids(db_session)


# --------------------------------------------------------------------------------------------------
# File B -- no fingerprint row but state='analyzed' -> INCLUDED in the fingerprint set.
# MUTATION (get_fingerprint_pending_files -> get_files_by_state(FileState.METADATA_EXTRACTED)-based
# UNION): a state='analyzed' file is excluded -- this assertion inverts (the cross-stage deadlock).
# --------------------------------------------------------------------------------------------------
async def test_unfingerprinted_with_advanced_state_is_included(db_session: AsyncSession) -> None:
    file_b = await _file(db_session, state=FileState.ANALYZED.value)  # advanced past METADATA_EXTRACTED, no fp row
    assert file_b.id in await _fingerprint_ids(db_session)


# --------------------------------------------------------------------------------------------------
# File C -- no analysis row but state='fingerprinted' -> INCLUDED in the analyze set.
# MUTATION (get_discovered_files_with_duration -> .where(FileRecord.state == FileState.DISCOVERED)):
# a state='fingerprinted' file is excluded -- this assertion inverts.
# --------------------------------------------------------------------------------------------------
async def test_unanalyzed_with_advanced_state_is_included(db_session: AsyncSession) -> None:
    file_c = await _file(db_session, state=FileState.FINGERPRINTED.value)  # advanced past DISCOVERED, no analysis row
    assert file_c.id in await _analyze_ids(db_session)


# --------------------------------------------------------------------------------------------------
# File D -- a failed-only fingerprint (ELIG-04 auto-retry) -> INCLUDED in the fingerprint set. Proves the
# derived eligible_clause(FINGERPRINT) subsumes the old failed-retry UNION with no lost coverage.
# MUTATION (revert to the state-based UNION keyed on METADATA_EXTRACTED, state here is 'fingerprinted'):
# File D is excluded -- this assertion inverts.
# --------------------------------------------------------------------------------------------------
async def test_failed_only_fingerprint_is_included(db_session: AsyncSession) -> None:
    file_d = await _file(db_session, state=FileState.FINGERPRINTED.value)
    db_session.add(FingerprintResult(file_id=file_d.id, engine="audfprint", status="failed"))
    await db_session.flush()

    assert file_d.id in await _fingerprint_ids(db_session)


# --------------------------------------------------------------------------------------------------
# Positive control: a genuinely-done fingerprint (success) is EXCLUDED even with state='metadata_extracted'
# (the state a pre-cutover UNION would INCLUDE). MUTATION (state-based UNION): File E rejoins -- inverts.
# --------------------------------------------------------------------------------------------------
async def test_success_fingerprint_with_ready_state_is_excluded(db_session: AsyncSession) -> None:
    file_e = await _file(db_session, state=FileState.METADATA_EXTRACTED.value)
    db_session.add(FingerprintResult(file_id=file_e.id, engine="audfprint", status="success"))
    await db_session.flush()

    assert file_e.id not in await _fingerprint_ids(db_session)
