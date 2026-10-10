"""Guard arms of ``services/stage_status`` that no end-to-end path reaches (phaze-227l5 branch-check).

Each is an explicit contract of the SQL builder: a downstream stage has no per-file ledger key, only the
enrich stages are retry-eligible, and an ineligible stage is refused loudly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import false

from phaze.enums.stage import Stage
from phaze.services.stage_status import eligible_clause, live_job_clause


def test_live_job_clause_is_false_for_a_stage_with_no_ledger_key() -> None:
    assert str(live_job_clause(Stage.PROPOSE)) == str(false())


def test_eligible_clause_refuses_a_downstream_stage() -> None:
    with pytest.raises(ValueError, match="only for the enrich stages"):
        eligible_clause(Stage.PROPOSE)
