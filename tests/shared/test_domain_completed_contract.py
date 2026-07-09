"""DB-free contract tests for the ``domain_completed`` twins (Phase 81, CR-02).

Bucket: ``shared`` (path segment immediately under ``tests/``). NO ``pytest.mark.integration``,
NO Postgres — ``domain_completed_clause`` only *constructs* a ``ColumnElement``, which needs no
connection, so both twins are exercised without a DB round-trip.

The D-17 formula ``done(stage) OR (failed(stage) AND FAILURE_IS_TERMINAL[stage])`` is defined ONLY
over the three enrich stages. The four downstream stages (tracklist / propose / review / apply) have
no domain predicate by design: ``phaze.tasks.reenqueue`` classifies them "live-keys-only" because
their terminal ack clears the ledger row on every outcome.

Before this lock the exclusion was implicit, and the two twins disagreed about it:

* ``domain_completed_clause(PROPOSE)`` raised a bare ``KeyError`` at clause-construction time.
* ``domain_completed({PROPOSE: DONE}, PROPOSE)`` returned ``True`` — the ``st is Status.DONE``
  disjunct short-circuits before the ``FAILURE_IS_TERMINAL`` subscript is ever evaluated.

So a ``DONE`` downstream row was silently domain-complete in Python and a crash in SQL. These tests
pin the exclusion as explicit, total, and *symmetric* across both halves of the drift-lock.
"""

from __future__ import annotations

import pytest

from phaze.enums.stage import FAILURE_IS_TERMINAL, Stage, Status, domain_completed
from phaze.services.stage_status import domain_completed_clause


ENRICH_STAGES = (Stage.METADATA, Stage.ANALYZE, Stage.FINGERPRINT)
DOWNSTREAM_STAGES = (Stage.TRACKLIST, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)


def test_failure_is_terminal_covers_exactly_the_enrich_stages() -> None:
    """The terminality table is the domain of both twins — pin its keys so neither can silently widen."""
    assert set(FAILURE_IS_TERMINAL) == set(ENRICH_STAGES)


def test_enrich_and_downstream_partition_the_stage_enum() -> None:
    """The two sets are a TOTAL, disjoint partition of Stage — no stage is silently undefined."""
    assert set(ENRICH_STAGES) | set(DOWNSTREAM_STAGES) == set(Stage)
    assert not set(ENRICH_STAGES) & set(DOWNSTREAM_STAGES)


@pytest.mark.parametrize("stage", DOWNSTREAM_STAGES)
def test_python_twin_rejects_downstream_stage_for_every_status(stage: Stage) -> None:
    """Python raises for a downstream stage at EVERY status — including DONE, which used to return True."""
    for status in Status:
        with pytest.raises(ValueError, match="defined only for the enrich stages"):
            domain_completed({stage: status}, stage)


@pytest.mark.parametrize("stage", DOWNSTREAM_STAGES)
def test_sql_twin_rejects_downstream_stage(stage: Stage) -> None:
    """SQL raises the same ValueError (not a bare KeyError) at clause-construction time."""
    with pytest.raises(ValueError, match="defined only for the enrich stages"):
        domain_completed_clause(stage)


@pytest.mark.parametrize("stage", DOWNSTREAM_STAGES)
def test_twins_reject_downstream_stages_symmetrically(stage: Stage) -> None:
    """CR-02 core: both twins must raise the SAME exception type for the SAME stage.

    A ``DONE`` status is the discriminating case — it short-circuits the Python twin's subscript,
    so this is precisely the cell where the two halves silently diverged.
    """
    with pytest.raises(ValueError) as py_exc:
        domain_completed({stage: Status.DONE}, stage)
    with pytest.raises(ValueError) as sql_exc:
        domain_completed_clause(stage)
    assert type(py_exc.value) is type(sql_exc.value)
    assert stage.value in str(py_exc.value)
    assert stage.value in str(sql_exc.value)


@pytest.mark.parametrize("stage", ENRICH_STAGES)
def test_enrich_stages_still_build_and_evaluate(stage: Stage) -> None:
    """The guard must not regress the three supported stages: both twins still answer."""
    assert domain_completed({stage: Status.DONE}, stage) is True
    assert domain_completed({stage: Status.NOT_STARTED}, stage) is False
    assert domain_completed({stage: Status.FAILED}, stage) is FAILURE_IS_TERMINAL[stage]
    assert domain_completed_clause(stage) is not None
