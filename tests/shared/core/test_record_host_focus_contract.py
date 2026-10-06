"""phaze-x1cv5 -- the record drawer's focus contract, pinned at the template-source level.

The behavioural proof (focus lands in the drawer before the record loads, Tab stays inside, Esc
restores focus to the opener, no aria-hidden console warning) lives in
``tests/browser/test_files_record.py::test_record_drawer_takes_focus_at_once_traps_tab_and_restores_focus``.
This fast-lane test pins the attributes and JS hooks that implement it, so a template edit that
silently drops one fails here without needing a browser.
"""

from __future__ import annotations

from pathlib import Path


_HOST = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates" / "shell" / "partials" / "record_host.html"


def _source() -> str:
    return _HOST.read_text()


def test_panel_is_a_focusable_focus_trapped_modal_without_the_aria_hidden_inert_modifier() -> None:
    source = _source()
    assert 'tabindex="-1"' in source.split('x-ref="panel"', 1)[1].split(">", 1)[0], "the panel itself cannot take focus while the record loads"
    assert 'x-trap.noscroll.noreturn="open"' in source, "the open drawer no longer traps Tab"
    assert "x-trap.inert.noscroll" not in source, "Alpine's .inert modifier applies aria-hidden to the page while the opener still holds focus"


def test_open_moves_focus_into_the_drawer_before_making_the_background_inert() -> None:
    source = _source()
    show = source.split("show(el) {", 1)[1].split("},", 1)[0]
    assert "this.takeFocus()" in show, "show() no longer moves focus into the drawer"
    take = source.split("takeFocus() {", 1)[1].split("setBackgroundInert(on)", 1)[0]
    assert take.index("panel.focus(") < take.index("setBackgroundInert(true)"), "the background is made inert BEFORE focus leaves the opener"
    assert "setAttribute('inert'" in source, "the background is not given the real inert attribute"


def test_close_removes_inert_before_returning_focus_to_the_opener() -> None:
    hide = _source().split("hide() {", 1)[1]
    assert hide.index("setBackgroundInert(false)") < hide.index("target.focus()"), "focus is returned to an opener that is still inert"
