"""THE sortable-column contract for every operator-facing table in phaze (phaze-a6hm.1).

This module is the SINGLE owner of "how an operator reorders a paginated table". Every surface that
lets a header change the row order composes the helpers here rather than re-deriving a whitelist, a
toggle, or a query string. Before adding a sortable column anywhere, read this docstring; it is the
contract, not a suggestion.

WHY THIS EXISTS
---------------
phaze-a6hm found that exactly ONE table in the repo sorts -- ``proposals/partials/proposal_table.html``
-- and that the table is in the UNREACHABLE legacy family, so in practice no operator-facing table in
phaze sorts at all. Meanwhile ``pipeline/partials/_file_table.html`` is included by NINE workspaces
(discover, fingerprint, propose, trackid, analyze, pending, tracklist_sets, files_table_view), every
one of them a paginated list the operator must scan by eye.

Two failure modes were waiting on the obvious fixes:

1. **Client-side sorting is silently WRONG on a paginated table.** These lists are bounded by the
   paging contract (:mod:`phaze.services.pagination`): the browser holds ``page_size`` rows out of a
   corpus of unknown size (rule 2 forbids the COUNT that would reveal it). Sorting those rows in
   JavaScript reorders ONE PAGE and presents the result as though it were the ordering of the whole
   set. The operator asked "which file has the lowest confidence?" and got "which of these fifty".
   There is no error, no empty state, and no way to tell from the screen that the answer is wrong.
   Sorting is therefore SERVER-SIDE, always, and the sort key travels in the URL so it composes with
   OFFSET paging instead of fighting it.

2. **A sort parameter is a column-injection surface, not just a bad request.** ``sort=`` arrives from
   the wire as an arbitrary string whose whole purpose is to select a COLUMN. Any implementation that
   reaches a column by NAME -- ``getattr(Model, sort)``, a dict lookup with a fallback bolted on
   afterwards, an f-string into ``text()`` -- is one refactor away from letting a request name a
   column, a relationship, or a Python attribute the table never meant to expose.

The defect class is one sentence: **a table's row order is chosen by an untrusted string that either
never reaches the server (so it reorders the wrong set) or reaches a column by name (so it selects
more than the table meant to offer).**

THE CONTRACT
------------

1. SORTING IS SERVER-SIDE. THE SORT KEY TRAVELS IN THE URL.
   A header emits an ``hx-get`` carrying ``sort`` and ``order``; the handler resolves them and the
   ORDER BY lands in SQL, inside the same ``paged_stmt`` that bounds the page. No table sorts rows in
   the browser, and no table sorts rows in Python after the read -- both reorder ONLY the page, which
   is the bug above. If you find yourself sorting ``page.rows``, you have sorted the wrong set.

2. THE WHITELIST IS A MAPPING TO COLUMN OBJECTS, NOT A SET OF NAMES.  <-- the crux
   A :class:`SortContract` holds :class:`SortableColumn` entries, each binding a wire ``key`` to an
   already-constructed SQLAlchemy ``expression``. Resolution is a LOOKUP IN THAT MAPPING and nothing
   else -- never ``getattr``, never string interpolation, never a name that is later turned into a
   column.

   This is deliberately a STRUCTURAL guarantee rather than a validation step. A validation step can be
   forgotten, reordered, or bypassed by the next caller; here an unwhitelisted key simply HAS NO
   COLUMN OBJECT to reach, so there is no code path along which it could become one. The safety
   property does not depend on anybody remembering to check.

3. AN UNKNOWN SORT VALUE DEGRADES TO THE DEFAULT. IT DOES NOT ``422``.
   :meth:`SortContract.resolve` maps any unrecognised ``sort`` to the contract's default key and any
   unrecognised ``order`` to its default direction.

   This is the SAME answer ``services/proposal_queries.py`` already gives (``valid_sort_columns`` ->
   fall back to ``confidence``), and the same answer this repo gives for every other render-path
   allowlist: ``/pipeline/files`` degrades an unknown ``stage``/``bucket`` rather than 422-ing the
   poll (T-87-14), ``/pipeline/analyze-files`` degrades an unknown ``status`` to the default view, and
   the paging contract's rule 5 clamps an out-of-range page instead of raising. Be consistent with
   them.

   **The boundary against ``request_guards.py`` rule 1 (422 for a malformed envelope), stated once so
   no handler is covered by both:** ``request_guards`` rejects a payload phaze could not UNDERSTAND,
   on a request that is performing a WRITE. A sort key is neither. It is a display preference on a
   hot GET render path, it is re-sent on every poll and every pager click, and a stale bookmark or an
   evicted history entry can carry an old one perfectly innocently. Answering 422 there blanks the
   whole workspace to punish a request whose worst outcome is "you got the default order".

   Degrading is NOT laxity here BECAUSE OF RULE 2: the unknown value never reached a column, so there
   is nothing left to defend against. The status code is a UX decision once the injection surface is
   already closed structurally.

4. SORTING PRESERVES THE REST OF THE VIEW STATE.
   A header click must not silently discard the operator's filter, search, page size, or stage lens.
   :meth:`SortContract.resolve` takes that state as ``view_state`` and :meth:`SortState.url_for`
   re-emits ALL of it alongside the new sort/order, so the click changes exactly one thing.

   The inverse also holds and is easy to forget: a PAGER click must not discard the SORT.
   :meth:`SortState.query_state` exists for that direction -- pagers append it to their own URLs so
   Prev/Next stay inside the operator's chosen order.

4a. A SELF-REFRESHING TABLE'S OWN POLL CARRIES THE SORT TOO.
   The same rule pointed at ``hx-trigger="every 5s"``, called out separately because it is the
   variant that fails INVISIBLY. A table that re-fetches itself hard-codes its endpoint in
   ``hx-get``; that URL carries no sort, so the poll re-requests the DEFAULT order and swaps it over
   the order the operator just chose. The sort survives for up to one poll interval and then snaps
   back on its own, with no error and no interaction to blame it on -- and, crucially, NO manual
   test shorter than the interval can see it.

   :meth:`SortState.poll_url` is the single spelling; a self-refreshing sortable table writes
   ``hx-get="{{ sort.poll_url() }}"`` and nothing else. It re-states the CURRENT order rather than
   toggling (a poll is a re-read, not a click), and carries ``view_state`` so a poll cannot silently
   drop a filter either. This applies to the recent-scans table (phaze-a6hm.6) and the admin agents
   table (phaze-a6hm.4) alike; both are polls first and tables second.

   Sorting RESETS to page 1 by design: the operator asking for a different order is asking about the
   top of that order, and holding page 7 across a re-sort shows a window that means nothing.

5. THE HEADER ANNOUNCES ITS OWN STATE (``aria-sort``).
   A sortable ``<th>`` carries ``aria-sort="ascending" | "descending" | "none"``, which is how a
   screen-reader user learns the table is sortable AND which column is active. A caret glyph alone
   conveys this to sighted users only, and a colour or weight change alone fails WCAG 1.4.1 the same
   way a hue-only status cell does (see ``_file_table.html``'s existing note). :meth:`SortState.aria_sort`
   is the single spelling; do not hand-write the attribute.

6. ONE TABLE, ONE CONTRACT OBJECT, CONSTRUCTED AT IMPORT TIME.
   A :class:`SortContract` is a module-level constant next to the handler that serves the table, not
   something built per request. Its ``__post_init__`` rejects duplicate keys, duplicate labels, and a
   ``default_key`` that is not among its columns -- so a mis-wired table fails LOUDLY at import,
   before any request, rather than degrading every sort click to a default that silently is not there.

7. ANY CLAIM THIS DOCSTRING MAKES IS A TEST OBLIGATION.
   Inherited verbatim from ``request_guards`` rule 6 and ``response_shape`` rule 5. In particular the
   rule-2 claim ships a regression test that sends an unwhitelisted ``sort`` value and asserts it
   cannot reach a column -- asserting the STATUS alone would pass against an implementation that
   happily ``getattr``-ed its way to one.

USING IT
--------
Wiring a new table is three edits. First, declare the contract next to the handler::

    from phaze.routers.column_sort import SortableColumn, SortContract

    TRACKID_SORT = SortContract(
        endpoint="/pipeline/trackid-files",
        target="#trackid-files-view",
        columns=(
            SortableColumn(key="file", label="File", expression=FileRecord.original_filename),
            SortableColumn(key="confidence", label="Confidence", expression=FingerprintResult.confidence),
        ),
        default_key="file",
    )

Second, resolve it in the handler and hand the state to the template::

    sort_state = TRACKID_SORT.resolve(sort=sort, order=order, view_state={"page_size": page_size})
    page = await get_trackid_files_page(session, page=page, page_size=page_size, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_trackid_files.html",
        context={"trackid_page": page, "host_id": "trackid-files-view", "sort": sort_state},
    )

Third, spend the state in the query -- one argument, inside the existing paging call::

    return paged_stmt(stmt, page=page, page_size=page_size, order_by=sort.order_by(), tiebreaker=(FileRecord.id,))

``_file_table.html`` needs NO edit: it renders a sortable header for any column whose label the
``sort`` context object recognises, and a plain header for every other column. A workspace that
passes no ``sort`` renders exactly as it did before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


__all__ = ["ASCENDING", "DESCENDING", "SortContract", "SortState", "SortableColumn"]


ASCENDING = "asc"
"""The ONE wire spelling for ascending order, so a header, a pager and a handler cannot disagree."""

DESCENDING = "desc"
"""The ONE wire spelling for descending order (see :data:`ASCENDING`)."""

_ARIA_BY_ORDER = {ASCENDING: "ascending", DESCENDING: "descending"}
"""``order`` -> the ``aria-sort`` token for the ACTIVE column (contract rule 5).

Inactive columns get ``"none"``, which is the ARIA value meaning "sortable but not currently sorted"
-- deliberately not the ABSENCE of the attribute, which means "not sortable at all".
"""


@dataclass(frozen=True, slots=True)
class SortableColumn:
    """ONE column an operator is allowed to sort by: a wire key, a header label, and a real column.

    The ``expression`` is the load-bearing field and the reason this is a dataclass rather than a
    string in a set (contract rule 2). It is an ALREADY-CONSTRUCTED SQLAlchemy column element, bound
    here at import time by code the operator cannot influence. Resolution can therefore only ever
    hand back an expression that some developer wrote down on purpose; there is no path from a
    request string to a column that was not enumerated here.

    Attributes:
        key: The value that appears in the URL as ``sort=<key>``. Stable, lowercase, and part of the
            surface's public URL contract -- bookmarks and history entries carry it.
        label: The header text. MUST match the string this table passes in ``columns`` to
            ``_file_table.html`` exactly -- that string is how the template recognises the header as
            sortable, so a typo here degrades the header to plain text rather than erroring.
        expression: The SQLAlchemy column (or expression) to ORDER BY. May be a mapped attribute, a
            subquery column, or any expression carrying ``.asc()`` / ``.desc()``.
    """

    key: str
    label: str
    expression: Any


@dataclass(frozen=True, slots=True)
class SortState:
    """The resolved sort for ONE render: a whitelisted column, a direction, and the view state around them.

    Produced ONLY by :meth:`SortContract.resolve` -- never constructed in a handler, because the
    constructor cannot enforce that ``key`` came from the whitelist. Handed to the template as
    ``sort`` and to the query builder as the ``order_by`` source, so exactly one object answers both
    "what does the header look like" and "what does SQL do".

    Attributes:
        key: The RESOLVED sort key. Guaranteed to name a column in ``contract`` (rule 2/3).
        order: The RESOLVED direction, one of :data:`ASCENDING` / :data:`DESCENDING`.
        contract: The contract this state was resolved against.
        view_state: The other view parameters to re-emit on every header link (rule 4).
    """

    key: str
    order: str
    contract: SortContract
    view_state: tuple[tuple[str, str], ...] = ()

    def _column(self, label: str) -> SortableColumn | None:
        """Return the whitelisted column carrying ``label``, or None when the header is not sortable."""
        return next((column for column in self.contract.columns if column.label == label), None)

    def is_sortable(self, label: str) -> bool:
        """Return True when the header ``label`` is one this table offers sorting on.

        The template's gate: ``_file_table.html`` renders a header button when this is True and plain
        text otherwise, which is what lets ONE partial serve nine workspaces with different column
        sets and no per-workspace branching.
        """
        return self._column(label) is not None

    def is_active(self, label: str) -> bool:
        """Return True when ``label`` is the column the table is CURRENTLY ordered by."""
        column = self._column(label)
        return column is not None and column.key == self.key

    def aria_sort(self, label: str) -> str:
        """Return the ``aria-sort`` token for ``label`` (contract rule 5).

        ``"ascending"``/``"descending"`` for the active column, ``"none"`` for any other SORTABLE
        column. A non-sortable header must not carry the attribute at all -- the template omits it
        rather than asking here, because ``aria-sort="none"`` on a plain header would announce a
        sorting affordance that does not exist.
        """
        if not self.is_active(label):
            return "none"
        return _ARIA_BY_ORDER.get(self.order, "none")

    def next_order(self, label: str) -> str:
        """Return the direction a click on ``label`` should request.

        Clicking the ACTIVE column toggles it; clicking any other column starts that column at
        :data:`ASCENDING` rather than inheriting the previous column's direction -- inheriting reads
        as the table having silently re-sorted itself in a direction the operator never chose.
        """
        if self.is_active(label) and self.order == ASCENDING:
            return DESCENDING
        return ASCENDING

    def url_for(self, label: str) -> str:
        """Return the full ``hx-get`` URL a click on ``label`` should issue (contract rule 4).

        Carries the new sort key, the toggled direction, and EVERY preserved view-state parameter, so
        the click changes the order and nothing else. Deliberately omits ``page``: a re-sort returns
        to page 1, because holding an offset across a change of order shows an arbitrary window.

        Returns the contract's endpoint unchanged when ``label`` is not sortable -- unreachable via
        the template (which gates on :meth:`is_sortable` first), but a safe answer rather than a
        render-time exception if some future caller asks without checking.
        """
        column = self._column(label)
        if column is None:
            return self.contract.endpoint
        params = [*self.view_state, ("sort", column.key), ("order", self.next_order(label))]
        return f"{self.contract.endpoint}?{urlencode(params)}"

    def indicator(self, label: str) -> str:
        """Return the caret glyph for ``label``: up when ascending, down when descending, empty when inactive.

        Purely decorative reinforcement of :meth:`aria_sort` -- the accessible signal is the ARIA
        attribute (rule 5), never this glyph, so the template marks it ``aria-hidden``.
        """
        if not self.is_active(label):
            return ""
        return "▲" if self.order == ASCENDING else "▼"

    def query_params(self) -> str:
        """Return ``"sort=<key>&order=<order>"`` -- the active sort as a bare query fragment, no separator.

        THE single spelling of "this sort, as URL parameters", which :meth:`query_state`,
        :meth:`poll_url` and any caller needing the sort on a DIFFERENT endpoint all build on. It
        exists so that no template hand-writes ``?sort={{ sort.key }}&order={{ sort.order }}``:
        that spelling reads fine and is exactly how a table drifts out of the contract -- it is
        also the spelling that silently omits ``view_state``.

        Prefer :meth:`query_state` when appending to a URL that already has a query string, and
        :meth:`poll_url` for a self-refreshing table's own ``hx-get``.
        """
        return urlencode([("sort", self.key), ("order", self.order)])

    def query_state(self) -> str:
        """Return ``"&sort=<key>&order=<order>"`` for a PAGER to append to its own URL (contract rule 4).

        The inverse of :meth:`url_for` and the easier half to forget: without this, Prev/Next drops
        the operator back into the default order mid-scan. Leads with ``&`` because every pager in
        this repo builds its query string by concatenation onto an existing ``?page=N``.
        """
        return f"&{self.query_params()}"

    def poll_url(self) -> str:
        """Return the URL a SELF-REFRESHING table's own poll must re-request (contract rule 4a).

        The third direction of rule 4, and the one that fails INVISIBLY. A table that re-fetches
        itself on ``hx-trigger="every 5s"`` hard-codes its own endpoint in ``hx-get``. That URL
        carries no sort, so the operator clicks a header, sees the table re-order, and up to five
        seconds later watches it silently snap back to the default -- the poll re-requested the
        unsorted view and swapped it over the sorted one. No error, no flicker worth noticing, and
        invisible to any manual test shorter than one poll interval.

        The fix is the same one rule 4 already applies to pagers: the poll re-states the ACTIVE view
        rather than the default one. Every self-refreshing sortable table spells its ``hx-get`` as
        ``{{ sort.poll_url() }}`` and inherits poll survival; none of them concatenates its own query
        string, so none of them can forget a parameter the others remember.

        Differs from :meth:`url_for` in the one way that matters: ``url_for`` is a CLICK, so it
        TOGGLES via :meth:`next_order`; a poll is a RE-READ of the view the operator is already
        looking at, so it re-states :attr:`order` unchanged. A poll that toggled would invert the
        table every five seconds.

        Like ``url_for`` this deliberately omits ``page``: a poll re-reads the top of the current
        order. Preserved ``view_state`` (filter, search, page size) rides along, so a poll cannot
        quietly drop a filter either -- the same defect one parameter over.
        """
        params = [*self.view_state, ("sort", self.key), ("order", self.order)]
        return f"{self.contract.endpoint}?{urlencode(params)}"

    def order_by(self) -> tuple[Any, ...]:
        """Return the ORDER BY clause to hand ``paged_stmt(order_by=...)`` (contract rule 1).

        A one-element tuple holding the resolved column in the resolved direction. This is the ONLY
        place a direction becomes SQL, and it is reached only through a whitelisted expression.

        The result is deliberately the DISPLAY order only, NOT a total order: ``paged_stmt`` still
        requires its own unique ``tiebreaker`` (paging contract rule 4), and an operator-chosen sort
        key ties more often than the default one does -- sorting by file type or status puts thousands
        of rows on one value. Never pass this as the tiebreaker.
        """
        column = next(column for column in self.contract.columns if column.key == self.key)
        return (column.expression.desc() if self.order == DESCENDING else column.expression.asc(),)


@dataclass(frozen=True, slots=True)
class SortContract:
    """The whitelist for ONE table: which columns may be sorted, where a click goes, and what it swaps.

    Declared as a module-level constant beside the handler that serves the table (contract rule 6) and
    validated at construction, so a mis-wired table fails at import rather than degrading silently on
    every click.

    Attributes:
        endpoint: The path a header click hx-gets. The SAME endpoint that rendered the table -- a sort
            is a re-render of this table under a new order, never a new surface.
        target: The ``hx-target`` selector the response is swapped into. Must be the table's EXISTING
            host container; this contract introduces no new swap target and no out-of-band fragment,
            so it cannot contribute a duplicate id (the defect class behind phaze-gzrd / op6f / 7j50).
        columns: The whitelisted columns. Order is irrelevant -- lookup is by key or label.
        default_key: The key used when the request names none or names one that is not whitelisted.
        default_order: The direction used when the request names none or names an invalid one.
    """

    endpoint: str
    target: str
    columns: tuple[SortableColumn, ...]
    default_key: str
    default_order: str = ASCENDING

    def __post_init__(self) -> None:
        """Reject a mis-wired contract at import time (contract rule 6).

        Every check here guards a failure that is INVISIBLE at runtime: duplicate keys silently make
        one column unreachable, duplicate labels make the template pick an arbitrary one of them, an
        unknown ``default_key`` makes :meth:`SortState.order_by` raise on the very first render, and a
        bad ``default_order`` silently inverts every table. All four are one-character typos, so they
        are caught where a typo is cheapest.

        Raises:
            ValueError: On an empty column set, duplicate keys, duplicate labels, a ``default_key``
                not among ``columns``, or a ``default_order`` that is neither ``asc`` nor ``desc``.
        """
        if not self.columns:
            raise ValueError(f"SortContract({self.endpoint!r}) needs at least one SortableColumn; an empty whitelist can never sort.")

        keys = [column.key for column in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError(f"SortContract({self.endpoint!r}) has duplicate column keys: {sorted(k for k in keys if keys.count(k) > 1)}")

        labels = [column.label for column in self.columns]
        if len(set(labels)) != len(labels):
            raise ValueError(f"SortContract({self.endpoint!r}) has duplicate column labels: {sorted(x for x in labels if labels.count(x) > 1)}")

        if self.default_key not in keys:
            raise ValueError(f"SortContract({self.endpoint!r}) default_key={self.default_key!r} is not one of its columns {sorted(keys)}.")

        if self.default_order not in (ASCENDING, DESCENDING):
            raise ValueError(f"SortContract({self.endpoint!r}) default_order={self.default_order!r} must be {ASCENDING!r} or {DESCENDING!r}.")

    def resolve(self, *, sort: str | None = None, order: str | None = None, view_state: Mapping[str, Any] | None = None) -> SortState:
        """Resolve untrusted ``sort``/``order`` against this whitelist (contract rules 2 and 3).

        THE gate. ``sort`` is matched against the enumerated keys by EQUALITY and nothing else -- no
        ``getattr``, no normalisation, no prefix matching -- so a value that is not literally one of
        this table's keys resolves to :attr:`default_key` and never names a column (rule 2). ``order``
        is matched the same way against the two legal directions.

        Neither case raises: an unrecognised value degrades to the default, matching every other
        render-path allowlist in this repo (rule 3). The safety of degrading rests entirely on the
        lookup above -- the unknown string is DISCARDED here, not carried forward and sanitised later.

        Args:
            sort: The raw ``sort`` query value, exactly as it arrived. ``None`` when absent.
            order: The raw ``order`` query value, exactly as it arrived. ``None`` when absent.
            view_state: The other view parameters to re-emit on every header link -- filter, search,
                page size, stage lens (rule 4). Entries whose value is ``None`` are dropped so an
                absent filter does not become the literal string ``"None"`` in the URL; every other
                value is stringified and URL-encoded by :meth:`SortState.url_for`. Do NOT include
                ``page`` here: a re-sort returns to page 1.

        Returns:
            A :class:`SortState` whose ``key`` is guaranteed to name a column in this contract.
        """
        keys = {column.key for column in self.columns}
        resolved_key = sort if sort in keys else self.default_key
        resolved_order = order if order in (ASCENDING, DESCENDING) else self.default_order
        pairs: Sequence[tuple[str, str]] = tuple((name, str(value)) for name, value in (view_state or {}).items() if value is not None)
        return SortState(key=resolved_key, order=resolved_order, contract=self, view_state=tuple(pairs))
