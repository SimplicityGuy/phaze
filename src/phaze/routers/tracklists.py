"""Tracklists admin UI router -- legacy bookmark redirect only.

phaze-y4s6: the entire interactive tracklists admin UI (list/scan/search/link/discogs-match/
inline-edit fragments) was orphaned by the v7.0 shell cutover. The live Tracklist stage is now
served entirely by ``routers/pipeline.py`` (the ``tracklist_workspace.html`` step cards +
``GET /pipeline/tracklist-sets`` bounded per-set table); nothing in the live shell ever hx-gets
an endpoint under this prefix any more (confirmed render-path-by-render-path: the per-set table
rows navigate to ``/record/{file_id}``, and ``search/partials/palette_results.html`` explicitly
documents linking to the generic ``/s/tracklist`` stage instead of any per-tracklist page,
"since there is no per-tracklist page" any more).

What's kept: the SHELL-05 (D-03) bookmark-compatibility redirect -- a plain GET to this legacy
canonical route still resolves an old bookmark into the v7.0 shell in one hop, same as every
other cutover router (``proposals.py``, ``tags.py``, ``cue.py``, ``duplicates.py``). Everything
downstream of that redirect (the whole fragment-rendering surface, ``templates/tracklists/partials/``
and ``templates/tracklists/scan.html``) had no live caller and has been deleted outright, not just
disconnected -- see the bead for the full accounting.
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse


router = APIRouter(prefix="/tracklists", tags=["tracklists"])


@router.get("/", response_class=RedirectResponse)
async def list_tracklists() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/tracklists/`` bookmark into the v7.0 shell.

    The entire in-page fragment UI this route used to serve (filter/paginate the tracklist
    list) has no live caller left post-cutover (phaze-y4s6) -- there is no HX filter branch to
    preserve here, unlike the sibling ``/proposals/`` redirect.
    """
    return RedirectResponse(url="/s/tracklist", status_code=302)
