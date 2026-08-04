"""Static-file serving with content-fingerprinted, cache-forever asset URLs (phaze-315t).

Supersedes the phaze-mw9l no-cache-only fix. ``/static/css/app.css`` (and every other file
under ``/static``) used to live at a stable, unfingerprinted URL. Stock Starlette
:class:`~starlette.staticfiles.StaticFiles` sends ``ETag``/``Last-Modified`` but no
``Cache-Control``, so browsers apply heuristic freshness and reuse a cached copy across
deploys without ever revalidating. phaze-mw9l closed that gap with ``Cache-Control:
no-cache`` on every response, forcing a conditional-request round trip before every reuse —
correct, but every hit still pays a network round trip even when the file has not changed.

phaze-315t was filed independently after phaze-mw9l shipped, re-confirming the same root
cause against THREE simultaneously-reported UI defects during the 2026.7.10 rollout (a
crushed/non-resizable files-table File column, an agent detail row rendering white in dark
mode, and the compute-lane detail pane rendering in-flow instead of as a fixed right
slide-in) and asking for the fix option it prefers: fingerprint the asset URL
(``?v=<hash>``) plus a long, immutable ``Cache-Control`` — no round trip at all once the
browser holds the current URL, and a deploy that changes any static file automatically mints
a new URL rather than depending on operator discipline (e.g. remembering to bump an app
version alongside a CSS tweak).

``STATIC_VERSION`` is a SHA-256 fingerprint over the **content** of every file under the
served directory (not just ``css/app.css`` — the bead is explicit that the fix must apply to
every static asset), computed once at import time. Read via ``importlib`` app-version schemes
(``importlib.metadata.version("phaze")``, used elsewhere in this codebase for the same
"version already available at render time" purpose) were considered and rejected here
specifically: the app version is a human-bumped release counter, and a CSS-only deploy that
does not also bump it would silently reproduce this exact bug class. A content hash cannot
miss a change — it does not depend on anyone remembering to touch a version string.

The compiled ``src/phaze/static/css/app.css`` is gitignored and produced by the Dockerfile's
``css-builder`` stage, then ``COPY --from=css-builder``'d into the final image (see
``.dockerignore``, which explicitly excludes any stale local copy from the build context so
the ``COPY`` from the builder stage is always what lands in the running container). Content
hashing at import time in the running process reads that baked-in file, so
``STATIC_VERSION`` reflects exactly what the container actually serves, in every deploy.

``RevalidatingStaticFiles`` keeps the phaze-mw9l no-cache behavior as the fallback for any
request that does not carry the current ``?v=`` fingerprint — including direct requests to
un-fingerprinted paths (e.g. a browser's own automatic ``/favicon.ico`` fetch, or
``site.webmanifest``'s internal icon ``src`` entries, neither of which is a template-emitted
URL) and stale HTML holding a previous deploy's fingerprint. Only a request whose ``v=``
query parameter matches the CURRENT ``STATIC_VERSION`` is safe to cache forever: at that
exact URL the content can never change (if it does, the fingerprint changes and so does the
URL), which is exactly what ``Cache-Control: max-age=31536000, immutable`` promises.
"""

import hashlib
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


# phaze/web/static.py -> phaze/ -> phaze/static
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# One year — the conventional ceiling browsers honor for `immutable` assets (RFC 8246 doesn't
# mandate a value; a year is the standard choice for content-addressed URLs that never reuse
# the same URL for different bytes).
_IMMUTABLE_MAX_AGE_SECONDS = 31_536_000
_IMMUTABLE_CACHE_CONTROL = f"max-age={_IMMUTABLE_MAX_AGE_SECONDS}, immutable"
_REVALIDATE_CACHE_CONTROL = "no-cache"

# Truncated to 12 hex chars: collision-astronomically-unlikely for a single-operator app's
# handful of static files, while keeping the query string short.
_VERSION_DIGEST_LENGTH = 12


def compute_static_version(directory: Path) -> str:
    """Content fingerprint over every file under ``directory``.

    Hashes each file's path (relative to ``directory``, so a rename also changes the
    fingerprint) and bytes, in a stable (sorted) order so the result is deterministic across
    runs of the same file set. Any change to any static file's content OR the set of files
    present changes the returned digest.
    """
    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hasher.update(path.relative_to(directory).as_posix().encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()[:_VERSION_DIGEST_LENGTH]


# Computed once at import time (process startup) from the real served directory — this is the
# value templates embed in every `/static/...` href and the value RevalidatingStaticFiles
# checks incoming requests against.
STATIC_VERSION = compute_static_version(STATIC_DIR)


def _version_query_param(scope: Scope) -> str | None:
    """Extract ``?v=`` from the request's raw query string, or ``None`` if absent."""
    query_string = scope.get("query_string", b"")
    decoded = query_string.decode("utf-8", errors="replace") if isinstance(query_string, bytes) else str(query_string)
    values = parse_qs(decoded).get("v")
    return values[0] if values else None


class RevalidatingStaticFiles(StaticFiles):
    """``StaticFiles`` that caches forever for a correctly-fingerprinted request, and falls
    back to ``Cache-Control: no-cache`` (revalidate-every-use) for everything else.

    ``version`` is the fingerprint this instance considers current (``STATIC_VERSION`` for the
    real app mount; tests pass their own so they stay independent of the app's real static
    directory). ``None`` disables fingerprint recognition entirely and every response gets the
    safe no-cache fallback — the exact phaze-mw9l behavior, preserved for callers that
    construct this class without opting into versioning.

    Overrides ``get_response`` rather than ``file_response`` so the header rides the ``304 Not
    Modified`` branch too — ``file_response`` never sees it, and a bare 304 would otherwise
    re-poison a cache entry with the wrong freshness metadata.
    """

    def __init__(self, *args: object, version: str | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.version = version

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        requested_version = _version_query_param(scope)
        if self.version is not None and requested_version == self.version:
            response.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
        else:
            response.headers["Cache-Control"] = _REVALIDATE_CACHE_CONTROL
        return response


def static_asset_url(relative_path: str, *, version: str = STATIC_VERSION) -> str:
    """Build a fingerprinted ``/static/...`` URL for a template to emit.

    ``relative_path`` is relative to the static mount (e.g. ``"css/app.css"``,
    ``"favicon-32.svg"``) — never include the ``/static/`` prefix.
    """
    return f"/static/{relative_path}?v={version}"
