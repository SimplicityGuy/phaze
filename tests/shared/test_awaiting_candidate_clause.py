"""DB-free contract tests for ``awaiting_candidate_clause`` (Phase 80, D-08/D-09/D-11).

Bucket: ``shared`` (path segment immediately under ``tests/``). NO ``pytest.mark.integration``,
NO Postgres — ``awaiting_candidate_clause`` only *constructs* a ``ColumnElement``, so both the
single-source builder and its LOCKED-composition guarantee are exercised without a DB round-trip.

D-08/D-09: the awaiting-cloud candidate predicate is
``status='awaiting' AND ~inflight_clause(ANALYZE) AND ~domain_completed_clause(ANALYZE)`` — never a
bare ``status=='awaiting'`` read. Extracting it into ONE named builder means the card, the drain, and
recovery can never disagree. These tests pin that the builder is a byte-identical composition of the
LOCKED ``inflight_clause`` / ``domain_completed_clause`` siblings (DERIV-04), NOT a re-spelled predicate.

D-11: ``domain_completed_clause``'s docstring must carry the ``~inflight_clause`` prohibition rationale
(adding it would make ``domain_completed`` False for every recovery candidate — every candidate is a
ledger row by construction — silently disabling the secondary over-enqueue net, the 44.5K incident class).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_
from sqlalchemy.sql.elements import BooleanClauseList
from sqlalchemy.sql.operators import and_ as and_op

from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.stage_status import (
    awaiting_candidate_clause,
    domain_completed_clause,
    inflight_clause,
)


def _sql(expr: ColumnElement[bool]) -> str:
    """Compile a clause to a literal-inlined SQL string (so status literals surface, not placeholders)."""
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


def test_awaiting_candidate_clause_is_constructible() -> None:
    """The builder exists, takes no ``stage`` argument, and returns a non-None ``ColumnElement``."""
    clause = awaiting_candidate_clause()
    assert clause is not None
    assert isinstance(clause, ColumnElement)


def test_awaiting_candidate_clause_is_a_three_conjunct_and() -> None:
    """It is an ``AND`` of exactly three conjuncts, in the D-08 order."""
    clause = awaiting_candidate_clause()
    assert isinstance(clause, BooleanClauseList)
    assert clause.operator is and_op
    assert len(clause.clauses) == 3


def test_awaiting_candidate_clause_composes_locked_builders_verbatim() -> None:
    """Byte-identical to ``and_(status==AWAITING, ~inflight(ANALYZE), ~domain_completed(ANALYZE))``.

    Mutation guard: a divergent conjunct order, a dropped ``~domain_completed_clause``, or a
    re-spelled inline predicate would change the compiled SQL and flip this RED.
    """
    expected = and_(
        CloudJob.status == CloudJobStatus.AWAITING.value,
        ~inflight_clause(Stage.ANALYZE),
        ~domain_completed_clause(Stage.ANALYZE),
    )
    assert _sql(awaiting_candidate_clause()) == _sql(expected)


def test_awaiting_candidate_clause_encodes_the_awaiting_status_literal() -> None:
    """The AWAITING status literal is present (guards a bare ``status`` typo/omission)."""
    assert "awaiting" in _sql(awaiting_candidate_clause())


def test_domain_completed_clause_docstring_records_the_d11_prohibition() -> None:
    """D-11 trap: the ``~inflight_clause`` prohibition rationale is recorded in the docstring."""
    doc = domain_completed_clause.__doc__ or ""
    assert "inflight_clause" in doc
    assert "MUST NEVER" in doc
