"""Task function definitions for arq workers."""

from typing import Any

from arq import Retry


async def process_file(ctx: dict[str, Any], file_id: int) -> dict[str, Any]:
    """Process a single file. Skeleton for Phase 4; analysis logic added in Phase 5.

    Per D-02: one job per file for granular retry and progress tracking.
    Per D-03: retries with exponential backoff (defer = job_try * 5 seconds).
    """
    try:
        # Phase 5 will add: BPM detection, fingerprinting, metadata extraction
        # CPU-bound work pattern:
        # result = await run_in_process_pool(ctx, cpu_bound_fn, file_id)
        return {"file_id": file_id, "status": "processed"}
    except Exception as exc:
        # Exponential backoff: 5s, 10s, 15s (job_try is 1-indexed)
        raise Retry(defer=ctx["job_try"] * 5) from exc
