"""phaze-7u4sy (C3): the docked record inspector's non-modal contract, pinned at the template-source level.

The behavioural proof (one owner per open, row focus retained, inside-only Escape, no inert or trap
across the xl boundary) lives in ``tests/browser/test_files_record_dock.py``. This fast-lane test pins
the source facts a template edit could silently drop: the dock must never grow a modal's mechanisms,
must live outside both swap hosts, and the two hosts must consult one owner decision.
"""

from __future__ import annotations

from pathlib import Path


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_DOCK = (_TEMPLATES / "shell" / "partials" / "record_dock.html").read_text()
_HOST = (_TEMPLATES / "shell" / "partials" / "record_host.html").read_text()
_SHELL = (_TEMPLATES / "shell" / "shell.html").read_text()


def _markup() -> str:
    """The dock's markup, without its comment block and script (both legitimately name the modal's mechanisms)."""
    without_comment = _DOCK.split("#}", 1)[1]
    return without_comment.split("<script>", 1)[0]


def test_the_dock_is_a_non_modal_aside_without_any_modal_mechanism() -> None:
    markup = _markup()
    assert "<aside " in markup
    for forbidden in ('role="dialog"', "aria-modal", "x-trap", "inert", 'x-ref="panel"'):
        assert forbidden not in markup, f"the dock grew {forbidden}: it is a non-modal inspector"
    script = _DOCK.split("<script>", 1)[1]
    assert "setAttribute('inert'" not in script, "the dock must never make the page inert"


def test_the_dock_is_hosted_outside_both_swap_hosts() -> None:
    assert '{% include "shell/partials/record_dock.html" %}' in _SHELL
    stage = _SHELL.split('<div id="stage-workspace"', 1)[1].split("</main>", 1)[0]
    assert "record_dock" not in stage, "the dock must not sit inside #stage-workspace"
    assert "files-table-view" not in _markup()


def test_both_hosts_consult_the_one_owner_decision() -> None:
    assert "window._recordHostFor" in _HOST, "the modal host no longer defers to the shared owner decision"
    assert "window._recordHostFor(el) !== 'dock'" in _DOCK
    assert "@record:close-all.window" in _HOST and "@record:close-all.window" in _DOCK, "a breakpoint crossing no longer closes both hosts"
    # The target is chosen in the CAPTURE phase, before htmx resolves it; a later attribute change does not retarget.
    assert "addEventListener('click', aim, true)" in _DOCK
    assert "beforeRequest" not in _DOCK.split("aim(event)", 1)[1].split("ArrowDown", 1)[0], "the target is chosen too late to take effect"
