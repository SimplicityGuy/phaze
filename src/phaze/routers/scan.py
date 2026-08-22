"""POST /pipeline/scans -- the operator-facing scan trigger (Phase 27 D-05..D-08).

Split out of `routers/pipeline_scans.py` by phaze-bk9el.17: that module carried the
trigger *and* every read/render/delete endpoint on the same prefix. This module is the
write path only; `pipeline_scans.py` keeps the HTMX poll partial, the recent-scans
table, the agent-roots swap and the delete endpoint. The code below is a verbatim move
-- no behaviour, ordering, message or status changed.

The operator picks an agent + scan_root + optional subpath from the Trigger Scan card on
the `/pipeline/` dashboard, and `trigger_scan`:

1. Validates the form server-side (T-27-03): joins root + subpath, NFC-normalizes,
   rejects literal `..` as a path *component* (see `_normalize_and_validate_scan_path`),
   enforces prefix-against `agent.scan_roots`, and verifies the agent is not revoked.
2. Creates a RUNNING `ScanBatch` row.
3. Enqueues `scan_directory(scan_path, batch_id)` via the lifespan-wired
   `AgentTaskRouter.enqueue_for_agent` (Phase 26 D-19) to the chosen agent's
   per-agent SAQ queue.
4. Returns the in-progress `scan_progress_card.html` markup for HTMX swap into
   `#scan-submit-result`.

`_normalize_and_validate_scan_path` and `_authorize_scan_root` are the two halves of a
security boundary -- together they decide which directory of the archive a scan may walk.
Their pin is `tests/shared/routers/pipeline_scans/test_authorization_characterization.py`.
"""

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
import unicodedata
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.response_shape import RENDERABLE_ALERT_STATUS
from phaze.schemas.agent_tasks import ScanDirectoryPayload
from phaze.schemas.pipeline_scans import TriggerScanForm
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.pg_text import contains_pg_invalid_chars


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/pipeline/scans", tags=["pipeline"])


def _render_scan_alert(request: Request, error_message: str) -> HTMLResponse:
    """Render `scan_submit_error.html` -- THE swappable-alert envelope for every
    `trigger_scan` failure branch (phaze-u1gf, `routers/response_shape.py` rule 3).

    Every failure below is a *renderable alert*, not a 4xx/5xx: the Trigger Scan form
    posts with `hx-target="#scan-submit-result" hx-swap="innerHTML"`, so there is a
    swap target the operator is looking at right now -- which is exactly the test in
    contract rule 4 that separates this module from `request_guards` rule 1's 422.
    htmx 2.x's default `responseHandling` maps `[45]..` to `{swap: false, error: true}`,
    so a non-2xx status here would mean the `role="alert"` card was fetched and then
    DISCARDED: the operator would see the spinner flash, an empty `#scan-submit-result`,
    and no indication the scan was rejected. The error semantics live in the BODY
    (`role="alert"` + prose), not the status line -- hence
    :data:`~phaze.routers.response_shape.RENDERABLE_ALERT_STATUS` (200) here. Note this
    is NOT "errors are 200" in general -- a genuinely unintelligible envelope (e.g. a
    missing `agent_id` form field) is still FastAPI's own 422, because there is no
    meaningful answer to render into anything; that case never reaches this helper.
    """
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_submit_error.html",
        context={"request": request, "error_message": error_message},
        status_code=RENDERABLE_ALERT_STATUS,
    )


def _normalize_and_validate_scan_path(request: Request, form: TriggerScanForm) -> HTMLResponse | str:
    """Phase 27 D-06 + T-27-03, ordered: join, NUL-reject, ``..``-reject, THEN canonicalize.

    The order matters and must not change:

    1. NFC-normalize the joined `scan_root + '/' + subpath` (Pitfall 3).
    2. Reject NUL (U+0000) / lone surrogates in the joined path (phaze-jpji): PostgreSQL
       cannot store these in a UTF8 text column and none of the layers below happen to
       catch them. Reject explicitly rather than sanitize -- silently stripping the NUL
       (services.pg_text.sanitize_pg_text) would point the scan at a DIFFERENT
       filesystem path than the operator typed, which is worse than refusing outright.
    3. Reject literal ``..`` as a path *component*, not a substring (WR-01). The simple
       ``".." in joined`` rejected any legitimate filename containing the literal
       substring ``..`` (e.g., ``"...thinking.mp3"``, ``"Album...Live"``,
       ``"..notes/file.mp3"``). Splitting on path separators and asserting that no
       component is exactly ``..`` blocks the intended traversal pattern
       (``../../etc/passwd``) without those false-positives.
    4. Canonicalize (phaze-0wme) -- AFTER the ``..`` rejection above, never before:
       `PurePosixPath` would silently resolve ``..`` components if it ran first, making
       traversal invisible. `str(PurePosixPath(joined))` drops a trailing slash,
       duplicate internal slashes, and ``.`` components, so every spelling of the same
       directory (`"2026"`, `"2026/"`, `"/2026//"`, `"./2026"`) collapses to one string.
       This canonical value -- not the pre-canonical join -- is what feeds the prefix
       check, `ScanBatch.scan_path`, and therefore
       `uq_scan_batches_agent_id_scan_path_running` (migration 044, phaze-1a71): all
       three must agree on one canonical string or the partial-unique duplicate-dispatch
       guard can be bypassed by a differently-spelled resubmit of the same path.

    Returns the canonical joined path, or a rendered alert on rejection.
    """
    joined_raw = f"{form.scan_root.rstrip('/')}/{form.subpath.lstrip('/')}" if form.subpath else form.scan_root
    joined = unicodedata.normalize("NFC", joined_raw)

    if contains_pg_invalid_chars(joined):
        return _render_scan_alert(request, "Subpath must not contain NUL bytes or invalid Unicode.")

    if ".." in PurePosixPath(joined).parts:
        return _render_scan_alert(request, "Subpath must not contain '..' path traversal.")

    return str(PurePosixPath(joined))


async def _authorize_scan_root(request: Request, session: AsyncSession, form: TriggerScanForm, joined: str) -> HTMLResponse | Agent:
    """Look up the agent and authorize `joined` against its configured `scan_roots`.

    Server-side authoritative gate even though the dropdown filters revoked agents
    client-side (defensive per threat model "Revoked agent attempting to be selected
    via direct POST").

    phaze-g0if + phaze-0wme: `joined` is already NFC-normalized and canonicalized, but
    `agent.scan_roots` is stored verbatim -- nothing upstream (TriggerScanForm, the
    CLI's `add_agent`, or the JSONB column itself) normalizes it. A root configured in a
    non-NFC form (NFD is the norm for paths sourced from an HFS+/macOS agent, e.g.
    "Café" decomposed as "Cafe" + combining acute) or with a trailing slash (e.g.
    "/archive/") then byte-differs from its own normalized/collapsed self, so a raw
    membership/prefix check can spuriously fail for a legitimately configured,
    un-traversed scan. Normalize + canonicalize every `r` (and `scan_root` itself) the
    same way `joined` was, so the membership gate, the prefix gate, and the persisted
    `scan_path` all agree on one normalization form.

    WR-05: the form-submitted ``scan_root`` MUST itself be one of the agent's configured
    ``scan_roots`` (literal membership) -- previously only the joined
    ``scan_root + '/' + subpath`` was validated against the prefix list, which allowed a
    partial match like ``scan_root="/data"`` + ``subpath="music/foo"`` to authorize
    ``/data/music/foo`` even though ``/data`` itself was never configured.

    D-06 prefix validation then confirms `joined` matches (or descends from) one of the
    agent's configured scan_roots, stripping the trailing slash on roots so
    `"/data/music"` matches both `"/data/music"` and `"/data/music/2026"`.

    Returns the authorized `Agent`, or a rendered alert on rejection.
    """
    agent = await session.get(Agent, form.agent_id)
    if agent is None or agent.revoked_at is not None:
        return _render_scan_alert(request, "Unknown or revoked agent.")

    scan_root_nfc = str(PurePosixPath(unicodedata.normalize("NFC", form.scan_root)))
    scan_roots_nfc = [str(PurePosixPath(unicodedata.normalize("NFC", r))) for r in agent.scan_roots]

    if scan_root_nfc not in scan_roots_nfc:
        return _render_scan_alert(request, "Selected scan root is not configured for this agent.")

    if not any(joined == r or joined.startswith(r.rstrip("/") + "/") for r in scan_roots_nfc):
        return _render_scan_alert(request, "Resolved path is outside the selected scan root.")

    return agent


async def _insert_running_scan_batch(request: Request, session: AsyncSession, form: TriggerScanForm, joined: str) -> HTMLResponse | ScanBatch:
    """Create + commit a RUNNING ScanBatch (D-08 + D-14), translating the duplicate guard.

    phaze-1a71: the insert is guarded by `uq_scan_batches_agent_id_scan_path_running`
    (migration 044) -- the durable, race-safe duplicate-dispatch guard. A read-then-insert
    check on the Python side is a TOCTOU (two concurrent submits can both pass a read
    before either commits), so the database enforces "at most one RUNNING batch per
    (agent_id, scan_path)" atomically at insert time instead. A double submit (a slow
    first request re-clicked, or a re-submit an hour into a scan that looks stalled)
    fails HERE with an `IntegrityError`, which this translates into the same alert shape
    as every other rejection rather than dispatching a second concurrent, unbounded full
    SHA-256 archive walk of the same tree. Do not replace this with a racy pre-check.

    phaze-266lc: no ``session.refresh(batch)`` after commit. The sessionmaker is
    ``expire_on_commit=False`` (database.py), so ``batch``'s attributes already survive
    the commit without a refresh -- refreshing was redundant and its sole effect was to
    autobegin a NEW transaction on this session, which then sat idle-in-transaction
    across the enqueue call that follows (the phaze-1v37 pool-drain class: PgBouncer
    SESSION mode pins an upstream server slot per checkout, and a long-open idle
    transaction also holds back the vacuum xmin horizon).

    Returns the committed `ScanBatch`, or a rendered alert if the duplicate guard fired.
    """
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=form.agent_id,
        scan_path=joined,
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
        # PR4: a freshly-created RUNNING batch starts with a heartbeat so the
        # stall reaper does not immediately consider it stalled.
        last_progress_at=datetime.now(UTC),
    )
    session.add(batch)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _render_scan_alert(request, "A scan is already running for this path.")
    return batch


async def _mark_scan_batch_failed_and_alert(request: Request, session: AsyncSession, batch: ScanBatch) -> HTMLResponse:
    """Mark `batch` FAILED after a definite (non-ambiguous) enqueue failure and render the alert.

    Split out of `_enqueue_scan_or_mark_failed`'s `except Exception` arm (WR-06): the batch write
    is wrapped in its own try/except so a secondary commit failure (e.g. Postgres also down) still
    produces the alert envelope instead of escaping the handler -- see that function's docstring
    for the full "definite failure" rationale. Pure extraction, no behavior change.
    """
    batch.status = ScanStatus.FAILED.value
    batch.error_message = "controller could not enqueue scan to agent worker"
    try:
        await session.commit()
    except Exception:
        # Don't let a rollback-commit failure escape the handler; the
        # operator's alert envelope is more important than the orphan-row
        # cleanup, and we already logged the original cause above.
        logger.exception("scan trigger: secondary commit failed for batch=%s", batch.id)
        await session.rollback()
    return _render_scan_alert(request, "The application server could not enqueue the scan. Try again in a moment.")


async def _enqueue_scan_or_mark_failed(
    request: Request,
    session: AsyncSession,
    batch: ScanBatch,
    form: TriggerScanForm,
    joined: str,
) -> HTMLResponse | None:
    """Enqueue `scan_directory` via AgentTaskRouter (Phase 26 D-19); reconcile failure.

    scan_directory is a long-running BULK archive walk (a full SHA-256 walk of a large
    network-mounted archive legitimately takes 1-2h). A fixed SAQ wall-clock timeout
    would kill a healthy, progressing scan and -- because SAQ retries restart the job
    FROM SCRATCH with no checkpoint -- loop forever, so a full scan could never
    complete. Disable the wall-clock timeout (timeout=0 -> wait_for(..., None)
    unbounded; Job.stuck stays False so the saq sweep never reaps it either) and disable
    retries (a restart-from-zero retry is wasteful; the operator re-triggers). Liveness
    is enforced by the progress-based stall reaper (config.scan_stall_seconds).

    Two failure shapes, both handled without letting an exception escape the handler:

    - **Ambiguous** (`AmbiguousEnqueueError`, phaze-0dfj4): raised AFTER the broker
      connection was already live -- the ``saq_jobs`` INSERT may already be durably
      committed even though this call never got its ack (a connection drop AFTER the
      server-side commit is the textbook in-doubt transaction). Marking the batch FAILED
      here would be a lie the operator acts on: they re-trigger, the uq constraint only
      covers RUNNING rows so a second batch + job is created, and the phantom first job
      ALSO dequeues -- walking the archive tree concurrently with the real scan before
      crash-looping on its first PATCH against the now-terminal batch. Leave the batch
      RUNNING instead (it was already committed RUNNING) and fall through to the same
      progress-card render a confirmed-successful enqueue gets: a genuinely-lost enqueue
      then has no agent that will ever report progress, and the progress-based stall
      reaper resolves it the same way it resolves an agent that silently died mid-scan --
      the same non-terminal contract phaze-9f82r established for an ambiguous tag-write
      enqueue.
    - **Definite** (any other `Exception`): mark the batch FAILED. WR-06: previously the
      failure path called ``session.delete(batch)`` + ``session.commit()``, but if THAT
      also raised (same network issue that broke the enqueue could have taken Postgres
      out, or the session was now in a tainted state), the exception escaped the handler
      -- FastAPI returned a generic 500 (losing the documented copy) and the orphan
      RUNNING ScanBatch row stayed visible to Recent Scans forever (no agent would ever
      PATCH it because nothing was enqueued). Marking FAILED instead of deleting is more
      honest: the operator sees a FAILED row in Recent Scans with a clear
      error_message. The secondary commit is wrapped in its own try/except so a
      Postgres-down scenario still produces the alert envelope instead of bubbling to a
      500 -- the operator's alert is more important than the orphan-row cleanup, and the
      original cause is already logged.

    Returns None on success (including the ambiguous case, batch left RUNNING), or a
    rendered alert on definite failure.
    """
    try:
        await request.app.state.task_router.enqueue_for_agent(
            agent_id=form.agent_id,
            task_name="scan_directory",
            payload=ScanDirectoryPayload(scan_path=joined, batch_id=batch.id, agent_id=form.agent_id),
            timeout=0,
            retries=0,
        )
    except AmbiguousEnqueueError:
        logger.error(
            "scan trigger: enqueue ambiguous for batch=%s -- broker connection was live, job may "
            "have landed; leaving batch RUNNING for the stall reaper to resolve",
            batch.id,
            exc_info=True,
        )
        return None
    except Exception:
        logger.exception("scan trigger: enqueue failed for batch=%s; marking FAILED", batch.id)
        return await _mark_scan_batch_failed_and_alert(request, session, batch)
    return None


@router.post("", response_class=HTMLResponse)
async def trigger_scan(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    # phaze-oldp: mirrors the sibling agent_roots_swap's `Query(pattern=..., max_length=128)`
    # (agent-roots swap target above), which is itself the wire mirror of the
    # `CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'")` on Agent.id (models/agent.py). Without
    # a signature-level bound, a NUL-bearing agent_id (never a valid id -- it can never match the
    # constraint) reached `session.get(Agent, form.agent_id)` below unfiltered and 500'd: Postgres
    # cannot bind NUL in a UTF8 text comparison at all, even to fail to match. A stricter
    # signature rejects it as a 422 at the boundary instead (this handler's own docstring carves
    # a 422 out for "a genuinely unintelligible envelope" -- an id that can never denote a real
    # agent is exactly that, unlike scan_root/subpath which stay free-form Form() since they are
    # validated against agent.scan_roots content, not a charset).
    agent_id: Annotated[str, Form(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)],
    scan_root: Annotated[str, Form()],
    subpath: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Form submit: validate, create ScanBatch, enqueue `scan_directory`.

    A thin coordinator over the ordered phases below -- each phase either returns the
    canonical value the next phase needs, or a rendered `scan_submit_error.html` alert
    that short-circuits the request:

    1. :func:`_normalize_and_validate_scan_path` -- NFC-normalize, reject NUL/invalid
       Unicode (phaze-jpji), reject `..` traversal (WR-01), canonicalize (phaze-0wme).
    2. :func:`_authorize_scan_root` -- look up the agent (reject missing/revoked),
       enforce literal `scan_root` membership (WR-05) and D-06 prefix containment.
    3. :func:`_insert_running_scan_batch` -- create + commit a RUNNING ScanBatch,
       translating the partial-unique duplicate guard (phaze-1a71) into an alert.
    4. :func:`_enqueue_scan_or_mark_failed` -- enqueue `scan_directory`, reconciling an
       ambiguous enqueue (leave RUNNING) vs a definite failure (mark FAILED).

    On success: render `scan_progress_card.html` in RUNNING state for HTMX swap into
    `#scan-submit-result`. See :func:`_render_scan_alert` for the STATUS CONTRACT
    governing every failure branch (phaze-u1gf, `routers/response_shape.py` rule 3).
    """
    form = TriggerScanForm(agent_id=agent_id, scan_root=scan_root, subpath=subpath)

    joined = _normalize_and_validate_scan_path(request, form)
    if isinstance(joined, HTMLResponse):
        return joined

    agent = await _authorize_scan_root(request, session, form, joined)
    if isinstance(agent, HTMLResponse):
        return agent

    batch = await _insert_running_scan_batch(request, session, form, joined)
    if isinstance(batch, HTMLResponse):
        return batch

    enqueue_alert = await _enqueue_scan_or_mark_failed(request, session, batch, form, joined)
    if enqueue_alert is not None:
        return enqueue_alert

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_progress_card.html",
        context={
            "request": request,
            "batch": batch,
            "agent_name": agent.name,
            "elapsed_seconds": 0,
            # Freshly-created batch: it just stamped last_progress_at, so it is
            # 0s since progress and never stalled.
            "seconds_since_progress": 0,
            "is_stalled": False,
        },
    )
