"""Baseline the Analyze-workspace slowdown at 200K scale (Phase 95, phaze-zqvh.1).

Standalone ``uv run`` companion to :mod:`scripts.perf_explain` / :mod:`scripts.seed_perf_corpus`
(Phase 82 PERF-02 harness). Run it AFTER seeding the ~200K corpus (``just perf-seed``) into the
dedicated perf DB (``just perf-db-up``). It measures the THREE hot paths the phase-95 epic names
as suspects, so the fix/verify beads have concrete before/after numbers to cite:

1. ``get_analyze_working_set`` (``services/pipeline.py``) -- DIRECT timing (no ASGI/template
   overhead) + returned row count, at corpus scale. This is the BOUNDED default per-file read
   (phaze-zqvh.2) that replaced the Phase-58 unbounded ``get_analyze_stage_files`` feeding the
   Analyze workspace table -- the "after" number to cite against the phaze-zqvh.1 92,335-row baseline.
2. ``GET /s/analyze`` -- the full-shell server render a direct/bookmark "open the workspace" nav
   takes (mirrors ``_render_stage`` in ``routers/shell.py``): wall-clock render time, response
   payload size in bytes, and an approximate per-file DOM row count (count of the per-row
   ``hx-get="/record/`` markers ``_file_table.html`` emits one per Analyze file row).
3. ``GET /pipeline/stats`` -- the 5s poll tick's OOB fan-out fragment (``stats_bar.html``):
   payload size in bytes per tick (companion to :func:`scripts.perf_explain.time_endpoint`'s
   latency numbers -- this adds the SIZE dimension to quantify the poll-tick churn).

Read-only: it issues only SELECT reads (route handlers) over the perf DB -- no seeding, no writes.

Usage::

    uv run python scripts/perf_analyze_workspace.py \\
        --dsn postgresql://phaze:phaze@localhost:5545/phaze_perf82 --iterations 10
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import time

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.main import create_app
from phaze.services.pipeline import get_analyze_working_set


_DEFAULT_DSN = "postgresql://phaze:phaze@localhost:5545/phaze_perf82"

# One marker per rendered Analyze file row (_file_table.html:52, emitted only when row_file_ids
# is supplied -- which analyze_workspace.html always does for the file table, Phase 61 RECORD-01).
_ROW_MARKER = re.compile(rb'hx-get="/record/')


def _percentiles(samples: list[float]) -> str:
    samples = sorted(samples)
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, round(0.95 * (len(samples) - 1)))]
    return f"p50={p50:.1f}ms  p95={p95:.1f}ms  min={samples[0]:.1f}ms  max={samples[-1]:.1f}ms  n={len(samples)}"


async def time_get_analyze_working_set(dsn: str, iterations: int) -> None:
    """Time ``get_analyze_working_set(session)`` DIRECTLY -- the BOUNDED default per-file read (phaze-zqvh.2).

    Replaces the Phase-58 ``get_analyze_stage_files`` DIRECT timing: that read returned the ENTIRE
    analyze-stage membership (92,335 rows at 200K scale -- the phaze-zqvh.1 baseline); this measures its
    bounded replacement (the active-first working set + a LIMIT-ed recent-completions window). The
    returned row count is the "after" number to cite against the 92,335 "before".
    """
    sa_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    samples: list[float] = []
    row_count = 0
    try:
        async with factory() as s:
            row_count = len(await get_analyze_working_set(s))  # warm-up (excluded), also captures the count
            print(f"\n===== get_analyze_working_set() DIRECT ({iterations} iterations) =====")  # noqa: T201
            print(f"returned row count: {row_count}")  # noqa: T201
            for _ in range(iterations):
                t0 = time.perf_counter()
                await get_analyze_working_set(s)
                samples.append((time.perf_counter() - t0) * 1000.0)
    finally:
        await engine.dispose()
    print(_percentiles(samples))  # noqa: T201


async def time_s_analyze(dsn: str, iterations: int) -> None:
    """Time GET /s/analyze (full-shell direct-nav render) -- wall time + payload size + row count."""
    sa_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    samples: list[float] = []
    sizes: list[int] = []
    row_counts: list[int] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://perf") as client:
            warm = await client.get("/s/analyze")
            print(f"\n===== GET /s/analyze ({iterations} iterations) =====")  # noqa: T201
            print(f"warm-up status={warm.status_code}")  # noqa: T201
            for _ in range(iterations):
                t0 = time.perf_counter()
                resp = await client.get("/s/analyze")
                samples.append((time.perf_counter() - t0) * 1000.0)
                if resp.status_code != 200:
                    print(f"WARNING non-200: {resp.status_code}")  # noqa: T201
                sizes.append(len(resp.content))
                row_counts.append(len(_ROW_MARKER.findall(resp.content)))
    finally:
        await engine.dispose()
    print(_percentiles(samples))  # noqa: T201
    print(  # noqa: T201
        f"payload bytes: mean={statistics.mean(sizes):.0f}  min={min(sizes)}  max={max(sizes)}  "
        f"approx DOM row count: mean={statistics.mean(row_counts):.0f}"
    )


async def time_pipeline_stats_payload(dsn: str, iterations: int) -> None:
    """Size the /pipeline/stats OOB fan-out fragment (stats_bar.html) per 5s poll tick.

    No SAQ ``saq_jobs`` table provisioning here (unlike :func:`scripts.perf_explain.time_endpoint`):
    an un-provisioned queue-activity read degrades to zero (service-owned SAVEPOINT/try-except, never
    raises), which does not affect the FRAGMENT SIZE this function measures -- only its numeric queue
    counts, which are irrelevant to the byte-count question. Skipping it avoids standing up a second
    SAQ connection pool purely to measure payload size.
    """
    sa_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    sizes: list[int] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://perf") as client:
            warm = await client.get("/pipeline/stats")
            print(f"\n===== GET /pipeline/stats OOB fragment size ({iterations} iterations) =====")  # noqa: T201
            print(f"warm-up status={warm.status_code}  bytes={len(warm.content)}")  # noqa: T201
            for _ in range(iterations):
                resp = await client.get("/pipeline/stats")
                sizes.append(len(resp.content))
    finally:
        await engine.dispose()
    print(  # noqa: T201
        f"payload bytes: mean={statistics.mean(sizes):.0f}  min={min(sizes)}  max={max(sizes)}"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baseline the Analyze-workspace slowdown at 200K scale (Phase 95, phaze-zqvh.1).")
    parser.add_argument("--dsn", default=_DEFAULT_DSN, help=f"asyncpg DSN of the seeded perf DB (default {_DEFAULT_DSN})")
    parser.add_argument("--iterations", type=int, default=10, help="iterations per measurement (default 10)")
    return parser.parse_args(argv)


async def _run_all(dsn: str, iterations: int) -> None:
    """Run all three measurements on ONE event loop (avoids cross-loop asyncpg teardown noise:

    separate ``asyncio.run()`` calls each spin up + tear down their own loop, and an engine
    disposed at the end of one call can have its underlying connection GC-finalized during a
    LATER call's loop, once the original loop is already closed -- harmless but noisy).
    """
    await time_get_analyze_working_set(dsn, iterations)
    await time_s_analyze(dsn, iterations)
    await time_pipeline_stats_payload(dsn, iterations)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_run_all(args.dsn, args.iterations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
