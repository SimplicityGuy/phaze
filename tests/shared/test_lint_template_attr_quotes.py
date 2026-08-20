"""Regression tests for ``scripts/lint_template_attr_quotes.py`` (phaze-e1zby).

``scripts/lint_template_attr_quotes.py`` is pre-commit-load-bearing: it is the replacement for a
warning comment that did not prevent a recurrence (see the script's own module docstring for the
full incident history). It gets its own tests for the same reason
``scripts/coverage_floor.py`` does (``tests/shared/test_coverage_floor.py``) -- a script that gates
commits needs proof it actually gates the thing it was written for, not just that it runs.

The load-bearing case is :func:`test_catches_the_reconstructed_2026_08_20_regression` -- AC1 is
explicit that a linter which cannot catch the bug that motivated it is not done. The fixture below
reconstructs that incident (a JS comment containing a literal ``"`` inside an ``x-data="..."``
block) rather than pointing at a historical commit, because the broken text was caught before it
was ever committed (see the script docstring) -- there is no "before" commit to diff against.

:func:`test_current_templates_have_no_truncated_directive_attributes` is AC2 made permanent: it
runs the real scanner over the real ``src/phaze/templates/`` tree and asserts zero findings, so a
future template edit that reintroduces this bug shape fails the unit suite, not just pre-commit on
whoever happens to type it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from types import ModuleType


# tests/shared/test_lint_template_attr_quotes.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "lint_template_attr_quotes.py"


def _load_lint_module() -> ModuleType:
    """Import the real ``scripts/lint_template_attr_quotes.py`` as a module.

    ``scripts/`` is not an importable package, so load it from its file path -- same approach as
    ``tests/shared/test_coverage_floor.py``. Exercising the real ``scan_text``/``main`` (rather
    than a reimplementation in the test file) keeps these tests honest about the shipped lint's
    actual behaviour.

    Registers the module in ``sys.modules`` before executing it -- required here (unlike
    ``coverage_floor``'s loader) because the lint script's ``Finding`` is a ``slots=True``
    dataclass under ``from __future__ import annotations``: the dataclasses machinery resolves its
    deferred field annotations via ``sys.modules[cls.__module__]``, which does not exist yet for a
    module that was merely ``exec``'d without ever being registered.
    """
    assert _SCRIPT.is_file(), f"lint script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("lint_template_attr_quotes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- AC1: must catch the real historical instance -----------------------------------------------


def test_catches_the_reconstructed_2026_08_20_regression(tmp_path: Path) -> None:
    """The literal reconstruction of the 2026-08-20 record_host.html incident must be flagged.

    A JS comment containing a literal ``"function"`` was added inside an ``x-data="{...}"`` block.
    HTML's delimiter-quote truncation cuts the attribute off mid-comment, leaving the object
    literal's opening braces unclosed -- exactly the brace-imbalance signature this lint looks for.
    """
    lint = _load_lint_module()
    broken = tmp_path / "broken_record_host_fragment.html"
    broken.write_text(
        """<div x-data="{
        open: false,
        opener: null,
        hide() {
            this.open = false;
            const target = this.opener;
            this.$nextTick(() => {
                // check target.focus is callable, e.g. typeof target.focus === "function"
                if (target && typeof target.focus === 'function') target.focus();
            });
        }
     }"
     @record:open.window="show($event.detail && $event.detail.el)">
</div>
""",
        encoding="utf-8",
    )

    findings = lint.scan_text(broken.read_text(encoding="utf-8"), broken)

    assert len(findings) == 1, "a linter that cannot catch the bug that motivated it is not done (AC1)"
    finding = findings[0]
    assert finding.attr == "x-data"
    assert not finding.unterminated
    assert finding.opens != finding.closes


def test_passes_on_the_corrected_shape(tmp_path: Path) -> None:
    """The actual fix (record_host.html's real hide(), no literal double quote in the comment) is clean."""
    lint = _load_lint_module()
    fixed = tmp_path / "fixed_record_host_fragment.html"
    fixed.write_text(
        """<div x-data="{
        open: false,
        opener: null,
        hide() {
            this.open = false;
            const target = this.opener;
            this.$nextTick(() => {
                if (target && target.isConnected && typeof target.focus === 'function') {
                    target.focus();
                } else if (window._focusStageHeading) {
                    window._focusStageHeading();
                }
            });
        }
     }"
     @record:open.window="show($event.detail && $event.detail.el)">
</div>
""",
        encoding="utf-8",
    )

    assert lint.scan_text(fixed.read_text(encoding="utf-8"), fixed) == []


# --- AC2, made permanent: the real template tree must stay clean ---------------------------------


def test_current_templates_have_no_truncated_directive_attributes() -> None:
    """A standing regression guard: every template in the tree, scanned with the shipped scanner.

    If this ever fails, treat it as AC2 does: either a real latent instance of the truncation bug
    (say so loudly, it's a live defect) or a false positive in the heuristic that needs fixing --
    never silence it by narrowing coverage without addressing which one it is.
    """
    lint = _load_lint_module()
    findings = [finding for path in lint._default_files() for finding in lint.scan_text(path.read_text(encoding="utf-8"), path)]
    assert findings == [], "\n".join(lint.format_finding(f) for f in findings)


def test_main_exits_zero_on_the_current_template_tree() -> None:
    """The CLI entry point pre-commit actually invokes, exercised end to end."""
    lint = _load_lint_module()
    assert lint.main([]) == 0


# --- Coverage (AC3): each directive family is actually recognised --------------------------------


def test_covers_x_show(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "x_show.html"
    # The first literal `"` (before `bar`) truncates the value at `{ a: 1, label: `, one `{` unclosed.
    path.write_text('<div x-show="{ a: 1, label: "bar" }"></div>\n', encoding="utf-8")
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].attr == "x-show"


def test_covers_alpine_bind_shorthand(tmp_path: Path) -> None:
    """``:ATTR`` (x-bind shorthand), e.g. ``:class="{'active': isActive === \\"x\\"}"``."""
    lint = _load_lint_module()
    path = tmp_path / "bind.html"
    path.write_text("""<div :class="{'active': isActive === "x"}"></div>\n""", encoding="utf-8")
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].attr == ":class"


def test_covers_alpine_event_shorthand(tmp_path: Path) -> None:
    """``@EVENT`` (x-on shorthand)."""
    lint = _load_lint_module()
    path = tmp_path / "at.html"
    path.write_text("""<button @click="if (x) { doThing("oops") }"></button>\n""", encoding="utf-8")
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].attr == "@click"


def test_covers_htmx_inline_handlers(tmp_path: Path) -> None:
    """``hx-on:EVENT`` and the ``hx-on::EVENT`` double-colon form."""
    lint = _load_lint_module()
    path = tmp_path / "hxon.html"
    path.write_text(
        """<div hx-on::after-swap="if (x) { console.log("oops") }"></div>\n""",
        encoding="utf-8",
    )
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].attr == "hx-on::after-swap"


def test_covers_hx_vals_when_double_quoted(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "hxvals.html"
    path.write_text("""<button hx-vals="{"delta": -10}"></button>\n""", encoding="utf-8")
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].attr == "hx-vals"


# --- Deliberately out of scope (AC3's "what we did not cover") -----------------------------------


def test_does_not_cover_single_quoted_attributes(tmp_path: Path) -> None:
    """The mirror-image hazard (a literal ``'`` truncating a single-quoted value) is out of scope."""
    lint = _load_lint_module()
    path = tmp_path / "single.html"
    # This codebase's real convention for hx-vals: single-quoted precisely so it can hold literal
    # double quotes safely. Unbalanced braces would still trip the scanner if it looked -- it must not.
    path.write_text("""<button hx-vals='{"delta": -10}"unbalanced'></button>\n""", encoding="utf-8")
    assert lint.scan_text(path.read_text(encoding="utf-8"), path) == []


def test_does_not_cover_plain_htmx_attributes(tmp_path: Path) -> None:
    """hx-target/hx-swap/etc. are plain strings, never JS/JSON -- out of scope even with stray braces."""
    lint = _load_lint_module()
    path = tmp_path / "plain.html"
    path.write_text('<div hx-target="#weird{selector"></div>\n', encoding="utf-8")
    assert lint.scan_text(path.read_text(encoding="utf-8"), path) == []


def test_does_not_cover_ordinary_html_attributes(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "ordinary.html"
    path.write_text('<div title="a stray { brace"></div>\n', encoding="utf-8")
    assert lint.scan_text(path.read_text(encoding="utf-8"), path) == []


# --- The other bug this same scan catches for free ------------------------------------------------


def test_flags_an_unterminated_attribute(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "unterminated.html"
    path.write_text('<div x-data="{ open: false\n', encoding="utf-8")
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    assert findings[0].unterminated


# --- Escape hatch -----------------------------------------------------------------------------


def test_escape_hatch_suppresses_a_single_attribute(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "allowed.html"
    path.write_text(
        "<div x-show=\"msg === 'unexpected }' /* phaze-e1zby:allow-brace-mismatch */\"></div>\n",
        encoding="utf-8",
    )
    assert lint.scan_text(path.read_text(encoding="utf-8"), path) == []


# --- Message quality (AC5) ------------------------------------------------------------------------


def test_failure_message_names_file_line_attribute_and_mechanism(tmp_path: Path) -> None:
    lint = _load_lint_module()
    path = tmp_path / "record_host.html"
    path.write_text(
        """

<div x-data="{ typeof target.focus === "function" }"></div>
""",
        encoding="utf-8",
    )
    findings = lint.scan_text(path.read_text(encoding="utf-8"), path)
    assert len(findings) == 1
    message = lint.format_finding(findings[0])
    assert str(path) in message or path.name in message
    assert ":3:" in message  # the attribute starts on line 3
    assert "x-data" in message
    assert "truncat" in message.lower()
    assert "unbalanced" in message.lower()
