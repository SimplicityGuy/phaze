"""PUT /api/internal/agent/fingerprints/{file_id}/{engine} -- idempotent fingerprint write (phase-25)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.fingerprint import FingerprintResult
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_fingerprint import FingerprintFailureResponse, FingerprintWriteRequest, FingerprintWriteResponse
from phaze.services.scheduling_ledger import clear_ledger_entry


router = APIRouter(prefix="/api/internal/agent/fingerprints", tags=["agent-internal"])


@router.put("/{file_id}/{engine}", status_code=status.HTTP_200_OK, response_model=FingerprintWriteResponse)
async def put_fingerprint(
    file_id: uuid.UUID,
    engine: str,
    body: FingerprintWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FingerprintWriteResponse:
    """Idempotently replace fingerprint result. Natural key: (file_id, engine) from models/fingerprint.py:25 (ix_fprint_file_engine).

    Last-write-wins per D-14. `agent_id` comes from auth dep, NEVER from body (AUTH-01).

    FAILURE MARKER (FAIL-04 / D-18): a `status='failed'` row written here IS the durable, per-engine
    fingerprint failure marker -- there is no separate failure table and no synthetic sentinel row.
    Crucially it is AUTO-RETRYABLE: `FAILURE_IS_TERMINAL[fingerprint] = False`, so `eligible(FINGERPRINT)`
    stays True for a FAILED engine (ELIG-04, `enums/stage.py:186`), unlike a terminal FAILED analyze.
    A sibling `success`/`completed` engine still wins the 1:N aggregation (DERIV-05), so one failed
    engine never blocks a file whose other engine succeeded. See `report_fingerprint_failed` for why
    the terminal-ack path deliberately writes NO row.

    PK NOTE: `FingerprintResult.id` declares Python-only `default=uuid.uuid4`
    (no server_default). `pg_insert(...).values()` bypasses ORM defaults, so
    we stamp `payload["id"] = uuid.uuid4()` explicitly. ON CONFLICT DO UPDATE
    preserves the existing row's id.
    """
    # Stamp PK explicitly because FingerprintResult.id has only a Python-side default.
    payload = {**body.model_dump(), "file_id": file_id, "engine": engine, "id": uuid.uuid4()}
    stmt = pg_insert(FingerprintResult).values([payload])
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id", "engine"],  # composite UQ per models/fingerprint.py:25
        set_={
            "status": stmt.excluded.status,
            "error_message": stmt.excluded.error_message,
        },
    )
    await session.execute(stmt)
    # Phase 45 (L-02): clear the fingerprint_file:<file_id> ledger row in the SAME transaction
    # as the fingerprint upsert. The ledger key is a SINGLE key per file (NOT per engine -- the
    # fingerprint_file task enqueues ONE job per file, keyed by file_id), so clearing on any
    # engine PUT is correct; a second engine PUT is a clean no-op. Key from the PATH file_id
    # ONLY (engine is NOT part of the ledger key; AUTH-01 / T-45-05).
    await clear_ledger_entry(session, f"fingerprint_file:{file_id}")
    await session.commit()
    return FingerprintWriteResponse(agent_id=agent.id, file_id=file_id, engine=engine)


@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=FingerprintFailureResponse)
async def report_fingerprint_failed(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FingerprintFailureResponse:
    """Terminal-ack for a retries-exhausted ``fingerprint_file`` run (Phase 45 L-02 / CR-02).

    PERSISTS NO ``fingerprint_results`` ROW, BY DESIGN (FAIL-04 / D-18). This endpoint's ONLY durable
    effect is clearing the ``fingerprint_file:<file_id>`` ledger row -- it deliberately writes no row
    of its own. The durable per-engine failure marker already lives elsewhere: the ``status='failed'``
    row written by :func:`put_fingerprint`. A synthetic terminal row such as
    ``fingerprint_results(engine='_task', status='failed')`` would POISON the two aliased per-engine
    outer-joins at ``services/pipeline.py:939-940`` (``audfprint`` / ``panako``) that feed
    :func:`~phaze.services.pipeline._trackid_engine_badge` (``services/pipeline.py:864``) -- the badge
    keys strictly on the real lowercase engine names, so a ``_task`` sentinel would surface as a bogus
    engine column and corrupt the Track-ID table. Hence FAIL-04 is "reused, not re-invented": the
    marker is the existing per-engine row, and a failed fingerprint stays auto-retryable
    (``FAILURE_IS_TERMINAL[fingerprint] = False``; see :func:`put_fingerprint`).

    ``put_fingerprint`` clears the ledger row on SUCCESS; this endpoint closes the
    terminal-failure hole so EVERY ``fingerprint_file`` run clears
    ``fingerprint_file:<file_id>`` exactly once. Without it, a terminally-failed
    fingerprint file stays in ``get_fingerprint_pending_files`` (METADATA_EXTRACTED files
    PLUS ``FingerprintResult(status="failed")`` rows both keep it in the pending set), so
    ``is_domain_completed`` can never fire and ``recover_orphaned_work`` re-enqueues it on
    every recovery pass forever -- the unbounded recovery re-enqueue loop the ledger was
    introduced to prevent (CR-02).

    The ledger key is a SINGLE key per file (NOT per engine -- the fingerprint_file task
    enqueues ONE job per file, keyed by file_id), so the ack path takes no ``engine`` and
    clears the single per-file key. ``agent`` is bound from the auth dep (token, never body
    -- AUTH-01); the clear key is reconstructed from the PATH ``file_id`` ONLY + the fixed
    function name, matching the deterministic WRITE key exactly, so a forged request cannot
    redirect the clear to another file's key (T-45-05). Clearing an absent row is a clean
    no-op (still 200). The endpoint writes no FileState -- fingerprinting has no dedicated
    terminal state; clearing the ledger row is the sole required control-side effect.
    """
    await clear_ledger_entry(session, f"fingerprint_file:{file_id}")
    await session.commit()
    # Touch ``agent`` so ARG001 doesn't fire; the binding's real role is auth-gating.
    _ = agent.id
    return FingerprintFailureResponse(agent_id=agent.id, file_id=file_id, cleared=True)
