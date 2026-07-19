"""Outage recovery: re-queue files whose fingerprint stage burned during an engine outage.

Filed as phaze-rf04.1, out of the 2026-07-18 incident. Both fingerprint sidecars were
down for ~8.5 hours (panako shipped without its jar -- phaze-vp07; audfprint could never
bootstrap its database -- phaze-6kw0). Every job in that window drained as
``status="partial"`` with ZERO engines succeeding, so per ELIG-04 aggregation
(``failed_clause(Stage.FINGERPRINT)``: no engine succeeded AND at least one failed)
11,427 files landed fingerprint-FAILED. Nothing re-drives them on its own.

This module exists so that recovery is a tracked, re-runnable repo capability rather
than a one-off shell pipeline -- if an engine outage recurs, the same command recovers
it. Surfaced as ``phaze fingerprint requeue`` (see :mod:`phaze.cli`).

Design constraints discovered while building this, each load-bearing:

  - **Scope by WINDOW, never by "all failed".** A blind retry of every failed file also
    re-drives genuinely corrupt input that a healthy engine correctly rejected. The
    window is required, not optional.
  - **There is no ``failed_at`` column** on ``fingerprint_results`` (unlike ``analysis``
    and ``metadata``). The window therefore rides ``FingerprintResult.updated_at`` on the
    failed engine rows. This is the one place the recovery query cannot reuse an existing
    predicate.
  - **Respect operator intent.** ``~skipped_clause`` keeps a deliberately force-SKIPPED
    file from being resurrected by a bulk recovery.
  - **Pausing does NOT block enqueue** (``phaze.tasks._shared.stage_control``); it parks
    the job at ``scheduled = SENTINEL``. Re-queueing while the stage is paused is
    therefore the CORRECT order: jobs land parked, and the subsequent
    ``POST /pipeline/stages/fingerprint/resume`` releases them only once the engines are
    proven healthy. Resuming first would simply burn the backlog a second time.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select
import structlog

from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.schemas.agent_tasks import FingerprintFilePayload
from phaze.services.pipeline import MUSIC_VIDEO_TYPES
from phaze.services.stage_status import Stage, dedup_resolved_clause, failed_clause, skipped_clause


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# phaze-e57w: SAQ statuses that mean the deterministic key is held by a job that is NOT genuinely
# in flight -- it is dead or dying and will never process the file, yet its surviving key blocks
# re-enqueue (SAQ's _enqueue upsert only overwrites keys in aborted/complete/failed, never
# 'aborting'). A file colliding with one of these is BLOCKED, not "already in flight".
_BLOCKED_STATUS_VALUES = frozenset({"aborting", "failed", "aborted"})
# Non-terminal statuses: genuinely in flight ONLY if the row is not also stale/stuck.
_LIVE_STATUS_VALUES = frozenset({"new", "deferred", "queued", "active"})


@dataclasses.dataclass(frozen=True)
class FingerprintEnqueueResult:
    """Outcome of an :func:`enqueue_fingerprint_jobs` batch, with in-flight and BLOCKED separated.

    phaze-e57w: a ``None`` return from ``queue.enqueue`` (a deterministic-key collision) used to be
    reported uniformly as "already in flight". That is FALSE for a file whose key is held by a dead
    ``aborting``/``failed``/``aborted`` row (or a stuck-past-timeout claimed row): the file is not in
    flight, it is BLOCKED and needs the reaper/operator. These counts keep the two apart so the
    benign case stays quiet and the blocked case is reported loudly.
    """

    accepted: int
    in_flight: int
    blocked: int
    blocked_keys: tuple[str, ...]


def _classify_collision(job: Any) -> str:
    """Classify a deterministic-key collision as ``"in_flight"`` or ``"blocked"``.

    ``job`` is the existing ``saq_jobs`` row (via ``queue.job(key)``) or ``None`` if it could not be
    looked up. A dead/dying status (aborting/failed/aborted) is BLOCKED; a live status (queued/active)
    is BLOCKED only when the row is also ``stuck`` (past its timeout/heartbeat -- a stale claimed row
    that will never make progress on its own), otherwise it is genuinely in flight. An unlookupable /
    unknown-status row degrades to in_flight (benign) rather than crying wolf.
    """
    if job is None:
        return "in_flight"
    status = getattr(job, "status", None)
    sval = getattr(status, "value", status)
    if sval in _BLOCKED_STATUS_VALUES:
        return "blocked"
    if sval in _LIVE_STATUS_VALUES and bool(getattr(job, "stuck", False)):
        return "blocked"
    return "in_flight"


def _to_naive_utc(value: datetime.datetime) -> datetime.datetime:
    """Return ``value`` as a naive UTC datetime (tz-aware input is converted, not truncated)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.UTC).replace(tzinfo=None)


async def enqueue_fingerprint_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> FingerprintEnqueueResult:
    """Enqueue ``fingerprint_file`` jobs with the COMPLETE payload; return a classified outcome.

    THE single fingerprint enqueue funnel -- the HTTP triggers in
    :mod:`phaze.routers.pipeline` and the recovery CLI both route through here so the
    payload shape can never drift between them.

    ``FingerprintFilePayload`` (``extra="forbid"``) requires file_id, original_path and
    agent_id; a ``file_id``-only enqueue dead-letters every job. The deterministic key
    (``fingerprint_file:<file_id>``) is applied centrally by the ``before_enqueue`` hook
    (35-01), so enqueueing a file that is already in flight collapses to a ``None``
    return. Those are NOT counted as accepted -- a caller reporting "re-queued N" must
    not count work it did not actually create.

    phaze-e57w: a ``None`` return no longer collapses to a single "already in flight" tally. Each
    collision is looked up (``queue.job(key)``) and classified: a live queued/active row is
    ``in_flight`` (benign), while a dead ``aborting``/``failed``/``aborted`` row -- or a stuck-past-
    timeout claimed row -- is ``blocked`` (the file is NOT in flight; its zombie key must be reaped
    before it can recover). The blocked keys are returned so the caller can report them loudly.
    """
    accepted = 0
    in_flight = 0
    blocked_keys: list[str] = []
    job_lookup = getattr(queue, "job", None)
    for f in files:
        payload = FingerprintFilePayload(
            file_id=f.id,
            original_path=f.original_path,
            agent_id=agent_id,
        )
        job = await queue.enqueue("fingerprint_file", **payload.model_dump(mode="json"))
        if job is not None:
            accepted += 1
            continue
        # Collision: the deterministic key already exists. Inspect the holder to tell a genuine
        # in-flight job from a zombie that permanently blocks this file.
        key = f"fingerprint_file:{f.id}"
        existing = await job_lookup(key) if callable(job_lookup) else None
        if _classify_collision(existing) == "blocked":
            blocked_keys.append(key)
        else:
            in_flight += 1
    return FingerprintEnqueueResult(
        accepted=accepted,
        in_flight=in_flight,
        blocked=len(blocked_keys),
        blocked_keys=tuple(blocked_keys),
    )


async def select_outage_failed_files(
    session: AsyncSession,
    since: datetime.datetime,
    until: datetime.datetime,
    limit: int | None = None,
) -> list[FileRecord]:
    """Return music/video files whose fingerprint stage FAILED inside ``[since, until]``.

    The window is matched against ``FingerprintResult.updated_at`` on the file's FAILED
    engine rows -- a file qualifies if at least one of its failed engine rows was written
    in the window. ``failed_clause`` already guarantees the file-level fact (no engine
    succeeded AND at least one failed), so this predicate only narrows WHICH failures.

    Excludes dedup-resolved files (they have no independent fingerprint obligation) and
    operator-SKIPPED files (recovery must not override a deliberate skip).
    """
    # `TimestampMixin` columns are TIMESTAMP WITHOUT TIME ZONE holding UTC, so asyncpg
    # rejects a tz-aware bound outright. Callers hand us aware datetimes (the CLI
    # normalizes to UTC precisely so the window cannot be misread); convert to naive UTC
    # HERE, at the DB boundary, rather than pushing naive datetimes through the API where
    # an accidental local-time value would silently shift the window by hours.
    since_naive = _to_naive_utc(since)
    until_naive = _to_naive_utc(until)

    failed_in_window = exists(
        select(FingerprintResult.id).where(
            FingerprintResult.file_id == FileRecord.id,
            FingerprintResult.status == "failed",
            FingerprintResult.updated_at >= since_naive,
            FingerprintResult.updated_at <= until_naive,
        ),
    )

    stmt = (
        select(FileRecord)
        .where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            failed_clause(Stage.FINGERPRINT),
            ~skipped_clause(Stage.FINGERPRINT),
            ~dedup_resolved_clause(),
            failed_in_window,
        )
        .order_by(FileRecord.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return list(result.scalars().all())
