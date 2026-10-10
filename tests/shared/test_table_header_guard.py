"""phaze-tsbdw: every table header in the app uses ONE shared style, and sortable headers carry the standard focus ring.

Before this bead there were three header variants -- Jura 11px letter-spaced (Files, Metadata, Analyze,
Tracklist, Apply) against Inter bold uppercase with no letter-spacing (Discover Recent scans, Agents,
Runtime config) and a third 10px one (the Analyze queues) -- and the sortable-header buttons fell back to the
browser's default outline in most of them. The shared classes live in ``assets/src/app.css``:

* ``table-th``      -- on every column ``<th>``: the type treatment (Jura, 11px, uppercase, letter-spaced, muted).
* ``table-head``    -- on every ``<thead>``: the band and its border.
* ``table-th-sort`` -- on a sortable header's button: the same type, hover colour, and the focus-visible ring.

The real-browser half (computed styles, the ring actually painting) is ``tests/browser/test_table_header_style.py``.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "src" / "phaze" / "templates"
_APP_CSS = (_ROOT / "assets" / "src" / "app.css").read_text()

_TAG = re.compile(r"<(th|thead)(?=[\s>])[^>]*>")
_SORT_BUTTON = re.compile(r'<button\b[^>]*hx-get="\{\{ sort\.url_for\([^"]*"[^>]*>', re.S)

# A <thead> that is deliberately NOT the neutral band: the Apply workspace's failed-rows table is tinted red so the
# error state reads as one. Its <th>s still carry table-th (the type is shared); only the band differs.
_TINTED_THEAD = "bg-red-50"


def _templates() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


def _classes(tag: str) -> list[str]:
    match = re.search(r'class="([^"]*)"', tag)
    return match.group(1).split() if match else []


def test_templates_are_found() -> None:
    assert len(_templates()) > 50


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(_TEMPLATES)))
def test_every_column_header_cell_carries_the_shared_class(path: Path) -> None:
    """A ``<th>`` without ``table-th`` is a fourth header variant waiting to happen. Row headers
    (``scope="row"`` or ``scope="rowgroup"``) describe body data, so they are exempt."""
    offenders = []
    for match in _TAG.finditer(path.read_text()):
        tag = match.group(0)
        if match.group(1) == "th":
            if 'scope="row"' in tag or 'scope="rowgroup"' in tag:
                continue
            if "table-th" not in _classes(tag):
                offenders.append(tag)
        elif _TINTED_THEAD not in tag and "table-head" not in _classes(tag):
            offenders.append(tag)
    assert not offenders, f"{path.name}: header markup without the shared class: {offenders}"


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(_TEMPLATES)))
def test_every_sortable_header_button_carries_the_shared_sort_class(path: Path) -> None:
    for match in _SORT_BUTTON.finditer(path.read_text()):
        assert "table-th-sort" in _classes(match.group(0)), f"{path.name}: sortable header button lacks table-th-sort: {match.group(0)}"


def test_the_guard_discriminates() -> None:
    """The guard must reject the wrong markup, not merely accept the right one."""
    assert "table-th" not in _classes('<th scope="col" class="px-4 py-3 font-semibold uppercase">')
    assert "table-th" not in _classes("<th>")
    assert "table-th" in _classes('<th scope="col" class="table-th px-4">')


def test_shared_classes_are_defined_with_the_standard_focus_ring() -> None:
    """``table-th-sort`` must define the app's standard ring (ring-2 ring-blue-500 on focus-visible, as the pager buttons
    and filter selects use), not fall back to the browser outline."""
    block = re.search(r"\.table-th-sort\s*\{([^}]*)\}", _APP_CSS)
    assert block, "app.css no longer defines .table-th-sort"
    rules = block.group(1)
    assert "focus-visible:ring-2" in rules and "focus-visible:ring-blue-500" in rules
    th = re.search(r"\.table-th\s*\{([^}]*)\}", _APP_CSS)
    assert th
    assert "'Jura'" in th.group(1) and "uppercase" in th.group(1) and "letter-spacing" in th.group(1)
    assert re.search(r"\.table-head\s*\{", _APP_CSS)
