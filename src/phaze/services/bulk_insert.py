"""Split a multi-row INSERT into statements that fit PostgreSQL's bind-parameter limit (phaze-syxv).

Why this exists: an explicit multi-row ``pg_insert(Model).values([row, row, ...])`` renders ONE
statement carrying ``len(rows) * params_per_row`` bind parameters. The PostgreSQL extended
protocol's Bind message carries the parameter count as a signed int16, so a single statement can
bind at most 32767 parameters; past that asyncpg raises
``InterfaceError: the number of query arguments cannot exceed 32767``. SQLAlchemy's
``insertmanyvalues`` batching does NOT save you here -- it applies to the ``executemany`` form, not
to an explicit multi-row ``VALUES``.

phaze-syxv was that failure on the analysis-window write path: ``AnalysisWritePayload.windows``
accepts up to 50,000 windows, but each row binds 12 parameters, so the single statement broke at
floor(32767 / 12) = 2,730 rows -- below the ~2,880 windows a 24h recording produces at 30s fine
windows. The break was TERMINAL, not merely a 500: ``FAILURE_IS_TERMINAL[ANALYZE]`` marks the
analyze stage permanently FAILED, so hours of essentia CPU were discarded and every retry
reproduced the same deterministic error.

The bound is derived from the ACTUAL parameter count of the rows being inserted, never hardcoded,
so adding a column to a bulk-inserted model cannot silently push a statement back over the limit.

ATOMICITY IS THE CALLER'S JOB, and it matters: these helpers only chunk the STATEMENTS. Every chunk
must be executed on one ``AsyncSession`` inside a single transaction, so a partially-written window
set can never be committed. A file holding half its windows reads as a complete analysis and is
worse than a clean failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from phaze.schemas.wire_bounds import INT16_MAX


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


# The PostgreSQL extended-protocol Bind message stores its parameter count as a signed int16, so one
# statement can carry at most 32767 parameters. This is a wire-protocol constant, not a tunable.
PG_MAX_BIND_PARAMS = INT16_MAX

# Soft cap on rows per statement, applied on TOP of the bind-limit bound. The bind limit alone would
# allow 2,730 rows for a 12-parameter row; capping at 1,000 keeps the peak cost of rendering and
# buffering any single statement modest without materially changing round-trip count. Lower it and
# you pay more round trips; raise it and the bind-limit bound still protects correctness.
MAX_ROWS_PER_STATEMENT = 1000


def rows_per_statement(params_per_row: int) -> int:
    """Return how many rows of ``params_per_row`` parameters fit in one INSERT statement.

    Always at least 1: a single row that on its own exceeds the bind limit cannot be split by
    chunking (it would need a different write strategy, e.g. COPY), so we emit it and let the
    driver raise rather than silently return an empty plan.
    """
    if params_per_row <= 0:
        return MAX_ROWS_PER_STATEMENT
    return max(1, min(PG_MAX_BIND_PARAMS // params_per_row, MAX_ROWS_PER_STATEMENT))


def chunk_rows(rows: Sequence[Mapping[str, Any]]) -> Iterator[list[Mapping[str, Any]]]:
    """Yield ``rows`` in slices small enough for one statement's bind-parameter budget.

    The parameter count per row is measured as the WIDEST row in the batch (``max`` over the row
    key counts), because SQLAlchemy renders a multi-row ``VALUES`` against the union of the keys
    present. Uniform-shaped rows -- the normal case, e.g. ``model_dump()`` output -- simply measure
    their own width.
    """
    if not rows:
        return
    size = rows_per_statement(max(len(row) for row in rows))
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])
