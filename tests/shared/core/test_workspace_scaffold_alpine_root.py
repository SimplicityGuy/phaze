"""phaze-8pmj — the workspace scaffold must always emit an Alpine root.

``_workspace_scaffold.html``'s ``ws.workspace()`` macro used to emit ``x-data`` on its ``<section>``
root ONLY when the caller passed ``x_data`` (``<section{% if x_data %} x-data="{{ x_data }}"{% endif
%}>``). 12 of its 13 real callers (every stage but ``discover_workspace.html``) never pass
``x_data``, so those stage fragments rendered with NO Alpine root at all.

Per this repo's own locked convention (see ``test_rail_root_carries_alpine_x_data`` below, and the
CR-01 comment in ``_file_table.html``): Alpine only initializes ``x-data``-ROOTED subtrees. A rail
click swap survives because Alpine's mutation observer initializes newly-inserted nodes regardless
of ancestry, but a FULL-DOCUMENT render (any direct navigation, reload, or bookmark of
``/s/<stage>`` — ``shell.py`` serves the full shell when ``wants_fragment`` is false) does not walk
back up to find an ancestor ``x-data``, and ``<html>``/``<body>`` carry none either. So on reload
every store-bound directive the header/actions slot renders was permanently inert: the sub-count's
bare ``x-text`` (no fallback text) rendered blank forever, and the R-4 double-enqueue busy-gate
``:disabled="$store.pipeline.<stage>Busy > 0"`` never disabled its EXTRACT ALL / FINGERPRINT ALL /
etc. button — the exact double-enqueue hazard the busy-gate exists to prevent.

The fix makes the root unconditional: ``<section x-data="{{ x_data or '{}' }}">`` — a bare ``{}``
when the caller passes none, mirroring the Phase-93 rail fix (``<aside x-data>``).

Like ``test_cmdk_palette_record_open_closes_palette.py``, this renders the real templates with no
DB and no client fixture (fast lane) and asserts by parsed-DOM attribute, never by id/position.
"""

from __future__ import annotations

from pathlib import Path
import re

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_SCAFFOLD = _TEMPLATES / "pipeline" / "partials" / "_workspace_scaffold.html"

# Jinja block comments -- {# ... #}, DOTALL so a multi-line comment (this file's docs) is stripped
# as one unit, not line by line. Mirrors _strip_comments in test_a11y_guards.py.
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_comments(text: str) -> str:
    return _JINJA_COMMENT_RE.sub("", text)


# The 12 real ws.workspace() callers that pass NO x_data (the failure surface) -- every stage but
# discover_workspace.html, the one caller that already supplies its own x_data. Each of these
# renders standalone with an empty Jinja context (no DB, no app), because none of them touch a
# variable that lacks safe Undefined-falsy fallback behavior in Jinja ({% if %} / {% for %} over an
# Undefined value degrades to "nothing", it does not raise) at the top level.
_STANDALONE_CALLERS = [
    "pipeline/partials/analyze_workspace.html",
    "pipeline/partials/dedupe_workspace.html",
    "pipeline/partials/trackid_workspace.html",
    "pipeline/partials/tagwrite_workspace.html",
    "pipeline/partials/cue_workspace.html",
    "pipeline/partials/rename_workspace.html",
    "pipeline/partials/metadata_workspace.html",
    "pipeline/partials/fingerprint_workspace.html",
    "pipeline/partials/move_workspace.html",
    "shell/partials/summary_placeholder.html",
]

# tracklist_workspace.html and propose_workspace.html need substantial route-supplied context
# (tracklist_steps, propose_pagination/propose_stats/propose_view, further nested includes) to
# render at all, so they are not exercised as a live render here -- the macro-level guard below
# (test_workspace_scaffold_macro_always_emits_alpine_root) covers them too, since it asserts the
# macro itself never gates the root behind the caller's x_data.
_CONTEXT_HEAVY_CALLERS = [
    "pipeline/partials/tracklist_workspace.html",
    "pipeline/partials/propose_workspace.html",
]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)


def test_workspace_scaffold_macro_always_emits_alpine_root() -> None:
    """The scaffold's <section> root must carry x-data UNCONDITIONALLY -- not gated by x_data.

    Without it, any ws.workspace() caller that has no need for its own component-local Alpine
    state (12 of 13 today) ships a store-bound subtree with nothing to bind it.
    """
    source = _strip_comments(_SCAFFOLD.read_text())
    m = re.search(r"<section\b[^>]*>", source)
    assert m, "expected the scaffold's <section> root"
    tag = m.group(0)
    assert "{% if x_data %}" not in tag, (
        f"the scaffold's <section> root ({tag!r}) still gates x-data behind the caller's x_data -- "
        f"it must always emit one (a bare '{{}}' when the caller passes none)"
    )
    assert re.search(r"\bx-data\b", tag), "the scaffold's <section> root must carry x-data unconditionally"


@pytest.mark.parametrize("template_path", _STANDALONE_CALLERS)
def test_workspace_fragment_root_carries_alpine_x_data(template_path: str) -> None:
    """Every real stage fragment that omits x_data must still render with a live Alpine root.

    Regression (phaze-8pmj): before the fix, none of these 10 stage fragments carried any
    x-data-rooted ancestor, so their sub-count x-text and R-4 busy-gate :disabled bindings were
    permanently inert on a direct navigation/reload/bookmark of the stage.
    """
    html = _env().get_template(template_path).render()
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find("section")
    assert isinstance(section, Tag), f"{template_path} did not render an outer <section>"
    assert section.has_attr("x-data"), (
        f"{template_path}'s <section> root has no x-data -- every store-bound directive inside it is inert on a full-document render"
    )


def test_context_heavy_callers_are_still_enumerated() -> None:
    """Sanity: the two callers this file can't live-render still exist and still omit x_data.

    If either template starts passing x_data (or stops importing the scaffold), this fails so the
    exemption list above gets revisited instead of silently going stale.
    """
    for rel_path in _CONTEXT_HEAVY_CALLERS:
        source = (_TEMPLATES / rel_path).read_text()
        assert "ws.workspace(" in source, f"{rel_path} no longer calls ws.workspace() -- update this test's caller inventory"
        call_start = source.index("ws.workspace(")
        call_end = source.index(")", call_start)
        assert "x_data" not in source[call_start:call_end], f"{rel_path} now passes x_data -- move it into _STANDALONE_CALLERS or re-verify manually"


def test_discover_workspace_keeps_its_own_x_data() -> None:
    """Sanity: the one caller that already supplies x_data is unaffected by the `or '{}'` fallback."""
    html = _env().get_template("pipeline/partials/discover_workspace.html").render()
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find("section")
    assert isinstance(section, Tag), "discover_workspace.html did not render an outer <section>"
    assert section.get("x-data") == "{ scanOpen: false }", (
        f"discover_workspace.html's own x_data was clobbered by the fallback: {section.get('x-data')!r}"
    )
