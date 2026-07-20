"""Guard: no browser timer may be started in markup without a matching teardown.

Several swap roots in this app are self-replacing polls that are *specified* to run
forever (``agents_table.html``'s ``#agents-table-section`` carries
``hx-swap="outerHTML"`` + ``hx-trigger="every 5s"`` and UI-SPEC §Polling LOCKS the
"never halts" invariant). htmx 2.x is loaded WITHOUT idiomorph, so an ``outerHTML``
swap genuinely destroys and recreates every node inside the root on each tick.

Alpine's ``destroyTree`` only runs cleanups it knows about — effects and directive
teardowns. A ``setInterval`` created imperatively inside ``x-init`` is never
registered, so each poll orphans a timer that keeps firing forever against a
detached reactive scope (and, via its closure, retains that scope on the JS heap).
On an always-open dashboard the orphan count grows without bound.

The documented Alpine mechanism for this is the ``destroy()`` method on the
component's ``x-data`` object: Alpine calls it before tearing the component down
(https://alpinejs.dev/globals/alpine-data#destroy-functions). It is present in the
pinned Alpine 3.15.12 bundle loaded by ``base.html``. Unlike ``$el._x_cleanups``
(internal API) it is public and version-stable.

The invariant asserted here is deliberately structural rather than a match on any
particular fix text: **any HTML tag whose attributes start a repeating/deferred
browser timer must, in the same tag, also clear it.** That catches a reintroduction
of the leak anywhere in the template tree, in any template, written any way.
"""

from __future__ import annotations

from pathlib import Path
import re


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"

# Repeating timers only. A ``setInterval`` orphaned by a swap fires FOREVER, so the
# orphan count on an always-on dashboard is unbounded — that is the leak this guards.
# One-shot ``setTimeout`` (the toast auto-dismiss idiom used in cue/toast/tag partials)
# fires once and retires, so it is a bounded, materially different shape and is
# deliberately NOT covered here.
_TIMER_PAIRS = (("setInterval(", "clearInterval("),)

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_jinja_comments(src: str) -> str:
    """Blank out ``{# ... #}`` blocks, preserving offsets, so prose never trips the scan."""
    return _JINJA_COMMENT.sub(lambda m: " " * len(m.group(0)), src)


def _html_tags(src: str) -> list[str]:
    """Return every opening tag's full source text, quote-aware.

    A naive ``<[^>]*>`` scan is wrong here: Alpine attribute values contain arrow
    functions (``() => now = new Date()``), so ``>`` occurs *inside* quoted values.
    This scanner tracks the active quote character and only treats an unquoted
    ``>`` as the end of the tag.
    """
    tags: list[str] = []
    i, n = 0, len(src)
    while i < n:
        if src[i] != "<" or i + 1 >= n or not (src[i + 1].isalpha()):
            i += 1
            continue
        j = i + 1
        quote: str | None = None
        while j < n:
            ch = src[j]
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == ">":
                break
            j += 1
        tags.append(src[i : j + 1])
        i = j + 1
    return tags


def test_no_markup_timer_without_teardown() -> None:
    """Every timer started in template markup is cleared in the same element."""
    offenders: list[str] = []
    for path in sorted(_TEMPLATES.rglob("*.html")):
        src = _strip_jinja_comments(path.read_text(encoding="utf-8"))
        for tag in _html_tags(src):
            for starter, clearer in _TIMER_PAIRS:
                if starter in tag and clearer not in tag:
                    rel = path.relative_to(_TEMPLATES)
                    offenders.append(f"{rel}: <{tag.lstrip('<').split()[0]} ...> starts {starter} with no {clearer}")

    assert not offenders, (
        "Timers started in markup must be torn down when the element is destroyed.\n"
        "Swap roots poll forever with hx-swap='outerHTML', so an uncleaned timer is\n"
        "orphaned on every tick. Use the documented Alpine x-data destroy() hook:\n"
        '  x-data="{ t: null, init() { this.t = setInterval(..., 1000) },\n'
        '            destroy() { clearInterval(this.t) } }"\n'
        "Offenders:\n  " + "\n  ".join(offenders)
    )
