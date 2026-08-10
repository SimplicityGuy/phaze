"""THE htmx response-shape contract for every handler in phaze (phaze-qi9j).

This module is the SINGLE owner of "what DOCUMENT SHAPE and what STATUS does this handler owe the
htmx client that is about to consume it". Every router that branches on ``HX-Request``, or that
renders an error the operator is meant to READ, composes the helpers here rather than re-deriving
the decision. Before adding a handler that returns anything other than one unconditional full page,
read this docstring; it is the contract, not a suggestion.

WHY THIS EXISTS
---------------
phaze-qi9j found ``audit_log`` (``routers/execution.py``) choosing fragment-vs-full-page on
``request.headers.get("HX-Request") == "true"`` alone, returning the chrome-less
``execution/partials/audit_content.html`` (filter tabs + table, no ``base.html``) for ANY htmx
request. But the audit filter tabs push URL state -- ``execution/partials/filter_tabs.html`` sets
``hx-push-url="true"`` on each ``/audit/?status=...`` tab. On a history-cache miss (htmx's
``historyCacheSize`` is 10, so eviction is routine; cleared ``localStorage`` and a fresh session do
it too), htmx re-fetches that pushed URL as a RESTORE request carrying BOTH ``HX-Request: true``
AND ``HX-History-Restore-Request: true``. On restore htmx ignores ``hx-target`` entirely and swaps
the response into the history element -- which, absent any ``[hx-history-elt]`` in this repo, is
``<body>``. The handler saw only ``HX-Request`` and answered with the fragment, so htmx replaced
the whole body with an orphaned filter-tab bar and table: no ``<h1>``, no nav, no theme toggle, and
no way out but a manual reload.

That ``HX-Request`` arrives at all on a restore is not an accident to be worked around; it is
htmx's documented default (``htmx.config.historyRestoreAsHxRequest: true``). The header answers
"did htmx issue this request", which is NOT the question a handler branching on document shape is
actually asking.

The second half of the class is the same mistake pointed at the status line instead of the body.
htmx 2.x ships ``htmx.config.responseHandling`` defaulting to
``[{code: "204", swap: false}, {code: "[23]..", swap: true}, {code: "[45]..", swap: false,
error: true}]``. A 4xx/5xx is therefore NOT SWAPPED -- it raises ``htmx:responseError`` and the
body is dropped on the floor. So a handler that lovingly renders an error card and returns it with
a 422 or a 500 has written markup no operator will ever see; the swap target keeps its stale
contents and the failure is invisible.

The defect class is one sentence: **a handler picks its response's document shape and status
without regard to how htmx will actually consume it, so a correct-looking 200 fragment is swapped
into the wrong place, or a correct-looking error body is silently discarded.**

THE CONTRACT
------------

1. ``HX-Request: true`` DOES NOT MEAN "SEND A FRAGMENT".
   It means "htmx issued this request", and htmx issues more kinds of request than the in-page
   swap the handler has in mind. No handler may branch on that header directly. The canonical
   predicate is :func:`wants_fragment`, and it is the ONLY sanctioned way to ask the question.

   The rule is deliberately phrased as a ban on the raw header rather than as advice, because the
   raw check READS correct at every call site that has one. Nothing local to ``audit_log`` looked
   wrong; the bug lived in the gap between what the header says and what the handler assumed.

2. A HISTORY-RESTORE REQUEST IS A FULL-DOCUMENT REQUEST.  <-- the crux
   ``HX-History-Restore-Request: true`` means htmx is rebuilding a whole history entry. It will
   swap whatever it receives into the history element -- ``<body>`` unless some element carries
   ``[hx-history-elt]`` -- and it IGNORES ``hx-target`` while doing so. A fragment sent here does
   not land in the fragment's usual home; it REPLACES THE PAGE.

   So the restore header dominates. When it is present the handler owes a full document, even
   though ``HX-Request`` is also present, even though the same URL served a fragment one request
   earlier, and even though the pushing element declared an ``hx-target``. There is no case in
   which a restore request wants a partial.

   :func:`wants_fragment` encodes exactly this: htmx asked AND it is not restoring.

3. A RENDERABLE ERROR IS A ``200`` WHOSE BODY CARRIES THE ERROR.
   If the handler's answer to a failure is markup intended to land in a swap target -- an inline
   alert, a failed-row replacement, a "could not load, retry" card -- it MUST be returned with
   :data:`RENDERABLE_ALERT_STATUS` (200). Under stock ``responseHandling`` a 4xx/5xx is not
   swapped, so any other status turns that markup into a no-op and the operator sees stale content
   with no indication anything went wrong.

   The error semantics then live in the BODY, not the status line: mark the alert
   ``role="alert"`` so it is announced to assistive technology, and say what failed in prose. A
   200 here is not a lie about success -- it is an accurate statement that the SERVER SUCCESSFULLY
   PRODUCED THE OPERATOR'S ANSWER, which happens to be bad news.

4. THE BOUNDARY AGAINST ``request_guards.py`` RULE 1, STATED ONCE.
   ``request_guards`` says a malformed envelope is **422, loudly**. This module says a renderable
   failure is **200 with an alert body**. Both are right; they answer different questions, and no
   handler is covered by both.

   The distinguishing test, one sentence: **is there a swap target waiting to display this
   answer?** If yes -- the request came from an htmx control aimed at some element, and the
   operator is staring at that element right now -- it is a 200 with an alert body (this
   contract). If no -- the envelope was unintelligible, there is nothing meaningful to render into
   anything, and the only honest answer is a protocol-level rejection -- it is a 422
   (``request_guards`` rule 1).

   Put the other way: 422 is for a request phaze could not UNDERSTAND; a 200 alert is for a
   request phaze understood perfectly and must report bad news about. A malformed JSON envelope
   can never be the second, and a "that file is already gone" message can never be the first.

5. ANY CLAIM THIS DOCSTRING MAKES IS A TEST OBLIGATION.
   Inherited verbatim from ``request_guards`` rule 6, and binding here for the same reason: this
   module exists because a handler's shape decision looked correct and was not. Every shape named
   above -- plain request, htmx swap, history restore, restore-without-``HX-Request`` -- ships a
   test asserting the predicate's answer, and the audit-log regression asserts the CHROME is
   present on restore rather than merely that the status is 200. A status assertion alone would
   have passed against the bug.

6. A ``wants_fragment`` FORK MUST MARK EVERY BRANCH UNCACHEABLE BY THE BROWSER (phaze-r6e5m).
   Rules 1 and 2 close the SERVER-side half of this class: a handler asked the right question and
   answers with the right document shape. But the browser's HTTP cache keys a response by URL
   ALONE -- it never sees ``HX-Request`` / ``HX-History-Restore-Request`` / ``HX-Target`` unless a
   response says those headers make its body vary, via ``Vary``. Absent that, a chrome-less
   fragment fetched for a live rail swap of ``GET /s/discover`` is cached under the exact same key a
   full-document navigation to ``/s/discover`` would use, and vice versa for a redirect-vs-fragment
   pair like ``GET /audit/``. On a browser Back/Forward whose bfcache entry is gone (routine --
   bfcache eligibility is narrow and htmx's own 10-entry history cache is unrelated to it), the
   browser reuses that stored HTTP-cache entry WITHOUT asking the server again: the stored fragment
   renders as the whole document -- no ``<head>``, no stylesheet, no htmx/Alpine, no rail, no
   header -- the exact "bare workspace with no way out but a manual reload" failure rule 2 exists
   to prevent, reintroduced one layer down, below the server entirely.

   Every response emitted by a ``wants_fragment`` fork -- EVERY branch, not just the fragment one,
   since the inverse poisoning (a cached full document swapped into a live target) is equally
   possible -- carries :data:`DUAL_SHAPE_RESPONSE_HEADERS`. ``Cache-Control: no-store`` is the
   effective half: it stops the browser from ever storing the response, so which headers it would
   have varied on stops mattering. ``Vary`` rides alongside as defense in depth for any
   intermediary that respects it over ``no-store``.

USING IT
--------
::

    from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS, wants_fragment

    @router.get("/audit/", response_class=HTMLResponse)
    async def audit_log(request: Request, ...) -> HTMLResponse:
        context = {...}

        # Fragment ONLY for a live in-page swap. A history restore falls through to the full
        # page, because htmx will put this response in <body>. BOTH branches carry
        # DUAL_SHAPE_RESPONSE_HEADERS (rule 6) -- the same URL answers with either shape depending
        # on request headers the browser's HTTP cache does not key on by default.
        if wants_fragment(request):
            return templates.TemplateResponse(
                request=request,
                name="execution/partials/audit_content.html",
                context=context,
                headers=DUAL_SHAPE_RESPONSE_HEADERS,
            )

        return templates.TemplateResponse(
            request=request, name="execution/audit_log.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
        )
"""

from fastapi import Request


__all__ = [
    "DUAL_SHAPE_RESPONSE_HEADERS",
    "RENDERABLE_ALERT_STATUS",
    "is_history_restore",
    "is_htmx_request",
    "wants_fragment",
]


RENDERABLE_ALERT_STATUS = 200
"""The ONE status code for an error the operator is meant to READ (contract rule 3).

Spelled once, here, so a second handler cannot quietly answer 422 or 500 for a body it intends
htmx to swap into a visible target. htmx 2.x's default ``responseHandling`` does not swap 4xx/5xx
at all, so any other value silently discards the markup.

This is NOT a general "errors are 200" rule -- see contract rule 4 for the boundary against
``request_guards.MALFORMED_PAYLOAD_STATUS`` (422), which remains correct for a request phaze could
not parse.
"""


DUAL_SHAPE_RESPONSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Vary": "HX-Request, HX-History-Restore-Request, HX-Target",
}
"""Headers EVERY branch of a ``wants_fragment`` fork must carry (contract rule 6, phaze-r6e5m).

The browser's HTTP cache keys a stored response by URL alone -- it never consults
``HX-Request`` / ``HX-History-Restore-Request`` / ``HX-Target`` unless told those headers make the
body vary. Without this, the SAME URL legitimately answering with two (or three -- see
``routers/shell.py``'s ``_render_stage``) different bodies lets the browser serve one shape's cached
bytes for a request that wanted the other -- most dangerously a cached chrome-less fragment
rendered as the WHOLE document on a Back/Forward navigation whose bfcache entry is gone.

``Cache-Control: no-store`` is the effective directive: it stops the browser from caching the
response at all, so an incomplete or since-widened ``Vary`` value can never matter. ``Vary`` rides
alongside as defense in depth for any intermediary that honors it in preference to ``no-store``; it
deliberately lists every header any in-tree ``wants_fragment`` fork discriminates on today
(``HX-Target`` for ``shell.py``'s propose-list narrow swap) rather than being hand-tuned per
call site, so one shared constant stays correct as new forks are added.

Pass directly as ``headers=`` to every ``TemplateResponse`` / ``RedirectResponse`` a
``wants_fragment``-forked handler returns -- see the module docstring's "USING IT" example.
"""


def is_htmx_request(request: Request) -> bool:
    """Return True if htmx issued this request at all.

    Answers ONLY "did htmx send this", which is a strictly weaker statement than "this wants a
    fragment" -- a history-restore request also sets the header (htmx's
    ``historyRestoreAsHxRequest`` defaults to true). Per contract rule 1 handlers choosing a
    document shape must call :func:`wants_fragment` instead; this helper is the shared, lowercase,
    single-spelling primitive that predicate is built from.

    Args:
        request: The inbound request.

    Returns:
        True when ``HX-Request`` is exactly ``"true"``. Header lookup is case-insensitive
        (Starlette normalises header names), the VALUE comparison is not -- htmx always sends the
        lowercase literal.
    """
    return request.headers.get("hx-request") == "true"


def is_history_restore(request: Request) -> bool:
    """Return True if htmx is rebuilding a history entry from the server (contract rule 2).

    htmx sends ``HX-History-Restore-Request: true`` when the user navigates Back/Forward to a
    pushed URL whose snapshot is not in the history cache. The response will be swapped into the
    history element (``<body>`` unless something carries ``[hx-history-elt]``) and ``hx-target`` is
    ignored, so the only correct answer to such a request is a FULL document.

    Args:
        request: The inbound request.

    Returns:
        True when ``HX-History-Restore-Request`` is exactly ``"true"``. Deliberately independent of
        :func:`is_htmx_request`: the restore header dominates, and a restore is a full-document
        request whether or not ``HX-Request`` accompanies it.
    """
    return request.headers.get("hx-history-restore-request") == "true"


def wants_fragment(request: Request) -> bool:
    """Return True if this request should be answered with a chrome-less fragment.

    THE canonical shape predicate (contract rules 1 and 2), and the only sanctioned way to ask the
    question. True for exactly one situation: htmx issued the request AND it is not a history
    restore -- i.e. a live in-page swap into a real target, which is the only case where omitting
    the page chrome is correct.

    Every other shape gets the full document, and the interesting one is the third:

    * plain browser navigation (no htmx headers)   -> False, obviously
    * htmx in-page swap                            -> True
    * htmx history restore (BOTH headers set)      -> False, because htmx swaps it into ``<body>``
    * restore header alone, no ``HX-Request``      -> False, same reason

    Args:
        request: The inbound request.

    Returns:
        True only for a live htmx swap; False for every full-document case above.
    """
    return is_htmx_request(request) and not is_history_restore(request)
