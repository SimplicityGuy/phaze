"""THE list-view state carrier for server-rendered, htmx-swapped tables (phaze-a6hm.2 / .9).

This module owns ONE question: *what subset of a list is the operator currently looking at, and
how do I re-emit that answer into every control that can change it?* Filter, search, page,
page size, sort and order are all the same kind of thing -- a small bundle of URL-borne
parameters that must survive an htmx swap, land in the address bar, and come back intact on a
history restore. They are carried together, as one immutable value, precisely so that no control
can preserve three of them and silently drop the fourth.

WHY THIS EXISTS
---------------
The v7 shell cutover replaced the proposals table with ``pipeline/partials/propose_workspace.html``
and lost status filtering, search, sorting, pagination and bulk approve/reject in the process. The
pre-cutover implementation is instructive about *why* it was easy to lose: every control built its
own URL by hand. ``proposals/partials/pagination.html`` still shows the shape -- the same
``?page={{ }}&status={{ }}&q={{ }}&sort={{ }}&order={{ }}&page_size={{ }}`` string is spelled out
SIX times, once per control, each an independent chance to omit a parameter. A pager written that
way does not "preserve the filter"; it re-states the filter correctly six times and preserves it by
luck. Adding a seventh parameter means finding and editing all six sites.

So the plumbing here is deliberately parameter-agnostic. :meth:`ListViewState.query` re-emits the
WHOLE state with only the caller's explicit overrides applied, which inverts the failure mode: a
control now says what it CHANGES (``view.query(page=2)``) and everything else rides along by
construction. Forgetting to preserve a parameter is no longer expressible.

ADDING A PARAMETER (the .10 hook)
---------------------------------
``sort`` and ``order`` are already fields, already parsed, already re-emitted by
:meth:`~ListViewState.query`, and already threaded through every control in the propose workspace
-- but this module deliberately does NOT decide what they MEAN. Sorting the propose workspace is
phaze-a6hm.10's bead, layered on the shared sortable-column contract from phaze-a6hm.1. This module
carries the two parameters so that .10 is an additive change to the TABLE HEADER and the QUERY, not
a rewrite of the pager, the tabs and the search box:

* to READ the current sort, a header renders ``view.sort`` / ``view.order`` (e.g. for ``aria-sort``);
* to SET a new sort, a header links ``view.query(sort="confidence", order="desc", page=1)`` --
  resetting ``page`` explicitly, because page 4 of an old ordering is meaningless under a new one;
* to APPLY it, pass ``view.sort`` / ``view.order`` into the service read.

Nothing else changes. That last step is the only one still open: the propose read currently accepts
the two values and lets ``get_proposals_page`` validate them, so an unrecognised ``sort`` degrades
to that function's documented default rather than erroring.

PARSING IS TOTAL
----------------
:meth:`~ListViewState.from_request` never raises and never propagates a 422. It reads a
user-editable, bookmarkable, history-restorable URL, where ``?page=banana`` is a thing that WILL
arrive -- from a truncated share, a stale bookmark, or a hand-edited address bar. The response the
operator deserves there is the first page of a sane view, not a stack trace, so every field falls
back to its default rather than rejecting the request. This is NOT in tension with
``request_guards`` rule 1 (a malformed envelope is 422, loudly): that rule governs request BODIES
the server cannot interpret, whereas these are optional display preferences with obvious, safe
defaults. There is no ambiguity about what ``?page=banana`` should show.

``page_size`` is additionally clamped to :data:`PAGE_SIZE_CHOICES` rather than merely floored,
because it is the one parameter that translates directly into query cost and response size --
``?page_size=100000`` is the flat-unpaginated-list defect the pagination bead exists to remove,
re-entered through the URL.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode


if TYPE_CHECKING:
    from fastapi import Request


__all__ = ["DEFAULT_PAGE_SIZE", "PAGE_SIZE_CHOICES", "ListViewState"]


PAGE_SIZE_CHOICES: Final[tuple[int, ...]] = (25, 50, 100)
"""The ONLY page sizes any list view will honour.

A closed set, not a range, so ``page_size`` can never be used to ask for an unbounded read. An
out-of-set value falls back to :data:`DEFAULT_PAGE_SIZE` instead of being clamped to the nearest
member: a request for 100000 is not a request for 100, it is a malformed preference, and answering
it with the default is both safer and less surprising than silently honouring a number the
operator never chose.
"""

DEFAULT_PAGE_SIZE: Final[int] = PAGE_SIZE_CHOICES[0]
"""The page size a view starts at -- the smallest choice, so the default render is the cheapest."""


@dataclass(frozen=True, slots=True)
class ListViewState:
    """One list view's URL-borne display state: filter, search, page, page size, sort, order.

    Frozen because it is read by templates during a render: a control that could mutate the state
    while emitting it would let the first control on the page change what every later control
    emits, which is exactly the cross-contamination this type exists to prevent. Use
    :meth:`with_` (or :meth:`query`, which does it internally) to derive a variant.

    Attributes:
        status: Status-filter value. ``"all"`` means unfiltered; any other value is matched against
            the underlying column by the service layer.
        q: Free-text search. Empty string means "no search" -- deliberately not ``None``, so
            templates can render it into an input's ``value`` without a guard.
        page: 1-based page number.
        page_size: Rows per page; always a member of :data:`PAGE_SIZE_CHOICES`.
        sort: Sort column key. Carried and re-emitted here, INTERPRETED by the service read
            (phaze-a6hm.10 -- see the module docstring's hook note).
        order: ``"asc"`` or ``"desc"``.
    """

    status: str = "pending"
    q: str = ""
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort: str = "confidence"
    order: str = "asc"

    @classmethod
    def from_request(cls, request: Request, **defaults: Any) -> ListViewState:
        """Parse display state from ``request.query_params``, falling back on anything unusable.

        Total by construction (see the module docstring): every field either parses or takes its
        default, so a hand-edited or truncated URL renders a sane view instead of erroring. An
        ABSENT parameter and an UNPARSEABLE one are treated identically and on purpose -- both mean
        "the operator expressed no usable preference", and inventing a distinction between them
        would only surface as an error page nobody can act on.

        Args:
            request: The inbound request; only ``query_params`` is read.
            **defaults: Per-view overrides of the class defaults (e.g. ``status="all"`` for a view
                whose natural landing filter is unfiltered). These become the fallbacks, so a view
                keeps its own identity when the URL says nothing.

        Returns:
            A fully-populated state. Never raises.
        """
        base = cls(**defaults)
        params = request.query_params

        page = base.page
        raw_page = params.get("page")
        if raw_page is not None:
            try:
                page = max(1, int(raw_page))
            except ValueError:
                page = base.page

        page_size = base.page_size
        raw_size = params.get("page_size")
        if raw_size is not None:
            try:
                candidate = int(raw_size)
            except ValueError:
                candidate = base.page_size
            page_size = candidate if candidate in PAGE_SIZE_CHOICES else base.page_size

        order = params.get("order", base.order)
        if order not in {"asc", "desc"}:
            order = base.order

        return cls(
            status=params.get("status") or base.status,
            q=params.get("q") or base.q,
            page=page,
            page_size=page_size,
            sort=params.get("sort") or base.sort,
            order=order,
        )

    def with_(self, **overrides: Any) -> ListViewState:
        """Return a copy with ``overrides`` applied; the receiver is unchanged."""
        return replace(self, **overrides)

    def query(self, *, omit: tuple[str, ...] = (), **overrides: Any) -> str:
        """Return the FULL state as a URL-encoded query string, with ``overrides`` applied.

        The whole point of the module, and the reason controls do not build URLs by hand: every
        parameter is emitted every time, so a control states only what it changes and cannot drop
        what it does not mention. ``urlencode`` also means a search containing ``&``, ``#`` or a
        space produces a correct URL rather than a silently truncated one -- the hand-rolled
        ``q={{ search_query }}`` interpolation it replaces did not.

        ``omit`` exists for ONE narrow case: a control that supplies a parameter from its own form
        value via ``hx-include`` must not also carry that parameter in its URL. htmx appends
        included values to the query string, and Starlette's ``query_params.get`` returns the FIRST
        occurrence -- so a search input whose URL already said ``q=old`` would send
        ``?q=old&q=new`` and the server would read ``old``, leaving the box permanently one
        keystroke behind. Omitting the parameter the control owns is what makes that impossible.
        Prefer ``overrides`` for everything else; ``omit`` drops a parameter, it does not reset it.

        Args:
            omit: Field names to leave OUT of the string entirely.
            **overrides: Field values to change for this one URL, e.g. ``page=2``.

        Returns:
            An encoded query string WITHOUT a leading ``?``.
        """
        state = self.with_(**overrides) if overrides else self
        params = {
            "status": state.status,
            "q": state.q,
            "page": state.page,
            "page_size": state.page_size,
            "sort": state.sort,
            "order": state.order,
        }
        return urlencode({key: value for key, value in params.items() if key not in omit})

    def url(self, path: str, *, omit: tuple[str, ...] = (), **overrides: Any) -> str:
        """Return ``path`` with the full state (plus ``overrides``) as its query string."""
        return f"{path}?{self.query(omit=omit, **overrides)}"
