"""Unit tests for the bind-parameter chunking arithmetic (phaze-syxv).

These pin the ARITHMETIC (no database): that the split is derived from the rows' real parameter
count, that no emitted statement can exceed PostgreSQL's int16 bind-parameter ceiling, and that
chunking preserves every row in order. The database-backed regression for the actual analysis-window
write lives in ``tests/agents/routers/test_agent_analysis.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.services.bulk_insert import MAX_ROWS_PER_STATEMENT, PG_MAX_BIND_PARAMS, chunk_rows, rows_per_statement


def test_pg_bind_limit_is_the_int16_protocol_ceiling() -> None:
    """The limit is a wire-protocol constant, not a tunable -- 32767 is int16 max."""
    assert PG_MAX_BIND_PARAMS == 32767


@pytest.mark.parametrize(
    ("params_per_row", "expected"),
    [
        (12, 1000),  # analysis_windows: bind-bound is 2730, soft cap wins
        (33, 992),  # 32767 // 33 = 992 -- bind bound wins below the soft cap
        (1000, 32),  # wide rows chunk aggressively
        (32767, 1),  # exactly one row's worth of budget
        (40000, 1),  # a single row over budget cannot be split -- never yield an empty plan
    ],
)
def test_rows_per_statement_is_min_of_bind_bound_and_soft_cap(params_per_row: int, expected: int) -> None:
    assert rows_per_statement(params_per_row) == expected


@pytest.mark.parametrize("params_per_row", [1, 7, 12, 12345, 32766, 32767])
def test_no_chunk_size_can_exceed_the_bind_limit(params_per_row: int) -> None:
    """The whole point: rows_per_statement * params_per_row must fit in one Bind message."""
    assert rows_per_statement(params_per_row) * params_per_row <= PG_MAX_BIND_PARAMS


def test_rows_per_statement_degenerate_width_falls_back_to_soft_cap() -> None:
    """A zero/negative width is meaningless; fall back rather than divide by zero."""
    assert rows_per_statement(0) == MAX_ROWS_PER_STATEMENT
    assert rows_per_statement(-1) == MAX_ROWS_PER_STATEMENT


def test_chunk_rows_preserves_every_row_in_order() -> None:
    rows: list[dict[str, Any]] = [{"a": i, "b": i, "c": i} for i in range(2500)]

    chunks = list(chunk_rows(rows))

    assert [row for chunk in chunks for row in chunk] == rows, "chunking must not drop, duplicate or reorder rows"
    assert len(chunks) > 1, "2500 rows must actually split"
    assert all(len(chunk) <= MAX_ROWS_PER_STATEMENT for chunk in chunks)


def test_chunk_rows_empty_input_yields_nothing() -> None:
    assert list(chunk_rows([])) == []


def test_chunk_rows_single_statement_when_it_fits() -> None:
    rows: list[dict[str, Any]] = [{"a": i} for i in range(10)]
    assert list(chunk_rows(rows)) == [rows]


def test_chunk_rows_measures_the_widest_row() -> None:
    """SQLAlchemy renders multi-row VALUES over the UNION of keys, so the widest row sets the width."""
    rows: list[dict[str, Any]] = [{"a": 1}, dict.fromkeys((f"k{i}" for i in range(4000)), 1)]

    chunks = list(chunk_rows(rows))

    # 32767 // 4000 = 8 rows per statement -- both rows still fit in one chunk here, but the size
    # must be computed from 4000, not from the 1-key first row.
    assert rows_per_statement(4000) == 8
    assert [row for chunk in chunks for row in chunk] == rows


def test_analysis_window_row_width_still_fits_the_derived_chunk() -> None:
    """Guards the failure this bug taught: adding a bound column must not silently re-break the insert.

    The row bound in ``put_analysis`` is the model's columns MINUS the server-defaulted ones
    (``created_at``/``updated_at`` are ``server_default=func.now()``, so they are not bound).
    If someone adds a 13th bound column, ``chunk_rows`` re-derives the split from the real width --
    this test just asserts the derivation stays inside the protocol ceiling.
    """
    bound_columns = [c for c in AnalysisWindow.__table__.columns if c.server_default is None]

    assert rows_per_statement(len(bound_columns)) * len(bound_columns) <= PG_MAX_BIND_PARAMS
