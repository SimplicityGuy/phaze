"""Unit tests for ``phaze.web.static.RevalidatingStaticFiles`` (phaze-mw9l).

The bug this guards against: ``/static/css/app.css`` lives at a stable URL and stock
``StaticFiles`` sends no ``Cache-Control``, so browsers cache it heuristically across
deploys. When phaze-2u8v.6's lane detail pane started depending on Tailwind utilities
absent from every earlier compiled stylesheet, a stale cached ``app.css`` left the pane
stranded on-screen as an undismissable "No lane selected" box. Serving every static
response with ``Cache-Control: no-cache`` makes the browser revalidate on each use — a
cheap conditional-request/304 exchange on the private network this app deploys to.

These tests mount the subclass over a tmp dir on a throwaway ``FastAPI()`` (no DB, no
Redis — the ``test_saq_mount.py`` idiom), plus one wiring assertion that ``create_app``'s
real ``/static`` mount actually uses the subclass, so a revert to stock ``StaticFiles``
cannot pass silently.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from phaze.main import create_app
from phaze.web.static import RevalidatingStaticFiles


def _client(tmp_path: Path) -> TestClient:
    (tmp_path / "app.css").write_text(".fixed{position:fixed}\n")
    app = FastAPI()
    app.mount("/static", RevalidatingStaticFiles(directory=tmp_path), name="static")
    return TestClient(app)


def test_static_200_carries_no_cache(tmp_path: Path) -> None:
    """A fresh fetch is served 200 with ``Cache-Control: no-cache`` — the browser must
    revalidate before every reuse instead of trusting heuristic freshness."""
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


def test_create_app_mounts_revalidating_static() -> None:
    """The real ``/static`` mount uses the subclass — reverting to stock ``StaticFiles``
    (no ``Cache-Control``) must fail here, not in an operator's browser next deploy."""
    app = create_app()
    mounts = {route.name: route for route in app.routes if getattr(route, "name", None) == "static"}
    assert "static" in mounts, "expected a /static mount named 'static'"
    assert isinstance(mounts["static"].app, RevalidatingStaticFiles)
