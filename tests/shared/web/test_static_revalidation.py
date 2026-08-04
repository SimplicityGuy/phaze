"""Unit tests for ``phaze.web.static.RevalidatingStaticFiles`` (phaze-mw9l, phaze-315t).

The bug this guards against: ``/static/css/app.css`` lives at a stable URL and stock
``StaticFiles`` sends no ``Cache-Control``, so browsers cache it heuristically across
deploys. When phaze-2u8v.6's lane detail pane started depending on Tailwind utilities
absent from every earlier compiled stylesheet, a stale cached ``app.css`` left the pane
stranded on-screen as an undismissable "No lane selected" box.

phaze-mw9l's fix (``Cache-Control: no-cache`` on every response) is preserved here as the
fallback for any request that does not carry the CURRENT content fingerprint. phaze-315t
layers fingerprinted, cache-forever URLs on top: a request whose ``?v=`` matches the
directory's current content hash is safe to cache for a year, because that exact URL can
never serve different bytes — if the content changes, so does the fingerprint and
therefore the URL.

These tests mount the subclass over a tmp dir on a throwaway ``FastAPI()`` (no DB, no
Redis — the ``test_saq_mount.py`` idiom), plus wiring assertions that ``create_app``'s real
``/static`` mount actually uses the subclass with the real ``STATIC_VERSION``, so a revert
to stock ``StaticFiles`` (or to an unversioned mount) cannot pass silently.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient as HttpxAsyncClient

from phaze.main import create_app
from phaze.web.static import STATIC_DIR, STATIC_VERSION, RevalidatingStaticFiles, compute_static_version, static_asset_url


def _client(tmp_path: Path, *, version: str | None = None) -> TestClient:
    (tmp_path / "app.css").write_text(".fixed{position:fixed}\n")
    app = FastAPI()
    app.mount("/static", RevalidatingStaticFiles(directory=tmp_path, version=version), name="static")
    return TestClient(app)


def test_static_200_carries_no_cache(tmp_path: Path) -> None:
    """A fresh fetch with no ``?v=`` is served 200 with ``Cache-Control: no-cache`` — the
    browser must revalidate before every reuse instead of trusting heuristic freshness."""
    with _client(tmp_path) as c:
        resp = c.get("/static/app.css")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
        assert "etag" in resp.headers


def test_static_304_carries_no_cache(tmp_path: Path) -> None:
    """The conditional-revalidation branch (``If-None-Match`` → 304) carries the header
    too: a 304 refreshes the cached entry's metadata, so a bare 304 would re-poison the
    cache with heuristic freshness — the exact branch ``file_response`` never sees."""
    with _client(tmp_path) as c:
        etag = c.get("/static/app.css").headers["etag"]
        resp = c.get("/static/app.css", headers={"If-None-Match": etag})
        assert resp.status_code == 304
        assert resp.headers["cache-control"] == "no-cache"


def test_static_404_is_unaffected(tmp_path: Path) -> None:
    """A miss still 404s (the override only decorates responses the parent returns)."""
    with _client(tmp_path) as c:
        assert c.get("/static/missing.css").status_code == 404


def test_no_configured_version_always_no_cache(tmp_path: Path) -> None:
    """``version=None`` (the default) disables fingerprint recognition entirely — even a
    request carrying a ``?v=`` gets the safe no-cache fallback, never the year-long cache."""
    with _client(tmp_path, version=None) as c:
        resp = c.get("/static/app.css?v=anything")
        assert resp.headers["cache-control"] == "no-cache"


def test_matching_version_gets_year_long_immutable_cache(tmp_path: Path) -> None:
    """A request whose ``?v=`` matches the instance's configured fingerprint is safe to
    cache forever: that exact URL can never later serve different bytes."""
    with _client(tmp_path, version="deadbeef1234") as c:
        resp = c.get("/static/app.css?v=deadbeef1234")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "max-age=31536000, immutable"


def test_mismatched_version_falls_back_to_no_cache(tmp_path: Path) -> None:
    """A stale fingerprint (an old deploy's URL still held by a warm browser) must NOT be
    cached forever — it falls back to no-cache exactly like an unversioned request."""
    with _client(tmp_path, version="deadbeef1234") as c:
        resp = c.get("/static/app.css?v=stale0000000")
        assert resp.headers["cache-control"] == "no-cache"


def test_absent_version_param_falls_back_to_no_cache_even_when_configured(tmp_path: Path) -> None:
    """A direct request with no ``?v=`` at all (e.g. a browser's own automatic
    ``/favicon.ico`` probe, or ``site.webmanifest``'s internal icon ``src`` entries) must
    stay on the safe no-cache path even though this mount has fingerprint recognition on."""
    with _client(tmp_path, version="deadbeef1234") as c:
        resp = c.get("/static/app.css")
        assert resp.headers["cache-control"] == "no-cache"


def test_compute_static_version_changes_when_content_changes(tmp_path: Path) -> None:
    """The whole point of fingerprinting: any content change moves the digest, so a new
    deploy mints a new URL rather than depending on an operator remembering to bump one."""
    (tmp_path / "app.css").write_text(".a{color:red}\n")
    before = compute_static_version(tmp_path)
    (tmp_path / "app.css").write_text(".a{color:blue}\n")
    after = compute_static_version(tmp_path)
    assert before != after


def test_compute_static_version_is_deterministic_for_unchanged_content(tmp_path: Path) -> None:
    """Same bytes, same fingerprint — restarting the process must not spuriously bust every
    warm browser's cache."""
    (tmp_path / "app.css").write_text(".a{color:red}\n")
    assert compute_static_version(tmp_path) == compute_static_version(tmp_path)


def test_static_asset_url_appends_version_query_param() -> None:
    assert static_asset_url("css/app.css", version="abc123") == "/static/css/app.css?v=abc123"


def test_static_asset_url_defaults_to_module_static_version() -> None:
    assert static_asset_url("css/app.css") == f"/static/css/app.css?v={STATIC_VERSION}"


def test_static_version_matches_real_static_dir() -> None:
    """The module-level constant templates embed must reflect the ACTUAL served directory —
    a drift here would mean templates ship a fingerprint nothing on disk matches."""
    assert compute_static_version(STATIC_DIR) == STATIC_VERSION


def test_create_app_mounts_revalidating_static() -> None:
    """The real ``/static`` mount uses the subclass, wired with the real ``STATIC_VERSION`` —
    reverting to stock ``StaticFiles`` (no ``Cache-Control``) or dropping the version must
    fail here, not in an operator's browser next deploy."""
    app = create_app()
    mounts = {route.name: route for route in app.routes if getattr(route, "name", None) == "static"}
    assert "static" in mounts, "expected a /static mount named 'static'"
    mounted = mounts["static"].app
    assert isinstance(mounted, RevalidatingStaticFiles)
    assert mounted.version == STATIC_VERSION


async def test_shell_page_emits_fingerprinted_stylesheet_link(client: HttpxAsyncClient) -> None:
    """End-to-end acceptance check: the rendered ``GET /`` shell HTML links ``app.css``
    with the CURRENT ``?v=`` fingerprint baked in — a returning browser holding a stale
    cached copy of the page never sees this markup change under it (a normal reload
    re-fetches the HTML, which always carries the fingerprint the running process just
    computed), so it always requests the correctly-versioned stylesheet URL, on a NORMAL
    reload, without a hard refresh."""
    response = await client.get("/")
    assert response.status_code == 200
    assert f'href="/static/css/app.css?v={STATIC_VERSION}"' in response.text
