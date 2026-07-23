"""POST /api/internal/agent/tracklists -- idempotent atomic Tracklist+Version+Tracks create (Phase 26 D-27).

Three-path idempotency model:
1. Fast path: ``tracklist_resp:{request_id}`` exists in Redis -> return cached JSON, no DB work.
2. Concurrent-writer path: another process owns this request_id (SET NX lost the race).
   Poll resp_key up to 10 * 50ms = 500ms; if still empty -> 409 Conflict.
3. Owner path: we won the SET NX race. Do the DB transaction, cache the response, return.

Multi-row write (one transaction):
- UPSERT Tracklist by external_id (UQ from models/tracklist.py:30).
- Compute next ``version_number = max(version_number) + 1`` OR 1 for first version.
- INSERT TracklistVersion(tracklist_id, version_number).
- INSERT N TracklistTrack rows (one per body.tracks).
- UPDATE Tracklist.latest_version_id = version.id.
- ``session.commit()`` exactly once.

Decisions implemented: D-27 (endpoint shape, request-id idempotency, 1h TTL).
"""

import asyncio
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
import redis.asyncio as redis_async
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_tracklists import (
    ScanTerminalAckResponse,
    TracklistCreatePayload,
    TracklistCreateResponse,
)
from phaze.services.scheduling_ledger import clear_ledger_entry


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/tracklists", tags=["agent-internal"])

_REQ_PREFIX = "tracklist_req:"
_RESP_PREFIX = "tracklist_resp:"
_TTL_SECONDS = 3600  # 1 hour idempotency window (D-27)
_CONCURRENT_POLL_INTERVAL_S = 0.05
_CONCURRENT_POLL_MAX_ATTEMPTS = 10  # 10 * 50ms = 500ms total wait


async def _get_redis(request: Request) -> redis_async.Redis:
    """Pull the Redis client from ``app.state`` (wired by Plan 26-12 main.py lifespan).

    ``main.py``'s FastAPI lifespan installs ``app.state.redis =
    redis_async.Redis.from_url(..., decode_responses=True)``; this dep is a thin
    pass-through so the handler stays testable (smoke-app fixture sets
    ``app.state.redis`` directly).
    """
    redis_client: redis_async.Redis = request.app.state.redis
    return redis_client


@router.post("", status_code=status.HTTP_200_OK, response_model=TracklistCreateResponse)
async def create_tracklist(
    body: TracklistCreatePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
) -> TracklistCreateResponse:
    """Idempotent atomic create keyed on ``body.request_id`` (D-27).

    Returns:
        TracklistCreateResponse: 200 with the new (or cached) tracklist/version IDs
        and the track count for the version just written.

    Raises:
        HTTPException(409): concurrent writer for this ``request_id`` is in flight
        and has not yet cached its response after a bounded 500ms wait.

    Security:
        - ``agent`` is bound from the auth dep, NEVER from the body (AUTH-01).
        - ``body.tracks`` is schema-capped at ``max_length=2000`` (T-26-07-DoS).

    Trust model (T-26-07-T, accepted):
        If a caller reuses the same ``request_id`` with a different payload,
        the cached response from the first call is returned (silent). This is
        acceptable for the project's single-operator deployment model. If
        future evidence reveals this is a real concern, hash the request body
        into the cache and compare on replay (RESEARCH Pitfall 4, ~5 lines).
    """
    req_key = f"{_REQ_PREFIX}{body.request_id}"
    resp_key = f"{_RESP_PREFIX}{body.request_id}"

    # 1. Fast path -- cached response exists; return it without DB work.
    cached = await redis_client.get(resp_key)
    if cached is not None:
        return TracklistCreateResponse.model_validate_json(cached)

    # 2. Try to acquire the owner lock atomically (SET NX EX).
    won = await redis_client.set(req_key, "1", nx=True, ex=_TTL_SECONDS)
    if not won:
        # Concurrent writer in progress -- poll resp_key briefly for their response.
        for _ in range(_CONCURRENT_POLL_MAX_ATTEMPTS):
            await asyncio.sleep(_CONCURRENT_POLL_INTERVAL_S)
            cached = await redis_client.get(resp_key)
            if cached is not None:
                return TracklistCreateResponse.model_validate_json(cached)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="duplicate in-flight request",
        )

    # 3. Owner path -- do the DB transaction.
    # We hold the owner lock (req_key). ANY failure before resp_key is cached (transient asyncpg
    # blip, deadlock, commit failure) must RELEASE the lock, or the 1h TTL strands every subsequent
    # delivery -- including this client's own retries -- on the 409 concurrent-writer path (which
    # agent_client never retries), permanently and silently discarding a matched tracklist. Release
    # on failure so the next retry can re-acquire (phaze-dwwj).
    #
    # phaze-42jr: catch BaseException, not Exception. A cancellation (CancelledError is a
    # BaseException in 3.14, e.g. on server shutdown) previously skipped the release entirely and
    # stranded the lock for the full 1h TTL. Tolerate the delete itself failing (another transient
    # Redis blip) so cleanup can never mask the original error.
    try:
        return await _run_owner_path(session, redis_client, resp_key, body, agent)
    except BaseException:
        try:
            await redis_client.delete(req_key)
        except Exception:
            logger.warning("Failed to release owner lock after owner-path error", req_key=req_key, exc_info=True)
        raise


async def _run_owner_path(
    session: AsyncSession,
    redis_client: redis_async.Redis,
    resp_key: str,
    body: TracklistCreatePayload,
    agent: Agent,
) -> TracklistCreateResponse:
    """Owner-path DB transaction + response cache. Raises on any failure so the caller releases the lock."""
    # Step A: UPSERT Tracklist by external_id. ON CONFLICT preserves the existing
    # row (so its id stays stable across replays with new request_ids); we update
    # only file_id + source for last-write-wins on those mutable fields. The
    # returning() clause yields the row id whether INSERT or UPDATE fired.
    upsert_tracklist = (
        pg_insert(Tracklist)
        .values(
            external_id=body.external_id,
            source_url="",  # agent does not know source URL; controller fills via Phase 27 if needed
            file_id=body.file_id,
            source=body.source,
            status="proposed",
        )
        .on_conflict_do_update(
            index_elements=["external_id"],
            set_={
                "file_id": body.file_id,
                "source": body.source,
                # TimestampMixin.updated_at's ORM onupdate=func.now() never fires on this Core
                # ON CONFLICT DO UPDATE path -- stamp it explicitly so a replayed tracklist
                # create bumps updated_at instead of freezing it at first write (phaze-c8nz).
                # created_at stays pinned: it means "first time this external_id was recorded".
                "updated_at": func.now(),
            },
        )
        .returning(Tracklist.id)
    )
    tracklist_id = (await session.execute(upsert_tracklist)).scalar_one()

    # Step B: compute next version_number under this tracklist (start at 1 if first).
    max_version = (
        await session.execute(select(func.max(TracklistVersion.version_number)).where(TracklistVersion.tracklist_id == tracklist_id))
    ).scalar_one_or_none() or 0
    new_version_number = max_version + 1

    # Step C: INSERT TracklistVersion (returning new row's id for FK linkage).
    version_insert = pg_insert(TracklistVersion).values(tracklist_id=tracklist_id, version_number=new_version_number).returning(TracklistVersion.id)
    version_id = (await session.execute(version_insert)).scalar_one()

    # Step D: INSERT N TracklistTrack rows under this version.
    if body.tracks:
        track_rows = [
            {
                "version_id": version_id,
                "position": t.position,
                "artist": t.artist,
                "title": t.title,
                "timestamp": t.timestamp,
                "confidence": t.confidence,
            }
            for t in body.tracks
        ]
        await session.execute(pg_insert(TracklistTrack).values(track_rows))

    # Step E: UPDATE Tracklist.latest_version_id pointer to the new version.
    await session.execute(update(Tracklist).where(Tracklist.id == tracklist_id).values(latest_version_id=version_id))

    # Phase 45 (L-02): clear the scan_live_set:<file_id> ledger row in the SAME owner-path
    # transaction as the tracklist write -- the MATCH terminal outcome for scan_live_set. The
    # fast-path/cached return above does NO DB work and so does NO clear: a replayed match
    # callback already cleared on its first delivery (and an absent-key clear is a no-op anyway).
    # Key from body.file_id (the trusted tracklist target) + the fixed function name (AUTH-01 /
    # T-45-05: agent identity is bound from the auth dep, never a redirect field).
    await clear_ledger_entry(session, f"scan_live_set:{body.file_id}")

    await session.commit()

    # Step F: cache the response under resp_key (with TTL) for future replays.
    response = TracklistCreateResponse(
        tracklist_id=tracklist_id,
        version=new_version_number,
        track_count=len(body.tracks),
    )
    # phaze-42jr: the cache write runs AFTER the durable commit, so a transient Redis failure here
    # must NOT surface as an error. Re-raising would trip the caller's release-and-raise path -> 500,
    # and agent_client's tenacity retry would re-run this whole owner path with the SAME request_id:
    # version_number = max+1 sidesteps the UNIQUE(tracklist_id, version_number) constraint, appending
    # a SECOND identical TracklistVersion and repointing latest_version_id at the duplicate. The
    # tracklist/version/tracks and the ledger clear are already committed, so treat a post-commit
    # cache miss as success: log and return the already-durable response. The lock stays held for its
    # TTL (a genuine replay simply 409s rather than duplicating) -- releasing it here would instead
    # invite the duplicate-version re-run we are preventing.
    try:
        await redis_client.set(resp_key, response.model_dump_json(), ex=_TTL_SECONDS)
    except Exception:
        logger.warning("Post-commit idempotency cache write failed; returning durable response", resp_key=resp_key, exc_info=True)
    # Touch ``agent`` so ARG001 doesn't fire; the binding's real role is auth-gating
    # (Depends() invocation enforces 401/403 before we reach this body).
    _ = agent.id
    return response


@router.post("/{file_id}/scanned", status_code=status.HTTP_200_OK, response_model=ScanTerminalAckResponse)
async def ack_scan_terminal(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScanTerminalAckResponse:
    """Terminal-ack for a no-match / failed ``scan_live_set`` run (Phase 45 L-02).

    ``create_tracklist`` clears the ledger row on a MATCH; this endpoint closes the
    no-match early-return AND the retries-exhausted terminal-failure holes so EVERY
    ``scan_live_set`` run clears ``scan_live_set:<file_id>`` exactly once -- a legitimate
    no-match scan can never re-enqueue on every recovery (Blocker 2 / T-45-16).

    ``agent`` is bound from the auth dep (token, never body -- AUTH-01); the clear key is
    reconstructed from the PATH ``file_id`` ONLY + the fixed function name, so a forged
    request cannot redirect the clear to another file's key (T-45-05). Clearing an absent
    row is a clean no-op (still 200) -- a re-delivered ack is harmless.
    """
    await clear_ledger_entry(session, f"scan_live_set:{file_id}")
    await session.commit()
    # Touch ``agent`` so ARG001 doesn't fire; the binding's real role is auth-gating.
    _ = agent.id
    return ScanTerminalAckResponse(file_id=file_id, cleared=True)
