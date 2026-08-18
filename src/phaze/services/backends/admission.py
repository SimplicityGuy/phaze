"""The single ``cloud_job.status='awaiting'`` writer + the parked ``push_file`` enqueue machinery.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df). Two closely
related concerns live here, both of which are *admission* into (or spill back out of) the bounded
cloud window rather than per-backend dispatch mechanics:

* :func:`hold_awaiting_cloud` -- the SINGLE go-forward writer of ``cloud_job.status='awaiting'``
  (D-01/D-02) and, since phaze-2mwyo, the one seam where a spent cloud budget becomes durable
  (:func:`_fold_spent_budget_if_edge`).
* the phaze-s5sz parked-enqueue trio (:func:`_park_push_file_enqueue` /
  :func:`drop_pending_push_file_enqueues` / :func:`flush_pending_push_file_enqueues`) plus the pure
  payload builder :func:`_build_push_file_enqueue_kwargs`, which keep a ``push_file`` enqueue OUT of
  the transaction that writes its ``cloud_job`` row.

Both are consumed by :mod:`~phaze.services.backends.compute_agent` and
:mod:`~phaze.services.backends.kueue`, and by the drain / the agent callback routers directly.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, NamedTuple, cast
import uuid

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services.cloud_budget import record_cloud_budget_spent
from phaze.tasks.push import PUSH_FILE_SAQ_RETRIES, push_file_saq_timeout_sec
from phaze.tasks.release_awaiting_cloud import push_file_job_key


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)


async def hold_awaiting_cloud(
    session: AsyncSession,
    file: FileRecord,
    *,
    attempts: int = 0,
    expect_status: Sequence[str] | None = None,
    expect_upload_id: str | None = None,
    clear_cloud_phase: bool = False,
) -> bool:
    """The SINGLE go-forward writer of ``cloud_job.status='awaiting'`` (D-01/D-02). NEVER commits.

    Shared by the hold path (``trigger_analysis``) and both over-cap spill paths
    (``report_upload_failed`` / ``report_push_mismatch``) so the hard shadow invariant
    ``AWAITING_CLOUD => cloud_job(status='awaiting')`` (verified pre-Phase-90 by the now-retired
    ``shadow_compare.py`` migration-verification script) holds for every go-forward hold instead of
    three hand-copied writers. ``expect_status`` selects one of two modes:

    * **Hold mode** (``expect_status is None``): the unconditional upsert. Phase 90 (D-09) removed the
      former AWAITING_CLOUD files.state dual-write, so this upserts ONLY the sidecar row keyed on ``file_id``
      (``uq_cloud_job_file_id``) INSERTing ``status='awaiting'`` / ``attempts=0`` (or ``on_conflict``
      re-stamping an existing row). Always returns ``True`` (the hold always writes).
    * **Spill mode** (``expect_status`` a non-empty status set): a rowcount-guarded CAS ONLY. UPDATEs the
      ``cloud_job`` row back to ``status='awaiting'`` iff its CURRENT status is in ``expect_status``,
      taking ``attempts`` from the argument so the spill caller retains
      ``attempts=cloud_submit_max_attempts`` as the budget-spent marker ``select_backend`` reads to route
      to local (D-03), and clearing ``cloud_phase`` iff ``clear_cloud_phase`` (the s3 spill sets it, the
      push spill must NOT touch it -- D-12). Returns ``res.rowcount > 0``: a ``False`` return means a
      late/duplicate callback matched an already-advanced row (0 rows), and the CALLER keeps its FULL
      no-op (no FileRecord write, no cleanup, no ledger clear -- D-10). This mode does NOT write
      ``file.state`` and does NOT touch the FileRecord: the caller owns the gated dual-write behind the
      returned bool. ``expect_upload_id``, when given, is ADDED to the CAS predicate
      (``CloudJob.upload_id == expect_upload_id``) -- phaze-wnp51, mirroring ``report_uploaded``'s
      phaze-p8h3 CAS. ``status`` alone is not a generation identifier: a row CAN legitimately
      RE-ENTER ``expect_status`` (``cloud_staging.redrive_upload`` re-stages a failed upload back into
      ``'uploading'`` with a FRESH ``upload_id``), so a caller whose observed status was read before a
      concurrent re-drive can otherwise win its CAS against a generation it never actually observed.
      Pinning ``upload_id`` too makes a re-drive's fresh generation fail the CAS -- a clean no-op --
      instead of silently spilling live work out from under its running job.

    ``'awaiting'`` is deliberately OUT of :data:`IN_FLIGHT`, so a held/re-stamped row never inflates any
    backend's ``in_flight_count`` (D-03). NEVER commits in EITHER mode -- the caller owns the commit
    boundary (the dispatch discipline at :meth:`Backend.dispatch`; a commit here would drop the tick's
    ``pg_advisory_xact_lock`` and re-open the over-stage class, Landmine L1).

    phaze-2mwyo -- THIS IS ALSO WHERE A SPENT BUDGET BECOMES DURABLE. Every caller that declares a file's
    cloud budget spent does it by calling this function with ``attempts >= cloud_submit_max_attempts``
    (the four spill sites plus :meth:`KueueBackend._reap_stranded_staging`, whose ``min(attempts + 1,
    cap)`` reaches the cap on its last reap). That made this the one seam where "a cloud chain has ended
    at its ceiling" is knowable, so the fold into the durable ``cloud_budget`` ledger
    (``services/cloud_budget.record_cloud_budget_spent``) happens here rather than being hand-copied into
    five callers -- the same single-writer argument that produced this function in the first place. It
    matters because ``routers/agent_analysis``'s D-14 reaper DELETES the row being written here as soon
    as the file reaches an analyze terminal, taking ``attempts`` with it; without the fold the next
    re-analysis starts a brand-new chain at 0 (phaze-wcrb's 8 pods = two chains of four).

    FOLD-ONCE is an EDGE trigger on the row ENTERING the terminal budget-spent state -- see
    :func:`_fold_spent_budget_if_edge` for the exact predicate and why "attempts crossed the cap" is
    NOT it (on the reconcile at-ceiling path ``attempts`` already equals the cap by the time the chain
    ends, because the last under-cap re-drive set it).
    """
    cap = cast("ControlSettings", get_settings()).cloud_submit_max_attempts
    # Pre-read ONLY on the terminal path (``attempts >= cap``), so the hot ``attempts=0`` hold path pays
    # nothing. Two things are needed and neither survives the write: whether the row was ALREADY parked
    # in the terminal budget-spent state (the edge trigger), and the chain's ``node_loss_redrives``
    # tally, which the fold accumulates SEPARATELY from ``attempts`` so the two causes stay legible on
    # the durable record exactly as phaze-1q4g kept them legible on the row.
    prior: _PriorChain | None = None
    if attempts >= cap:
        row = (
            await session.execute(select(CloudJob.status, CloudJob.attempts, CloudJob.node_loss_redrives).where(CloudJob.file_id == file.id))
        ).first()
        prior = None if row is None else _PriorChain(str(row[0]), int(row[1]), int(row[2]))

    if expect_status is None:
        # Hold mode: the unconditional cloud_job upsert; always writes -> return True.
        # Phase 90 (D-09): the AWAITING_CLOUD files.state dual-write was removed; the cloud_job row
        # (status='awaiting') is the sole derived authority PR-A reads.
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly (CR-01 defensive; mirrors ComputeAgentBackend.dispatch).
            id=uuid.uuid4(),
            file_id=file.id,
            status=CloudJobStatus.AWAITING.value,
            attempts=attempts,
        )
        stmt = stmt.on_conflict_do_update(
            # uq_cloud_job_file_id -> a plain INSERT is unsafe on the spill re-stamp case; upsert on file_id.
            index_elements=["file_id"],
            # phaze-ekgk: bump updated_at on the DO UPDATE branch. CloudJob.updated_at is a client-side
            # ``onupdate=func.now()`` (TimestampMixin) that SQLAlchemy does NOT inject into an ON CONFLICT SET
            # (and there is no DB trigger), so re-holding a pre-existing row would leave updated_at frozen at
            # the PREVIOUS burst's write. That column is surfaced as ``lane_entered_at`` (pipeline.get_cloud_
            # staging_candidates), and ``select_backend`` reads it for the D-01/D-03 local-spill staleness gate
            # (``waited = now - lane_entered_at >= cloud_spill_to_local_after_seconds``). A stale clock makes
            # ``waited`` immediately True, so a re-held file bypasses the wait window and spills to local while
            # cloud lanes are merely momentarily full. Stamp it so a fresh hold restarts the lane-entry clock.
            set_={"status": stmt.excluded.status, "attempts": stmt.excluded.attempts, "updated_at": func.now()},
        )
        await session.execute(stmt)
        await _fold_spent_budget_if_edge(session, file.id, attempts=attempts, cap=cap, prior=prior)
        return True

    # Spill mode: rowcount-guarded CAS ONLY. Preserve the shipped D-09/D-10 guard -- an unconditional
    # upsert here would clobber an already-advanced row back to AWAITING_CLOUD (T-83-01/T-83-PUSH-CLOBBER).
    # Build the values so ``cloud_phase`` is ABSENT unless the caller asked to clear it (D-12): the s3 spill
    # clears it (WR-01, off the "Running" tile), the push spill must NOT touch it.
    values: dict[str, Any] = {"status": CloudJobStatus.AWAITING.value, "attempts": attempts}
    if clear_cloud_phase:
        values["cloud_phase"] = None
    conditions = [CloudJob.file_id == file.id, CloudJob.status.in_(expect_status)]
    if expect_upload_id is not None:
        # phaze-wnp51: pin the CAS to the observed generation too, not just status -- see the
        # ``expect_upload_id`` docstring paragraph above for why status alone is not enough here.
        conditions.append(CloudJob.upload_id == expect_upload_id)
    res = cast(
        "CursorResult[Any]",
        await session.execute(update(CloudJob).where(*conditions).values(**values)),
    )
    wrote = res.rowcount > 0
    if wrote:
        # Only a CAS that actually MOVED the row ends a chain. A 0-row late/duplicate callback is a full
        # no-op here too -- folding on it would double-count a chain some earlier writer already recorded.
        await _fold_spent_budget_if_edge(session, file.id, attempts=attempts, cap=cap, prior=prior)
    return wrote


class _PriorChain(NamedTuple):
    """The ``cloud_job`` row as it stood BEFORE an at-cap :func:`hold_awaiting_cloud` write.

    Read once, only on the terminal path, because none of it survives the write: the status is
    overwritten to ``'awaiting'`` and ``attempts`` to the cap. ``node_loss_redrives`` is untouched by
    that write, so reading it before is merely the cheapest place to get it.
    """

    status: str
    attempts: int
    node_loss_redrives: int


async def _fold_spent_budget_if_edge(session: AsyncSession, file_id: uuid.UUID, *, attempts: int, cap: int, prior: _PriorChain | None) -> None:
    """Fold a just-ended cloud chain into the durable ``cloud_budget`` ledger, iff this write ended it (phaze-2mwyo).

    THE EDGE is the row ENTERING the terminal budget-spent state -- ``status='awaiting'`` WITH the budget
    stamped spent -- from anywhere else. Two conditions, both load-bearing:

    1. this write declares the budget spent (``attempts >= cap``); and
    2. the row was not ALREADY parked there (``prior.status == 'awaiting' and prior.attempts >= cap``).

    "``attempts`` crossed the cap" is the obvious predicate and is WRONG. On the reconcile at-ceiling
    path ``attempts`` already equals the cap when the chain ends: the last under-cap re-drive set it and
    re-submitted, and the ceiling is only detected on the NEXT terminal (``attempts + 1 > cap``), which
    deliberately does not increment again. A crossing test would therefore skip the fold on the single
    most common way a chain dies -- the exact path phaze-wcrb's four files took.

    Condition 2 is what makes it exactly-once. In spill mode it is nearly free: no caller's
    ``expect_status`` contains ``'awaiting'``, so a late/duplicate CAS against an already-spilled row
    matches 0 rows and never reaches here. Stating it anyway covers hold mode (an unconditional upsert)
    and any future caller, so a second at-cap write can never charge the same chain twice and drag the
    file toward the lifetime ceilings for events that did not happen.

    ``prior is None`` (no ``cloud_job`` row -- e.g. the reaper already removed it) is NOT "already
    spent": there is no parked chain to double-count, so an at-cap write there is a genuine burnout.

    The node-loss tally comes from ``prior`` because the write does not touch ``node_loss_redrives``;
    with no prior row there were no node-loss re-drives to charge, so 0 is the truth, not a fallback.
    NEVER commits -- see :func:`hold_awaiting_cloud`.
    """
    if attempts < cap:
        return
    if prior is not None and prior.status == CloudJobStatus.AWAITING.value and prior.attempts >= cap:
        return
    await record_cloud_budget_spent(session, file_id, attempts=attempts, node_loss_redrives=prior.node_loss_redrives if prior is not None else 0)


def _build_push_file_enqueue_kwargs(
    file: FileRecord,
    agent_id: str,
    *,
    dest_host: str,
    dest_scratch_dir: str,
    dest_ssh_user: str | None,
) -> dict[str, Any]:
    """Build the ``queue.enqueue("push_file", **kwargs)`` kwargs -- PURE, no I/O (D-02/D-03 payload).

    Builds the four push-initiation ``PushFilePayload`` fields (the FileRecord's ``id`` /
    ``original_path`` / ``file_type`` plus the resolved fileserver ``agent_id``) AND stamps the
    Phase-73 per-file destination (``dest_host`` / ``dest_scratch_dir`` / ``dest_ssh_user``, D-02: the
    dispatch-side record-don't-rederive stamp), then serializes via ``model_dump(mode="json")`` so the
    UUID round-trips as a string under ``extra="forbid"``. Split out from the old ``_enqueue_push_file``
    (phaze-s5sz) so ``ComputeAgentBackend.dispatch`` can PARK these kwargs instead of firing the enqueue
    inline -- see :func:`_park_push_file_enqueue`.
    """
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=agent_id,
        dest_host=dest_host,
        dest_scratch_dir=dest_scratch_dir,
        dest_ssh_user=dest_ssh_user,
    )
    # WR-03: stamp an explicit SAQ job-net timeout strictly above the agent's asyncio outer guard so
    # a job-net cancellation can never fire before the guard reaps the rsync child. phaze-2qpn: scale
    # it with the file size (the guard is size-derived on the agent) so a healthy multi-GB push is not
    # cancelled by a fixed cap, and allow retries so a killed push resumes via rsync --partial.
    return {
        "key": push_file_job_key(file.id),
        "timeout": push_file_saq_timeout_sec(file.file_size),
        "retries": PUSH_FILE_SAQ_RETRIES,
        **payload.model_dump(mode="json"),
    }


# phaze-s5sz: the session.info key under which ComputeAgentBackend.dispatch PARKS its push_file
# enqueue until the drain has durably committed the cloud_job SUBMITTED row (mirrors
# cloud_staging._PENDING_ENQUEUE_KEY / phaze-grzo's identical fix for the kueue s3_upload leg).
# Enqueue-before-commit was the same dual-write ordering hole here: SAQ's PostgresQueue enqueues on
# its OWN psycopg pool and commits the job durably + immediately, independent of THIS asyncpg
# session, so a fast rsync push could POST /pushed and CAS the row (see report_pushed's
# ``status == 'submitted'`` guard) before the drain's single post-loop commit landed it -- the
# callback then saw the row still at its PREVIOUS committed status (typically 'awaiting'), matched
# 0 rows, and took the idempotent-no-op hold FOREVER (nothing else owns recovery for an in-flight
# cloud_job). Parking the enqueue removes it from the transaction entirely, so the worker-visible
# job can never precede the committed row it reads.
_PENDING_PUSH_FILE_ENQUEUE_KEY = "backends_pending_push_file_enqueues"


@dataclasses.dataclass(frozen=True)
class _PendingPushFileEnqueue:
    """One deferred ``push_file`` enqueue: the resolved queue + the enqueue kwargs, flushed post-commit."""

    queue: Any
    enqueue_kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)


def _park_push_file_enqueue(session: AsyncSession, pending: _PendingPushFileEnqueue) -> None:
    """Record a deferred ``push_file`` enqueue on the session, to be flushed AFTER the caller commits."""
    session.info.setdefault(_PENDING_PUSH_FILE_ENQUEUE_KEY, []).append(pending)


def drop_pending_push_file_enqueues(session: AsyncSession) -> None:
    """Discard any parked ``push_file`` enqueues WITHOUT firing them (phaze-s5sz).

    The caller MUST call this whenever the transaction that produced the parked enqueues is rolled
    back: firing an enqueue whose ``cloud_job`` upsert was rolled back is the ORPHANING half of the
    dual-write hole (a job runs against a row that never committed). Mirrors
    ``cloud_staging.drop_pending_s3_enqueues``.
    """
    session.info.pop(_PENDING_PUSH_FILE_ENQUEUE_KEY, None)


async def flush_pending_push_file_enqueues(session: AsyncSession) -> int:
    """Fire every ``push_file`` enqueue parked on ``session`` and return the count fired (phaze-s5sz).

    MUST be called ONLY after the caller has committed the ``cloud_job`` SUBMITTED row(s) the parked
    jobs depend on, so the worker-visible side effect can never precede its committed row. Best-effort
    per item: an enqueue failure leaves that file's row committed-but-SUBMITTED, a row
    :meth:`ComputeAgentBackend._reap_stranded_submitted` (phaze-j7m18) now reaps once
    ``cloud_submitted_stale_after_sec`` elapses with no live ``push_file:<file_id>`` broker key -- and
    must not block the remaining enqueues. The list is popped up front so a partial flush never
    double-fires. Mirrors ``cloud_staging.flush_pending_s3_enqueues``.
    """
    pending: list[_PendingPushFileEnqueue] = session.info.pop(_PENDING_PUSH_FILE_ENQUEUE_KEY, [])
    fired = 0
    for item in pending:
        try:
            # Phase 36: the PostgresQueue broker pool is built open=False; connect() is idempotent.
            await item.queue.connect()
            job = await item.queue.enqueue("push_file", **item.enqueue_kwargs)
            if job is None:
                # A deterministic-key dedup against a still-incomplete push_file:<file_id> job -- the
                # file is already being pushed. Benign: the prior job's own callback owns the row.
                logger.warning(
                    "flush_pending_push_file_enqueues: push_file enqueue deduped against a still-incomplete job",
                    key=item.enqueue_kwargs.get("key"),
                )
            else:
                fired += 1
        except Exception:
            # A parked enqueue that fails leaves the committed SUBMITTED row for a future compute-lane
            # recovery mechanism; never let one failed enqueue abort the rest of the flush.
            logger.warning(
                "flush_pending_push_file_enqueues: parked push_file enqueue failed",
                key=item.enqueue_kwargs.get("key"),
                exc_info=True,
            )
    return fired
