"""THE paging contract for every operator-facing list in phaze (phaze-5462).

This module is the SINGLE owner of "how a list is bounded". Every list surface -- the enrich
workspaces, search results, the audit log, duplicate groups, the CUE list -- composes the helpers
here rather than re-deriving offsets, re-picking a page size, or re-inventing a ``has_next`` probe.
Before adding a new paged read, read this docstring; it is the contract, not a suggestion.

WHY THIS EXISTS
---------------
phaze-5462 found the Analyze workspace server-rendering its ENTIRE working set inline (10,132 rows
/ 12.7 MB, ~180x its sibling tabs) behind a docstring that merely ASSERTED the set was "naturally
bounded". Nothing enforced it. The same latent cliff sat under the metadata and fingerprint
workspaces, whose pending-set reads are likewise unbounded and render zero rows today only because
those backlogs happen to be empty. An assumption is not a bound. A bound is a ``LIMIT``.

THE CONTRACT
------------

1. OFFSET PAGING, NOT CURSOR PAGING.
   Offset is what every existing phaze pager already uses, it supports the Prev/Next affordance the
   templates render, and it composes with arbitrary operator-chosen sort orders (a cursor must be
   re-derived per sort key). The corpus is single-user and index-backed, so deep-offset cost is
   acceptable; correctness and one consistent shape beat a micro-optimization. Do NOT introduce a
   parallel cursor pager -- if a surface genuinely needs one, change it HERE for everyone.

2. NEVER EMIT A WHOLE-CORPUS ``COUNT``.
   ``has_next`` rides a ``page_size + 1`` SENTINEL row (:func:`paged_stmt` / :func:`split_sentinel`),
   never ``SELECT count(*)``. A COUNT re-introduces a full scan on EVERY page render -- the T-87-11
   DoS mitigation. This is why the UI shows "Page N" with Prev/Next and NEVER "page X of Y": the
   total is deliberately unknown. Do not add a total to a template.

3. ONE PAGE SIZE, OWNED HERE.
   :data:`DEFAULT_PAGE_SIZE` is the default for every surface; :data:`MIN_PAGE_SIZE` /
   :data:`MAX_PAGE_SIZE` clamp any caller- or request-supplied value. Routers must NOT spell their
   own numeric default -- import the constant, so changing the page size is a ONE-line change here
   rather than a grep across routers that silently misses one.

4. MANDATORY UNIQUE TIEBREAKER ON EVERY ``ORDER BY``.  <-- the crux
   SQL gives NO stability guarantee for rows that tie on the sort key. Under ``LIMIT``/``OFFSET``
   Postgres may order tied rows differently between two queries, so a row can be SKIPPED entirely
   or DUPLICATED onto two pages -- silently, with no error, and invisibly to tests that only ever
   look at page 1. Any non-unique sort key (``ts_rank``, ``executed_at``, ``created_at``, a
   group-name, a score) is therefore INSUFFICIENT ON ITS OWN.

   :func:`paged_stmt` REQUIRES a ``tiebreaker`` argument and raises :class:`ValueError` if it is
   missing, so the failure mode is a loud error at construction time rather than quiet data loss in
   production. The tiebreaker MUST be a column (or column tuple) that is UNIQUE across the result
   set -- in practice a primary key, e.g. ``FileRecord.id``. Its direction should match the primary
   key's so the composite order stays intuitive.

   Note that ``created_at`` is NOT a valid tiebreaker in phaze: Postgres timestamp defaults are
   transaction-time constant, so every row inserted in one transaction ties exactly.

5. OUT-OF-RANGE INPUTS CLAMP; THEY NEVER RAISE AND NEVER 422 INTO A RENDER.
   :func:`clamp_page` maps anything below 1 (zero, negative, absurd) to page 1.
   :func:`clamp_page_size` clamps into ``[MIN_PAGE_SIZE, MAX_PAGE_SIZE]``.
   A page past the end is NOT an error -- it yields an EMPTY page with ``has_next=False``, which the
   templates already render as the normal empty state. These reads ride hot render paths and must
   degrade, never 500.

   (phaze-hpo9 owns adding the matching request-layer validation for negative ``limit``/``offset``;
   the defined answer it should apply is exactly this clamp, so the service layer stays safe even if
   a route forgets a ``ge=`` guard. Clamping here is the belt; the route guard is the braces.)

6. DEGRADE-SAFE READS.
   A paged read on a render path wraps its execution in a SAVEPOINT (``session.begin_nested()``) and
   returns an EMPTY :class:`Page` on any error rather than propagating. Rolling back the nested scope
   alone keeps the outer request transaction usable for the rest of the page.

7. A BOUNDED RENDER READ IS NEVER THE ENQUEUE SET.
   Where a "pending" set feeds BOTH a table and a bulk-enqueue button, the render gets the bounded
   page and the enqueue keeps the UNBOUNDED set. Bounding a shared reader would silently
   under-enqueue -- a much worse bug than a long list. Keep the two readers separate and say so at
   both call sites.

USING IT
--------
::

    stmt = paged_stmt(
        select(Thing).where(...),
        page=page,
        page_size=page_size,
        order_by=(Thing.created_at.desc(),),   # the non-unique display order
        tiebreaker=(Thing.id.desc(),),         # REQUIRED, unique -- rule 4
    )
    rows = (await session.execute(stmt)).all()
    rows, has_next = split_sentinel(rows, page_size)

``page``/``page_size`` are clamped inside :func:`paged_stmt`, so pass request values through
directly; clamp once, here, not at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select


# The ONE page size for every operator-facing list (contract rule 3). Routers import this constant
# instead of spelling their own numeric default, so this is the single edit point.
DEFAULT_PAGE_SIZE = 50

# Clamp bounds for any caller- or request-supplied page size (contract rule 5). MAX bounds the
# worst-case payload a single request can ask for; MIN keeps a pager from degenerating into a
# one-row-per-page scroll that hammers the DB.
MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


def clamp_page(page: int) -> int:
    """Clamp a 1-based page number to ``>= 1`` (contract rule 5).

    Zero, negative and nonsense values collapse to page 1 rather than raising -- these reads ride
    hot render paths where a 422 would blank the whole workspace. A page PAST the end is NOT clamped
    (the row count is deliberately unknown -- rule 2); it simply yields an empty page.
    """
    return max(page, 1)


def clamp_page_size(page_size: int) -> int:
    """Clamp a page size into ``[MIN_PAGE_SIZE, MAX_PAGE_SIZE]`` (contract rule 5). Never raises."""
    return min(max(page_size, MIN_PAGE_SIZE), MAX_PAGE_SIZE)


@dataclass
class Page[T]:
    """One bounded page of rows. ``has_next`` comes from the +1 sentinel -- NEVER a COUNT (rule 2).

    There is deliberately no ``total`` / ``page_count`` field: supplying one would require the
    whole-corpus COUNT this contract forbids. Templates render "Page N" with Prev/Next affordances,
    never "page X of Y".
    """

    rows: list[T] = field(default_factory=list)
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    has_next: bool = False

    @property
    def has_prev(self) -> bool:
        """True when a previous page exists (i.e. this is not the first page)."""
        return self.page > 1

    @property
    def show_pager(self) -> bool:
        """True when the pager nav is worth rendering at all (some neighbouring page exists)."""
        return self.has_prev or self.has_next


def paged_stmt(
    stmt: Select[Any],
    *,
    page: int,
    page_size: int,
    order_by: Sequence[Any],
    tiebreaker: Sequence[Any],
) -> Select[Any]:
    """Apply the contract's ORDER BY + OFFSET + ``page_size + 1`` sentinel LIMIT to ``stmt``.

    ``order_by`` is the display order (may be empty, and may tie). ``tiebreaker`` is the REQUIRED
    unique suffix -- a primary key or other set-unique column -- appended AFTER ``order_by`` so tied
    rows get a total order and OFFSET paging can never skip or duplicate a row across pages
    (contract rule 4).

    Raises :class:`ValueError` when ``tiebreaker`` is empty. That is deliberate and load-bearing: a
    missing tiebreaker corrupts paging SILENTLY in production, so this fails loudly at construction
    time instead. Do NOT "work around" it by passing the sort key twice -- pass a unique column.

    ``page`` and ``page_size`` are clamped here (rule 5), so request values may be passed straight
    through; clamping happens once, in one place.
    """
    if not tiebreaker:
        raise ValueError(
            "paged_stmt() requires a unique `tiebreaker` (e.g. a primary key). A non-unique ORDER BY under "
            "LIMIT/OFFSET silently skips and duplicates rows across pages -- see the paging contract in "
            "phaze.services.pagination (rule 4)."
        )
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    # +1 sentinel -> has_next WITHOUT a whole-corpus COUNT (contract rule 2).
    return stmt.order_by(*order_by, *tiebreaker).offset((page - 1) * page_size).limit(page_size + 1)


def split_sentinel[T](rows: Sequence[T], page_size: int) -> tuple[list[T], bool]:
    """Split a ``page_size + 1`` sentinel result into ``(page_rows, has_next)`` (contract rule 2).

    ``page_size`` is clamped identically to :func:`paged_stmt` so the split can never disagree with
    the LIMIT that produced ``rows`` -- passing the raw request value to both is safe by construction.
    """
    page_size = clamp_page_size(page_size)
    return list(rows[:page_size]), len(rows) > page_size
