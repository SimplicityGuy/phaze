"""SAQ task function for AI proposal generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select

from phaze.config import settings
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.date_convention import annotate_date_conventions
from phaze.services.proposal import (
    ProposalService,
    build_file_context,
    check_rate_limit,
    fetch_companion_contents,
    load_companion_targets,
    store_proposals,
)


if TYPE_CHECKING:
    from phaze.schemas.agent_tasks import CompanionReadItem


async def generate_proposals(ctx: dict[str, Any], *, file_ids: list[str], batch_index: int) -> dict[str, Any]:
    """Generate AI filename proposals for a batch of files.

    Per D-14: Fixed-size batches.
    Per D-16: Runs through SAQ worker pool.
    Per D-19: Rate-limited via Redis counter.

    Args:
        ctx: SAQ job context (contains queue, redis cache handle, proposal_service).
        file_ids: List of file UUID strings to process in this batch.
        batch_index: Index of this batch (for logging/tracking).

    Returns:
        Dict with batch index, count of proposals stored, and status.
    """
    # phaze-6fvu: DO NOT hold a DB connection across the rate-limit backoff and the LLM call.
    # check_rate_limit loops `asyncio.sleep(2.0)` while the shared window is over llm_max_rpm, and
    # generate_batch is a full LLM round-trip (routinely 30-120s). A pipeline drain enqueues one job
    # per batch across the whole corpus and the controller worker runs worker_max_jobs concurrently,
    # so holding the session open here pins that many PgBouncer SESSION-mode connections
    # idle-in-transaction for the hours the drain lasts (and holds back the vacuum xmin horizon),
    # reproducing the documented pool-exhaustion incident. Nothing DB-side is needed between the reads
    # and the writes, and build_file_context returns plain dicts (no live ORM refs), so split the work
    # into two short sessions with the network I/O in between.
    #
    # phaze-potg5: the SAME rule applies to the per-file companion read. Since phaze-6bkk it is NOT a
    # local read -- it is a `queue.apply("read_companion_files", timeout=30, ...)` request/response
    # job dispatched to the owning agent's meta lane. Only the DB portion that resolves WHICH agent
    # owns each companion path (`load_companion_targets`) runs inside the read session below; the
    # agent round-trip itself (`fetch_companion_contents`) is deferred to step 1b, after the session
    # has closed, exactly like the LLM call and rate-limit backoff.
    task_router = ctx.get("task_router")

    # 1. Build context for each file, then RELEASE the connection (the `async with` closes the
    #    session -- and its read transaction -- before any network I/O).
    files_context: list[dict[str, Any]] = []
    valid_file_ids: list[str] = []
    companion_targets: list[dict[str, list[CompanionReadItem]]] = []
    async with ctx["async_session"]() as session:
        # phaze-vu88k.4: the three reads below used to be three round trips PER FILE inside the loop
        # (3N for a batch of N). They are now three `IN (...)` reads for the WHOLE batch, keyed back
        # to each file by id. This is a query-SHAPE change only: same session, same read transaction,
        # same resulting `files_context` order, and the phaze-6fvu / phaze-potg5 rule that no session
        # is held across network I/O is untouched (the LLM call, the rate-limit backoff and the
        # companion CONTENT fetch all still happen after this block closes).
        #
        # `scalar_one_or_none()` could never have been a multi-row case here, so a dict keyed on
        # file_id is exactly equivalent: `analysis.file_id` and `metadata.file_id` are both
        # `unique=True` at the DB level (models/analysis.py, models/metadata.py). A file with no
        # analysis/metadata row still yields None, and a file_id with no `files` row is still
        # skipped. The loop iterates `file_ids` (NOT the dicts), so a repeated id still produces one
        # context per occurrence, as before.
        uids = [uuid.UUID(fid) for fid in file_ids]
        files_by_id = {row.id: row for row in (await session.execute(select(FileRecord).where(FileRecord.id.in_(uids)))).scalars()}
        analysis_by_id = {
            row.file_id: row for row in (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_(uids)))).scalars()
        }
        metadata_by_id = {row.file_id: row for row in (await session.execute(select(FileMetadata).where(FileMetadata.file_id.in_(uids)))).scalars()}

        for fid, uid in zip(file_ids, uids, strict=True):
            file_record = files_by_id.get(uid)
            if file_record is None:
                continue

            analysis = analysis_by_id.get(uid)
            metadata = metadata_by_id.get(uid)

            # phaze-6bkk: the controller is fileless (DIST-01), so the companion read is dispatched
            # to the owning agent through the shared task router the controller already holds.
            # phaze-potg5: only the TARGETS (which agent owns which companion path) are resolved
            # here, from the read session -- the agent round-trip that fetches the actual text
            # happens in step 1b below, with no session held open across it.
            by_agent = await load_companion_targets(session, uid, task_router=task_router)

            ctx_dict = build_file_context(file_record, analysis, [], metadata=metadata)
            ctx_dict["index"] = len(files_context)
            files_context.append(ctx_dict)
            valid_file_ids.append(fid)
            companion_targets.append(by_agent)

        # phaze-5fta.4: resolve each file's scene date and attach its provenance, in the SAME read
        # session (one batched store query, no extra connection). This is a no-op returning 0 --
        # and issuing no query at all -- while `convention_date_fallback_enabled` is off. It now
        # defaults ON (phaze-5fta.5's external validation passed and the operator enabled it), so
        # this is the live path; setting the flag false restores the byte-identical pre-phaze-5fta
        # behavior without a code change.
        await annotate_date_conventions(
            session,
            files_context,
            enabled=settings.convention_date_fallback_enabled,
            min_supporting=settings.convention_date_min_supporting,
            min_purity=settings.convention_date_min_purity,
        )

    if not files_context:
        return {"batch": batch_index, "count": 0, "status": "empty"}

    # 1b. Fetch companion CONTENTS from each owning agent -- network I/O, deliberately AFTER the
    #     read session above has closed (phaze-potg5), mirroring how the LLM call and rate-limit
    #     backoff were moved out of the read session in phaze-6fvu.
    for fid, ctx_dict, by_agent in zip(valid_file_ids, files_context, companion_targets, strict=True):
        ctx_dict["companions"] = await fetch_companion_contents(by_agent, settings.llm_max_companion_chars, task_router, uuid.UUID(fid))

    # 2. Rate limit via Redis counter on the DEDICATED cache-redis handle. Phase 36: the
    # broker is Postgres now, so `ctx["queue"]` (a PostgresQueue) has no `.redis`. The
    # control worker stashes a decoupled cache client at `ctx["redis"]` (controller.startup).
    # NO DB connection is held across this backoff loop (phaze-6fvu).
    await check_rate_limit(ctx["redis"], settings.llm_max_rpm)

    # 3. Call LLM -- also with NO DB connection held (phaze-6fvu).
    proposal_service: ProposalService = ctx["proposal_service"]
    batch_response = await proposal_service.generate_batch(files_context)

    # 4. Open a FRESH short session only for the writes + commit (phaze-6fvu). store_proposals upserts
    #    by file_id (pg_insert), so it needs no live ORM objects from the read phase.
    async with ctx["async_session"]() as session:
        stored = await store_proposals(session, valid_file_ids, batch_response, files_context)
        await session.commit()

    return {"batch": batch_index, "count": stored, "status": "ok"}
