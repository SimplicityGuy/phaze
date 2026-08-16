"""CUE sheet management UI router -- per-file generation, plus a legacy bookmark redirect.

phaze-y4s6: the standalone CUE management LIST page (``GET /cue/`` fragment branch,
``cue/partials/cue_list.html``) and the batch-generate action (``POST /cue/generate-batch``,
which rendered the same list) had no live caller left post-v7-cutover -- the live Cue workspace
(``pipeline/partials/cue_workspace.html``) renders its cards inline with no list/pagination UI and
no bulk-generate control. Both were deleted; ``GET /cue/`` now only resolves the legacy bookmark
into the shell (SHELL-05).
"""

from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.schemas.agent_tasks import WriteCueSheetPayload
from phaze.services.cue_generator import CueTrackData, generate_cue_content, parse_timestamp_string
from phaze.services.stage_status import applied_clause, is_applied


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/cue", tags=["cue"])


# The CUE list's display order (phaze-hdho): alphabetically by artist/event.
#
# phaze-0jpe: the leading conjunct used to be ``(Tracklist.source == "fingerprint").desc()`` -- the
# D-02 / CUE-01 preference for putting audio-fingerprint-sourced tracklists first, because they
# carried per-track timestamps that scraped 1001tracklists rows often lacked. Fingerprinting is gone
# and no code path creates a ``source='fingerprint'`` row any more, so that conjunct now sorts on a
# constant and only obscures the real order. Whether the historical rows themselves are purged is a
# data question deliberately left to the removal runbook, NOT settled here -- and either answer
# leaves this ordering correct.
#
# BOTH ``Tracklist.artist`` and ``Tracklist.event``
# are nullable ``Text`` (models/tracklist.py), so a set of tracklists sharing a source and the same
# -- or NULL -- artist/event forms a tie group. This tuple alone is NOT a unique sort key; every
# caller MUST append a ``Tracklist.id`` tiebreaker (directly, or via :func:`paged_stmt`) before
# relying on it for anything that slices or pages the result set (paging contract rule 4).
_ELIGIBLE_DISPLAY_ORDER: tuple[Any, ...] = (
    Tracklist.artist,
    Tracklist.event,
)


def _eligible_tracklist_stmt() -> Select[Any]:
    """Build the base (UNORDERED) SELECT for approved tracklists with EXECUTED files that have >=1 timestamped track.

    Shared by every eligible-tracklist reader so the join/filter logic lives in exactly one place.
    Deliberately carries NO ``ORDER BY`` -- callers compose :data:`_ELIGIBLE_DISPLAY_ORDER` (+ the
    mandatory ``Tracklist.id`` tiebreaker) themselves, so it is applied exactly once per statement
    instead of risking a double-appended sort key.

    phaze-dboy: the timestamp-existence check is scoped to ``Tracklist.latest_version_id`` --
    NOT "any version ever" -- because that is the ONLY version actual generation ever reads
    (``_build_cue_tracks(session, tracklist.latest_version_id)`` in ``generate_cue``). A
    re-scrape can create a newer ``latest_version_id``
    whose tracks carry no timestamps while an OLDER version still has them; scoping this
    predicate to "any version" previously listed that tracklist as eligible with a Generate
    button that could never succeed (always "No tracks have timestamps"), permanently
    inflating ``eligible`` past ``generated`` with no way to converge.
    """
    # Subquery: TracklistVersion ids that have at least one track with a timestamp. Matched
    # against ``Tracklist.latest_version_id`` below (NOT ``Tracklist.id`` via ``TracklistVersion.
    # tracklist_id``) so this evaluates the SAME version generation reads -- see phaze-dboy above.
    has_timestamp_subq = select(TracklistTrack.version_id).where(TracklistTrack.timestamp.is_not(None)).distinct()

    return (
        select(Tracklist, FileRecord)
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(
            Tracklist.status == "approved",
            Tracklist.file_id.is_not(None),
            applied_clause(),
            Tracklist.latest_version_id.in_(has_timestamp_subq),
        )
    )


async def _get_eligible_tracklist_query(session: AsyncSession, *, limit: int | None = None) -> list[tuple[Tracklist, FileRecord]]:
    """Query approved tracklists with EXECUTED files that have at least one timestamped track.

    Ordered by :data:`_ELIGIBLE_DISPLAY_ORDER` with ``Tracklist.id`` appended as a tiebreaker so a
    caller-supplied ``limit`` (a bare SQL ``LIMIT``, no ``OFFSET``) is deterministic even when many
    rows tie on source/artist/event -- WITHOUT it, WHICH rows fall inside the cap could vary between
    executions (phaze-hdho).

    Pass ``limit`` to bound the result set at the SQL level. WR-03: the review-card consumer
    (``services.review.get_cue_review_cards``) passes ``limit=_MAX_REVIEW_ROWS`` so the DB never
    returns more than the render cap -- the eligible half is then genuinely memory-bounded, not just
    loop-capped after materializing every eligible pair. This is the ONLY live reader of the
    eligible set left in this router (phaze-y4s6 removed the dead ``list_cue`` / ``generate_batch``
    legacy list-page routes, whose only purpose was rendering the now-deleted
    ``cue/partials/cue_list.html``).
    """
    stmt = _eligible_tracklist_stmt().order_by(*_ELIGIBLE_DISPLAY_ORDER, Tracklist.id)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return list(result.tuples().all())


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
    """Build CueTrackData list from a tracklist version's tracks + Discogs links."""
    # Load tracks
    version_result = await session.execute(
        select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == version_id)
    )
    version = version_result.scalar_one_or_none()
    if not version:
        return []

    tracks = sorted(version.tracks, key=lambda t: t.position)
    track_ids = [t.id for t in tracks]

    # Load accepted Discogs links for these tracks
    discogs_by_track: dict[uuid.UUID, DiscogsLink] = {}
    if track_ids:
        discogs_stmt = select(DiscogsLink).where(
            DiscogsLink.track_id.in_(track_ids),
            DiscogsLink.status == "accepted",
        )
        discogs_result = await session.execute(discogs_stmt)
        for link in discogs_result.scalars().all():
            discogs_by_track[link.track_id] = link

    cue_tracks: list[CueTrackData] = []
    for track in tracks:
        ts = parse_timestamp_string(track.timestamp)
        discogs_link = discogs_by_track.get(track.id)
        cue_tracks.append(
            CueTrackData(
                position=track.position,
                title=track.title,
                artist=track.artist,
                timestamp_seconds=ts,
                genre=None,  # DiscogsLink has no genre field (D-09)
                label=discogs_link.discogs_label if discogs_link else None,
                year=discogs_link.discogs_year if discogs_link else None,
            )
        )

    return cue_tracks


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

    cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)

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


async def _get_track_count(session: AsyncSession, version_id: uuid.UUID | None) -> int:
    """Count tracks for a tracklist version (0 if there is none)."""
    if not version_id:
        return 0
    count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == version_id))
    return count_result.scalar() or 0


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
    eligible = False
    cue_text: str | None = None
    if file_record is not None and tracklist.status == "approved" and tracklist.latest_version_id and await is_applied(session, file_record.id):
        cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)
        if any(t.timestamp_seconds is not None for t in cue_tracks):
            eligible = True
            cue_text = generate_cue_content(Path(file_record.current_path).name, file_record.file_type, cue_tracks)

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
