"""Canonical per-stage control constants (DB-free, agent-boundary-safe) -- Phase 37.

Single source of truth for the three agent pipeline stages, their registered SAQ
function names, and the pause "park" sentinel. Every downstream consumer imports
these EXACT constants rather than re-deriving them:

- the before-enqueue ``apply_stage_control`` hook (Plan 37-02) -- stamps new jobs,
- the pause endpoint (Plan 37-04) -- parks the queued backlog with ``scheduled = SENTINEL``,
- the resume guard (Plan 37-04) -- un-parks ONLY rows whose ``scheduled == SENTINEL``.

``STAGE_TO_FUNCTION`` maps each stage label to the registered SAQ function name
(``job.function``), verified against ``_KEY_BUILDERS`` in
:mod:`phaze.tasks._shared.deterministic_key`. The raw ``saq_jobs`` UPDATEs filter on
``key LIKE '<function>:%'`` (there is no ``function`` column), so this mapping is also
the source of the key prefixes.

``SENTINEL = 9999999999`` is a fixed epoch-seconds value (year 2286) that is far beyond
any legitimate ``scheduled`` (retry backoffs are ``now + small delay``; cron jobs are
``now + interval``). It is a SINGLE shared constant so the resume guard's
``scheduled == SENTINEL`` comparison is exact -- never recompute it per call (37-RESEARCH
SENTINEL value / Anti-Patterns).

CRITICAL boundary rule (37-RESEARCH Pitfall 4): this module must NOT import
``phaze.database``, ``phaze.tasks.session``, or ``sqlalchemy.ext.asyncio``. It stays
pure-constants so the agent worker can import it without pulling the ORM/DB layer across
the agent import boundary (covered by ``tests/test_task_split.py``). The
``apply_stage_control`` hook + its TTL cache arrive in Plan 02 -- this module is
interface-first.
"""

from __future__ import annotations

import structlog


logger = structlog.get_logger(__name__)


# Stage label -> registered SAQ function name (job.function). Verified against
# _KEY_BUILDERS in phaze.tasks._shared.deterministic_key. The deterministic key form
# is "<function>:<file_id>", so the saq_jobs filter is `key LIKE '<function>:%'`.
STAGE_TO_FUNCTION: dict[str, str] = {
    "metadata": "extract_file_metadata",
    "analyze": "process_file",
    "fingerprint": "fingerprint_file",
}

# Exact inverse: registered function name -> stage label. Used by the enqueue hook to
# resolve job.function back to its stage (non-stage jobs map to None and are untouched).
_FUNCTION_TO_STAGE: dict[str, str] = {v: k for k, v in STAGE_TO_FUNCTION.items()}

# Pause "park" value: a far-future epoch-seconds timestamp (2286-11-20). A queued job
# with scheduled = SENTINEL fails the dequeue's `now >= scheduled` gate, so it parks.
# Resume un-parks ONLY rows whose scheduled == SENTINEL, structurally protecting genuine
# retry backoffs (which use now + delay, never == SENTINEL).
SENTINEL: int = 9999999999


__all__ = ["SENTINEL", "STAGE_TO_FUNCTION", "_FUNCTION_TO_STAGE"]
