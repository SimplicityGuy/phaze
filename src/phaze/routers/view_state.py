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

WHO SPELLS A SORT URL (settled by .10)
--------------------------------------
``sort`` and ``order`` are fields here, parsed here, and re-emitted here by
:meth:`~ListViewState.query` -- but this module deliberately does NOT decide what they MEAN, and as
of phaze-a6hm.10 it does not spell their URLs either. That split is worth stating plainly, because
this module and ``column_sort`` each own a ``query``/``url``-shaped method and letting BOTH emit
header URLs is how the two contracts would rot apart:

* ``column_sort.SortState.url_for`` owns SORT-HEADER urls. It is what ``_file_table.html`` calls,
  for all nine workspaces that include it, so it is the only spelling that reaches a header at all.
* :meth:`~ListViewState.query` owns every OTHER control's url -- the tabs, the search box, the pager
  and the page-size selector -- and carries ``sort``/``order`` through them untouched, which is what
  keeps a pager click inside the operator's chosen order.
* :meth:`~ListViewState.sort_view_state` is the seam that joins them: it hands the sort contract the
  state a header must preserve, derived from :meth:`~ListViewState.params` so neither side can
  enumerate a parameter the other forgot.

So a header does NOT link ``view.query(sort=..., order=..., page=1)``. Doing so would be a second
implementation of sorting living beside the shared contract -- exactly what .10's acceptance
forbids -- and it would bypass the whitelist that makes ``sort`` safe (``column_sort`` rule 2).
Reading the CURRENT sort from ``view.sort`` / ``view.order`` is still correct and is how the router
seeds ``resolve``.

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


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE", "PAGE_SIZE_CHOICES", "ListViewState"]


MAX_PAGE: Final[int] = 1_000_000
"""The largest ``page`` value ``from_request`` will honour (phaze-h9oz).

``page`` was the one field ``from_request`` only floored, never capped: every sibling field is
clamped into a closed set (``page_size`` against :data:`PAGE_SIZE_CHOICES`, ``order`` against
``{"asc", "desc"}``), but Python ints are unbounded, so a hand-edited or truncated URL could carry
a ``page`` whose ``(page - 1) * page_size`` OFFSET exceeds Postgres's ``bigint`` range. asyncpg
then fails to encode the bind parameter, and the caller's error-handling degrades to an empty page
with zeroed stats -- indistinguishable from a genuinely empty corpus. A page beyond the largest
page size (:data:`PAGE_SIZE_CHOICES`'s max is 100) times this bound is already far past anything a
real result set produces, so the cap is generous while keeping every possible OFFSET many orders
of magnitude below the ``int8`` overflow point.
"""

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
        sort: Sort column key. Carried and re-emitted here, RESOLVED against a
            ``column_sort.SortContract`` whitelist by the router (phaze-a6hm.10). Untrusted until
            that resolution: it arrives from a hand-editable URL and is never itself a column name.
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
                page = min(max(1, int(raw_page)), MAX_PAGE)
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

    def params(self, **overrides: Any) -> dict[str, Any]:
        """Return the WHOLE state as a plain mapping of wire name -> value.

        THE single enumeration of this type's parameters. :meth:`query` encodes it, and
        :meth:`sort_view_state` narrows it; neither re-lists the fields. That indirection is the
        whole anti-drift property of this module (see the module docstring): adding a seventh
        parameter is one edit here, not one edit per consumer.
        """
        state = self.with_(**overrides) if overrides else self
        return {
            "status": state.status,
            "q": state.q,
            "page": state.page,
            "page_size": state.page_size,
            "sort": state.sort,
            "order": state.order,
        }

    def sort_view_state(self) -> dict[str, Any]:
        """Return the parameters a SORT HEADER must carry, for ``SortContract.resolve(view_state=...)``.

        THE seam between this module and the sortable-column contract (phaze-a6hm.1 / .10), and the
        reason the propose workspace does not spell its URL twice. ``column_sort.SortState.url_for``
        owns header URLs -- it is what ``_file_table.html`` calls, so re-spelling those URLs here
        would mean a bespoke second sorting path, which phaze-a6hm.10 forbids. This method instead
        feeds that owner the state it must preserve, derived from :meth:`params` so the two can
        never enumerate different parameter sets.

        Three keys are withheld, each for a reason the contract states:

        * ``page`` -- ``resolve`` documents that a re-sort returns to page 1, so carrying an offset
          across a change of order would show an arbitrary window (column_sort rule 4);
        * ``sort`` / ``order`` -- ``url_for`` appends the NEW key and the TOGGLED direction itself,
          so including the current ones would emit each parameter twice and Starlette would read
          the stale first occurrence (the same duplicate-parameter trap ``query(omit=...)`` exists
          for).
        """
        return {key: value for key, value in self.params().items() if key not in {"page", "sort", "order"}}

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
        params = self.params(**overrides)
        return urlencode({key: value for key, value in params.items() if key not in omit})

    def url(self, path: str, *, omit: tuple[str, ...] = (), **overrides: Any) -> str:
        """Return ``path`` with the full state (plus ``overrides``) as its query string."""
        return f"{path}?{self.query(omit=omit, **overrides)}"
