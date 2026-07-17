"""Raw ``saq_jobs`` backlog-mutation helpers for the per-stage control plane (Phase 37).

The before-enqueue ``apply_stage_control`` hook (``phaze.tasks._shared.stage_control``) only
stamps NEW jobs. These three helpers mutate the EXISTING queued backlog so an operator action
takes effect immediately on jobs already enqueued:

- :func:`set_stage_priority` -- reorder the queued backlog (lower ``priority`` dequeues sooner);
- :func:`pause_stage` -- park the queued backlog (``scheduled = SENTINEL``); active jobs drain;
- :func:`resume_stage` -- un-park ONLY pause-parked rows (sentinel-guarded so retry backoffs,
  whose ``scheduled = now + delay`` is never ``== SENTINEL``, are preserved -- REQ-37-3).

``saq_jobs`` has NO ``function`` column (the function name lives inside the serialized ``job``
BYTEA blob), so each helper filters on the deterministic key prefix ``key LIKE '<fn>:%'`` --
exact because Phase 35 made keys ``<function>:<file_id>``. The ``status = 'queued'`` guard on
every UPDATE is what makes drain (active jobs untouched) AND the no-double-pickup guarantee
safe: it contends with the dequeue's ``FOR UPDATE SKIP LOCKED`` on the same row lock, so a
being-picked-up job is unmutatable (37-RESEARCH Concurrency Safety, T-37-03).

Security (T-37-01): the only operator-supplied value reaching SQL is ``stage``, validated
against the :data:`STAGE_TO_FUNCTION` allowlist BEFORE the ``key LIKE`` prefix is built; every
parameter (``:p`` / ``:s`` / ``:pfx``) is bound via :func:`sqlalchemy.text` -- the SQL strings
are static module constants with no user input interpolated.

These helpers run ONLY in the API / controller process (they take an ``AsyncSession`` and may
use SQLAlchemy); they are NEVER imported by the agent worker (unlike the enqueue hook). They
do NOT commit -- the calling endpoint owns the transaction so the control-row ORM update and
the backlog UPDATE land atomically. They do NOT scope by ``queue`` -- stages span every
per-agent queue and the global ``key`` PK disambiguates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from phaze.tasks._shared.stage_control import SENTINEL, STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Static, bound-param SQL (no user input interpolated -- T-37-01). ``:pfx`` carries the
# allowlisted key prefix; ``:p`` / ``:s`` carry the new priority / sentinel.
_SET_PRIORITY_SQL = text("UPDATE saq_jobs SET priority = :p WHERE status = 'queued' AND key LIKE :pfx")
_PAUSE_SQL = text("UPDATE saq_jobs SET scheduled = :s WHERE status = 'queued' AND key LIKE :pfx")
# Resume is sentinel-guarded: only rows pause parked (scheduled == SENTINEL) are un-parked,
# so a genuine retry backoff (scheduled = now + delay, never == SENTINEL) is never clobbered.
#
# CRITICAL (phaze-01aq): un-park the scheduled COLUMN *and* the serialized ``job`` BYTEA blob. A job
# enqueued WHILE paused is stamped ``job.scheduled = SENTINEL`` by ``apply_stage_control`` before
# enqueue, which SAQ serializes into the JSON blob (SENTINEL != the default 0 that ``Job.to_dict``
# omits). Resetting only the column leaves the blob at SENTINEL: on dequeue SAQ deserializes it back
# into ``job.scheduled`` (even re-writing the column to SENTINEL), and because this project never
# overrides ``retry_delay``, SAQ's ``_retry`` falls to ``scheduled = job.scheduled or now_seconds()``
# -> the row re-parks at SENTINEL forever, invisible to every recovery path. We therefore strip the
# ``scheduled`` key from the JSON blob (``jsonb - 'scheduled'``), which is exactly the shape SAQ emits
# for an unparked job (``to_dict`` omits the default-0 field), so the deserialized job.scheduled is 0.
# The blob is default-JSON (queue_factory sets no custom dump/load), stored as UTF-8 BYTEA.
_RESUME_SQL = text(
    "UPDATE saq_jobs "
    "SET scheduled = 0, "
    "job = convert_to(((convert_from(job, 'UTF8')::jsonb) - 'scheduled')::text, 'UTF8') "
    "WHERE status = 'queued' AND key LIKE :pfx AND scheduled = :s"
)


def _key_prefix(stage: str) -> str:
    """Return the ``key LIKE`` prefix for ``stage`` after allowlist validation (T-37-01).

    Raises :class:`ValueError` for an unknown stage so the router can convert it to a 422 --
    the allowlist check happens BEFORE the prefix is built so no unvalidated value reaches SQL.
    """
    if stage not in STAGE_TO_FUNCTION:
        raise ValueError(f"unknown stage: {stage!r}")
    return f"{STAGE_TO_FUNCTION[stage]}:%"


async def set_stage_priority(session: AsyncSession, stage: str, new_priority: int) -> None:
    """Reorder the queued backlog for ``stage`` to ``new_priority`` (lower dequeues sooner)."""
    pfx = _key_prefix(stage)
    await session.execute(_SET_PRIORITY_SQL, {"p": new_priority, "pfx": pfx})


async def pause_stage(session: AsyncSession, stage: str) -> None:
    """Park the queued backlog for ``stage`` (``scheduled = SENTINEL``); active jobs drain."""
    pfx = _key_prefix(stage)
    await session.execute(_PAUSE_SQL, {"s": SENTINEL, "pfx": pfx})


async def resume_stage(session: AsyncSession, stage: str) -> None:
    """Un-park ONLY pause-parked rows for ``stage`` (sentinel-guarded; retry backoffs kept)."""
    pfx = _key_prefix(stage)
    await session.execute(_RESUME_SQL, {"pfx": pfx, "s": SENTINEL})


__all__ = ["pause_stage", "resume_stage", "set_stage_priority"]
