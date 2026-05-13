"""Execution-status enum (DB-free).

Lives outside :mod:`phaze.models` so that :mod:`phaze.schemas.agent_execution`
(loaded inside the agent worker process) does not transitively pull in
SQLAlchemy / :mod:`phaze.database`. See Phase 26 D-03 / Plan 11.

:mod:`phaze.models.execution` re-imports and re-exports this symbol so legacy
imports ``from phaze.models.execution import ExecutionStatus`` keep working.
"""

from __future__ import annotations

import enum


class ExecutionStatus(enum.StrEnum):
    """Status of a file operation execution.

    Monotonic ladder enforced at the PATCH /execution-log/{id} router
    (Phase 25 D-15): PENDING < IN_PROGRESS < COMPLETED < FAILED.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
