"""``not_running_clause`` -- ``¬running`` as a conjunction, not a negated disjunction (phaze-ejs09).

On host-prod (2026-09-25, 2026.9.4) the analyze orphan split planned at a cost of 1,694,212. Under
``NOT (live_job OR cloud_busy)`` Postgres keeps both ``EXISTS`` probes as per-row SubPlans and costs
them as correlated probes. The ``saq_jobs`` probe's cost grows with that table's index pages over its
live rows, and the broker's churn leaves a bloated index over a handful of live jobs. That put the
statement past ``jit_inline_above_cost``, so every ``/s/analyze`` render and ``/pipeline/stats`` poll
paid 163-179 ms of JIT compilation for an 11.5 ms statement. ``NOT live AND NOT busy`` has the same
truth value and lets the planner anti-join the probe.

Two claims, two tests:

* **Equivalence.** Over every combination of broker state x compute-lane state x ledger row x domain
  outcome, the rewritten predicates select the IDENTICAL file set / ledger-key set as the pre-rewrite
  ``not_(running_clause(...))`` spelling, for every consumer: ``orphaned_clause`` (the bucket orphan
  split, the ``/pipeline/files`` orphan lens and the per-file orphan detail) and both reaper target sets
  (computed through the reaper's own SELECT builders -- nothing is deleted).
* **Plan shape.** Under a churn-bloated ``saq_jobs`` with a handful of live rows, the pre-rewrite
  statement crosses ``jit_inline_above_cost`` (the control: it proves the fixture reproduces the
  hazard) and the rewritten one stays below it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import itertools
import json
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import ColumnElement, and_, func, not_, or_, select, text
from sqlalchemy.dialects import postgresql

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage
from phaze.models.file import FileRecord
from phaze.services.pipeline.common import MUSIC_VIDEO_TYPES
from phaze.services.stage_status import (
    CLOUD_LANE_FUNCTIONS,
    _recovery_domain_completed_clause,
    cloud_busy_clause,
    cloud_lane_completed_clause,
    inflight_clause,
    inflight_for_function,
    ledger_key_for_function,
    live_job_for_function,
    orphaned_clause,
    running_clause,
)
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.ledger_reaper import _resolved_cloud_keys_subquery, _resolved_keys_subquery


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_STAGES: tuple[Stage, ...] = tuple(sorted(ELIGIBLE_AFTER_FAILURE, key=lambda s: s.value))
_FUNCTIONS: tuple[str, ...] = (*(STAGE_TO_FUNCTION[s.value] for s in _STAGES), *CLOUD_LANE_FUNCTIONS)

_SAQ_STATES: tuple[str | None, ...] = ("queued", "active", "complete", None)
_CLOUD_STATES: tuple[str | None, ...] = ("uploading", "uploaded", "submitted", "running", "awaiting", "succeeded", "failed", None)
_LEDGER_STATES: tuple[bool, ...] = (True, False)
# failed_old: failed BEFORE the ledger row was written (a lost operator retry, D-10). failed_new: after it.
_OUTCOMES: tuple[str, ...] = ("done", "skipped", "failed_old", "failed_new", "none")


# The pre-rewrite spellings, verbatim: the oracle the rewrite must match.
def _old_orphaned(stage: Stage) -> ColumnElement[bool]:
    return and_(inflight_clause(stage), not_(running_clause(stage)), not_(_recovery_domain_completed_clause(stage)))


def _old_resolved(stage: Stage) -> ColumnElement[bool]:
    return and_(inflight_clause(stage), not_(running_clause(stage)), _recovery_domain_completed_clause(stage))


def _old_resolved_cloud(func_name: str) -> ColumnElement[bool]:
    return and_(inflight_for_function(func_name), not_(or_(live_job_for_function(func_name), cloud_busy_clause())), cloud_lane_completed_clause())


async def _saq_table(session: AsyncSession) -> None:
    """Pin a ``saq_jobs`` this module controls (DROP + CREATE, undone by the per-test rollback)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    await session.commit()


async def _seed_matrix(session: AsyncSession) -> int:
    """One file per (saq x cloud x ledger x outcome) cell; every function key shares the cell's state."""
    now = datetime.now(UTC)
    files: list[dict[str, Any]] = []
    saq: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    cloud: list[dict[str, Any]] = []
    analysis: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    for saq_state, cloud_state, has_ledger, outcome in itertools.product(_SAQ_STATES, _CLOUD_STATES, _LEDGER_STATES, _OUTCOMES):
        fid = uuid.uuid4()
        files.append({"id": fid, "sha": uuid.uuid4().hex + uuid.uuid4().hex, "path": f"/test/matrix/{fid}.mp3"})
        for fn in _FUNCTIONS:
            if saq_state is not None:
                saq.append({"key": f"{fn}:{fid}", "status": saq_state})
            if has_ledger:
                ledger.append({"key": f"{fn}:{fid}", "function": fn, "payload": json.dumps({"file_id": str(fid)})})
        if cloud_state is not None:
            cloud.append({"id": uuid.uuid4(), "file_id": fid, "status": cloud_state})
        if outcome == "done":
            analysis.append({"id": uuid.uuid4(), "file_id": fid, "completed": now, "failed": None})
            metadata.append({"id": uuid.uuid4(), "file_id": fid, "failed": None})
        elif outcome == "skipped":
            skips.extend({"id": uuid.uuid4(), "file_id": fid, "stage": s.value} for s in _STAGES)
        elif outcome in ("failed_old", "failed_new"):
            when = now - timedelta(days=1) if outcome == "failed_old" else now + timedelta(days=1)
            analysis.append({"id": uuid.uuid4(), "file_id": fid, "completed": None, "failed": when})
            metadata.append({"id": uuid.uuid4(), "file_id": fid, "failed": when})

    await session.execute(
        text(
            "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
            "VALUES (:id, :sha, :path, 'm.mp3', :path, 'mp3', 1024, 'test-fileserver')"
        ),
        files,
    )
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"), saq)
    await session.execute(
        text("INSERT INTO scheduling_ledger (key, function, routing, payload) VALUES (:key, :function, 'agent', CAST(:payload AS jsonb))"),
        ledger,
    )
    await session.execute(text("INSERT INTO cloud_job (id, file_id, status) VALUES (:id, :file_id, :status)"), cloud)
    await session.execute(
        text("INSERT INTO analysis (id, file_id, analysis_completed_at, failed_at, error_message) VALUES (:id, :file_id, :completed, :failed, NULL)"),
        analysis,
    )
    await session.execute(text("INSERT INTO metadata (id, file_id, failed_at) VALUES (:id, :file_id, :failed)"), metadata)
    await session.execute(text("INSERT INTO stage_skip (id, file_id, stage, reason) VALUES (:id, :file_id, :stage, 'test')"), skips)
    await session.commit()
    return len(files)


async def _ids(session: AsyncSession, clause: ColumnElement[bool]) -> set[uuid.UUID]:
    return set((await session.execute(select(FileRecord.id).where(clause))).scalars().all())


async def _keys(session: AsyncSession, stmt: Any) -> set[str]:
    return set((await session.execute(stmt)).scalars().all())


async def test_rewrite_selects_the_identical_sets_for_every_consumer(session: AsyncSession) -> None:
    """Old (``not_(running)``) vs new (``not_running_clause``) over the full state matrix, per consumer."""
    await _saq_table(session)
    total = await _seed_matrix(session)
    assert total == len(_SAQ_STATES) * len(_CLOUD_STATES) * len(_LEDGER_STATES) * len(_OUTCOMES)

    for stage in _STAGES:
        old, new = await _ids(session, _old_orphaned(stage)), await _ids(session, orphaned_clause(stage))
        assert new == old, f"orphaned_clause({stage.value}) drifted: +{len(new - old)} / -{len(old - new)}"
        assert 0 < len(new) < total, f"orphaned_clause({stage.value}) matrix cell is degenerate ({len(new)}/{total})"

        func_name = STAGE_TO_FUNCTION[stage.value]
        old_keys = await _keys(session, select(ledger_key_for_function(func_name)).where(_old_resolved(stage)))
        new_keys = await _keys(session, _resolved_keys_subquery(stage))
        assert new_keys == old_keys, f"reaper target set for {stage.value} drifted: +{len(new_keys - old_keys)} / -{len(old_keys - new_keys)}"
        assert 0 < len(new_keys) < total

    for func_name in CLOUD_LANE_FUNCTIONS:
        old_keys = await _keys(session, select(ledger_key_for_function(func_name)).where(_old_resolved_cloud(func_name)))
        new_keys = await _keys(session, _resolved_cloud_keys_subquery(func_name))
        assert new_keys == old_keys, f"cloud reaper target set for {func_name} drifted: +{len(new_keys - old_keys)} / -{len(old_keys - new_keys)}"
        assert 0 < len(new_keys) < total


# ~host-prod's shape (25,866 files, 11,428 media) and its saq_jobs bloat (725 pkey pages over 12 live
# rows, 2026-09-25). The churn is what bloats the index; the live rows are what the stats see.
_PERF_MEDIA = 11_428
_PERF_COMPANIONS = 14_438
_SAQ_CHURN = 60_000
_SAQ_LIVE = 15


async def _explain_cost(session: AsyncSession, stmt: Any) -> float:
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    # `sql` is compiled from this module's own fixed statements (literal binds, no external input).
    plan = (await session.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"))).scalar_one()
    return float(plan[0]["Plan"]["Total Cost"])


def _orphan_split(clause: ColumnElement[bool]) -> Any:
    """The exact ``_safe_orphan_split`` statement shape (services/pipeline/buckets.py)."""
    return select(func.count()).select_from(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).where(clause)


async def test_orphan_split_stays_below_the_jit_inline_cliff_under_a_bloated_saq_jobs(session: AsyncSession) -> None:
    """MUTATION-CHECK: under prod-shaped bloat the old spelling crosses the cliff and the rewrite does not."""
    await _saq_table(session)
    # Planner settings as host-prod's configuration file sets them (read 2026-09-25).
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    await session.execute(text("SET LOCAL work_mem = '64MB'"))
    await session.execute(
        text(
            "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
            "SELECT gen_random_uuid(), md5(g::text) || md5((g + 1)::text), '/test/perf/' || g, 'f', '/test/perf/' || g, "
            "CASE WHEN g <= :media THEN 'mp3' ELSE 'txt' END, 1024, 'test-fileserver' FROM generate_series(1, :n) g"
        ),
        {"media": _PERF_MEDIA, "n": _PERF_MEDIA + _PERF_COMPANIONS},
    )
    await session.execute(
        text("INSERT INTO saq_jobs (key, status) SELECT 'process_file:' || gen_random_uuid(), 'complete' FROM generate_series(1, :n)"),
        {"n": _SAQ_CHURN},
    )
    await session.execute(text("DELETE FROM saq_jobs"))
    await session.execute(
        text("INSERT INTO saq_jobs (key, status) SELECT 'process_file:' || gen_random_uuid(), 'queued' FROM generate_series(1, :n)"),
        {"n": _SAQ_LIVE},
    )
    for table in ("files", "saq_jobs", "cloud_job", "analysis", "stage_skip", "scheduling_ledger", "metadata"):
        await session.execute(text(f"ANALYZE {table}"))
    cliff = float((await session.execute(text("SHOW jit_inline_above_cost"))).scalar_one())

    for stage in _STAGES:
        new_cost = await _explain_cost(session, _orphan_split(orphaned_clause(stage)))
        assert new_cost < cliff, f"orphan split ({stage.value}) plans at {new_cost:.0f}, past jit_inline_above_cost {cliff:.0f}"

    # Control: the pre-rewrite analyze spelling DOES cross it here, so the fixture reproduces the hazard
    # and the assertion above is not vacuous.
    old_cost = await _explain_cost(session, _orphan_split(_old_orphaned(Stage.ANALYZE)))
    assert old_cost >= cliff, f"fixture no longer reproduces the bloat hazard: old spelling plans at {old_cost:.0f} < {cliff:.0f}"
