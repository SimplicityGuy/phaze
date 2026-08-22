"""Characterization pin for `src/phaze/routers/shell.py` -- written BEFORE the phaze-bk9el.16 split.

WHAT THIS IS. A characterization test, not a feature test. ``test_shell_routes.py`` and
``test_hx_target_contracts.py`` already exist and are real, but they were written as feature
tests: each asserts that a particular behaviour is present, none asserts that the SET of
behaviours is unchanged. Splitting a 1,392-line router into four modules can preserve every
individual assertion in those files and still drop a stage, re-home a route, swap which
template a given ``HX-Target`` selects, or lose a header. This file is the discharge of
phaze-bk9el.16's "no behaviour change" criterion under CLAUDE.md rule 1.

GENERATED, NOT HAND-LISTED. Every route assertion here is derived from the LIVE app object
(``create_app()`` walked by ``tests._route_introspection``), never from globbing router modules
or reading ``@router.get`` decorators. That distinction is the whole point after a split: the
source layout is exactly what is about to change, and a check that measures the source layout
would keep passing while the served URL space moved underneath it.

WHAT IT PINS:

* **The routes shell.py owns**, identified by endpoint ``__module__`` -- so a route that moves
  to a new module inside the ``phaze.routers.shell`` PACKAGE keeps passing, while a route that
  disappears, changes path/method, or is renamed does not.
* **The whole app's (method, path) table.** A split of one router is not supposed to change
  the URL space at all; this is the assertion that says so, and it also catches the opposite
  error -- a route accidentally registered twice, or a router silently dropped from
  ``create_app``.
* **The three static stage maps and their ORDER** -- ``STAGE_PARTIALS`` (whose key order is a
  documented contract: it mirrors the 57-UI-SPEC DAG rail), ``UTILITY_PANES``,
  ``DOCUMENT_TITLES``, plus the container-id constants and the per-stage context-builder map.
  Order is pinned as a list of pairs, not compared as a dict, because dict equality would not
  see a reordering.
* **What each stage actually renders**, for both response shapes: the exact template names
  (captured by spying on ``templates.TemplateResponse``, so the assertion is about which
  template was chosen rather than about markup that will legitimately change), the status, the
  document/fragment verdict, the ``<title>``, and the cache headers.
* **The full HX-Target discrimination matrix** -- every stage crossed with every discriminating
  target value, including the empty one. ``_render_stage_fragment`` has three narrow branches
  guarded by ``(stage, target)`` pairs; a split that mis-transcribes one of those conditions
  produces a wrong template for exactly one cell of this matrix and nothing else.
* **Every HTMX binding in every served document** -- for each rendered stage, the sorted set of
  (element, id, hx-attributes) tuples. This is the "every HTMX target binding" half of the
  acceptance criterion, and it is taken from the RENDERED response rather than by parsing
  template sources, so bindings that only appear under a particular stage's context are
  covered.
* **The two shapes that are easy to lose in a move**: a history restore gets the full document
  (phaze-64uy -- the fragment answer destroyed the rail), and an unknown stage 404s.

THE GOLDEN. ``shell_characterization_golden.json``, recorded on the unmodified tree at the
phaze-bk9el.1 baseline against an EMPTY database, which is what makes it reproducible. It is a
RECORDING, not a specification: a failure here means "something changed" and the response is to
find out what, not to re-record the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.main import create_app
from phaze.routers import shell as shell_mod
from tests._route_introspection import app_route_table, iter_effective_routes
from tests.conftest import install_fake_queues


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_GOLDEN_PATH = Path(__file__).with_name("shell_characterization_golden.json")

# Every htmx attribute the shell's templates can carry. Deliberately a fixed list rather than
# "any attribute starting with hx-": the pin must fail when a binding DISAPPEARS, and a
# prefix match would happily report an empty set for an element whose bindings were all
# renamed to something the prefix no longer matches.
_HX_ATTRS = (
    "hx-get",
    "hx-post",
    "hx-put",
    "hx-patch",
    "hx-delete",
    "hx-target",
    "hx-swap",
    "hx-trigger",
    "hx-push-url",
    "hx-select",
    "hx-include",
    "hx-vals",
    "hx-indicator",
    "hx-confirm",
    "hx-swap-oob",
    "hx-boost",
    "hx-disabled-elt",
    "hx-ext",
    "hx-on::after-request",
)

# The response headers that carry contract meaning. `vary` is the phaze-r6e5m dual-shape
# cache guard (this URL serves several bodies, so the browser must not substitute one for
# another on Back/Forward); `cache-control` and `content-type` complete the envelope.
_CONTRACT_HEADERS = frozenset({"vary", "cache-control", "content-type"})

# The HX-Target values `_render_stage_fragment` discriminates on, plus the empty string (a
# live swap that named no target). Crossed with every stage below.
_DISCRIMINATING_TARGETS = (
    "stage-workspace",
    "files-table-view",
    shell_mod.PROPOSE_LIST_CONTAINER_ID,
    shell_mod.CHANGES_LIST_CONTAINER_ID,
    "",
)


def _golden() -> dict[str, Any]:
    with _GOLDEN_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _stages() -> list[str]:
    return list(shell_mod.STAGE_PARTIALS) + list(shell_mod.UTILITY_PANES)


def _hx_bindings(html: str) -> list[list[str]]:
    """Every element in `html` that carries at least one htmx attribute, as sortable rows."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for element in soup.find_all(True):
        present = {attr: element.get(attr) for attr in _HX_ATTRS if element.get(attr) is not None}
        if not present:
            continue
        rows.append([element.name, element.get("id") or "", json.dumps(present, sort_keys=True)])
    return sorted(rows)


def _contract_headers(response: Any) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower() in _CONTRACT_HEADERS}


@pytest.fixture
def rendered_templates(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the template NAME of every `templates.TemplateResponse` the shell router builds.

    Asserting on the template name rather than on markup is deliberate. The markup inside a
    partial is expected to keep changing; WHICH partial a given (stage, HX-Target) pair
    resolves to is the contract the split must not move, and it is invisible in the response
    body of two partials that happen to render similarly.
    """
    recorded: list[str] = []
    real = shell_mod.templates.TemplateResponse

    def spy(*args: Any, **kwargs: Any) -> Any:
        recorded.append(kwargs.get("name") or (args[1] if len(args) > 1 else args[0]))
        return real(*args, **kwargs)

    monkeypatch.setattr(shell_mod.templates, "TemplateResponse", spy)
    return recorded


@pytest_asyncio.fixture
async def shell_client(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """A client over a freshly created app against the EMPTY test database.

    Empty is load-bearing: every count, badge and empty-state branch the rendered documents
    below contain is a function of the row counts, so the golden is only reproducible against
    a database with no rows in it. The `session` fixture provides exactly that.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        install_fake_queues(client)
        yield client


def test_shell_router_owns_exactly_the_recorded_routes() -> None:
    """The (path, method, name) rows whose endpoint lives in `phaze.routers.shell`.

    Generated by walking the live app and filtering on the endpoint's ``__module__``, so a
    split into a ``phaze.routers.shell`` PACKAGE moves the rows' module strings but the check
    only cares that the module is ``phaze.routers.shell`` itself -- i.e. this fails loudly if
    ``shell_home`` / ``shell_stage`` are re-homed to a differently-named module, which is a
    real decision the split has to make rather than an incidental one.
    """
    observed = sorted(
        [route.path, method, route.name]
        for route in iter_effective_routes(create_app())
        if getattr(getattr(route, "endpoint", None), "__module__", "") == "phaze.routers.shell"
        for method in sorted(route.methods or ())
    )
    assert observed == _golden()["SHELL_ROUTES"]


def test_whole_app_route_table_is_unchanged() -> None:
    """Every (method, path) the app serves.

    A router split is not supposed to change the URL space AT ALL. This is the assertion that
    says so, and it is the one that catches the two failures the shell-scoped check above
    cannot see: a route accidentally registered twice under two includes, and a router dropped
    from ``create_app`` entirely.
    """
    observed = sorted(f"{route.method} {route.path}" for route in app_route_table(create_app()))
    assert observed == _golden()["APP_ROUTE_TABLE"]


def test_stage_maps_and_their_order_are_unchanged() -> None:
    """The three static stage maps, as ORDERED pairs, plus the id constants and builder map.

    Order matters and is asserted as a list rather than a dict: ``STAGE_PARTIALS``' own
    docstring pins its key order VERBATIM to the 57-UI-SPEC DAG rail table, and a dict
    comparison would not notice a reordering. ``UTILITY_PANES`` is deliberately a separate map
    from ``STAGE_PARTIALS`` (audit/agents are not DAG stages and carry no rail badge); folding
    the two together during a split would falsify that documented invariant, and this test is
    where that shows up.
    """
    golden = _golden()
    assert [list(pair) for pair in shell_mod.STAGE_PARTIALS.items()] == golden["STAGE_PARTIALS"]
    assert [list(pair) for pair in shell_mod.UTILITY_PANES.items()] == golden["UTILITY_PANES"]
    assert [list(pair) for pair in shell_mod.DOCUMENT_TITLES.items()] == golden["DOCUMENT_TITLES"]
    observed_ids = {
        "PROPOSE_LIST_CONTAINER_ID": shell_mod.PROPOSE_LIST_CONTAINER_ID,
        "CHANGES_LIST_CONTAINER_ID": shell_mod.CHANGES_LIST_CONTAINER_ID,
        "_EMPTY_STATE_PARTIAL": shell_mod._EMPTY_STATE_PARTIAL,
    }
    assert observed_ids == golden["CONTAINER_IDS"]
    assert sorted([stage, builder.__name__] for stage, builder in shell_mod._STAGE_CONTEXT_BUILDERS.items()) == golden["STAGE_CONTEXT_BUILDERS"]


@pytest.mark.asyncio
async def test_every_stage_document_render_is_unchanged(
    shell_client: AsyncClient,
    rendered_templates: list[str],
) -> None:
    """Direct navigation to `/` and every `/s/<stage>`: status, templates, headers, title, shape.

    One test over the whole stage set rather than one per stage, so a stage that DISAPPEARS
    fails here instead of quietly reducing the parametrization.
    """
    golden = _golden()["DOCUMENT_RENDERS"]
    observed: dict[str, Any] = {}
    for url in ["/", *[f"/s/{stage}" for stage in _stages()]]:
        rendered_templates.clear()
        response = await shell_client.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        observed[f"GET {url}"] = {
            "status": response.status_code,
            "templates": list(rendered_templates),
            "headers": _contract_headers(response),
            "is_document": "<html" in response.text.lower(),
            "title": soup.title.string if soup.title else None,
        }
    assert observed == golden


@pytest.mark.asyncio
async def test_every_stage_fragment_render_is_unchanged(
    shell_client: AsyncClient,
    rendered_templates: list[str],
) -> None:
    """The live-htmx-swap shape of the same URLs: chrome-less fragment, same headers.

    The paired assertion to the one above. A fragment that starts carrying ``<html>`` corrupts
    the shell (a ROADMAP-locked anti-pattern), so ``is_document`` being False here is a real
    contract and not incidental.
    """
    golden = _golden()["FRAGMENT_RENDERS"]
    observed: dict[str, Any] = {}
    for url in ["/", *[f"/s/{stage}" for stage in _stages()]]:
        rendered_templates.clear()
        response = await shell_client.get(url, headers={"HX-Request": "true", "HX-Target": "stage-workspace"})
        observed[f"GET {url}"] = {
            "status": response.status_code,
            "templates": list(rendered_templates),
            "headers": _contract_headers(response),
            "is_document": "<html" in response.text.lower(),
        }
    assert observed == golden


@pytest.mark.asyncio
async def test_hx_target_discrimination_matrix_is_unchanged(
    shell_client: AsyncClient,
    rendered_templates: list[str],
) -> None:
    """Every stage x every discriminating HX-Target -> the exact template chosen.

    ``_render_stage_fragment``'s three narrow branches are guarded by ``(stage, target)``
    conditions -- one of them a set membership over three stages. Mis-transcribing any of them
    during a split changes exactly one cell of this matrix and leaves every other test in the
    tree green, which is precisely the class of silent regression this bead exists to catch.
    """
    golden = _golden()["HX_TARGET_MATRIX"]
    observed: dict[str, Any] = {}
    for stage in _stages():
        for target in _DISCRIMINATING_TARGETS:
            rendered_templates.clear()
            response = await shell_client.get(f"/s/{stage}", headers={"HX-Request": "true", "HX-Target": target})
            observed[f"{stage}|{target}"] = {"status": response.status_code, "templates": list(rendered_templates)}
    assert observed == golden


@pytest.mark.asyncio
async def test_every_htmx_binding_in_every_served_document_is_unchanged(shell_client: AsyncClient) -> None:
    """The complete set of htmx bindings the shell serves, per stage.

    Taken from the RENDERED response, not from parsing template sources: a binding that only
    appears under one stage's context (an empty-state trigger, a disabled control) is in the
    served document and not reliably visible in a static template parse. Each row is
    (element name, id, the element's htmx attributes as sorted JSON), so a changed ``hx-target``
    on one control fails with that control's id in the diff.
    """
    golden = _golden()["HX_BINDINGS"]
    observed: dict[str, Any] = {}
    for url in ["/", *[f"/s/{stage}" for stage in _stages()]]:
        response = await shell_client.get(url)
        observed[f"GET {url}"] = _hx_bindings(response.text)
    assert observed == golden


@pytest.mark.asyncio
async def test_history_restore_still_gets_the_full_document(
    shell_client: AsyncClient,
    rendered_templates: list[str],
) -> None:
    """phaze-64uy: HX-Request AND HX-History-Restore-Request -> the full shell, not a fragment.

    On a restore htmx ignores ``hx-target`` and swaps the response into ``<body>``, so
    answering with a fragment deletes the rail, header, palette launcher and status strip and
    leaves no way out but a manual reload. Reachable from every stage in the app. It is one
    ``and not`` inside ``wants_fragment``, which is exactly the kind of condition a move drops.
    """
    rendered_templates.clear()
    response = await shell_client.get(
        "/s/analyze",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true", "HX-Target": "stage-workspace"},
    )
    observed = {
        "status": response.status_code,
        "templates": list(rendered_templates),
        "is_document": "<html" in response.text.lower(),
    }
    assert observed == _golden()["HISTORY_RESTORE"]


@pytest.mark.asyncio
async def test_unknown_stage_still_404s(shell_client: AsyncClient) -> None:
    """A stage in neither map 404s and is NEVER used to build a template path (T-57-01).

    The whitelist is the template-path-injection boundary. A split that reconstructs the
    membership check from one map instead of two would 500 on the utility panes; one that drops
    it entirely would splice operator input into a template path.
    """
    response = await shell_client.get("/s/definitely-not-a-stage")
    assert {"status": response.status_code} == _golden()["UNKNOWN_STAGE"]
