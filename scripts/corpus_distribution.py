"""One-command corpus-distribution probe for picking duration/size bounds (phaze-d2hgv.5).

ADR-0012 SS7 R3 / guardrail G3's distribution clause: a claim about the archive's distribution
must be discharged against the archive's distribution -- a query, not a test. `phaze-1b39` is the
entire justification -- a 6600s (110min, ~1h50m) wall-clock analysis-kill bound (the removed
`analysis_inner_timeout_sec`) was adequate for every input the tests used, then SIGTERM'd
legitimate 2-6h analyses and stalled the burst lane. One query over the real duration distribution
would have shown the bound sat inside it. This is that query, made cheap enough to run at the
moment a bound is being picked, rather than costing an ad-hoc SQL session that gets skipped.

Emits AGGREGATE STATISTICS ONLY -- a population count and a fraction, never filenames, paths, or
other row-level identifiers (CLAUDE.md, "No local identifiers in tracked files"). Issues exactly
one read-only SELECT per bound requested; no writes, no schema changes, safe to run against a
production database by construction.

Usage:
    uv run python scripts/corpus_distribution.py --duration-sec 6600
    uv run python scripts/corpus_distribution.py --size-bytes 5000000000
    uv run python scripts/corpus_distribution.py --duration-sec 6600 --size-bytes 5000000000
    just corpus-distribution 6600            # duration bound only
    just corpus-distribution 6600 5000000000 # duration and size bounds
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from phaze.config import get_settings
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata


async def duration_fraction(dsn: str, threshold_sec: float) -> tuple[int, int]:
    """Return (population, exceeding) for files with a KNOWN duration on record.

    The population is files that have reached the metadata stage with a non-null duration, not
    every row in `files` -- a file with no duration on record can neither exceed nor fall under a
    duration bound, and folding it into the denominator would understate the reported fraction.
    """
    stmt = select(
        func.count(FileMetadata.id),
        func.count(FileMetadata.id).filter(FileMetadata.duration > threshold_sec),
    ).where(FileMetadata.duration.is_not(None))
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            population, exceeding = (await conn.execute(stmt)).one()
    finally:
        await engine.dispose()
    return int(population), int(exceeding)


async def size_fraction(dsn: str, threshold_bytes: int) -> tuple[int, int]:
    """Return (population, exceeding) over ALL files -- `files.file_size` is non-nullable."""
    stmt = select(
        func.count(FileRecord.id),
        func.count(FileRecord.id).filter(FileRecord.file_size > threshold_bytes),
    )
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            population, exceeding = (await conn.execute(stmt)).one()
    finally:
        await engine.dispose()
    return int(population), int(exceeding)


def _report(label: str, population_desc: str, population: int, exceeding: int, threshold_repr: str) -> str:
    if population == 0:
        return f"{label}: population is 0 ({population_desc}) -- no fraction to report."
    fraction = exceeding / population
    return f"{label}: {exceeding} of {population} {population_desc} exceed {threshold_repr} ({fraction:.1%})."


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="What fraction of the corpus exceeds a given duration or size?")
    parser.add_argument("--dsn", default=None, help="asyncpg SQLAlchemy DSN; defaults to the app's configured DATABASE_URL")
    parser.add_argument("--duration-sec", type=float, default=None, help="duration bound in seconds (phaze-1b39's bound was 6600)")
    parser.add_argument("--size-bytes", type=int, default=None, help="file-size bound in bytes")
    args = parser.parse_args(argv)
    if args.duration_sec is None and args.size_bytes is None:
        parser.error("at least one of --duration-sec or --size-bytes is required")
    return args


async def _run(args: argparse.Namespace) -> None:
    dsn = args.dsn or get_settings().database_url
    if args.duration_sec is not None:
        population, exceeding = await duration_fraction(dsn, args.duration_sec)
        print(_report("Duration", "files with a known duration", population, exceeding, f"{args.duration_sec:g}s"))  # noqa: T201
    if args.size_bytes is not None:
        population, exceeding = await size_fraction(dsn, args.size_bytes)
        print(_report("Size", "files total", population, exceeding, f"{args.size_bytes:,}B"))  # noqa: T201


def main(argv: list[str] | None = None) -> int:
    asyncio.run(_run(_parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
