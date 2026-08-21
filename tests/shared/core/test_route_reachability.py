"""Static template↔endpoint reachability (phaze-u2p8s).

Two of the four P1s this repo's bug hunts have ever filed were *reachability severances*,
and neither was a bug in any endpoint or any template considered on its own:

* the Phase-62 shell cutover left ``POST /pipeline/analyze`` with no caller in any served
  template -- the analyze stage was silently unreachable while every one of its tests
  stayed green;
* Execute Approved was stranded when the only partial hosting its control stopped being
  rendered.

(A third -- scan management orphaned from the v7 shell -- is the same class.) Every one of
them was invisible to the whole suite for the same structural reason: tests call endpoints
directly, so they exercise the endpoint and never the *edge* from a rendered document to
it. This module asserts that missing edge, in both directions:

* **Dangling** -- a URL a served template will actually request that resolves to no route.
  That is a 404 the moment an operator clicks it.
* **Orphaned** -- a routed endpoint no served document can reach. That is a feature the
  operator can no longer get to, which is what all three P1s were.

Both directions run off the SAME two harvests, so neither can drift from the other.

What counts as a caller
-----------------------

1. **Template attributes** -- ``hx-get`` / ``hx-post`` / ``hx-put`` / ``hx-delete`` /
   ``hx-patch``, ``sse-connect``, a ``<form action=>`` (verb from its ``method=``), and an
   ``<a href=>``. These carry a statically-known verb.
2. **URL literals inside Jinja expressions** -- ``{{ ui.confirmation(action="/execution/start", ...) }}``
   is how Execute Approved's own control is wired, so an attribute-only parse would have
   reported the *fixed* endpoint as orphaned. String concatenation is folded
   (``"/tags/" ~ id ~ "/write"`` resolves to ``/tags/{file_id}/write``) rather than
   harvested as two useless fragments.
3. **URLs built in the HTML layer** -- ``src/phaze/routers/**`` and ``src/phaze/web/**``
   hand URLs to templates as context (``detail_url=f"/admin/agents/{agent.id}/_activity"``,
   ``SortContract(endpoint="/admin/agents/_table")``, ``RedirectResponse("/s/cue")``), which
   the template then renders through an opaque ``hx-get="{{ row.detail_url }}"``. Without
   this harvest a third of the UI would read as orphaned and the allowlist would have to
   absorb it -- which would defeat the check.

   ``services/agent_client.py`` is deliberately NOT harvested even though it is full of
   correct URLs: it is the remote agent's HTTP client, not the browser UI. Counting it
   would auto-rescue every ``/api/internal/agent/**`` route and quietly convert an explicit,
   commented allowlist decision into an invisible one.

Two ``@router.get("/pipeline/analyze")``-shaped false positives are excluded by
construction: decorator arguments, and ``APIRouter(prefix=...)`` / ``Mount(...)`` arguments.
A route is not its own caller.

What is NOT covered, stated plainly
-----------------------------------

* A fully dynamic attribute (``hx-get="{{ url }}"``) contributes nothing on its own; it is
  the router-built harvest that supplies the URL behind it. If a URL is ever computed by a
  layer this does not read, its route reads as orphaned -- report it, do not allowlist it.
* Wildcard segments match permissively; see ``reference_matches`` in
  ``tests/_route_introspection`` for the one hiding case that admits.
* The app issues no request from JavaScript (no ``fetch``, no source-less ``htmx.ajax()``
  -- ``shell/shell.html`` documents that as a deliberate rule), so templates plus the HTML
  layer are the complete caller surface. If that ever stops being true, this check goes
  blind in exactly that spot.

"Served" means reachable from a router
--------------------------------------

The caller harvest walks only templates a router actually renders, transitively through
``{% extends %}`` / ``{% include %}`` / ``{% import %}``. That is the Execute Approved shape
specifically: a partial that stops being included must stop counting as a caller, or this
check would keep reporting a stranded endpoint as reachable. ``test_dead_template_guard``
asserts the complement (no template is unreachable), so today the served set is the whole
tree -- the closure is what keeps that an assertion rather than an assumption.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import shutil

from bs4 import BeautifulSoup
from jinja2 import Environment, meta, nodes
import pytest

from phaze.main import create_app
from tests._route_introspection import WILDCARD, AppRoute, app_route_table, resolve_reference
from tests.shared.core.test_redirect_resolution import CANONICAL as _LEGACY_BOOKMARKS


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "phaze"
_TEMPLATES = _SRC / "templates"
_ROUTERS = _SRC / "routers"

# The layers that build a URL and hand it to a template. Router modules render the HTML;
# `web/` mounts and wires it. Anything else that builds a URL is talking to something other
# than this app's own UI -- see the module docstring on `services/agent_client.py`.
_URL_BUILDING_DIRS = ("routers", "web")

# `hx-*` attribute -> the HTTP verb htmx will use.
_HX_VERBS = {
    "hx-get": "GET",
    "hx-post": "POST",
    "hx-put": "PUT",
    "hx-delete": "DELETE",
    "hx-patch": "PATCH",
}

# A Jinja block of any kind, replaced wholesale by WILDCARD: its rendered value is unknown
# here, but it occupies path (or query) text. Comments are included so `{# ... #}` inside an
# attribute cannot leave a stray brace behind.
_JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)

# Any quoted "....html" literal in router source -- the entry set for the served-template
# closure. Matches `name="x.html"`, positional `_render_partial(request, "x.html")`, and
# ternary-assigned template variables alike, for the same reasons spelled out in
# `test_dead_template_guard`.
_HTML_LITERAL = re.compile(r"""["']([^"']+\.html)["']""")


# --------------------------------------------------------------------------------------
# Allowlist -- routes that are intentionally template-less.
#
# Every entry needs a reason that says WHY no document calls it. An entry that merely
# records that nothing calls it belongs in the findings ledger below instead: this list is
# for routes whose caller is something other than a browser document, and if it grows to
# absorb findings the check stops meaning anything.
# --------------------------------------------------------------------------------------

# Whole routers that are non-UI by construction. Prefix form is deliberate: these are entire
# API surfaces, not individual exceptions, and enumerating their 27 endpoints (22 + 5 today)
# one by one would be a list nobody reads. `test_allowlisted_prefixes_are_all_live` fails if
# a prefix stops matching anything.
_NON_UI_PREFIXES: dict[str, str] = {
    # Consumed exclusively by the remote agent over HTTP (`services/agent_client.py` is the
    # only client). No browser document touches these and none should: they carry the agent
    # bearer identity, not a session.
    "/api/internal/agent/": "remote-agent HTTP API -- called by services/agent_client.py, never by a document",
    # Programmatic trigger API for scripts and curl. The UI drives the same work through the
    # `/pipeline/*` endpoints, which ARE template-called and checked here.
    "/api/v1/": "public programmatic API -- the UI uses the /pipeline/* equivalents instead",
}

# Individually allowlisted endpoints.
_NON_UI_ROUTES: dict[str, str] = {
    # Container liveness probe (docker-compose / k8s). Its caller is an orchestrator.
    "GET /health": "liveness probe -- called by the container orchestrator",
}

# Legacy bookmark redirects (SHELL-05 / D-03): a plain GET resolves an operator's saved
# bookmark into the v7.0 shell in one hop. The caller IS the bookmark -- by design no
# template links to them, because the shell links to the `/s/<stage>` targets instead.
#
# DERIVED, not retyped: the contract lives in `test_redirect_resolution.CANONICAL` and is
# imported so that deleting a redirect there cannot leave a stale waiver here.
_LEGACY_BOOKMARK_ROUTES: dict[str, str] = {
    f"GET {path}": f"legacy bookmark redirect -> {target} (test_redirect_resolution.CANONICAL)" for path, target in _LEGACY_BOOKMARKS.items()
}

ALLOWLIST: dict[str, str] = {**_NON_UI_ROUTES, **_LEGACY_BOOKMARK_ROUTES}


# --------------------------------------------------------------------------------------
# Findings ledger -- real orphans, recorded so they cannot be forgotten.
#
# These are NOT waivers. Each is an endpoint that renders HTML for an operator who has no
# way to reach it, i.e. exactly the defect this module exists to catch, found by this module
# on its first run. They are parked here (rather than fixed) only because fixing them means
# either deleting a route or adding a control, both of which are product decisions and both
# of which land in routers other agents own on this branch.
#
# `test_findings_ledger_has_no_stale_entries` makes the ledger self-cleaning: an entry whose
# route no longer exists, or which has acquired a caller, FAILS with an instruction to delete
# the line. So the ledger cannot rot into a permanent allowlist.
# --------------------------------------------------------------------------------------
KNOWN_ORPHANS: dict[str, str] = {
    # (phaze-7tiqp's `PATCH /proposals/bulk-approve-high-confidence` entry lived here as a
    # merge-window placeholder. That route is now deleted, so the ledger's own staleness test
    # failed and the line was removed with it -- which is the self-cleaning property working,
    # not a maintenance chore.)
    # Severed by main's re-homing of the files workspace: `FILES_SORT.endpoint` (and the
    # filter/pager/sort controls that render from it) now point at `/s/files`, so every live
    # caller of the files table addresses the shell route and nothing addresses this one.
    # Several templates still DESCRIBE it in prose ("GET /pipeline/files returns for every
    # filter/sort/pager hx-get", files_workspace.html:7) -- stale comments, not callers,
    # which is what made this invisible. Owned by another agent on this branch.
    "GET /pipeline/files": "superseded by GET /s/files when FILES_SORT was re-homed -- decide: redirect it, or delete it",
    # (phaze-ur8o3 resolved this module's `GET /duplicates/{group_hash}/compare` entry by taking
    # the "delete it" branch: the route, `duplicates/partials/comparison_table.html`, and the
    # compare-only tests are gone, so the ledger line went with them. That is the self-cleaning
    # property working -- `test_findings_ledger_has_no_stale_entries` fails on an entry whose route
    # no longer exists, which is what forces the decision to actually be recorded.)
    # Row-expansion detail fragment for the legacy proposals table, whose host templates were
    # deleted in the cutover; the v7 propose workspace renders `_diff_row.html` instead.
    "GET /proposals/{proposal_id}/detail": "host table deleted in the v7 cutover -- decide: wire it up or delete it",
    # Same cutover, same shape: the analysis timeline is now included directly into
    # `record_body.html` rather than hx-get from this endpoint.
    "GET /proposals/{proposal_id}/timeline": "timeline is {% include %}d into record_body.html now -- decide: wire it up or delete it",
}


@dataclass(frozen=True)
class Reference:
    """One URL a document (or the code that renders it) will request."""

    url: str
    """Path only, query and fragment stripped, dynamic runs collapsed to ``WILDCARD``."""

    method: str | None
    """``None`` when the verb is not statically knowable -- see ``resolve_reference``."""

    origin: str
    """Human-readable ``file:line`` plus the raw text, for the failure message."""

    def display(self) -> str:
        return f"{self.method or 'ANY'} {self.url.replace(WILDCARD, '*')}  <- {self.origin}"


@dataclass(frozen=True)
class Reachability:
    """The result of one pass over a template tree."""

    dangling: tuple[Reference, ...]
    callers: dict[AppRoute, tuple[str, ...]]

    def orphans(self) -> tuple[AppRoute, ...]:
        """Routed endpoints with no caller, minus the ones intentionally template-less."""
        return tuple(route for route in _route_table() if route not in self.callers and not _is_allowlisted(route))


def _is_allowlisted(route: AppRoute) -> bool:
    return str(route) in ALLOWLIST or any(route.path.startswith(prefix) for prefix in _NON_UI_PREFIXES)


def _normalize(raw: str) -> str | None:
    """Reduce a raw attribute value to a comparable path, or ``None`` if it is not one.

    Jinja is substituted BEFORE the query string is stripped, so a URL that builds its own
    ``?`` inside an expression (``{{ ('?' ~ sort.query_params()) if sort else '' }}``) does
    not survive as query text. Anything not starting with ``/`` -- a fragment, a relative
    path, an external link -- is not ours to resolve.
    """
    collapsed = _JINJA_BLOCK.sub(WILDCARD, raw).strip()
    collapsed = collapsed.split("?", 1)[0].split("#", 1)[0]
    if not collapsed.startswith("/"):
        return None
    return re.sub(f"{WILDCARD}+", WILDCARD, collapsed)


def _fold(node: nodes.Node) -> str:
    """Flatten a Jinja expression to text, with unknown operands as ``WILDCARD``."""
    if isinstance(node, nodes.Const) and isinstance(node.value, str):
        return node.value
    if isinstance(node, nodes.Concat):
        return "".join(_fold(part) for part in node.nodes)
    if isinstance(node, nodes.Add):
        return _fold(node.left) + _fold(node.right)
    return WILDCARD


def _jinja_expression_urls(source: str) -> list[str]:
    """URL literals appearing in Jinja expressions, with concatenations folded.

    Descending into a ``Concat``'s constant children is deliberately skipped: ``"/tags/" ~
    id ~ "/write"`` must yield the one real URL and not also the fragment ``/write``, which
    resolves to nothing and would be reported as dangling.
    """
    found: list[str] = []

    def walk(node: nodes.Node) -> None:
        if isinstance(node, (nodes.Concat, nodes.Add)):
            folded = _fold(node)
            if folded.startswith("/"):
                found.append(folded)
            for child in node.iter_child_nodes():
                if not isinstance(child, nodes.Const):
                    walk(child)
            return
        if isinstance(node, nodes.Const):
            if isinstance(node.value, str) and node.value.startswith("/"):
                found.append(node.value)
            return
        for child in node.iter_child_nodes():
            walk(child)

    walk(Environment(autoescape=True).parse(source))
    return found


def served_templates(templates_root: Path) -> tuple[Path, ...]:
    """Templates a router renders, transitively closed over extends/include/import."""
    env = Environment(autoescape=True)
    queue = [name for module in sorted(_ROUTERS.rglob("*.py")) for name in _HTML_LITERAL.findall(module.read_text(encoding="utf-8"))]
    seen: set[str] = set()
    while queue:
        name = queue.pop()
        path = templates_root / name
        if name in seen or not path.is_file():
            # A non-template `.html` literal has no on-disk target; harmless, just dropped.
            continue
        seen.add(name)
        # `find_referenced_templates` yields None for a dynamic `{% include var %}` target;
        # drop the sentinel rather than weakening the closure.
        queue.extend(ref for ref in meta.find_referenced_templates(env.parse(path.read_text(encoding="utf-8"))) if ref)
    return tuple(sorted(templates_root / name for name in seen))


def template_references(templates_root: Path) -> list[Reference]:
    """Every URL the served templates under ``templates_root`` will request."""
    refs: list[Reference] = []
    for path in served_templates(templates_root):
        source = path.read_text(encoding="utf-8")
        label = path.relative_to(templates_root)

        # Raw-source parse: attribute values keep their `{{ }}` placeholders, which is what
        # `_normalize` collapses. html.parser (not a strict tree builder) tolerates Jinja
        # control flow interleaved with tags.
        for element in BeautifulSoup(source, "html.parser").find_all(True):
            candidates: list[tuple[str, str | None]] = []
            for attribute, verb in _HX_VERBS.items():
                value = element.get(attribute)
                if isinstance(value, str):
                    candidates.append((value, verb))
            sse = element.get("sse-connect")
            if isinstance(sse, str):
                candidates.append((sse, "GET"))
            if element.name == "form" and isinstance(element.get("action"), str):
                # A plain form's verb is its `method=`, defaulting to GET as HTML does.
                candidates.append((element["action"], str(element.get("method") or "GET").upper()))
            if element.name in {"a", "area"} and isinstance(element.get("href"), str):
                candidates.append((element["href"], "GET"))
            for raw, verb in candidates:
                url = _normalize(raw)
                if url is not None:
                    refs.append(Reference(url, verb, f"{label}:{element.sourceline} {raw.strip()[:70]}"))

        for raw in _jinja_expression_urls(source):
            url = _normalize(raw)
            if url is not None:
                # The verb lives on whatever `hx-*` attribute finally carries this URL --
                # usually inside the macro it is passed to -- so it is not knowable here.
                refs.append(Reference(url, None, f"{label} (jinja expression) {raw[:70]}"))
    return refs


def _fstring_text(node: ast.JoinedStr) -> str:
    return "".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else WILDCARD for part in node.values)


def router_built_references() -> list[Reference]:
    """URLs the HTML layer builds and hands to a template as context."""
    refs: list[Reference] = []
    for directory in _URL_BUILDING_DIRS:
        for module in sorted((_SRC / directory).rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            excluded: set[int] = set()
            for node in ast.walk(tree):
                # A route is not its own caller: `@router.post("/pipeline/analyze")` and
                # `APIRouter(prefix="/cue")` DECLARE the URL space this check measures.
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for decorator in node.decorator_list:
                        excluded |= {id(sub) for sub in ast.walk(decorator)}
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"APIRouter", "Mount"}:
                    excluded |= {id(sub) for sub in ast.walk(node)}
            for node in ast.walk(tree):
                if id(node) in excluded:
                    continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                elif isinstance(node, ast.JoinedStr):
                    text = _fstring_text(node)
                else:
                    continue
                # A URL has no whitespace and no newline; prose in a docstring does.
                if not text.startswith("/") or any(char.isspace() for char in text):
                    continue
                url = _normalize(text)
                if url is not None:
                    origin = f"{module.relative_to(_REPO_ROOT)}:{node.lineno} {text[:70]}"
                    # The verb is decided by the template attribute that renders this URL.
                    refs.append(Reference(url, None, origin))
    return refs


def analyse(templates_root: Path) -> Reachability:
    """Resolve every harvested reference against the live route table."""
    table = _route_table()
    dangling: list[Reference] = []
    callers: dict[AppRoute, list[str]] = {}

    for ref in template_references(templates_root):
        matched = resolve_reference(ref.url, ref.method, table)
        if not matched:
            dangling.append(ref)
        for route in matched:
            callers.setdefault(route, []).append(ref.origin)

    # Router-built URLs are read for the orphan direction only. They are NOT checked for
    # dangling: a `/`-prefixed string in a router can be a filesystem path or a log key, and
    # a check that has to guess which would either miss real dangles or fail on innocent
    # strings. Dangling is asserted where it is unambiguous -- in the markup.
    for ref in router_built_references():
        for route in resolve_reference(ref.url, ref.method, table):
            callers.setdefault(route, []).append(ref.origin)

    return Reachability(dangling=tuple(dangling), callers={route: tuple(origins) for route, origins in callers.items()})


def _route_table() -> tuple[AppRoute, ...]:
    """The live app's endpoints.

    Built from the app object on every call, never from router modules or decorators: this
    branch decomposed `routers/pipeline.py` into a package and main re-homed endpoints
    (`/pipeline/files` -> `/s/files`), and a check keyed on source layout would have
    measured neither correctly. Rebuilding per call (rather than caching at import) keeps
    the mutation tests honest -- they mutate templates, and the route table must be the
    real one they are mutated against.
    """
    return app_route_table(create_app())


@pytest.fixture(scope="module")
def baseline() -> Reachability:
    return analyse(_TEMPLATES)


# --------------------------------------------------------------------------------------
# The two directions.
# --------------------------------------------------------------------------------------


def test_no_dangling_template_reference(baseline: Reachability) -> None:
    """Every URL a served template requests resolves to a route.

    A failure here is a 404 waiting for an operator to click it -- a typo, or an endpoint
    renamed/moved without its callers.
    """
    assert not baseline.dangling, "template URLs that resolve to no route:\n  " + "\n  ".join(ref.display() for ref in baseline.dangling)


def test_no_orphaned_ui_route(baseline: Reachability) -> None:
    """Every UI-facing endpoint has at least one caller in a served document.

    A failure here is the P1 shape: the endpoint works, its tests pass, and no operator can
    get to it. Fix it by restoring the caller or deleting the endpoint. Add it to
    `ALLOWLIST` ONLY if its caller is genuinely not a document (an agent, a probe, a
    bookmark) -- and say which, in a comment.
    """
    unexpected = [route for route in baseline.orphans() if str(route) not in KNOWN_ORPHANS]
    assert not unexpected, "routed endpoints no served template can reach:\n  " + "\n  ".join(str(route) for route in unexpected)


# --------------------------------------------------------------------------------------
# Seeded-mutation proof: the check fails on the two defect shapes it claims to catch.
# Both mutate a COPY of the template tree -- the real one is never touched -- and both
# assert the unmutated copy is clean first, so neither can pass vacuously.
# --------------------------------------------------------------------------------------


def _copy_templates(tmp_path: Path) -> Path:
    destination = tmp_path / "templates"
    shutil.copytree(_TEMPLATES, destination)
    return destination


def _rewrite(root: Path, old: str, new: str) -> int:
    """Replace ``old`` with ``new`` across the tree; return how many files changed."""
    changed = 0
    for path in root.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if old in source:
            path.write_text(source.replace(old, new), encoding="utf-8")
            changed += 1
    return changed


def test_copied_template_tree_is_clean(tmp_path: Path, baseline: Reachability) -> None:
    """Control for the two mutation tests: an unmutated copy reproduces the real result."""
    control = analyse(_copy_templates(tmp_path))
    assert not control.dangling
    assert set(control.orphans()) == set(baseline.orphans())


def test_seeded_deletion_of_a_template_caller_is_caught(tmp_path: Path) -> None:
    """Mutation 1 -- delete the template caller of a routed endpoint.

    Re-seeds the original Phase-62 P1 exactly: `POST /pipeline/analyze` keeps working and
    keeps its tests, and no served document asks for it any more. Both of its callers are
    repointed at a sibling route so the mutation severs a caller WITHOUT introducing a
    dangling reference -- proving the orphan direction on its own.
    """
    root = _copy_templates(tmp_path)
    severed = next(route for route in _route_table() if str(route) == "POST /pipeline/analyze")
    assert severed in analyse(root).callers, "precondition: /pipeline/analyze must start out reachable"

    assert _rewrite(root, 'hx-post="/pipeline/analyze"', 'hx-post="/pipeline/extract-metadata"') == 2

    mutated = analyse(root)
    assert severed in mutated.orphans(), "orphan check missed an endpoint whose only callers were removed"
    assert not mutated.dangling, "the mutation should sever a caller, not create a dangling reference"


def test_seeded_typo_in_an_hx_get_url_is_caught(tmp_path: Path) -> None:
    """Mutation 2 -- typo an ``hx-get`` URL.

    The shell's stats poller is repointed at `/pipeline/statz`, which routes nowhere. The
    dangling direction must name it, and the orphan direction must independently notice
    that `/pipeline/stats` lost its only caller.
    """
    root = _copy_templates(tmp_path)
    assert _rewrite(root, 'hx-get="/pipeline/stats"', 'hx-get="/pipeline/statz"') == 1

    mutated = analyse(root)
    assert [ref.url for ref in mutated.dangling] == ["/pipeline/statz"], "dangling check missed a typoed hx-get URL"
    orphaned = next(route for route in _route_table() if str(route) == "GET /pipeline/stats")
    assert orphaned in mutated.orphans(), "the typo also orphaned the real endpoint, which should be reported too"


# --------------------------------------------------------------------------------------
# The allowlist and the findings ledger must not rot.
# --------------------------------------------------------------------------------------


def test_allowlisted_prefixes_are_all_live() -> None:
    """Every non-UI prefix still matches a real route (no waiver for a deleted router)."""
    table = _route_table()
    dead = [prefix for prefix in _NON_UI_PREFIXES if not any(route.path.startswith(prefix) for route in table)]
    assert not dead, f"allowlisted prefixes matching no route -- delete them: {dead}"


def test_allowlisted_routes_are_all_live() -> None:
    """Every individually allowlisted endpoint still exists, spelled exactly as written."""
    live = {str(route) for route in _route_table()}
    dead = sorted(set(ALLOWLIST) - live)
    assert not dead, f"allowlist entries matching no route -- delete them: {dead}"


def test_allowlist_entries_all_carry_a_reason() -> None:
    """An allowlist is only trustworthy if each entry says why it is there."""
    blank = sorted(key for key, reason in {**ALLOWLIST, **_NON_UI_PREFIXES}.items() if not reason.strip())
    assert not blank, f"allowlist entries with no justification: {blank}"


def test_findings_ledger_has_no_stale_entries(baseline: Reachability) -> None:
    """The ledger is self-cleaning: it may only hold routes that are still real orphans.

    Two failure modes, both of which mean "delete the line":

    * the route was deleted (the fix for several of these), so the entry is a waiver for
      nothing;
    * the route acquired a caller, so it is no longer a finding.

    Without this, the ledger would decay into the reflexive allowlist the whole check exists
    to avoid.
    """
    live = {str(route) for route in _route_table()}
    orphaned = {str(route) for route in baseline.orphans()}
    gone = sorted(set(KNOWN_ORPHANS) - live)
    fixed = sorted(set(KNOWN_ORPHANS) & live - orphaned)
    assert not gone, f"ledger entries whose route no longer exists -- delete these lines: {gone}"
    assert not fixed, f"ledger entries that now have a caller -- delete these lines: {fixed}"
