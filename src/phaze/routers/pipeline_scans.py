"""POST /pipeline/scans (admin trigger) + GET /pipeline/scans/{batch_id} (HTMX poll) + GET /pipeline/scans/agent-roots (agent-dropdown swap) -- Phase 27 D-05..D-08.

This is the operator-facing admin router (Wave 3) that closes SCAN-01. The operator
picks an agent + scan_root + optional subpath from the Trigger Scan card on the
`/pipeline/` dashboard, and this handler:

1. Validates the form server-side (T-27-03): joins root + subpath, NFC-normalizes,
   rejects literal `..` (mirrors `routers/scan.py:41`), enforces prefix-against
   `agent.scan_roots`, and verifies the agent is not revoked.
2. Creates a RUNNING `ScanBatch` row.
3. Enqueues `scan_directory(scan_path, batch_id)` via the lifespan-wired
   `AgentTaskRouter.enqueue_for_agent` (Phase 26 D-19) to the chosen agent's
   per-agent SAQ queue.
4. Returns the in-progress `scan_progress_card.html` markup for HTMX swap into
   `#scan-submit-result`.

The poll endpoint (`GET /pipeline/scans/{batch_id}`) returns the same template
keyed off `batch.status`; terminal-state markup OMITS `hx-trigger`/`hx-get` so the
HTMX `outerHTML` swap halts polling automatically (Pitfall 6).

The agent-roots swap (`GET /pipeline/scans/agent-roots`) re-renders the
`scan_path_picker.html` partial with the chosen agent's `scan_roots` jsonb entries
populated; missing/revoked/empty agents render the yellow-surface empty state.
"""

from datetime import UTC, datetime
import logging
from pathlib import Path, PurePosixPath
from typing import Annotated
import unicodedata
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.schemas.agent_tasks import ScanDirectoryPayload
from phaze.schemas.pipeline_scans import TriggerScanForm


logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/pipeline/scans", tags=["pipeline"])


def _elapsed_seconds(batch: ScanBatch) -> int:
    """Compute integer seconds elapsed since `batch.created_at`.

    `TimestampMixin.created_at` is server-side naive UTC (`func.now()` without
    timezone() wrapping), so we compute "now" via `datetime.now(UTC)` and
    strip the tzinfo to keep the comparison consistent. `created_at` is
    NOT NULL at the ORM layer (Mapped[datetime] without `| None`), so no
    None branch is needed.
    """
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    return int((now_naive - batch.created_at).total_seconds())


@router.get("/agent-roots", response_class=HTMLResponse)
async def agent_roots_swap(
    request: Request,
    agent_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """HTMX swap target: render `scan_path_picker.html` for the chosen agent.

    Empty/missing/revoked agents render the yellow-surface empty state
    (UI-SPEC §"Empty scan_roots case" lines 245-250).
    """
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.revoked_at is not None or not agent.scan_roots:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_path_picker.html",
            context={"request": request, "agent": None},
        )
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_path_picker.html",
        context={"request": request, "agent": agent},
    )


@router.get("/{batch_id}", response_class=HTMLResponse)
async def scan_progress(
    request: Request,
    batch_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """HTMX poll endpoint: return `scan_progress_card.html`.

    The template branches on `batch.status`; terminal-state markup OMITS
    `hx-trigger`/`hx-get` so HTMX halts polling automatically (Pitfall 6).
    """
    batch = await session.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
    agent = await session.get(Agent, batch.agent_id)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_progress_card.html",
        context={
            "request": request,
            "batch": batch,
            "agent_name": agent.name if agent is not None else batch.agent_id,
            "elapsed_seconds": _elapsed_seconds(batch),
        },
    )


@router.post("", response_class=HTMLResponse)
async def trigger_scan(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent_id: Annotated[str, Form()],
    scan_root: Annotated[str, Form()],
    subpath: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Form submit: validate, create ScanBatch, enqueue `scan_directory`.

    Validation layers (T-27-03):
    1. NFC-normalize the joined `scan_root + '/' + subpath` (Pitfall 3).
    2. Reject literal `..` (mirrors `routers/scan.py:41`).
    3. Look up the agent; reject if missing or revoked.
    4. Enforce prefix-against `agent.scan_roots` (D-06).

    On success: create a RUNNING ScanBatch, enqueue `scan_directory`, return
    the in-progress `scan_progress_card.html` for HTMX swap.

    On enqueue failure: rollback the just-created batch and return 503 +
    `scan_submit_error.html` (UI-SPEC failure-surfacing copy).
    """
    form = TriggerScanForm(agent_id=agent_id, scan_root=scan_root, subpath=subpath)

    # Phase 27 D-06 + T-27-03: join, NFC-normalize, reject ".." traversal.
    #
    # WR-01: check ".." as a path *component*, not a substring. The simple
    # ``".." in joined`` rejected any legitimate filename containing the literal
    # substring ``..`` (e.g., ``"...thinking.mp3"``, ``"Album...Live"``,
    # ``"..notes/file.mp3"``). Splitting on path separators and asserting that
    # no component is exactly ``..`` blocks the intended traversal pattern
    # (``../../etc/passwd``) without false-positives on triple-dot filenames.
    joined_raw = f"{form.scan_root.rstrip('/')}/{form.subpath.lstrip('/')}" if form.subpath else form.scan_root
    joined = unicodedata.normalize("NFC", joined_raw)
    if ".." in PurePosixPath(joined).parts:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_submit_error.html",
            context={"request": request, "error_message": "Subpath must not contain '..' path traversal."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Lookup agent; reject unknown/revoked. Server-side authoritative gate even
    # though the dropdown filters revoked agents client-side (defensive per
    # threat model "Revoked agent attempting to be selected via direct POST").
    agent = await session.get(Agent, form.agent_id)
    if agent is None or agent.revoked_at is not None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_submit_error.html",
            context={"request": request, "error_message": "Unknown or revoked agent."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # WR-05: the form-submitted ``scan_root`` MUST itself be one of the agent's
    # configured ``scan_roots``. Previously only the joined ``scan_root + '/' +
    # subpath`` was validated against the prefix list, which allowed a partial
    # match like ``scan_root="/data"`` + ``subpath="music/foo"`` to authorize
    # ``/data/music/foo`` even though ``/data`` itself was never configured. The
    # planning invariant documents ``scan_root rejected when not in selected
    # agent's scan_roots``; tighten the check to require literal membership.
    if form.scan_root not in agent.scan_roots:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_submit_error.html",
            context={"request": request, "error_message": "Selected scan root is not configured for this agent."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # D-06 prefix validation: joined path must match (or descend from) one of
    # the agent's configured scan_roots. Strip trailing slash on roots so
    # `"/data/music"` matches both `"/data/music"` and `"/data/music/2026"`.
    if not any(joined == r or joined.startswith(r.rstrip("/") + "/") for r in agent.scan_roots):
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_submit_error.html",
            context={"request": request, "error_message": "Resolved path is outside the selected scan root."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Create RUNNING ScanBatch (D-08 + D-14).
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=form.agent_id,
        scan_path=joined,
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    # Enqueue scan_directory via AgentTaskRouter (Phase 26 D-19). On enqueue
    # failure: mark the batch FAILED and return 503 with the documented copy.
    #
    # WR-06: previously the failure path called ``session.delete(batch)`` +
    # ``session.commit()``, but if THAT also raised (same network issue that
    # broke the enqueue could have taken Postgres out, or the session was now
    # in a tainted state), the exception escaped the handler -- FastAPI
    # returned a generic 500 (losing the documented 503 copy) and the orphan
    # RUNNING ScanBatch row stayed visible to Recent Scans forever (no agent
    # would ever PATCH it because nothing was enqueued).
    #
    # Switch to "mark FAILED" instead of "delete". The operator now sees a
    # FAILED row in Recent Scans with a clear error_message, which is more
    # honest than silently deleting evidence of the attempt. Wrap the secondary
    # commit in its own try/except so a Postgres-down scenario still produces
    # the 503 envelope instead of bubbling to a 500.
    try:
        await request.app.state.task_router.enqueue_for_agent(
            agent_id=form.agent_id,
            task_name="scan_directory",
            payload=ScanDirectoryPayload(scan_path=joined, batch_id=batch.id, agent_id=form.agent_id),
        )
    except Exception:
        logger.exception("scan trigger: enqueue failed for batch=%s; marking FAILED", batch.id)
        batch.status = ScanStatus.FAILED.value
        batch.error_message = "controller could not enqueue scan to agent worker"
        try:
            await session.commit()
        except Exception:
            # Don't let a rollback-commit failure escape the handler; the
            # operator's 503 envelope is more important than the orphan-row
            # cleanup, and we already logged the original cause above.
            logger.exception("scan trigger: secondary commit failed for batch=%s", batch.id)
            await session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_submit_error.html",
            context={"request": request, "error_message": "The application server could not enqueue the scan. Try again in a moment."},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Render scan_progress_card.html in RUNNING state for HTMX swap.
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_progress_card.html",
        context={
            "request": request,
            "batch": batch,
            "agent_name": agent.name,
            "elapsed_seconds": 0,
        },
    )
