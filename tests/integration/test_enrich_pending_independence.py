"""SC#1 all-orderings independence + the A1 cloud double-dispatch regression (READ-01).

The milestone thesis made executable: the three enrich pending sets (metadata / fingerprint /
analyze) must each surface every not-done, not-in-flight, not-dedup-resolved music/video file
**independent of the other two** and **independent of ``FileRecord.state``**. A single file completes
all three stages in ANY order; after each partial completion the completed stage's pending set
EXCLUDES the file while the not-yet-done stages STILL INCLUDE it. This dissolves the cross-stage
deadlock that state-gating created (once ``state`` advanced past ``METADATA_EXTRACTED`` / ``DISCOVERED``
a file could never re-enter the sibling pending set).

Load-bearing regressions in this file:

* **Deadlock-detection cells** -- a metadata-done file whose ``state`` was advanced (dual-write
  reality) is STILL in the analyze set, and an analyzed-but-never-fingerprinted file is STILL in the
  fingerprint set. Both are RED against the pre-cutover state-gated code and GREEN post-cutover -- they
  are the cells that DRIVE the cutover.
* **A1 cloud-exclusion** -- a file dispatched to a compute/Kueue backend (its ``cloud_job`` row in an
  ACTIVE, non-terminal status) must NEVER be a local analyze candidate (double-dispatch / cost DoS,
  T-82-A1). A cloud analysis that already terminally FAILED (``cloud_job.status='failed'`` with no
  ``AnalysisResult``) is NOT excluded -- it is a legitimate local-retry candidate.
* **Pitfall 1 file-type scope** -- the old state-gated analyze query was file-type-agnostic; the
  derived query is scoped to ``MUSIC_VIDEO_TYPES``, so a non-music DISCOVERED file is now absent.
* **dedup-exclusion** -- a dedup-resolved file is absent from all three sets.

Real-PG ``db_session`` fixture + ``_file`` seed helper + the destructive ``*_test`` DB guard are copied
from ``tests/integration/test_dedup_divergence.py``. Output rows are written to advance stages (never a
bare ``state`` mutation); ``state`` is dual-written to whatever the real writer would stamp so every
assertion proves the derived reader IGNORES it. Run with real PG via ``just test-bucket integration``
on port 5433 (export ``TEST_DATABASE_URL``).

NOTE (cloud status vocabulary, execution finding): the plan's A1 cells named cloud statuses
``'pushing'``/``'pushed'``; those are ``FileState`` members, NOT ``cloud_job`` statuses (the
``ck_cloud_job_status_enum`` CHECK forbids them). The real ``cloud_job`` lifecycle for a compute burst
is ``awaiting -> submitted (state PUSHING) -> succeeded (state PUSHED)``; the Kueue burst adds
``uploading/uploaded/running``. This file seeds the REAL active statuses (every non-``failed`` member)
so the CHECK constraint accepts the rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
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
        f"Refusing to run enrich-pending integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "legacy-application-server"

_STAGES = ("metadata", "fingerprint", "analyze")

# The dual-write ``state`` the real writer would stamp for each completed stage -- written alongside the
# output row so every assertion proves the derived reader IGNORES ``state`` (independence from state).
_STAGE_STATE: dict[str, FileState] = {
    "metadata": FileState.METADATA_EXTRACTED,
    "fingerprint": FileState.FINGERPRINTED,
    "analyze": FileState.ANALYZED,
}

# Every ACTIVE (non-terminal-failed) cloud_job status: a file carrying one of these is being handled by
# the cloud path and must NEVER be a local analyze candidate (A1 double-dispatch guard).
_ACTIVE_CLOUD_STATUSES = [
    CloudJobStatus.AWAITING.value,
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.SUBMITTED.value,  # FileState PUSHING
    CloudJobStatus.RUNNING.value,
    CloudJobStatus.SUCCEEDED.value,  # FileState PUSHED (compute: analysis still running on the agent)
]


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
        session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, file_type: str = "mp3", state: str = FileState.DISCOVERED.value) -> FileRecord:
    """Seed a bare FileRecord (``file_type`` music by default); return the ORM object (id is set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{fid}.{file_type}",
        original_filename=f"{fid}.{file_type}",
        current_path=f"/media/{fid}.{file_type}",
        file_type=file_type,
        file_size=1234,
        state=state,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _complete(session: AsyncSession, stage: str, file: FileRecord) -> None:
    """Write the OUTPUT row that marks ``stage`` done for ``file`` + dual-write the real writer's ``state``."""
    if stage == "metadata":
        session.add(FileMetadata(file_id=file.id, failed_at=None))  # D-03: row present + failed_at NULL => done
    elif stage == "fingerprint":
        session.add(FingerprintResult(file_id=file.id, engine="audfprint", status="success"))  # DERIV-05
    elif stage == "analyze":
        session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC)))  # DERIV-03
    else:  # pragma: no cover - guarded by _STAGES
        raise AssertionError(stage)
    file.state = _STAGE_STATE[stage].value  # dual-write reality: the derived reader must ignore this
    await session.flush()


async def _pending_ids(session: AsyncSession) -> dict[str, set[uuid.UUID]]:
    """Return the id set of each of the three enrich pending helpers, keyed by stage name."""
    return {
        "metadata": {f.id for f in await get_metadata_pending_files(session)},
        "fingerprint": {f.id for f in await get_fingerprint_pending_files(session)},
        "analyze": {r.id for r, _duration in await get_discovered_files_with_duration(session)},
    }


# --------------------------------------------------------------------------------------------------
# SC#1: all six orderings -- each stage's pending set is independent of the other two AND of state.
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("order", list(permutations(_STAGES)))
async def test_enrich_pending_sets_are_independent(db_session: AsyncSession, order: tuple[str, ...]) -> None:
    file = await _file(db_session)

    initial = await _pending_ids(db_session)
    for stage in _STAGES:
        assert file.id in initial[stage], f"a fresh music file must start in the {stage} pending set"

    remaining = set(_STAGES)
    for stage in order:
        await _complete(db_session, stage, file)
        remaining.discard(stage)
        sets = await _pending_ids(db_session)
        assert file.id not in sets[stage], f"completing {stage} must remove the file from the {stage} set"
        for other in remaining:
            assert file.id in sets[other], f"completing {stage} must NOT remove the file from the not-yet-done {other} set"

    done = await _pending_ids(db_session)
    for stage in _STAGES:
        assert file.id not in done[stage], f"after all three stages the file must be absent from the {stage} set"


# --------------------------------------------------------------------------------------------------
# Deadlock-detection: metadata done + state advanced => STILL in the analyze set (RED pre-cutover: the
# state-gated analyze query keyed on state == DISCOVERED never re-surfaces an advanced file).
# --------------------------------------------------------------------------------------------------
async def test_metadata_done_state_advanced_still_in_analyze_set(db_session: AsyncSession) -> None:
    file = await _file(db_session)
    db_session.add(FileMetadata(file_id=file.id, failed_at=None))
    file.state = FileState.METADATA_EXTRACTED.value  # dual-write reality
    await db_session.flush()

    sets = await _pending_ids(db_session)
    assert file.id not in sets["metadata"]  # metadata IS done -> excluded from its own set
    assert file.id in sets["analyze"]  # RED pre-cutover: analyze read state == DISCOVERED
    assert file.id in sets["fingerprint"]  # no fingerprint row yet -> still eligible


# --------------------------------------------------------------------------------------------------
# Deadlock-detection: analyzed but never fingerprinted => STILL in the fingerprint set (RED pre-cutover:
# the state-gated fingerprint query keyed on state == METADATA_EXTRACTED skips an analyzed file).
# --------------------------------------------------------------------------------------------------
async def test_analyzed_but_unfingerprinted_still_in_fingerprint_set(db_session: AsyncSession) -> None:
    file = await _file(db_session)
    db_session.add(FileMetadata(file_id=file.id, failed_at=None))
    db_session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC)))
    file.state = FileState.ANALYZED.value  # dual-write reality: state advanced past METADATA_EXTRACTED
    await db_session.flush()

    sets = await _pending_ids(db_session)
    assert file.id in sets["fingerprint"]  # RED pre-cutover: fingerprint read state == METADATA_EXTRACTED
    assert file.id not in sets["metadata"]  # metadata done -> excluded
    assert file.id not in sets["analyze"]  # analyze done -> excluded


# --------------------------------------------------------------------------------------------------
# A1 (T-82-A1): a cloud-dispatched file (active cloud_job status) is ABSENT from the analyze set.
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", _ACTIVE_CLOUD_STATUSES)
async def test_cloud_dispatched_file_absent_from_analyze_set(db_session: AsyncSession, status: str) -> None:
    file = await _file(db_session, state=FileState.PUSHING.value)  # dual-write reality (cloud in-flight state)
    db_session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=status))
    await db_session.flush()

    sets = await _pending_ids(db_session)
    assert file.id not in sets["analyze"], f"a cloud_job(status={status!r}) file must never be a local analyze candidate"


# --------------------------------------------------------------------------------------------------
# A1 negative: a TERMINALLY-FAILED cloud burst (no AnalysisResult) IS a legitimate local-retry candidate.
# Proves the exclusion is scoped to ACTIVE cloud statuses, not "any cloud_job row exists".
# --------------------------------------------------------------------------------------------------
async def test_cloud_failed_file_is_local_analyze_candidate(db_session: AsyncSession) -> None:
    file = await _file(db_session)
    db_session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.FAILED.value))
    await db_session.flush()

    sets = await _pending_ids(db_session)
    assert file.id in sets["analyze"], "a terminally-failed cloud burst with no AnalysisResult must be re-analyzable locally"


# --------------------------------------------------------------------------------------------------
# Pitfall 1: the derived analyze set is file-type-scoped -- a non-music DISCOVERED file is now absent
# (RED pre-cutover: the state-gated query was file-type-agnostic).
# --------------------------------------------------------------------------------------------------
async def test_non_music_file_absent_from_analyze_set(db_session: AsyncSession) -> None:
    music = await _file(db_session, file_type="mp3")
    other = await _file(db_session, file_type="txt")

    sets = await _pending_ids(db_session)
    assert music.id in sets["analyze"]
    assert other.id not in sets["analyze"]  # RED pre-cutover: DISCOVERED + no file_type filter -> included


# --------------------------------------------------------------------------------------------------
# dedup-exclusion: a dedup-resolved file is absent from ALL THREE enrich pending sets.
# --------------------------------------------------------------------------------------------------
async def test_dedup_resolved_file_absent_from_all_three_sets(db_session: AsyncSession) -> None:
    resolved = await _file(db_session)
    canonical = await _file(db_session)
    db_session.add(DedupResolution(file_id=resolved.id, canonical_file_id=canonical.id))
    await db_session.flush()

    sets = await _pending_ids(db_session)
    for stage in _STAGES:
        assert resolved.id not in sets[stage], f"a dedup-resolved file must be absent from the {stage} set"
        assert canonical.id in sets[stage], f"the (unresolved) canonical file must stay in the {stage} set"
