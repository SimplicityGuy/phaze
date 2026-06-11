"""Unit tests for the SAQ-dashboard mount helper ``phaze.web.saq_mount.build_saq_app``.

These deliberately do NOT use the default ``client`` conftest fixture: that fixture
skips the app lifespan (where the real ``/saq`` mount lives in Wave 2), so it would
never wire the dashboard. Instead each test mounts ``build_saq_app`` onto a throwaway
``FastAPI()`` and drives it with a synchronous ``TestClient``, feeding ``FakeQueue``
doubles (``tests/_queue_fakes.py``) whose Redis-free ``.info()`` the dashboard renders
over — so routes, queue listing, and the globals-clobber contract are exercised with no
DB, no Redis, and no live workers (RESEARCH Pitfall 2).
"""

import ast
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import saq.web.starlette as saq_starlette

from phaze.config import settings
from phaze.web.saq_mount import build_saq_app
from tests._queue_fakes import FakeQueue


def _mount(*queues: FakeQueue) -> FastAPI:
    app = FastAPI()
    app.mount("/saq", build_saq_app(list(queues)))
    return app


def test_build_saq_app_routes_and_root_renders() -> None:
    """Helper returns a Starlette app exposing ``/`` and ``/api/queues``; the rendered
    root carries the ``/saq/static/`` asset prefix (root_path baked correctly)."""
    dashboard = build_saq_app([FakeQueue("controller")])
    route_paths = {getattr(r, "path", None) for r in dashboard.routes}
    assert "/" in route_paths
    assert "/api/queues" in route_paths

    app = _mount(FakeQueue("controller"))
    with TestClient(app) as c:
        resp = c.get("/saq/")
        assert resp.status_code == 200
        assert "/saq/static/" in resp.text


def test_api_queues_reuses_passed_instances_no_pool() -> None:
    """``/saq/api/queues`` lists exactly the passed queue names — proving the dashboard
    reuses the PASSED instances via ``.info()`` — and the helper constructs no pool."""
    app = _mount(FakeQueue("controller"), FakeQueue("phaze-agent-nox"))
    with TestClient(app) as c:
        resp = c.get("/saq/api/queues")
        assert resp.status_code == 200
        names = {q["name"] for q in resp.json()["queues"]}
        assert names == {"controller", "phaze-agent-nox"}

    # Assert the helper constructs no pool. The construction tokens
    # (Queue.from_url / Redis / connect) appear in the docstring prose describing
    # what the helper does NOT do, so a raw substring scan is wrong; instead AST-walk
    # the function body (docstrings are string constants, not Call nodes) and assert
    # the only call is saq_web — never a pool/Redis/from_url/connect construction.
    source = Path(build_saq_app.__code__.co_filename).read_text(encoding="utf-8")
    func = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "build_saq_app")
    called = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    assert called == {"saq_web"}
    assert "from_url" not in called
    assert "connect" not in called


def test_enable_saq_ui_flag_defaults_true() -> None:
    """Wave 2 gates the mount on this default-on flag."""
    assert settings.enable_saq_ui is True


def test_saq_web_single_call_contract() -> None:
    """A second ``build_saq_app`` clobbers the first call's queue registry: ``saq_web``
    keeps its registry in module globals and clears them per call, so production MUST
    mount the dashboard exactly once per process."""
    build_saq_app([FakeQueue("a")])
    build_saq_app([FakeQueue("b")])
    assert set(saq_starlette.QUEUES) == {"b"}
