"""The DERIVED per-stage reporting buckets -- the five ``stage_status_case`` counts plus the
D-01a ``orphaned`` carve-out, corpus-wide and per-agent.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). The corpus-wide
:func:`_safe_bucket_counts` and the per-agent :func:`_agent_stage_buckets` are DELIBERATELY kept in
ONE module: D-04 makes the second a one-conjunct clone of the first, and the drift between them is
the hazard both docstrings are written against. Splitting them across modules would put the two
halves of that invariant where a reader of either cannot see the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select
import structlog

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage, Status
from phaze.models.file import FileRecord
from phaze.services.pipeline.common import MUSIC_VIDEO_TYPES
from phaze.services.stage_status import (
    orphaned_clause,
    stage_status_case,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# phaze-2u8v.2 / D-01a: the SIXTH reporting bucket, carved out of ``in_flight``. It is deliberately NOT
# a :class:`~phaze.enums.stage.Status` member -- the derived per-file status, ``eligible_clause`` and
# recovery are all unchanged; this is a reporting refinement only (see the D-01a record in
# ``services/stage_status.py``). Keeping it out of ``Status`` is what stops it perturbing the DERIV-04
# equivalence lock, the per-file pill ladder and the over-enqueue guard.
ORPHANED_BUCKET = "orphaned"


def _empty_buckets() -> dict[str, int]:
    """Return the zero-filled SIX-key reporting bucket dict (the five ``Status`` values + ``orphaned``)."""
    out: dict[str, int] = {s.value: 0 for s in Status}
    out[ORPHANED_BUCKET] = 0
    return out


async def _safe_orphan_split(session: AsyncSession, stage: Stage, buckets: dict[str, int], *, agent_id: str | None = None) -> None:
    """Move ``stage``'s ORPHANED files out of the ``in_flight`` bucket, in place. Degrade-safe (D-01a).

    ``in_flight`` as derived by :func:`~phaze.services.stage_status.stage_status_case` is bare
    scheduling-ledger existence, which is "scheduled and unresolved" -- the UNION of work that is really
    running and work that was scheduled and then lost. On the live archive that second population had
    grown to 2146 of 4963 analyze rows, so the counter contradicted SAQ by more than a factor of two.
    This carves the lost half out into :data:`ORPHANED_BUCKET` using
    :func:`~phaze.services.stage_status.orphaned_clause`, whose set is definitionally the one
    ``recover_orphaned_work`` re-drives.

    NOT a relabelling and NOT a subtraction that loses files: the six buckets still sum to the same
    total, ``in_flight`` now means what its name says, and the carved-out count is rendered as its own
    cell next to it.

    Relationship to :func:`get_stage_orphan_counts` (the amber rail badge): SAME definition, different
    scope and substrate. That one materializes the whole ledger in Python and counts EVERY row for the
    stage's function; this one is the SQL twin restricted to the music/video corpus the bucket dict is
    defined over (and, in :func:`_agent_stage_buckets`, to one agent). The two therefore agree except
    on ledger rows for files outside that scope -- which is the correct behavior for a per-corpus
    bucket, not a drift. Both are pinned to recovery's candidate set by their own tests.

    Degrade discipline (the whole point of doing this HERE rather than inside the locked CASE ladder):
    the count runs in its own SAVEPOINT and ANY error -- most plausibly an unreadable/absent ``saq_jobs``
    in a pre-migration or test environment -- leaves ``orphaned`` at 0 and ``in_flight`` at exactly
    today's ledger-only value. Only the three enrich stages have a per-file ledger key; every other stage
    is a no-op (``orphaned_clause`` raises on them by design).
    """
    if stage not in ELIGIBLE_AFTER_FAILURE:  # enrich stages only -- orphaned_clause raises on downstream
        return
    stmt = select(func.count()).select_from(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).where(orphaned_clause(stage))
    if agent_id is not None:
        stmt = stmt.where(FileRecord.agent_id == agent_id)
    try:
        async with session.begin_nested():
            orphaned = int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("stage_orphan_split_degraded", stage=stage.value, agent_id=agent_id, exc_info=True)
        return
    # Clamp: the bucket read and this count are separate statements, so a concurrent enqueue between
    # them could in principle report more orphans than in-flight. Never publish a negative in_flight.
    orphaned = min(orphaned, buckets[Status.IN_FLIGHT.value])
    buckets[ORPHANED_BUCKET] = orphaned
    buckets[Status.IN_FLIGHT.value] -= orphaned


async def _safe_bucket_counts(session: AsyncSession, stage: Stage) -> dict[str, int]:
    """Return the five-way ``{not_started, in_flight, done, skipped, failed}`` count for ``stage``, degrade-safe.

    ONE ``GROUP BY stage_status_case(stage)`` scoped to music/video files. Because every music/video
    file resolves to exactly one of the five :func:`phaze.services.stage_status.stage_status_case`
    buckets (precedence ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``; ``skipped`` is the
    Phase-87 force-skip marker, enrich-only), the five counts SUM to
    ``music_video_total`` on a healthy query. Reuses the LOCKED ``stage_status_case`` ``CASE`` ladder
    verbatim -- NEVER a fresh ``CASE`` (D-04) -- so the buckets can never drift from the DERIV-04
    equivalence lock (and, transitively, the Python resolver).

    Mirrors the :func:`_safe_count` degrade discipline (INFLIGHT-02): the dict zero-fills first, and the
    read runs in a SAVEPOINT so ANY exception can be logged without rolling back the caller's outer
    transaction. It returns the all-zero dict -- it NEVER raises into the hot 5s /pipeline/stats poll. On that fail-safe-to-zero
    degrade the five buckets intentionally do NOT sum to ``music_video_total``; the sum-to-total
    invariant is a healthy-query property only, NEVER a runtime assertion in the poll path (Pitfall 3).
    """
    return (await _safe_bucket_snapshot(session, stage)).counts


async def _agent_stage_buckets(session: AsyncSession, agent_id: str, stage: Stage) -> dict[str, int]:
    """Per-agent five-way ``{not_started, in_flight, done, skipped, failed}`` count for ``stage``, degrade-safe.

    A one-conjunct clone of :func:`_safe_bucket_counts` (DRILL-02 / D-04): the SAME GroupingError-safe
    inner-subquery-then-``GROUP BY``-scalar-label shape, with the SINGLE addition of
    ``.where(FileRecord.agent_id == agent_id)`` on the inner subquery so the aggregate counts ONLY the
    music/video files THIS agent owns. Reuses the LOCKED :func:`phaze.services.stage_status.stage_status_case`
    ``CASE`` ladder verbatim (D-00a / DERIV-04) -- NEVER a fresh ``CASE`` -- so the per-agent buckets can
    never drift from the single derivation (and, transitively, the Python resolver).

    Because every one of the agent's music/video files resolves to exactly one of the five
    ``stage_status_case`` buckets (precedence ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``), the
    five counts SUM to the agent's music/video total on a HEALTHY query -- a healthy-path property only,
    NEVER a runtime assertion in the poll path (Pitfall 3). On ANY query error this mirrors the
    :func:`_safe_bucket_counts` degrade discipline (INFLIGHT-02 / D-00b): it logs a warning and returns
    the all-zero dict -- it NEVER raises into the hot ``/admin/agents/{id}/_activity`` poll. On that
    fail-safe degrade the five buckets intentionally do NOT sum to the total.

    The read runs inside a SAVEPOINT (``begin_nested``) so a bucket-query error rolls back the NESTED
    scope ALONE -- recovering the aborted transaction WITHOUT expiring the caller's already-loaded
    ``agent`` ORM object. ``agent_activity`` loads ``agent`` BEFORE these six bucket reads and renders
    its attributes AFTER, so a plain ``session.rollback()`` here would expire ``agent`` and 500 the
    render on the next lazy load (CR-01) -- exactly the hazard :func:`get_agent_recent_scans` guards
    against on the same object.
    """
    out: dict[str, int] = _empty_buckets()
    # Materialize the per-row status label in an inner subquery FIRST, then GROUP BY the scalar label in
    # the outer query -- grouping directly by ``stage_status_case(stage)`` fails on Postgres (the CASE
    # ladder embeds correlated ``exists(... == FileRecord.id)`` subqueries; a top-level GROUP BY re-projects
    # the ungrouped ``files.id`` -> "subquery uses ungrouped column" GroupingError). The ONLY delta from
    # :func:`_safe_bucket_counts` is the ``FileRecord.agent_id == agent_id`` conjunct (D-04).
    status_subq = (
        select(stage_status_case(stage).label("status"))
        .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
        .where(FileRecord.agent_id == agent_id)
        .subquery()
    )
    stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
    try:
        # SAVEPOINT degrade (CR-01 / D-00b): roll back the NESTED scope alone on error so the aborted
        # transaction recovers WITHOUT expiring the caller's already-loaded ``agent`` (a plain
        # ``session.rollback()`` would expire it and 500 the render on the next lazy load).
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("agent_stage_bucket_degraded", stage=stage.value, agent_id=agent_id, exc_info=True)
        return out
    for status_label, n in rows:
        if status_label in out:
            out[status_label] = int(n)
    # phaze-2u8v.2 / D-01a: carve ORPHANED out of in_flight, scoped to THIS agent's files (the same
    # single ``agent_id`` conjunct that scopes the bucket read above). Own SAVEPOINT, own zero degrade.
    await _safe_orphan_split(session, stage, out, agent_id=agent_id)
    return out


@dataclass(frozen=True)
class StageBucketSnapshot:
    """Canonical stage buckets plus whether their aggregation succeeded."""

    counts: dict[str, int]
    available: bool


def _stage_bucket_stmt(stage: Stage) -> Select[Any]:
    status_subq = select(stage_status_case(stage).label("status")).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).subquery()
    return select(status_subq.c.status, func.count()).group_by(status_subq.c.status)


async def _safe_bucket_snapshot(session: AsyncSession, stage: Stage) -> StageBucketSnapshot:
    """Availability-bearing form of :func:`_safe_bucket_counts` for truthful metric consumers."""
    out: dict[str, int] = _empty_buckets()
    # Materialize the per-row status label in an inner subquery FIRST, then GROUP BY the label in the
    # outer query. Grouping directly by ``stage_status_case(stage)`` fails on Postgres -- the CASE ladder
    # embeds correlated ``exists(... == FileRecord.id)`` subqueries, and a top-level GROUP BY on that
    # expression re-projects the ungrouped ``files.id`` ("subquery uses ungrouped column" GroupingError).
    # The derived-table form evaluates the per-file status once per row (where ``files.id`` is in scope),
    # so the outer aggregation groups a plain scalar label.
    stmt = _metadata_status_stmt() if stage is Stage.METADATA else _stage_bucket_stmt(stage)
    try:
        async with session.begin_nested():
            for status_label, n in (await session.execute(stmt)).all():
                if status_label in out:
                    out[status_label] = int(n)
    except Exception:
        logger.warning("stage_bucket_degraded", stage=stage.value, exc_info=True)
        return StageBucketSnapshot(counts=out, available=False)
    # phaze-2u8v.2 / D-01a: carve ORPHANED out of in_flight (own SAVEPOINT, own zero degrade). Skipped on
    # the degrade path above -- all-zero buckets have nothing to split, and the session may be recovering.
    await _safe_orphan_split(session, stage, out)
    return StageBucketSnapshot(counts=out, available=True)


def _metadata_status_stmt() -> Select[Any]:
    """Build the canonical metadata status aggregation used by the workspace metrics."""
    return _stage_bucket_stmt(Stage.METADATA)
