"""phaze-tp52f: rewriting an ``hx-<verb>`` attribute at runtime is INERT unless the node is re-processed.

The general form of the defect this bead fixed, made permanent. ``_changes_list.html``'s ``sync()``
rewrote each facet tab's ``hx-get`` to carry the operator's row selection; the attribute genuinely
changed, and nothing else did. htmx 2.0.10 reads a verb's path exactly once, in ``processVerbs``,
and closes over the string::

    const path = getAttributeValue(elt, 'hx-' + verb)     // read at processNode time
    addTriggerHandler(elt, spec, nodeData, function (node, evt) {
        issueAjaxRequest(verb, path, elt, evt)             // the captured string, never re-read
    })

So the DOM said ``selected=<id>`` and the socket carried the URL the element was SERVER-RENDERED
with. Measured consequence: an operator could not DESELECT a row -- unticking it and switching facet
tab shipped the stale id, and the row came back checked with the bulk approve control re-enabled, on
every switch. ``htmx.process(element)`` after the write re-runs ``processVerbs`` and re-captures the
path; that is the fix, and ``tests/browser/test_changes_facet_selection_wire.py`` is its runtime
evidence, read off the wire rather than off the attribute.

Why a scan and not just a comment at the call site
==================================================

CLAUDE.md rule 5: a lesson recorded at one site states its general form or says why it has none.
This one has a general form -- *any* runtime write to an htmx verb attribute is inert on its own --
and the failure it produces is invisible to every cheap check. The attribute assertion passes. The
template renders. The click "works", against the wrong URL. ``tests/browser/test_changes_review.py``
held exactly such an assertion, green, for the whole life of the bug, and was named as though it
crossed a tab boundary when it never did. A future third rewrite site would reproduce that silently,
which is what this file exists to prevent.

What it does NOT assert
=======================

That the re-processing is CORRECT, or that it happens on the right element, or that it happens
before the next request -- those are runtime properties no static scan can see, and the browser
suite above owns them. Proximity is the heuristic: an ``htmx.process`` call within
:data:`_WINDOW` lines of the write. That is a tripwire, not a proof. It cannot parse JavaScript and
does not try to; its job is to make a new rewrite site impossible to add without a human looking at
this docstring.

Alternatives that satisfy it by dropping out of the scanned set are fine and sometimes better: carry
the value in ``hx-vals`` (which htmx evaluates at request time) or in the request body, or re-render
the element server-side. Only the attribute-rewrite shape needs the re-process.

Why this file exists at all, rather than the coverage gate
==========================================================

The general form, stated because the specific case is the less useful half: **a template-only defect
is invisible to the coverage gate in principle.** phaze-tp52f's entire product fix is one Jinja
template -- zero Python statements added or removed under ``src/phaze`` -- so the repo's 95% floor
saw no delta, and would have seen none however wrong the template was. The floor could not have
caught this defect and cannot catch the next one of its shape. That is not a criticism of the floor;
it measures Python statements, and this class of bug has none.

So for behaviour that lives in a template or in front-end JavaScript, the browser suite and this scan
ARE the evidence, not a supplement to a coverage number that structurally cannot see them. Read a
green coverage report on a template change as silence, never as agreement.

The corollary is what makes it actionable: a front-end change whose only evidence is "the suite is
green and coverage held" has no evidence at all. It needs an assertion that reaches the DOM, the
wire, or -- as here -- the source text itself.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCES = (
    _REPO_ROOT / "src" / "phaze" / "templates",
    _REPO_ROOT / "src" / "phaze" / "static",
)

# How far after a rewrite an `htmx.process` call still counts as belonging to it. Ten lines covers
# "rewrite href, rewrite hx-get, re-process" with room to spare, while staying far too small to be
# satisfied by an unrelated call elsewhere in the same handler.
_WINDOW = 10

# Both spellings htmx honours, and both ways to write one from JavaScript: `setAttribute('hx-get',
# ...)` and the `dataset` form of `data-hx-get`. Quote style is free; the verb list is htmx's.
_VERBS = ("get", "post", "put", "patch", "delete")
_SET_ATTRIBUTE = r"""setAttribute\(\s*["'`](?:data-)?hx-(?:{verbs})\b""".format(verbs="|".join(_VERBS))
_DATASET = r"""dataset\s*\.\s*hx(?:{verbs})\s*=""".format(verbs="|".join(verb.capitalize() for verb in _VERBS))
_REWRITE = re.compile(f"{_SET_ATTRIBUTE}|{_DATASET}", re.I)
_REPROCESS = re.compile(r"htmx\s*\.\s*process\s*\(|\breprocess\s*\(")

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _without_comments(source: str) -> str:
    """Blank out comment bodies, preserving newlines so reported line numbers stay true.

    Load-bearing here for the same reason as in the sibling guard scan: this file's own mechanism is
    explained in a long HTML comment sitting directly above the code it describes, and that comment
    quotes ``setAttribute("hx-get", ...)`` verbatim. Scanning raw text would report the explanation
    as a violation.
    """

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _HTML_COMMENT.sub(_blank, _JINJA_COMMENT.sub(_blank, source))


def _rewrite_sites() -> list[tuple[str, int, list[str]]]:
    """Every runtime htmx-verb rewrite in the shipped front end, with its following window."""
    sites: list[tuple[str, int, list[str]]] = []
    for root in _SOURCES:
        for path in sorted([*root.rglob("*.html"), *root.rglob("*.js")]):
            lines = _without_comments(path.read_text()).splitlines()
            for index, line in enumerate(lines):
                if _REWRITE.search(line):
                    sites.append((path.relative_to(_REPO_ROOT).as_posix(), index + 1, lines[index : index + 1 + _WINDOW]))
    return sites


def test_every_runtime_htmx_verb_rewrite_reprocesses_its_node() -> None:
    """A rewrite with no ``htmx.process`` nearby updates the DOM and nothing else."""
    sites = _rewrite_sites()

    # The scan must be able to find what it claims is absent. Two sites exist today, both in
    # _changes_list.html's sync(); a scan that silently matched nothing would pass forever.
    assert len(sites) >= 2, f"the scan found only {len(sites)} runtime htmx-verb rewrites; phaze-tp52f left 2 — the survey itself has regressed"

    inert = [
        f"{path}:{line} rewrites an htmx verb attribute with no htmx.process() within {_WINDOW} lines"
        for path, line, window in sites
        if not any(_REPROCESS.search(one) for one in window)
    ]
    assert not inert, (
        "htmx captures a verb's path at processNode time and never re-reads the attribute, so these rewrites never reach the wire:\n  "
        + "\n  ".join(inert)
    )
