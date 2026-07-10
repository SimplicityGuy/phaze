"""Load-bearing divergence guard for the Phase-84 dedup marker cutover (D-14, SC#1).

On a *consistent* corpus (``marker ≡ state``) NO test can tell "reads the marker" from "reads
``FileRecord.state``" — both return identical rows, so a green guard proves nothing. This file seeds a
deliberately **inconsistent** corpus and asserts the marker wins at every dedup reader:

* **File A** — a ``dedup_resolution`` marker present + ``state='analyzed'`` → must be **EXCLUDED**
  (the marker is authority; the stale ``analyzed`` state must not resurface it).
* **File B** — ``state='duplicate_resolved'`` + **no** marker → must be **INCLUDED** (a backfilled /
  pre-marker resolved file with no marker reappears for re-review; state is NOT authority).

The five dedup readers are covered: ``find_duplicate_groups``,
``find_duplicate_groups_with_metadata``, ``count_duplicate_groups``, ``get_duplicate_stats``, and
``resolve_group``'s selection. Every assertion is designed so that reverting THAT reader's
``~dedup_resolved_clause()`` back to ``FileRecord.state != FileState.DUPLICATE_RESOLVED`` inverts it —
see the ``MUTATION`` comment on each test.

Corpus (two hash groups):

* ``H1`` = {A(marker,analyzed), B(no-marker,duplicate_resolved), C(no-marker,analyzed)} — drives the
  *membership* divergence (A vs B in/out of the group). Marker code → group {B, C}; state code →
  group {A, C}.
* ``H2`` = {D(no-marker,duplicate_resolved), E(no-marker,analyzed)} — drives the *count/stats*
  divergence. Marker code → H2 is a 2-member group; state code → D is excluded so H2 collapses to a
  single non-group.

Real-PG ``db_session`` fixture is copied from ``tests/integration/test_shadow_compare.py:84-113`` (NOT
the SAQ ``PostgresQueue`` fixture from ``test_pg_dedup.py`` — this phase reads no ``saq_jobs`` and the
SAQ stub is a per-bucket isolation hazard). Run with real PG via ``just test-bucket integration`` on
port 5433 (export ``TEST_DATABASE_URL``).
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
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord, FileState
from phaze.services.dedup import (
    count_duplicate_groups,
    find_duplicate_groups,
    find_duplicate_groups_with_metadata,
    get_duplicate_stats,
    resolve_group,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

# DSN derivation + destructive-DB guard, identical to test_shadow_compare.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run dedup divergence integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "legacy-application-server"

HASH_1 = "1" * 64
HASH_2 = "2" * 64


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent (copied from test_shadow_compare)."""
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
        session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, sha256: str, state: str) -> FileRecord:
    """Seed a bare FileRecord at ``state`` on ``sha256``; return the ORM object (id is set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=sha256,
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


async def _seed_inconsistent_corpus(session: AsyncSession) -> dict[str, FileRecord]:
    """Seed the deliberately inconsistent (marker ≢ state) corpus. Returns {A,B,C,D,E}."""
    # H1 — membership divergence.
    a = await _file(session, sha256=HASH_1, state=FileState.ANALYZED.value)  # marker + analyzed
    b = await _file(session, sha256=HASH_1, state=FileState.DUPLICATE_RESOLVED.value)  # no marker + resolved
    c = await _file(session, sha256=HASH_1, state=FileState.ANALYZED.value)  # normal partner
    # H2 — count/stats divergence.
    d = await _file(session, sha256=HASH_2, state=FileState.DUPLICATE_RESOLVED.value)  # no marker + resolved
    e = await _file(session, sha256=HASH_2, state=FileState.ANALYZED.value)  # normal partner
    # Only File A carries a marker.
    session.add(DedupResolution(file_id=a.id, canonical_file_id=c.id))
    await session.flush()
    return {"A": a, "B": b, "C": c, "D": d, "E": e}


def _members(groups: list[dict[str, object]], sha256: str) -> set[str]:
    grp = next((g for g in groups if g["sha256_hash"] == sha256), None)
    if grp is None:
        return set()
    return {f["id"] for f in grp["files"]}  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------------------
# Reader 1: find_duplicate_groups — H1 members are {B, C}; File A (marker) is excluded, File B is in.
# MUTATION (dedup.py:81/93 → `FileRecord.state != FileState.DUPLICATE_RESOLVED`): A joins the group and
# B drops out — both asserts below invert.
# --------------------------------------------------------------------------------------------------
async def test_find_duplicate_groups_marker_is_authority(db_session: AsyncSession) -> None:
    corpus = await _seed_inconsistent_corpus(db_session)
    members = _members(await find_duplicate_groups(db_session), HASH_1)

    assert str(corpus["B"].id) in members  # duplicate_resolved + no marker → INCLUDED
    assert str(corpus["C"].id) in members
    assert str(corpus["A"].id) not in members  # marker present → EXCLUDED (despite state='analyzed')


# --------------------------------------------------------------------------------------------------
# Reader 2: find_duplicate_groups_with_metadata — same membership divergence.
# MUTATION (dedup.py:131/144 revert): inverts.
# --------------------------------------------------------------------------------------------------
async def test_find_duplicate_groups_with_metadata_marker_is_authority(db_session: AsyncSession) -> None:
    corpus = await _seed_inconsistent_corpus(db_session)
    members = _members(await find_duplicate_groups_with_metadata(db_session), HASH_1)

    assert str(corpus["B"].id) in members
    assert str(corpus["C"].id) in members
    assert str(corpus["A"].id) not in members


# --------------------------------------------------------------------------------------------------
# Reader 3: count_duplicate_groups — marker code sees BOTH H1 and H2 as groups (== 2). State code
# collapses H2 (D excluded by state, only E remains) → 1.
# MUTATION (dedup.py:191 revert): count drops to 1 and this assertion fails.
# --------------------------------------------------------------------------------------------------
async def test_count_duplicate_groups_marker_is_authority(db_session: AsyncSession) -> None:
    await _seed_inconsistent_corpus(db_session)
    assert await count_duplicate_groups(db_session) == 2


# --------------------------------------------------------------------------------------------------
# Reader 4: get_duplicate_stats — marker code: groups=2, total_files=4 (B,C,D,E). State code would give
# groups=1, total_files=2 (A,C — H2 collapses; D excluded).
# MUTATION (dedup.py:191/211/223 revert): groups→1, total_files→2, both asserts fail.
# --------------------------------------------------------------------------------------------------
async def test_get_duplicate_stats_marker_is_authority(db_session: AsyncSession) -> None:
    await _seed_inconsistent_corpus(db_session)
    stats = await get_duplicate_stats(db_session)

    assert stats["groups"] == 2
    assert stats["total_files"] == 4  # B, C, D, E — the four no-marker files; A (marker) excluded


# --------------------------------------------------------------------------------------------------
# Reader 5: resolve_group's selection — resolving H1 with canonical=C selects the non-canonical,
# no-marker file B (marker code). State code would instead select A (state='analyzed', not resolved)
# and skip B (state='duplicate_resolved').
# MUTATION (dedup.py:263 selection revert): the returned id set flips from {B} to {A}.
# --------------------------------------------------------------------------------------------------
async def test_resolve_group_selection_marker_is_authority(db_session: AsyncSession) -> None:
    corpus = await _seed_inconsistent_corpus(db_session)
    _count, file_states = await resolve_group(db_session, HASH_1, corpus["C"].id)
    selected = {fs["id"] for fs in file_states}

    assert str(corpus["B"].id) in selected  # no-marker file is a live non-canonical member → selected
    assert str(corpus["A"].id) not in selected  # already marker-resolved → NOT re-selected
