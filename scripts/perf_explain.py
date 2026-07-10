"""EXPLAIN (ANALYZE, BUFFERS) the Phase-82 derived hot queries + time the /pipeline/stats endpoint (PERF-02).

Standalone ``uv run`` companion to :mod:`scripts.seed_perf_corpus`. Run it AFTER seeding a ~200K corpus into
a perf DB at migration HEAD (>=036, so the migration-032 partial indexes exist). It:

1. Rebuilds the FIVE hot derived query shapes from Plans 82-02/03 using the REAL clause builders
   (``eligible_clause`` / ``dedup_resolved_clause`` / ``stage_status_case`` imported from
   ``phaze.services``), compiles each to literal-bound SQL, and runs ``EXPLAIN (ANALYZE, BUFFERS)`` --
   so the recorded plan is the ACTUAL query the app issues, not a hand-rewrite:
     * get_metadata_pending_files   -- eligible_clause(METADATA)
     * get_fingerprint_pending_files -- eligible_clause(FINGERPRINT)
     * get_discovered_files_with_duration -- eligible_clause(ANALYZE) + LEFT JOIN + cloud-exclusion
     * four-bucket GROUP BY stage_status_case(METADATA | FINGERPRINT | ANALYZE)
2. Times the full ``GET /pipeline/stats`` endpoint (the real ASGI route via httpx ``ASGITransport`` with
   the DB session pointed at the perf DB), N iterations, reporting p50/p95.

The printed EXPLAIN plans + endpoint timings are the PERF-02 deliverable -- they license (or veto) the
DENORM-01 YAGNI decision against the ``< ~1s`` budget (D-07). Read-only: it issues only SELECT/EXPLAIN.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import time

import asyncpg  # type: ignore[import-untyped]
from httpx import ASGITransport, AsyncClient
from sqlalchemy import exists, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.enums.stage import Stage
from phaze.main import create_app
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.pipeline import _ACTIVE_CLOUD_STATUSES, MUSIC_VIDEO_TYPES, get_stage_progress
from phaze.services.stage_status import dedup_resolved_clause, eligible_clause, stage_status_case
from phaze.tasks._shared.queue_factory import build_pipeline_queue


_DEFAULT_DSN = "postgresql://phaze:phaze@localhost:5433/phaze_perf82"


def _bucket_stmt(stage: Stage):  # type: ignore[no-untyped-def]
    """Rebuild the exact ``_safe_bucket_counts`` four-bucket query for ``stage`` (pipeline.py)."""
    status_subq = select(stage_status_case(stage).label("status")).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).subquery()
    return select(status_subq.c.status, func.count()).group_by(status_subq.c.status)


def _hot_statements() -> dict[str, object]:
    """Return {label: SQLAlchemy Select} for the five derived hot shapes (verbatim from Plans 82-02/03)."""
    return {
        "get_metadata_pending_files": select(FileRecord).where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            eligible_clause(Stage.METADATA),
            ~dedup_resolved_clause(),
        ),
        "get_fingerprint_pending_files": select(FileRecord).where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            eligible_clause(Stage.FINGERPRINT),
            ~dedup_resolved_clause(),
        ),
        "get_discovered_files_with_duration": select(FileRecord, FileMetadata.duration)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            eligible_clause(Stage.ANALYZE),
            ~dedup_resolved_clause(),
            ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))),
        ),
        "four_bucket[metadata]": _bucket_stmt(Stage.METADATA),
        "four_bucket[fingerprint]": _bucket_stmt(Stage.FINGERPRINT),
        "four_bucket[analyze]": _bucket_stmt(Stage.ANALYZE),
    }


def _compile(stmt: object) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


async def run_explains(dsn: str) -> None:
    """EXPLAIN (ANALYZE, BUFFERS) every hot statement and print the plan under its label."""
    conn = await asyncpg.connect(dsn)
    try:
        n_files = await conn.fetchval("SELECT count(*) FROM files")
        print(f"\n===== EXPLAIN (ANALYZE, BUFFERS) @ {n_files} files =====")  # noqa: T201
        for label, stmt in _hot_statements().items():
            sql = _compile(stmt)
            rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS) {sql}")
            print(f"\n----- {label} -----")  # noqa: T201
            for r in rows:
                print(r[0])  # noqa: T201
    finally:
        await conn.close()


async def _provision_saq_tables(dsn: str, redis_url: str) -> bool:
    """Idempotently create the SAQ-owned ``saq_jobs`` tables in the perf DB via the project queue seam.

    ``/pipeline/stats`` fans out reads against ``saq_jobs`` (SAQ owns that table -- it is NOT
    Alembic-managed, so a freshly-migrated perf DB lacks it). Without it the endpoint's queue-activity
    reads take the degrade path and the timing measures error-handling overhead, not the real DB cost.
    ``PostgresQueue.connect()`` runs the idempotent ``CREATE TABLE IF NOT EXISTS saq_jobs/...`` init_db,
    giving us an EMPTY (idle) queue -- the representative measurement. Returns True on success.
    """
    q = build_pipeline_queue("perf-bench", dsn, cache_redis_url=redis_url, min_size=1, max_size=2)
    try:
        await q.connect()
        return True
    except Exception as exc:
        print(f"WARNING could not provision saq_jobs ({exc!r}); endpoint queue-activity reads will degrade")  # noqa: T201
        return False
    finally:
        with contextlib.suppress(Exception):
            await q.disconnect()


async def time_endpoint(dsn: str, iterations: int, redis_url: str) -> None:
    """Time the real GET /pipeline/stats endpoint against the perf DB, N iterations, print p50/p95."""
    await _provision_saq_tables(dsn, redis_url)
    sa_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    samples: list[float] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://perf") as client:
            # One warm-up request (connection + plan cache) excluded from the sample.
            warm = await client.get("/pipeline/stats")
            print(f"\n===== GET /pipeline/stats ({iterations} iterations) =====")  # noqa: T201
            print(f"warm-up status={warm.status_code}")  # noqa: T201
            for _ in range(iterations):
                t0 = time.perf_counter()
                resp = await client.get("/pipeline/stats")
                samples.append((time.perf_counter() - t0) * 1000.0)
                if resp.status_code != 200:
                    print(f"WARNING non-200: {resp.status_code}")  # noqa: T201
    finally:
        await engine.dispose()
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, round(0.95 * (len(samples) - 1)))]
    print(f"p50={p50:.1f}ms  p95={p95:.1f}ms  min={samples[0]:.1f}ms  max={samples[-1]:.1f}ms  n={len(samples)}")  # noqa: T201


async def time_stage_progress(dsn: str, iterations: int) -> None:
    """Time ``get_stage_progress(session)`` DIRECTLY -- the DENORM-relevant DB-bound core of /pipeline/stats.

    This isolates the pure DB cost that a denormalized stage-bitmap column (DENORM-01) would replace: the
    three four-bucket ``GROUP BY stage_status_case`` reads plus the other per-node counts, with NONE of the
    ASGI / Redis-degrade / template overhead. It is the cleanest number for the go/no-go call against D-07.
    """
    sa_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    samples: list[float] = []
    try:
        async with factory() as s:
            await get_stage_progress(s)  # warm-up (excluded)
            print(f"\n===== get_stage_progress() DIRECT ({iterations} iterations) =====")  # noqa: T201
            for _ in range(iterations):
                t0 = time.perf_counter()
                await get_stage_progress(s)
                samples.append((time.perf_counter() - t0) * 1000.0)
    finally:
        await engine.dispose()
    samples.sort()
    p95 = samples[min(len(samples) - 1, round(0.95 * (len(samples) - 1)))]
    print(f"p50={statistics.median(samples):.1f}ms  p95={p95:.1f}ms  min={samples[0]:.1f}ms  max={samples[-1]:.1f}ms  n={len(samples)}")  # noqa: T201


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EXPLAIN the derived hot queries + time /pipeline/stats (Phase 82 PERF-02).")
    parser.add_argument("--dsn", default=_DEFAULT_DSN, help=f"asyncpg DSN of the seeded perf DB (default {_DEFAULT_DSN})")
    parser.add_argument(
        "--redis-url", default="redis://localhost:6380/0", help="Redis DSN for the SAQ queue cache handle (default redis://localhost:6380/0)"
    )
    parser.add_argument("--iterations", type=int, default=20, help="endpoint timing iterations (default 20)")
    parser.add_argument("--skip-endpoint", action="store_true", help="run only the EXPLAINs (skip the ASGI endpoint timing)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(run_explains(args.dsn))
    asyncio.run(time_stage_progress(args.dsn, args.iterations))
    if not args.skip_endpoint:
        asyncio.run(time_endpoint(args.dsn, args.iterations, args.redis_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
