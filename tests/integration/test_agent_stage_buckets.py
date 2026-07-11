"""Per-agent stage-bucket aggregate (`_agent_stage_buckets`) — real-PG GROUP BY (DRILL-02 / D-04 / D-00a).

`_agent_stage_buckets(session, agent_id, stage)` is a one-conjunct clone of
`phaze.services.pipeline._safe_bucket_counts`: the SAME GroupingError-safe inner-subquery-then-
GROUP-BY-scalar-label shape, with the single addition of `.where(FileRecord.agent_id == agent_id)`
so the five-way `{not_started,in_flight,done,skipped,failed}` count is scoped to ONE agent's owned
music/video files. It reuses the LOCKED `stage_status_case` derivation (D-00a/DERIV-04) verbatim —
NEVER a fresh CASE ladder.

Load-bearing cells (mirrors `tests/integration/test_stage_progress_buckets.py` discipline):

* **runs on real PG without GroupingError** — grouping directly by `stage_status_case(stage)` throws
  a Postgres GroupingError (the CASE ladder embeds correlated `exists(... == FileRecord.id)`
  subqueries); the inner-subquery-first shape dodges it. SQLite would NOT catch this — real PG (5433)
  is mandatory.
* **sum-to-total invariant** — for a healthy corpus the five buckets sum to the agent's music/video
  count (the same invariant `_safe_bucket_counts` documents — a healthy-path property only, NEVER a
  runtime assertion in the degrade-safe poll path).
* **agent_id conjunct is load-bearing (mutation-check)** — a second agent's files are EXCLUDED from
  the first agent's counts; removing `FileRecord.agent_id == agent_id` makes these assertions RED.

Real-PG `db_session` fixture + `_file` seed helper + the destructive `*_test` DB guard are copied
from `test_stage_progress_buckets.py`. Output rows are written to place each file in a known per-stage
bucket (never a bare `FileRecord.state` mutation — the buckets derive from the output tables + the
scheduling ledger, not `state`). Run with real PG via `just test-bucket integration` on port 5433
(export `TEST_DATABASE_URL`).
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

from phaze.enums.stage import Stage, Status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.pipeline import _agent_stage_buckets
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

# DSN derivation + destructive-DB guard, identical to test_stage_progress_buckets.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run agent-stage-bucket integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_AGENT_A = "agent-a-drill"
_AGENT_B = "agent-b-drill"

_ENRICH_STAGES = (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE)
_FIVE_BUCKETS = (
    Status.NOT_STARTED.value,
    Status.IN_FLIGHT.value,
    Status.DONE.value,
    Status.SKIPPED.value,
    Status.FAILED.value,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the two FK agents (copied from the bucket harness)."""
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
        # Idempotent FK-agent seed for BOTH drill agents (the shared *_test DB may already carry a
        # committed row from a sibling bucket's committing test — re-adding raises at flush).
        for agent_id in (_AGENT_A, _AGENT_B):
            if await session.get(Agent, agent_id) is None:
                session.add(Agent(id=agent_id, name=agent_id))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, agent_id: str, file_type: str = "mp3") -> FileRecord:
    """Seed a bare FileRecord OWNED BY ``agent_id`` and return the ORM object (id is set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{agent_id}/{fid}.{file_type}",
        original_filename=f"{fid}.{file_type}",
        current_path=f"/media/{agent_id}/{fid}.{file_type}",
        file_type=file_type,
        file_size=1234,
        state=FileState.DISCOVERED.value,
        agent_id=agent_id,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _ledger(session: AsyncSession, stage: Stage, file: FileRecord) -> None:
    """Seed a scheduling_ledger row on the deterministic ``<function>:<file_id>`` key (in_flight bucket)."""
    func_name = STAGE_TO_FUNCTION[stage.value]
    session.add(
        SchedulingLedger(
            key=f"{func_name}:{file.id}",
            function=func_name,
            routing="agent",
            payload={"file_id": str(file.id)},
        )
    )
    await session.flush()


async def _seed_agent_a_corpus(session: AsyncSession) -> int:
    """Seed a healthy mixed music/video corpus OWNED BY agent A; return its music/video ``total``.

    Each file lands in exactly ONE bucket per stage via ``stage_status_case`` precedence
    (in_flight > done > skipped > failed > not_started). A single non-music file is added to prove it
    is EXCLUDED from every enrich total (music/video scope).
    """
    # 3 bare music files -> not_started for all three enrich stages.
    for _ in range(3):
        await _file(session, agent_id=_AGENT_A)

    # metadata: 2 done (row + failed_at NULL), 1 failed-only (failed_at set), 1 in_flight (ledger).
    for _ in range(2):
        f = await _file(session, agent_id=_AGENT_A)
        session.add(FileMetadata(file_id=f.id, failed_at=None))
    f = await _file(session, agent_id=_AGENT_A)
    session.add(FileMetadata(file_id=f.id, failed_at=datetime.now(UTC)))
    f = await _file(session, agent_id=_AGENT_A)
    await _ledger(session, Stage.METADATA, f)

    # fingerprint: 1 done (a success engine), 1 failed-only (a failed engine, no success).
    f = await _file(session, agent_id=_AGENT_A)
    session.add(FingerprintResult(file_id=f.id, engine="audfprint", status="success"))
    f = await _file(session, agent_id=_AGENT_A)
    session.add(FingerprintResult(file_id=f.id, engine="audfprint", status="failed"))

    # analyze: 1 done (completed_at set), 1 failed (failed_at set), 1 in_flight (ledger).
    f = await _file(session, agent_id=_AGENT_A)
    session.add(AnalysisResult(file_id=f.id, analysis_completed_at=datetime.now(UTC)))
    f = await _file(session, agent_id=_AGENT_A)
    session.add(AnalysisResult(file_id=f.id, failed_at=datetime.now(UTC)))
    f = await _file(session, agent_id=_AGENT_A)
    await _ledger(session, Stage.ANALYZE, f)

    await session.flush()
    music_video_total = 3 + 4 + 2 + 3  # every seeded music file above (11)

    # One non-music file owned by agent A -- must NOT count toward any enrich total (music/video scope).
    await _file(session, agent_id=_AGENT_A, file_type="txt")

    return music_video_total


async def _seed_agent_b_metadata_done(session: AsyncSession, n: int) -> None:
    """Seed ``n`` metadata-done music files OWNED BY agent B (the exclusion control)."""
    for _ in range(n):
        f = await _file(session, agent_id=_AGENT_B)
        session.add(FileMetadata(file_id=f.id, failed_at=None))
    await session.flush()


async def test_runs_on_real_pg_and_buckets_sum_to_agent_total(db_session: AsyncSession) -> None:
    """The per-agent GROUP BY runs on real PG (no GroupingError) and the five buckets sum to the agent's total."""
    total = await _seed_agent_a_corpus(db_session)
    # A second agent's files must never leak into agent A's counts.
    await _seed_agent_b_metadata_done(db_session, n=5)

    for stage in _ENRICH_STAGES:
        buckets = await _agent_stage_buckets(db_session, _AGENT_A, stage)
        for key in _FIVE_BUCKETS:
            assert key in buckets, f"{stage.value} missing bucket key {key!r}"
        five_sum = sum(int(buckets[b] or 0) for b in _FIVE_BUCKETS)
        # Healthy-query invariant ONLY: the aggregate degrades fail-safe to all-zero on a query error.
        assert five_sum == total, f"{stage.value}: buckets {five_sum} must sum to agent A total {total}"


async def test_seeded_failed_and_in_flight_are_visible(db_session: AsyncSession) -> None:
    """Agent A's seeded failed / in-flight rows surface in the per-agent buckets (derived truth)."""
    await _seed_agent_a_corpus(db_session)

    meta = await _agent_stage_buckets(db_session, _AGENT_A, Stage.METADATA)
    assert int(meta[Status.DONE.value] or 0) == 2
    assert int(meta[Status.FAILED.value] or 0) == 1
    assert int(meta[Status.IN_FLIGHT.value] or 0) == 1

    fp = await _agent_stage_buckets(db_session, _AGENT_A, Stage.FINGERPRINT)
    assert int(fp[Status.DONE.value] or 0) == 1
    assert int(fp[Status.FAILED.value] or 0) == 1

    an = await _agent_stage_buckets(db_session, _AGENT_A, Stage.ANALYZE)
    assert int(an[Status.DONE.value] or 0) == 1
    assert int(an[Status.FAILED.value] or 0) == 1
    assert int(an[Status.IN_FLIGHT.value] or 0) == 1


async def test_agent_id_conjunct_is_load_bearing(db_session: AsyncSession) -> None:
    """A second agent's files are EXCLUDED — the mutation-check that ``FileRecord.agent_id`` is filtered.

    Agent A seeds exactly 2 metadata-done files; agent B seeds 5 metadata-done files. If the aggregate
    ignored ``agent_id`` it would report 7 done for agent A. This assertion goes RED the instant the
    ``.where(FileRecord.agent_id == agent_id)`` conjunct is removed.
    """
    await _seed_agent_a_corpus(db_session)
    await _seed_agent_b_metadata_done(db_session, n=5)

    a_meta = await _agent_stage_buckets(db_session, _AGENT_A, Stage.METADATA)
    b_meta = await _agent_stage_buckets(db_session, _AGENT_B, Stage.METADATA)

    assert int(a_meta[Status.DONE.value] or 0) == 2, "agent A must see ONLY its own metadata-done files"
    assert int(b_meta[Status.DONE.value] or 0) == 5, "agent B's own count is independent"
    # The two agents' done counts differ — proving the conjunct partitions the corpus.
    assert a_meta[Status.DONE.value] != b_meta[Status.DONE.value]


async def test_downstream_stage_runs_without_grouping_error(db_session: AsyncSession) -> None:
    """A downstream stage (propose) aggregates without GroupingError and yields only not_started here.

    The three downstream stages (propose/review/apply) have no in-flight/skip markers seeded, so every
    file resolves to not_started — but the aggregate must still RUN (the 6-stage router loop calls it).
    """
    total = await _seed_agent_a_corpus(db_session)
    propose = await _agent_stage_buckets(db_session, _AGENT_A, Stage.PROPOSE)
    assert int(propose[Status.NOT_STARTED.value] or 0) == total
    assert sum(int(propose[b] or 0) for b in _FIVE_BUCKETS) == total


async def test_unknown_agent_returns_all_zero(db_session: AsyncSession) -> None:
    """An agent that owns no files yields an all-zero (never raising) bucket dict."""
    await _seed_agent_a_corpus(db_session)
    empty = await _agent_stage_buckets(db_session, "nobody-owns-me", Stage.METADATA)
    assert empty == dict.fromkeys(_FIVE_BUCKETS, 0)
