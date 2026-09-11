"""Shared paging contract for every operator-facing list (phaze-5462).

Every render-facing list composes these helpers rather than re-deriving offsets, limits, or
``has_next``. The incident measurement that motivated the contract -- 10,132 rows, 12.7 MB,
about 180 times the sibling tabs -- is preserved in ``docs/architecture.md`` under "List Paging
Contract".

The contract:

1. Use OFFSET paging consistently; arbitrary operator-selected sort orders do not share one
   reusable cursor shape.
2. Derive ``has_next`` from a ``page_size + 1`` sentinel, never a whole-corpus ``COUNT``.
3. Import the page-size constants from this module; request values clamp into their bounds.
4. Every ``ORDER BY`` has a unique tiebreaker. Postgres does not stabilize ties, and
   transaction-time ``created_at`` values are not unique.
5. Out-of-range inputs clamp rather than failing a render; a page past the end is empty.
6. Render reads use a SAVEPOINT and degrade to an empty :class:`Page` on database errors.
7. A bounded render reader is never reused as an enqueue set, which must remain exhaustive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from phaze.schemas.wire_bounds import INT64_MAX


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

# phaze-u2c4: an upper bound on `page` isn't a business cap (rule 5 says a page past the end must
# clamp, never raise) -- it exists purely so ``(page - 1) * page_size`` in paged_stmt() can never
# exceed what an int8 OFFSET bind can hold. asyncpg fails to encode an OFFSET past INT64_MAX, which
# reaches this same "clamp, don't raise" contract from the opposite direction: an absurdly large
# page must degrade to the same empty-page-past-the-end behavior a merely-large one already gets,
# not 500 the render. Divide by the largest page_size a caller can ask for so the product is safe
# regardless of which page_size accompanies it.
MAX_PAGE = INT64_MAX // MAX_PAGE_SIZE


def clamp_page(page: int) -> int:
    """Clamp a 1-based page number to ``[1, MAX_PAGE]`` (contract rule 5).

    Zero, negative and nonsense values collapse to page 1 rather than raising -- these reads ride
    hot render paths where a 422 would blank the whole workspace. A page PAST the end is not an
    error EITHER (the row count is deliberately unknown -- rule 2): moderately-large values simply
    yield an empty page, and absurdly large ones (an int8-OFFSET-overflowing page number) clamp to
    ``MAX_PAGE`` so the OFFSET arithmetic in :func:`paged_stmt` stays representable instead of
    raising when asyncpg encodes the bind.
    """
    return min(max(page, 1), MAX_PAGE)


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
