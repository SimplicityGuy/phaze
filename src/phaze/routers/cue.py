"""CUE sheet management UI router -- per-file generation, plus a legacy bookmark redirect.

phaze-y4s6: the standalone CUE management LIST page (``GET /cue/`` fragment branch,
``cue/partials/cue_list.html``) and the batch-generate action (``POST /cue/generate-batch``,
which rendered the same list) had no live caller left post-v7-cutover -- the live Cue workspace
(``pipeline/partials/cue_workspace.html``) renders its cards inline with no list/pagination UI and
no bulk-generate control. Both were deleted; ``GET /cue/`` now only resolves the legacy bookmark
into the shell (SHELL-05).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.schemas.agent_tasks import WriteCueSheetPayload
from phaze.services import cue_review
from phaze.services.cue_generator import CueTrackData, generate_cue_content
from phaze.services.stage_status import is_applied


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/cue", tags=["cue"])


# phaze-b4u3p: the eligible-tracklist query surface (display order, base SELECT, and the
# paginated/limited reader) moved to ``services/cue_review.py`` -- it was reached into directly by
# ``services/review.py`` (a service importing this router's underscore-prefixed helpers, a
# layering inversion) and shared its join/filter core near-verbatim with the "gated" statement
# review.py built independently, which was the 14-line clone repowise flagged between the two
# files. Re-imported here under the SAME names so this router's own routes, and the existing
# white-box tests that import them via ``phaze.routers.cue``, are unaffected.
_ELIGIBLE_DISPLAY_ORDER = cue_review.ELIGIBLE_DISPLAY_ORDER
_eligible_tracklist_stmt = cue_review.eligible_tracklist_stmt
_get_eligible_tracklist_query = cue_review.get_eligible_tracklist_query


# phaze-6bkk / phaze-9dwb: ``_get_cue_version`` lived here and did ``base_cue.exists()`` plus a full
# ``parent.iterdir()`` regex sweep of the archive directory, synchronously on the API event loop.
# It was wrong on three counts and is gone rather than fixed in place:
#   1. DIST-01 -- the api container mounts no media, so it could only ever report 0.
#   2. Redundant -- ``next_cue_path_and_version`` already computes the number during the write; this
#      re-walked the same directory microseconds later purely to recover it. The version now travels
#      back with the write, agent-side.
#   3. On-loop blocking I/O -- an O(directory entries) syscall sweep of a concert-set directory
#      (thousands of siblings), and on the error path it re-scanned a directory already known to be
#      misbehaving. There is no filesystem call left in this router at all.


async def _build_cue_tracks(
    session: AsyncSession,
    version_id: uuid.UUID,
) -> list[CueTrackData]:
    """Build CueTrackData list from a tracklist version's tracks + Discogs links.

    phaze-b4u3p: a single-version call into :func:`phaze.services.cue_review.build_cue_tracks_for_versions`
    -- the batched form of this exact query pair, built to fix the cross-function N+1
    ``services.review.get_cue_review_cards`` had calling this once per eligible tracklist. Kept as
    a named, single-version wrapper here (rather than inlining the batched call at each of this
    router's three single-tracklist call sites) so their call shape is unchanged.
    """
    return (await cue_review.build_cue_tracks_for_versions(session, [version_id])).get(version_id, [])


async def _load_tracklist_with_file(session: AsyncSession, tracklist_id: uuid.UUID) -> tuple[Tracklist | None, FileRecord | None]:
    """Load tracklist joined with file record."""
    stmt = select(Tracklist, FileRecord).join(FileRecord, Tracklist.file_id == FileRecord.id, isouter=True).where(Tracklist.id == tracklist_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]


@router.get("/", response_class=RedirectResponse)
async def list_cue() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/cue/`` bookmark into the v7.0 shell.

    phaze-y4s6: this used to also serve an in-page HX-filtered/paginated list (rendering
    ``cue/partials/cue_list.html``), but the live v7.0 Cue workspace
    (``pipeline/partials/cue_workspace.html``) renders its cards inline from
    ``services.review.get_cue_review_cards`` with no pagination and never hx-gets this bare
    path -- there is no live caller left to preserve an HX-filter branch for (unlike the sibling
    ``/proposals/`` redirect). The dead list/pagination logic and its template were deleted
    outright.
    """
    return RedirectResponse(url="/s/cue", status_code=302)


@router.post("/{tracklist_id}/generate", response_class=HTMLResponse)
async def generate_cue(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    version_id: uuid.UUID | None = Form(None),
) -> HTMLResponse:
    """Generate a CUE file for a specific tracklist.

    phaze-ce65s: ``version_id`` is the tracklist version the OPERATOR reviewed in the preview
    card (carried back via the APPROVE button's ``hx-vals``, see ``_cue_preview.html``). It is
    optional -- the legacy ``cue/partials/cue_row.html`` surface has no live caller and never
    sends it, so omitting it preserves that branch's old always-latest behavior -- but when
    present it MUST match ``tracklist.latest_version_id`` or nothing is written; see
    :func:`_render_stale_version_response`.
    """
    tracklist, file_record = await _load_tracklist_with_file(session, tracklist_id)

    if tracklist is None:
        # phaze-bg1dk: htmx does not swap non-2xx responses, and the shell's only 404 rescue
        # handler is scoped to `#record-body` -- never `cue-card-*` -- so a bare 404 here produces
        # NO feedback on the v7 workspace card (silent no-op, hx-disabled-elt just re-enables the
        # button). Mirror tags.py's `_tagwrite_stale_toast_response`: a toast-only 200 whose empty
        # primary body lets htmx's outerHTML swap remove the stale card. The non-HX/legacy caller
        # (no `cue-card-` target) still gets the honest 404.
        hx_target = request.headers.get("HX-Target", "")
        if hx_target.startswith("cue-card-"):
            return _cue_stale_toast_response(request, "Tracklist not found -- it may have been removed.")
        return HTMLResponse(content="Tracklist not found", status_code=404)

    resolved = await _resolve_generate_target(request, session, tracklist, file_record, version_id)
    if isinstance(resolved, HTMLResponse):
        return resolved
    file_record = resolved.file_record

    cue_tracks = await _build_cue_tracks(session, resolved.version_id)

    # Validate at least one track has timestamps
    if not any(t.timestamp_seconds is not None for t in cue_tracks):
        toast_msg = "No tracks have timestamps. CUE sheets require per-track timing data from the tracklist source."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # phaze-6bkk (DIST-01): render the CUE text HERE -- it is pure string work over rows the api
    # already holds -- and dispatch the BYTES to the agent that owns the media mount. The api
    # container has none, so the previous in-process ``write_cue_file`` could only ever raise
    # FileNotFoundError on a parent directory that does not exist in this container.
    audio_path = Path(file_record.current_path)
    content = generate_cue_content(audio_path.name, file_record.file_type, cue_tracks)
    try:
        await request.app.state.task_router.enqueue_for_file(
            file_record=file_record,
            task_name="write_cue_sheet",
            payload=WriteCueSheetPayload(
                file_id=file_record.id,
                tracklist_id=tracklist.id,
                agent_id=file_record.agent_id,
                audio_path=file_record.current_path,
                content=content,
            ),
        )
    except Exception as exc:
        # The old advice ("Check filesystem permissions on the destination directory") was the exact
        # wrong diagnosis for the failure that actually fired in production -- the directory was not
        # mounted at all. A dispatch failure is a control-plane/broker problem, so say so.
        logger.warning("cue dispatch failed", tracklist_id=str(tracklist.id), agent_id=file_record.agent_id, exc_info=True)
        toast_msg = (
            f"Could not queue the CUE write for agent {file_record.agent_id}: {exc}. "
            "Check that the agent is registered and the task broker is reachable, then retry."
        )
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    toast_msg = (
        f"CUE sheet queued for {audio_path.stem} on agent {file_record.agent_id}. "
        "The file server writes it next to the audio file, versioning it if one already exists."
    )

    return await _render_generate_success(request, session, tracklist, file_record, audio_path, content, toast_msg)


async def _get_track_count(session: AsyncSession, version_id: uuid.UUID | None) -> int:
    """Count tracks for a tracklist version (0 if there is none)."""
    if not version_id:
        return 0
    count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == version_id))
    return count_result.scalar() or 0


@dataclass(frozen=True, slots=True)
class _GenerateTarget:
    """The validated subject of a ``/generate``: an applied file and the version under review."""

    file_record: FileRecord
    version_id: uuid.UUID


async def _resolve_generate_target(
    request: Request,
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
    version_id: uuid.UUID | None,
) -> _GenerateTarget | HTMLResponse:
    """The applied file + reviewed version a ``/generate`` may write for, or the refusal response.

    Three data-gap gates (file not executed, tracklist not approved, no version) plus the
    phaze-ce65s stale-version gate. Each refusal returns the surface the click targeted, carrying
    the reason as a toast -- never a bare non-2xx, which htmx would not swap.

    Returning the resolved pair rather than a bare None is what keeps ``file_record`` and
    ``latest_version_id`` NARROWED for the caller: these gates are the only proof either is
    non-None, so handing back just "no refusal" would silently discard that proof.
    """
    # Validate the file is applied (READ-05/D-01: an executed proposal exists, NOT files.state).
    if file_record is None or not await is_applied(session, file_record.id):
        toast_msg = "File must be executed before generating a CUE sheet. Run the pipeline to move the file to its destination."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Validate tracklist is approved
    if tracklist.status != "approved":
        toast_msg = "Tracklist must be approved before generating a CUE sheet."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Build CUE tracks
    if not tracklist.latest_version_id:
        toast_msg = "No tracks have timestamps. CUE sheets require per-track timing data from the tracklist source."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    if version_id is not None and version_id != tracklist.latest_version_id:
        # phaze-ce65s: a background re-scrape (tracklist_drain._append_version) moved
        # latest_version_id between the workspace render and this click -- the content the
        # operator reviewed is not the content `_build_cue_tracks` would build now. Refuse the
        # write (nothing is written without review, the same guarantee phaze-p35v enforced for
        # proposals) and hand back a FRESH preview of the current version instead.
        return await _render_stale_version_response(request, session, tracklist, file_record, tracklist.latest_version_id)

    return _GenerateTarget(file_record=file_record, version_id=tracklist.latest_version_id)


async def _render_generate_success(
    request: Request,
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord,
    audio_path: Path,
    content: str,
    toast_msg: str,
) -> HTMLResponse:
    """Render the surface a SUCCESSFUL ``/generate`` targeted, with the confirmation toast.

    Mirrors :func:`_render_generate_error`'s ``cue-card-`` / legacy split (phaze-js16): the v7
    workspace card re-renders ``_cue_preview.html`` with a fresh in-memory preview of the CUE
    now on its way to disk, while the legacy row surface keeps ``cue_row.html``.
    """
    # Return updated row + OOB toast
    track_count = await _get_track_count(session, tracklist.latest_version_id)

    # Detect which surface the request came from via HX-Target.
    hx_target = request.headers.get("HX-Target", "")

    # phaze-js16: the v7 cue-workspace card's APPROVE targets #cue-card-{id} -- mirror the
    # cue-card- branch _render_generate_error already has (phaze-2w49) so a SUCCESSFUL approve
    # re-renders the same _cue_preview.html card instead of falling through to the legacy
    # cue/partials/cue_row.html markup. The dispatch succeeded, so the card stays eligible with a
    # fresh in-memory preview of the CUE that is on its way to disk (no extra query -- `content` IS it).
    if hx_target.startswith("cue-card-"):
        card = {
            "tracklist_id": tracklist.id,
            "file_id": file_record.id,
            "set_name": audio_path.stem,
            "eligible": True,
            "build_error": False,
            "cue_text": content,
            # phaze-ce65s: pin the NEXT preview's approve to the version this content was
            # actually built from, so a subsequent stale click is caught the same way.
            "version_id": tracklist.latest_version_id,
        }
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_cue_preview.html",
            context={"request": request, "card": card, "toast_message": toast_msg},
        )

    row_data: dict[str, Any] = {
        "id": tracklist.id,
        "artist": tracklist.artist or "Unknown Artist",
        "event": tracklist.event or "",
        "date": tracklist.date,
        "track_count": track_count,
        # phaze-6bkk: the version is decided agent-side when the file is actually written, and the
        # api has no way to observe it (no mount, and the write has not happened yet at response
        # time). Reported as 0 -- "not known here" -- rather than a fabricated number.
        "cue_version": 0,
        "source": tracklist.source,
    }

    return templates.TemplateResponse(
        request=request,
        name="cue/partials/cue_row.html",
        context={
            "request": request,
            "tracklist": row_data,
            "toast_message": toast_msg,
        },
    )


async def _eligible_cue_text(session: AsyncSession, tracklist: Tracklist, file_record: FileRecord | None) -> str | None:
    """The rendered CUE text when ``tracklist`` is genuinely eligible right now, else None.

    This is the single-tracklist mirror of the eligibility rule in
    ``services.review.get_cue_review_cards``: a card is eligible only when the file exists and
    is applied, the tracklist is approved and versioned, and at least one track carries a
    timestamp. Returning the TEXT rather than a bool keeps "is it eligible" and "what would we
    render" from drifting apart -- they are the same computation, and a card that claims
    eligibility with no text is the stale-APPROVE-button bug phaze-2w49 fixed.
    """
    if file_record is None or tracklist.status != "approved" or not tracklist.latest_version_id:
        return None
    if not await is_applied(session, file_record.id):
        return None
    cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)
    if not any(t.timestamp_seconds is not None for t in cue_tracks):
        return None
    return generate_cue_content(Path(file_record.current_path).name, file_record.file_type, cue_tracks)


async def _build_generate_error_card(
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
) -> dict[str, Any]:
    """Rebuild the pipeline preview card's context after a failed generate (phaze-2w49).

    Mirrors the eligibility rule in ``services.review.get_cue_review_cards`` for a single
    tracklist so the re-rendered card reflects the tracklist's REAL current state: the write-
    failure branch stays eligible (a genuine retry, matching the error message's own promise),
    while a data-gap branch (not applied/approved/timestamped) renders the honest gated state
    instead of a stale APPROVE button.
    """
    cue_text = await _eligible_cue_text(session, tracklist, file_record)
    eligible = cue_text is not None

    return {
        "tracklist_id": tracklist.id,
        "file_id": file_record.id if file_record is not None else None,
        "set_name": Path(file_record.current_path).stem if file_record is not None else str(tracklist.id),
        "eligible": eligible,
        "build_error": False,
        "cue_text": cue_text,
        # phaze-ce65s: pin a retried APPROVE to the version THIS card was rebuilt from.
        "version_id": tracklist.latest_version_id if eligible else None,
    }


async def _render_generate_error(
    request: Request,
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
    message: str,
) -> HTMLResponse:
    """Re-render the surface a failed ``/generate`` targeted, with the error as an OOB toast.

    phaze-2w49: htmx's oobSwap strips the OOB toast element from the response fragment
    unconditionally, then runs the PRIMARY ``outerHTML`` swap against the now-empty remainder --
    with no empty-guard, ``swapOuterHTML`` inserts nothing and calls ``target.remove()``. A
    toast-only 200 therefore deletes the very card/row the toast is complaining about on either
    generate surface (the pipeline preview card or the cue row -- phaze-y4s6 removed the third,
    the tracklist card, along with the rest of the dead legacy tracklists UI). Every error branch
    must re-render its own primary content alongside the toast instead.
    """
    hx_target = request.headers.get("HX-Target", "")

    if hx_target.startswith("cue-card-"):
        card = await _build_generate_error_card(session, tracklist, file_record)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_cue_preview.html",
            context={"request": request, "card": card, "toast_message": message},
        )

    row_data: dict[str, Any] = {
        "id": tracklist.id,
        "artist": tracklist.artist or "Unknown Artist",
        "event": tracklist.event or "",
        "date": tracklist.date,
        "track_count": await _get_track_count(session, tracklist.latest_version_id),
        # phaze-9dwb: previously an unconditional archive ``exists()`` + ``iterdir()`` computed
        # BEFORE the cue-card branch below returned -- so on the only live surface the scan ran and
        # was discarded, and on the write-failure branch it re-scanned a directory already known to
        # be misbehaving, on the event loop. There is no filesystem here to scan any more.
        "cue_version": 0,
        "source": tracklist.source,
    }
    return templates.TemplateResponse(
        request=request,
        name="cue/partials/cue_row.html",
        context={"request": request, "tracklist": row_data, "toast_message": message},
    )


def _cue_stale_toast_response(request: Request, toast_message: str) -> HTMLResponse:
    """A cue-card whose tracklist has vanished entirely: OOB toast only, status 200 (phaze-bg1dk).

    Mirrors ``tags.py``'s ``_tagwrite_stale_toast_response`` (phaze-nvll defect 3): there is no
    tracklist left to rebuild a card from, so the response's main (non-OOB) body is empty --
    htmx's ``outerHTML`` swap then removes the stale card from the DOM -- while the toast still
    surfaces the failure, instead of the bare 404 htmx silently drops for a non-2xx status on this
    target (the shell's only 404 rescue handler is scoped to ``#record-body``).

    Unlike ``_render_generate_error`` (phaze-2w49), an EMPTY primary body is the desired outcome
    here, not a bug: the tracklist is genuinely gone, so there is nothing valid left to redraw.
    """
    return templates.TemplateResponse(
        request=request,
        name="cue/partials/toast.html",
        context={"request": request, "toast_message": toast_message},
    )


async def _build_cue_preview_card(
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord,
    version_id: uuid.UUID,
) -> dict[str, Any]:
    """Build a fresh eligible preview card for ``tracklist`` pinned to ``version_id``.

    Shared by :func:`_render_stale_version_response` (phaze-ce65s) so a stale-version refusal
    hands back the identical card shape a normal render would, pinned to the version it was
    actually built from. ``version_id`` is taken as an explicit non-optional argument (rather than
    read back off ``tracklist.latest_version_id``) so the caller's already-checked "is there a
    version at all" guard carries through instead of re-widening to ``UUID | None`` here.
    """
    cue_tracks = await _build_cue_tracks(session, version_id)
    audio_path = Path(file_record.current_path)
    content = generate_cue_content(audio_path.name, file_record.file_type, cue_tracks)
    return {
        "tracklist_id": tracklist.id,
        "file_id": file_record.id,
        "set_name": audio_path.stem,
        "eligible": True,
        "build_error": False,
        "cue_text": content,
        "version_id": version_id,
    }


async def _render_stale_version_response(
    request: Request,
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
    current_version_id: uuid.UUID,
) -> HTMLResponse:
    """phaze-ce65s: the submitted ``version_id`` no longer matches ``current_version_id``.

    A background re-scrape (``tracklist_drain._append_version``) moved the pointer between the
    workspace render and this APPROVE click, so the content the operator reviewed is not the
    content generation would build now -- writing it would bypass the product's core review gate
    (the same check-then-act shape ``phaze-p35v`` fixed for proposal approval). Refuse the write
    and hand back a FRESH preview of the CURRENT version instead, so the operator can review and
    re-approve rather than getting a bare failure with no path forward. ``current_version_id`` is
    taken explicitly (rather than re-read off ``tracklist.latest_version_id``) so the caller's
    already-checked "there IS a version" guard carries through without re-widening to optional.
    """
    message = "The tracklist changed since this preview was rendered -- review the refreshed sheet below and approve again."
    hx_target = request.headers.get("HX-Target", "")

    if hx_target.startswith("cue-card-") and file_record is not None:
        card = await _build_cue_preview_card(session, tracklist, file_record, current_version_id)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_cue_preview.html",
            context={"request": request, "card": card, "toast_message": message},
        )

    # No live caller sends `version_id` on the legacy row surface (or with no file_record at all)
    # -- degrade the same way the error branch does if one somehow did.
    return await _render_generate_error(request, session, tracklist, file_record, message)
