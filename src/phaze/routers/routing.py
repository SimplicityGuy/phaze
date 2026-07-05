"""Force-local master routing override -- thin write endpoint (Phase 71, BEUI-02).

A single POST that flips the durable ``route_control`` ``'global'`` row's ``force_local`` flag and
returns the re-rendered header pill (swapped in place) plus an OOB polite-aria-live toast. It is the
WRITE surface for the routing override whose MECHANISM (the control row + degrade-safe reader
``phaze.services.route_control.get_route_control`` + the two routing gates) shipped in Plan 02;
engaging it makes every routing path behave like an all-local registry with no redeploy, and it is
fully reversible in one click (D-08).

Mirrors the ``pipeline_stages`` thin-endpoint discipline EXACTLY: load-or-defensively-create the
control row, mutate, commit in a SINGLE transaction, then return the partial the UI swaps in place.

Security (threat model):
- T-71-07 (Tampering / input): ``engage`` is ``Annotated[bool, Form()]`` -- boolean-coerced, no
  free-text; the DB column defaults false. No app-layer auth (T-37-04 internal realm, the same
  reverse-proxy trust boundary as the rest of ``/pipeline/*`` and the per-stage pause/resume controls).
- T-71-10 (state lie): the returned pill state comes from the JUST-COMMITTED row, never an optimistic
  client value, so a failed write never leaves the pill lying about routing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from phaze.database import get_session
from phaze.models.route_control import RouteControl


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pipeline"])

# UI-SPEC BEUI-02 confirmation copy carried by the OOB polite-aria-live toast (engage vs revert).
_ENGAGE_TOAST = "Routing forced to LOCAL — cloud & Kueue backends bypassed."
_REVERT_TOAST = "Cloud routing restored — backends dispatch by rank."


@router.post("/pipeline/routing/force-local", response_class=HTMLResponse)
async def force_local(
    request: Request,
    engage: Annotated[bool, Form()],
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Flip the ``'global'`` force-local row to ``engage`` and return the re-rendered pill + toast.

    ``engage`` is a boolean form field (V5 boolean coercion -- no free-text; T-71-07). The row is
    loaded or defensively created (Migration seeds it in production, but a fresh / partially-migrated
    DB must not 500 the first toggle), mutated, and committed in one transaction. The returned pill
    reflects the JUST-COMMITTED state (authoritative, never optimistic -- T-71-10).
    """
    row = await session.get(RouteControl, "global")
    if row is None:
        row = RouteControl(id="global", force_local=False)
        session.add(row)
    row.force_local = engage
    await session.commit()
    return templates.TemplateResponse(
        request=request,
        name="shell/partials/_force_local_pill.html",
        context={
            "force_local": row.force_local,
            "toast_message": _ENGAGE_TOAST if row.force_local else _REVERT_TOAST,
        },
    )
