"""Four-bucket-sums-to-total invariant + a visible failed count per enrich stage (READ-02).

Phase 82 extends ``get_stage_progress``'s three enrich nodes (metadata / fingerprint / analyze) from
the old ``{done, total}`` shape to the four-bucket ``{not_started, in_flight, done, failed, total}``,
derived through ONE ``GROUP BY stage_status_case(stage)`` per stage (scoped to music/video files).
Because every music/video file falls into exactly one of the four ``stage_status_case`` buckets, the
four counts SUM to ``total`` (== ``music_video_total``) by construction on a healthy query.

Load-bearing cells:

* **sum-to-total invariant** -- for each enrich stage, ``not_started + in_flight + done + failed ==
  total``. Asserted ONLY on a healthy corpus: ``_safe_bucket_counts`` degrades fail-safe to all-zero
  on a query error (never 500s the 5s poll), so on a degrade the sum would intentionally be 0 while
  ``total`` is nonzero -- the invariant is a healthy-path property, NEVER a runtime assertion in the
  poll path (Pitfall 3).
* **visible failed count** -- a stage with >= 1 genuinely-failed row reports ``failed >= 1``. Before
  this cutover ``get_stage_progress`` reported only ``done`` for the enrich nodes, so an operator could
  not see how many files a stage had given up on. This makes the failure bucket visible.

Real-PG ``db_session`` fixture + ``_file`` seed helper + the destructive ``*_test`` DB guard are copied
from ``tests/integration/test_enrich_pending_independence.py``. Output rows are written to place each
file in a known per-stage bucket (never a bare ``state`` mutation -- the four buckets derive from the
output tables + the scheduling ledger, not ``FileRecord.state``). Run with real PG via
``just test-bucket integration`` on port 5433 (export ``TEST_DATABASE_URL``).

NOTE: ``phaze.services.pipeline.get_stage_progress`` already exists, but the four-bucket keys land in
Task 2 -- this file is RED at RUN time (the ``not_started`` / ``in_flight`` / ``failed`` keys are absent
and the sum invariant fails) until then, which is the intended TDD RED state.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.enums.stage import Status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.pipeline import get_stage_progress
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

# DSN derivation + destructive-DB guard, identical to test_enrich_pending_independence.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run stage-progress-bucket integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "legacy-application-server"

_ENRICH_STAGES = ("metadata", "fingerprint", "analyze")
_FOUR_BUCKETS = (Status.NOT_STARTED.value, Status.IN_FLIGHT.value, Status.DONE.value, Status.FAILED.value)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent (copied from the pending harness)."""
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
        # Idempotent FK-agent seed: the shared ``*_test`` DB may already carry a committed agent row
        # (a sibling bucket's committing test) -- re-adding would raise UniqueViolationError at flush.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, file_type: str = "mp3") -> FileRecord:
    """Seed a bare music/video FileRecord and return the ORM object (id is set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{fid}.{file_type}",
        original_filename=f"{fid}.{file_type}",
        current_path=f"/media/{fid}.{file_type}",
        file_type=file_type,
        file_size=1234,
        state=FileState.DISCOVERED.value,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _ledger(session: AsyncSession, stage: str, file: FileRecord) -> None:
    """Seed a scheduling_ledger row on the deterministic ``<function>:<file_id>`` key (in_flight bucket)."""
    func_name = STAGE_TO_FUNCTION[stage]
    session.add(
        SchedulingLedger(
            key=f"{func_name}:{file.id}",
            function=func_name,
            routing="agent",
            payload={"file_id": str(file.id)},
        )
    )
    await session.flush()


async def _seed_mixed_corpus(session: AsyncSession) -> dict[str, int]:
    """Seed a healthy mixed music/video corpus that places files in known per-stage buckets.

    Each file lands in exactly ONE bucket per stage via ``stage_status_case`` precedence
    (in_flight > done > failed > not_started). Returns the expected music/video ``total`` so the
    caller can assert the four-bucket sum. A single non-music file is added to prove it is EXCLUDED
    from every enrich total (music/video scope, Pitfall 1).
    """
    # 3 bare music files -> not_started for all three enrich stages.
    for _ in range(3):
        await _file(session)

    # metadata: 2 done (row + failed_at NULL), 1 failed-only (failed_at set), 1 in_flight (ledger).
    for _ in range(2):
        f = await _file(session)
        session.add(FileMetadata(file_id=f.id, failed_at=None))
    f = await _file(session)
    session.add(FileMetadata(file_id=f.id, failed_at=datetime.now(UTC)))
    f = await _file(session)
    await _ledger(session, "metadata", f)

    # fingerprint: 1 done (a success engine), 1 failed-only (a failed engine, no success).
    f = await _file(session)
    session.add(FingerprintResult(file_id=f.id, engine="audfprint", status="success"))
    f = await _file(session)
    session.add(FingerprintResult(file_id=f.id, engine="audfprint", status="failed"))

    # analyze: 1 done (completed_at set), 1 failed (failed_at set), 1 in_flight (ledger).
    f = await _file(session)
    session.add(AnalysisResult(file_id=f.id, analysis_completed_at=datetime.now(UTC)))
    f = await _file(session)
    session.add(AnalysisResult(file_id=f.id, failed_at=datetime.now(UTC)))
    f = await _file(session)
    await _ledger(session, "analyze", f)

    await session.flush()
    music_video_total = 3 + 4 + 2 + 3  # every seeded music file above

    # One non-music file -- must NOT count toward any enrich total (music/video scope).
    await _file(session, file_type="txt")

    return {"music_video_total": music_video_total}


async def test_enrich_nodes_are_four_bucket_summing_to_total(db_session: AsyncSession) -> None:
    """Each enrich node carries {not_started,in_flight,done,failed,total} and the four buckets sum to total."""
    expected = await _seed_mixed_corpus(db_session)
    progress = await get_stage_progress(db_session)

    for stage in _ENRICH_STAGES:
        node = progress[stage]
        for key in (*_FOUR_BUCKETS, "total"):
            assert key in node, f"{stage} node is missing the '{key}' key"
        four_sum = sum(int(node[bucket] or 0) for bucket in _FOUR_BUCKETS)
        total = int(node["total"] or 0)
        # Healthy-query invariant ONLY (Pitfall 3): _safe_bucket_counts degrades fail-safe to all-zero,
        # so this sum is asserted on a consistent corpus, never as a runtime poll-path assertion.
        assert four_sum == total, f"{stage}: buckets {four_sum} must sum to total {total}"
        assert total == expected["music_video_total"], f"{stage}: total must be the music/video count (non-music excluded)"


async def test_seeded_failed_rows_are_visible_per_stage(db_session: AsyncSession) -> None:
    """A stage with >= 1 genuinely-failed row reports a visible ``failed`` count (READ-02)."""
    await _seed_mixed_corpus(db_session)
    progress = await get_stage_progress(db_session)

    # The corpus seeds exactly one failed row for each enrich stage.
    assert int(progress["metadata"]["failed"] or 0) >= 1, "a metadata failure-only row must surface in failed"
    assert int(progress["fingerprint"]["failed"] or 0) >= 1, "a failed-only fingerprint must surface in failed"
    assert int(progress["analyze"]["failed"] or 0) >= 1, "a failed analyze row must surface in failed"


async def test_in_flight_bucket_reflects_scheduling_ledger(db_session: AsyncSession) -> None:
    """A file with only a scheduling-ledger row lands in the in_flight bucket (precedence: ledger wins)."""
    await _seed_mixed_corpus(db_session)
    progress = await get_stage_progress(db_session)

    # metadata + analyze each seeded exactly one ledger-only (in_flight) file above.
    assert int(progress["metadata"]["in_flight"] or 0) >= 1
    assert int(progress["analyze"]["in_flight"] or 0) >= 1


async def test_downstream_nodes_keep_done_total_shape(db_session: AsyncSession) -> None:
    """Non-enrich nodes are UNTOUCHED -- they keep the {done, total} shape (no four-bucket keys)."""
    await _seed_mixed_corpus(db_session)
    progress = await get_stage_progress(db_session)

    for node_name in ("discovery", "scan_search", "scrape", "match", "proposals", "execute"):
        node = progress[node_name]
        assert "done" in node
        assert "total" in node
        assert Status.NOT_STARTED.value not in node, f"{node_name} must not have grown four-bucket keys"
