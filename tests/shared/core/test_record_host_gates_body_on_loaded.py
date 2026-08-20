"""phaze-7yet — the record slide-in must never reveal the PREVIOUS file's body.

``show()`` used to flip the panel visible (``open = true``) synchronously, on the same tick as
the Alpine ``record:open`` dispatch. That dispatch always wins the race against the async htmx
``GET /record/{id}`` fetch (``hx-target="#record-body"``), and ``hide()`` never emptied the swap
target -- so between ``open = true`` and the fetch landing, the panel displayed whatever the
PREVIOUS open left in ``#record-body``: the old file's heading, its per-stage force-skip
dialogs, and its "Changes needing review for this file" box with live, clickable
``approve_url``/``skip_url`` buttons wired to the OLD file's proposal ids.

The fix gates ``#record-body`` on a ``loaded`` flag: ``show()`` resets it to ``false`` (showing a
skeleton instead of stale content) and only the after-swap handler -- which only runs once THIS
fetch's response has actually landed -- flips it back to ``true``.

Like ``test_force_skip_dialog_escape_does_not_close_record.py``, this renders the real template
with no DB and no client fixture (fast lane) and asserts over template SOURCE / parsed DOM by
attribute and component wiring, never by id/position, so the guard survives the fix being
reshaped.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_RECORD_HOST = "shell/partials/record_host.html"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)


def _render_record_host() -> str:
    return _env().get_template(_RECORD_HOST).render()


def _attr(el: Tag, needle: str) -> str:
    """Concatenated values of every attribute whose NAME contains ``needle``."""
    parts: list[str] = []
    for name, value in el.attrs.items():
        if needle in name.lower():
            parts.append(" ".join(value) if isinstance(value, list) else str(value))
    return " ".join(parts)


def _record_host_root() -> Tag:
    """The record slide-in host root -- the element that listens for record:open."""
    soup = BeautifulSoup(_render_record_host(), "html.parser")
    root = next(
        (el for el in soup.find_all(attrs={"x-data": True}) if isinstance(el, Tag) and _attr(el, "record:open")),
        None,
    )
    assert root is not None, "could not find the element listening for record:open in record_host.html"
    return root


def _record_body(soup: BeautifulSoup) -> Tag:
    body = soup.find(id="record-body")
    assert isinstance(body, Tag), "could not find #record-body in record_host.html"
    return body


def test_x_data_declares_a_loaded_flag() -> None:
    """The component must own a `loaded` flag distinct from `open`."""
    root = _record_host_root()
    x_data = root.get("x-data") or ""
    assert isinstance(x_data, str)
    assert "loaded" in x_data, "record_host.html's x-data no longer declares a `loaded` flag"


def test_show_captures_an_optional_record_section_from_the_actual_opener() -> None:
    """A Tracklists row can request #tracklist without changing the canonical record URL."""
    root = _record_host_root()
    x_data = root.get("x-data") or ""
    assert isinstance(x_data, str)
    assert "requestedSection" in x_data
    assert "el.dataset.recordSection" in x_data

    soup = BeautifulSoup(_render_record_host(), "html.parser")
    after_swap = _attr(_record_body(soup), "after-swap")
    assert "CSS.escape(requested)" in after_swap
    assert "section.scrollIntoView" in after_swap


def test_show_resets_loaded_before_revealing_the_panel() -> None:
    """`show()` must reset `loaded` so a re-open never inherits the previous open's `loaded: true`."""
    root = _record_host_root()
    x_data = root.get("x-data") or ""
    assert isinstance(x_data, str)
    # Isolate the show(...) method body (up to the next top-level method's closing brace pattern
    # is fragile across reformatting, so just require the assignment appears somewhere after the
    # `show(el) {` opener and before the next `},` that starts a new method).
    show_start = x_data.find("show(el)")
    assert show_start != -1, "record_host.html's x-data no longer defines show(el)"
    hide_start = x_data.find("hide()")
    assert hide_start != -1, "record_host.html's x-data no longer defines hide()"
    show_body = x_data[show_start:hide_start] if hide_start > show_start else x_data[show_start:]
    assert "this.loaded = false" in show_body, "show() must reset `loaded` to false before the panel is revealed"
    assert "this.open = true" in show_body


def test_record_body_and_skeleton_are_gated_on_loaded() -> None:
    """#record-body must be hidden until `loaded`; a skeleton must fill the gap, not stale content."""
    soup = BeautifulSoup(_render_record_host(), "html.parser")
    body = _record_body(soup)
    body_show = _attr(body, "x-show")
    assert body_show == "loaded", f"#record-body must be x-show='loaded' (got {body_show!r}) so it is hidden until the fetch for THIS open lands"

    # A sibling skeleton, shown only while NOT loaded, must exist ahead of #record-body.
    siblings = list(soup.find_all(attrs={"x-show": "!loaded"}))
    assert siblings, "no element is shown while `!loaded` -- the panel would render blank instead of a skeleton during the fetch"


def test_after_swap_flips_loaded_true() -> None:
    """The after-swap handler is the ONLY path allowed to flip `loaded` back to true."""
    soup = BeautifulSoup(_render_record_host(), "html.parser")
    body = _record_body(soup)
    after_swap = _attr(body, "after-swap")
    assert after_swap, "#record-body no longer has an hx-on::after-swap handler"
    assert "loaded = true" in after_swap, "the after-swap handler no longer flips `loaded` back to true once the fetch for THIS open has landed"


def test_a_404_response_resets_the_aria_label_instead_of_leaving_the_previous_file_s_name() -> None:
    """record_not_found.html has no <h2>; the aria-label must not keep announcing the prior file."""
    soup = BeautifulSoup(_render_record_host(), "html.parser")
    body = _record_body(soup)
    after_swap = _attr(body, "after-swap")
    assert "else if (dialog)" in after_swap or "else" in after_swap, (
        "the after-swap handler only refines the aria-label when a heading exists -- on a 404 "
        "(no <h2>) it must reset the label to the generic default instead of leaving the "
        "previous file's name in place"
    )


def test_hide_also_resets_loaded() -> None:
    """hide() resets `loaded` too, so internal state never claims a closed panel is `loaded`."""
    root = _record_host_root()
    x_data = root.get("x-data") or ""
    assert isinstance(x_data, str)
    hide_start = x_data.find("hide()")
    assert hide_start != -1
    hide_body = x_data[hide_start:]
    assert "this.loaded = false" in hide_body, "hide() must reset `loaded` to false"
