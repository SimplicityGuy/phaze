"""phaze-tckiy: every htmx-verb ``<form>`` with no ``method`` must guard its submit controls.

A ``<form>`` carrying ``hx-post``/``hx-patch``/``hx-put``/``hx-delete`` but no ``method`` and no
``action`` relies entirely on htmx intercepting its submit. htmx wires that interception up inside
its deferred SETTLE task, not synchronously on swap, so a click landing between "attached to the
DOM" and "processed by htmx" falls through to the browser's own native submission: no method -> GET,
no action -> the current URL, hidden fields -> query parameters. A full page navigation that looks
like an ordinary reload while the intended write silently never happens. Measured at 4/40 (10%) for
a click fired the instant the form attached (phaze-boyl9). ``hx-patch`` is affected identically --
browsers have no native PATCH, so it degrades to the same GET.

phaze-5i74w fixed two such forms and asserted they were the only two. They were not: a multi-line
regex found ELEVEN, and phaze-tckiy fixed the remaining nine. This guard exists because that first
survey's *method* was the defect -- a single-line ``grep -o '<form[^>]*hx-post[^>]*>'`` cannot match
a ``<form>`` whose attributes wrap across lines, and it never looked for ``hx-patch`` at all. A
survey is evidence only if it could actually have found what it claims is absent, and a survey that
has to be re-run by hand will not be.

What it does NOT assert
=======================

That the guard *works* -- only that it is declared. Whether ``disabled`` + ``data-hx-guard`` is
actually released once htmx has processed the node, and whether an ``hx-confirm`` prompt therefore
becomes unskippable, is a runtime property of shell.html's ``htmx:load`` listener that no static
scan can see. ``tests/browser/test_htmx_native_submit_guard.py`` races real clicks in a real browser
for that half. This file is the cheap, always-on half: it fails the moment a TWELFTH such form is
added without a guard, which is the failure mode that produced this bead.

Adding ``method="post"`` + a real ``action`` to a form is an equally valid fix (progressive
enhancement) and satisfies this guard by dropping out of the scanned set entirely -- see
phaze-5i74w for why the guard attribute was chosen over that for these particular forms.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATES = _REPO_ROOT / "src" / "phaze" / "templates"

_HTMX_VERBS = ("hx-post", "hx-patch", "hx-put", "hx-delete")

# `re.S` is the entire point: these tags wrap across lines and the original single-line survey could
# not see them. `[^>]*` still stops at the first `>`, which is what keeps a match to one tag.
_FORM_OPEN = re.compile(r"<form\b[^>]*>", re.S)
_FORM_CLOSE = re.compile(r"</form\s*>", re.S)
_SUBMIT_CONTROL = re.compile(r"<(?:button|input)\b[^>]*>", re.S)

# Prose talks about `data-hx-guard` constantly in these files -- the comments explaining the
# mechanism sit right next to the buttons that use it -- so a naive substring search over a form's
# body is satisfied by a comment that mentions the fix rather than by markup that applies it.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _without_comments(source: str) -> str:
    """Blank out comment bodies, preserving newlines so reported line numbers stay true."""

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _HTML_COMMENT.sub(_blank, _JINJA_COMMENT.sub(_blank, source))


def _form_bodies(source: str) -> list[tuple[int, str, str]]:
    """Yield ``(line, open_tag, body)`` for every ``<form>`` in ``source``.

    Nesting-aware rather than "text up to the next ``</form>``": ``_changes_list.html`` closes its
    outer bulk form AFTER an inner form, so a naive first-close scan attributes the inner form's
    buttons to the outer one and both pass on the strength of one guard.
    """
    events: list[tuple[int, str]] = [(m.start(), "open") for m in _FORM_OPEN.finditer(source)]
    events += [(m.start(), "close") for m in _FORM_CLOSE.finditer(source)]
    events.sort()

    forms: list[tuple[int, str, str]] = []
    stack: list[int] = []
    for position, kind in events:
        if kind == "open":
            stack.append(position)
            continue
        if not stack:
            continue
        start = stack.pop()
        open_tag = _FORM_OPEN.match(source, start)
        assert open_tag is not None
        # Only this form's OWN markup: strip out every nested form's region so an inner form's
        # guarded button cannot vouch for its unguarded parent.
        body = source[open_tag.end() : position]
        for nested in forms:
            body = body.replace(nested[1] + nested[2], "")
        forms.append((source[:start].count("\n") + 1, open_tag.group(0), body))
    return forms


def test_every_method_less_htmx_form_guards_its_submit_controls() -> None:
    """The multi-line-aware scan from phaze-tckiy's Design section, as a permanent assertion."""
    unguarded: list[str] = []
    scanned = 0

    for path in sorted(_TEMPLATES.rglob("*.html")):
        source = _without_comments(path.read_text())
        for line, open_tag, body in _form_bodies(source):
            if not any(verb in open_tag for verb in _HTMX_VERBS) or "method=" in open_tag:
                continue
            scanned += 1
            where = f"{path.relative_to(_TEMPLATES).as_posix()}:{line}"
            submits = [tag for tag in _SUBMIT_CONTROL.findall(body) if 'type="submit"' in tag]
            if not submits:
                unguarded.append(f"{where}: no submit control found — a method-less htmx form with no button to guard")
                continue
            unguarded += [f"{where}: submit control declares no data-hx-guard: {tag.strip()[:120]}" for tag in submits if "data-hx-guard" not in tag]

    # phaze-tckiy counted 11. phaze-ur8o3 removed ONE of them with its template --
    # duplicates/partials/comparison_table.html's "Review decision" form, verified unreachable (its
    # sole host, GET /duplicates/{hash}/compare, had had no caller since the v7 cutover deleted
    # group_card.html) -- so the counted population is 10, not 10 because a guard went missing.
    # This number is EVIDENCE OF A COUNTED POPULATION, not a magic constant: lower it only alongside
    # the removal of a specific form you can name here, never to make a red run go green.
    assert scanned >= 10, f"the scan found only {scanned} method-less htmx forms; phaze-ur8o3 left 10 — the survey itself has regressed"
    assert not unguarded, "method-less htmx <form>s can fall through to a native GET:\n  " + "\n  ".join(unguarded)
