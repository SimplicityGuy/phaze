"""Static-file serving that browsers must revalidate on every use (phaze-mw9l).

``/static/css/app.css`` lives at a stable URL with no fingerprint in its name. Starlette's
stock :class:`~starlette.staticfiles.StaticFiles` sends ``ETag``/``Last-Modified`` but NO
``Cache-Control``, so browsers fall back to heuristic freshness (typically 10% of the time
since ``Last-Modified``) and serve a cached copy across deploys without ever revalidating.
That is exactly how phaze-mw9l's operator hit an undismissable "No lane selected" box:
phaze-2u8v.6 rebuilt the lane detail pane around Tailwind utilities (``inset-y-0`` /
``right-0`` / ``translate-x-full`` / ``translate-x-0``) present in no earlier compiled
``app.css``, and a heuristically-cached pre-rework stylesheet left the pane ``fixed z-40``
at its static position — stranded over the workspace, with the ✕ and Esc dismiss both
gated on an ``open`` state the resting pane never has. Any future first-use utility
re-opens the same trap, so the fix belongs on the transport, not on that one template.

``Cache-Control: no-cache`` means "store, but revalidate before every use" — each hit
becomes a conditional request answered ``304 Not Modified`` until the file actually
changes. On the single-operator private network this app is deployed to, that round-trip
is trivially cheap; correctness across deploys is not.
"""

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class RevalidatingStaticFiles(StaticFiles):
    """``StaticFiles`` whose every response carries ``Cache-Control: no-cache``.

    Overrides ``get_response`` rather than ``file_response`` so the header rides the
    ``304 Not Modified`` branch too — ``file_response`` never sees it. Misses raise
    ``HTTPException(404)`` before this returns and are unaffected.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
