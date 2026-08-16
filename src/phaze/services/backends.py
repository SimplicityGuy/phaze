"""The internal ``Backend`` protocol + its three re-homed implementations (Phase 68, BACK-01/03).

This is the phase's center of gravity. It houses one ``typing.Protocol`` (design §4.2 shape --
``is_available`` / ``in_flight_count`` / ``dispatch`` / ``reconcile``) and the three implementations
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` that **re-home** the existing staging /
push / submit / reconcile bodies verbatim -- this is a behavior-preserving refactor, NOT a rewrite.

Phase 68 was a **lay-and-prove** phase (D-02): it defined the protocol + the uniform per-backend
``in_flight_count`` substrate and proved its equivalence to the (now Phase-69-retired) global
FileState ``{PUSHING, PUSHED}`` window for the single-backend case. Phase 69 (SCHED-02) then flipped
the drain (``release_awaiting_cloud.stage_cloud_window``) onto these per-backend caps: it snapshots
each backend's ``in_flight_count`` once per tick and enforces the per-backend ``cap``. The protocol
methods are per-backend and unit-tested as such.

Decisions realized here:

* **D-01a** -- the GATE-1 asymmetry lives in per-kind ``is_available``: compute REQUIRES a live compute
  agent; Kueue deliberately probes the cluster with NO compute-agent dependency; local is always up.
* **D-02** -- ``in_flight_count`` is the uniform ``cloud_job``-derived per-backend count; the equivalence
  invariant is the characterization proof that the new substrate matches the old count.
* **D-03** -- ``dispatch`` owns BOTH the ``FileState -> PUSHING`` flip AND the ``cloud_job`` upsert in the
  SAME caller-passed session, before/with the flip, NEVER after a separate commit (Pitfall 4 limbo guard).
* **D-05** -- ``KueueBackend`` calls ``_stage_file_to_s3`` / ``kube_staging`` threaded THIS backend's
  own ``KubeConfig`` + D-06 bucket (Phase 70 MKUE-01/02 retired the ``active_kube`` / ``active_bucket``
  module-global reads: one control plane dispatches to N distinct clusters/buckets).
* **D-07** -- the raise-on-``>1``-non-local guard is Phase-69-retired from :func:`resolve_backends` (N
  non-local backends now resolve; SCHED-01). Phase 70 (MKUE-01) further generalizes
  :func:`resolved_non_local_kind` to return ``"kueue"`` for ANY-kueue registry (N Kueue backends are the
  literal MKUE-01 scenario), retaining the fail-fast only for the ambiguous compute-only ``>1`` case;
  ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this module logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast
import uuid

from sqlalchemy import CursorResult, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services import kube_staging, s3_staging
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.cloud_budget import record_cloud_budget_spent
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.enqueue_router import LANES, NoActiveAgentError, lane_for_task, select_active_agent, select_agent_by_id
from phaze.services.pipeline import MUSIC_VIDEO_TYPES, _cloud_window_clauses, _safe_bucket_counts, get_live_job_keys
from phaze.services.route_control import get_route_control
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.services.stage_status import inflight_clause
from phaze.tasks.push import PUSH_FILE_SAQ_RETRIES, push_file_saq_timeout_sec
from phaze.tasks.reconcile_cloud_jobs import _reconcile_one
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY, push_file_job_key


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from phaze.config import ControlSettings
    from phaze.config_backends import BackendConfig, ComputeBackend, KubeConfig
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


# D-10 (Q3): the exact non-terminal / in-flight CloudJobStatus set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned against the live CloudJobStatus members in models/cloud_job.py.
IN_FLIGHT: tuple[CloudJobStatus, ...] = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)

# phaze-ul2v: the PRE-SUBMIT half of :data:`IN_FLIGHT`. These two statuses consume a lane cap slot
# exactly like {SUBMITTED, RUNNING}, but nothing in the reconcile cron selected them -- they are
# terminalized ONLY by the agent HTTP callbacks (``/uploaded``, ``/failed``). A dead agent or a lost
# ``s3_upload`` SAQ job strands the row here forever. :meth:`KueueBackend._reap_stranded_staging` is
# the age-bounded safety net that covers exactly this set.
STAGING: tuple[CloudJobStatus, ...] = (CloudJobStatus.UPLOADING, CloudJobStatus.UPLOADED)


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


class Backend(Protocol):
    """The single internal dispatch seam that removes the ``if kind == …`` cloud-target fork (§4.2).

    Structural (``typing.Protocol``): the three impls below conform by shape, no explicit subclassing.
    ``id`` / ``rank`` / ``cap`` mirror the Phase-67 registry submodel fields (cost-tier rank, concurrency
    cap); the four async methods are the per-backend dispatch lifecycle.
    """

    id: str
    rank: int
    cap: int

    async def is_available(self, session: AsyncSession) -> bool:
        """Whether this backend can accept a dispatch right now (compute: agent gate; kueue: cluster probe)."""
        ...

    async def in_flight_count(self, session: AsyncSession) -> int:
        """COUNT(cloud_job WHERE backend_id == self.id AND status IN {in-flight}) -- the D-02 substrate."""
        ...

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` into the cloud window + write its ``cloud_job`` row, IN the caller's txn (D-03). Never commits.

        Returns ``True`` when new dispatch work was actually enqueued (a genuine stage) and ``False``
        when the enqueue was a deterministic-key dedup no-op / a clean hold -- the drain counts the
        former as ``staged`` and the latter as ``skipped`` (preserves the Phase-50 tally semantics).
        """
        ...

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:
        """Advance this backend's in-flight ``cloud_job`` rows toward terminal (kueue: cron read; local/compute: no-op).

        Returns a per-backend outcome ``tally`` dict for the cron to aggregate (kueue), or ``None`` for
        the callback-driven no-op backends (local/compute) that own no cron read.
        """
        ...


class _BaseBackend:
    """Shared ``id`` / ``rank`` / ``cap`` carrier + the uniform ``cloud_job``-derived ``in_flight_count``.

    Each concrete backend binds to a single Phase-67 registry entry (``config``). The shared
    ``in_flight_count`` is the D-02/D-10 substrate: a pure DB COUNT filtered by ``backend_id`` + the
    in-flight status set (the per-backend replacement for the Phase-69-retired global window count).
    """

    def __init__(self, *, id: str, rank: int, cap: int, config: BackendConfig | None = None) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.config = config

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return COUNT(cloud_job WHERE backend_id == self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})."""
        return int(
            (
                await session.execute(
                    select(func.count(CloudJob.id)).where(
                        CloudJob.backend_id == self.id,
                        CloudJob.status.in_([status.value for status in IN_FLIGHT]),
                    )
                )
            ).scalar()
            or 0
        )


class LocalBackend(_BaseBackend):
    """On-prem/all-local backend -- analysis runs on the fileserver agent via ``process_file`` (no cloud_job).

    ``is_available`` is unconditionally True (local dispatch needs no remote cloud agent);
    ``in_flight_count`` is the REAL ledger-derived running count (phaze-xd8k, see below); ``reconcile``
    is a no-op (local completion is synchronous, no cron read). ``dispatch`` re-homes the ``process_file``
    local enqueue path (Phase-69 scheduler uses it; unit-tested here, NOT wired into the single-path drain).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; local needs no session probe
        """Always True -- local dispatch never depends on a remote cloud agent."""
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return the REAL local-lane running count (phaze-xd8k), not a hardcoded 0.

        A local burst writes NO ``cloud_job`` row, so :class:`_BaseBackend`'s ``cloud_job``-derived
        COUNT is structurally always 0 for this lane -- that hardcode was the observability bug: the
        ANALYZE header's ``analyzeActive`` (:func:`phaze.services.stage_status.inflight_clause` on
        ``Stage.ANALYZE``, the SAME scheduling-ledger ``process_file:<file_id>`` predicate the DAG
        derives ``analyzeActive`` from, D-01 authoritative source) counts EVERY in-flight ``process_file``
        job regardless of which agent it was routed to, while this lane rendered a literal 0 even when
        thousands of files were actively analyzing locally.

        The fix: count music/video files that are ``inflight_clause(ANALYZE)`` (a live
        ``process_file:<file_id>`` scheduling-ledger row) AND carry NO in-flight ``cloud_job`` row
        (D-10's ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}`` set). A cloud-routed file's ``process_file``
        is enqueued on ITS agent under the SAME deterministic ledger key
        (:func:`phaze.services.analysis_enqueue.process_file_job_key`), so the ledger key alone cannot
        distinguish "local" from "cloud" -- the ``~exists(cloud_job in-flight)`` exclusion is what carves
        out the local-only slice: a compute/kueue file either has no ``cloud_job`` row yet (still
        pre-stage) or still carries its in-flight ``cloud_job`` row while its ``process_file`` runs
        remotely (kueue's Job IS its ``process_file`` execution; compute's row stays SUBMITTED until the
        ``/pushed`` callback flips it, well past when ITS ledger row would appear). The one known gap
        (documented, not fixed here -- out of phaze-xd8k's scope): a COMPUTE file's ``cloud_job`` row is
        terminalized SUCCEEDED by ``report_pushed`` in the SAME transaction that enqueues its remote
        ``process_file`` (``routers/agent_push.py``), so for the brief window a compute agent is actually
        running analysis, this lane's real-count query cannot distinguish it from a genuinely local file
        and would over-count local by that amount. Harmless where no ``compute`` backend is configured
        (this bug's confirmed scenario: local + kueue only) and bounded by compute lane concurrency caps
        elsewhere.
        """
        stmt = (
            select(func.count(FileRecord.id))
            .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
            .where(inflight_clause(Stage.ANALYZE))
            .where(~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_([status.value for status in IN_FLIGHT]))))
        )
        return int((await session.execute(stmt)).scalar() or 0)

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` to LOCAL_ANALYZING then enqueue ``process_file`` on the fileserver queue -- one txn, no commit.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> ``False``),
        matching the cron no-op discipline -- never a raise.

        CR-01 (SCHED-01/03): AFTER the fileserver gate (so an absent agent leaves the file untouched) and
        BEFORE the enqueue, the file is enqueued for local analysis in the caller-passed session. Phase 90
        (D-09) removed the former LOCAL_ANALYZING files.state flip; the file leaves the cloud-staging
        candidate set via its ``process_file:<id>`` scheduling-ledger row (the derived inflight source), so a
        locally-spilled file is no longer a drain candidate and can NOT be double-dispatched to a cloud
        backend while its ``process_file`` is in flight (the Backend.dispatch contract: dispatch "removes
        the file from further drain consideration"). NEVER commits -- the drain owns the single post-loop
        commit under the advisory lock, so the flip+enqueue are atomic (a rollback leaves the file
        AWAITING_CLOUD, safe to re-try, never a limbo LOCAL_ANALYZING without a queued job).
        """
        cfg = cast("ControlSettings", get_settings())
        try:
            agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
            return False
        # Phase 90 (D-09): the LOCAL_ANALYZING files.state dual-write was removed. The file leaves the
        # AWAITING_CLOUD candidate set via the process_file:<id> scheduling-ledger row that
        # enqueue_process_file's before_enqueue hook writes (the derived inflight_clause source PR-A reads).
        queue = task_router.queue_for(agent.id, lane_for_task("process_file"))
        job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
        # WR-01: a deterministic-key ``process_file:<id>`` dedup returns None (the file is already being
        # analyzed locally) -> report NOT-newly-staged so the drain's staged tally is honest; a genuine
        # enqueue returns a saq.Job -> staged. Mirrors ComputeAgentBackend/KueueBackend's return contract.
        # The state flip above stands regardless of the dedup outcome (the file has left AWAITING_CLOUD).
        return job is not None

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002 -- protocol signature; local has no cron read
        """No-op: local analysis completion is synchronous -- there is no cron read to run."""
        return None


class ComputeAgentBackend(_BaseBackend):
    """Cloud-compute (rsync/push over Tailscale) backend -- re-homes the ``push_file`` control-side enqueue leg.

    ``is_available`` re-homes GATE-1 (``release_awaiting_cloud`` L145-150): True iff a compute agent is
    online. ``dispatch`` owns a NEW in-txn ``cloud_job`` write (Pitfall 1 / D-03) then PARKS the
    ``push_file`` enqueue (phaze-s5sz, :func:`_park_push_file_enqueue`). Compute terminalization is
    PRIMARILY the existing ``/pushed``/``/mismatch``/``/failed`` callback path (§4.2, D-08); ``reconcile``
    (phaze-j7m18) is the age-bounded safety net for a row those callbacks never reach --
    :meth:`_reap_stranded_submitted`, the compute twin of ``KueueBackend._reap_stranded_staging``.
    ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).

    Phase 72 (MCOMP-01/D-02): ``is_available`` resolves THIS backend's bound ``agent_ref``
    (``self._agent_ref()``) against ``Agent.id`` per-call -- the record-don't-rederive twin of
    ``KueueBackend._kube()`` -- replacing the retired ``select_active_agent(kind="compute")``
    single-active-compute pick. Each compute entry gates on ITS bound agent, not "the single active
    compute agent" (Phase 73 builds dispatch/push/reconcile on this per-agent binding).
    """

    def _agent_ref(self) -> str:
        """Return THIS backend's bound ``agent_ref`` (the Phase-67 compute entry's dispatch node, D-02).

        ``self.config`` is the ``ComputeBackend`` submodel bound in ``resolve_backends``; its
        ``agent_ref`` is the ``Agent.id`` this backend dispatches to. Fail-loud (``ValueError`` naming
        ``self.id``) if a compute backend somehow has no ``agent_ref`` bound -- the
        ``_require_dispatch_fields`` validator already guarantees it non-empty at construction, so this
        is defense-in-depth (mirrors ``KueueBackend._kube()``).
        """
        agent_ref = getattr(self.config, "agent_ref", None)
        if not agent_ref:
            raise ValueError(f"compute backend {self.id!r} has no agent_ref bound")
        return cast("str", agent_ref)

    def _destination(self) -> tuple[str, str, str | None]:
        """Return THIS backend's push destination ``(push_host, scratch_dir, ssh_user)`` (D-02).

        ``self.config`` is typed ``BackendConfig | None`` (the discriminated union), so DIRECT attribute
        access fails mypy on the union -- read via the union-safe ``getattr`` idiom (mirrors
        ``_agent_ref()`` / ``KueueBackend._kube()``). ``push_host`` and ``scratch_dir`` are guaranteed
        non-empty at construction by ``ComputeBackend._require_dispatch_fields``, so a missing value here
        is a bound-config invariant break -- fail loud (``ValueError`` naming ``self.id``) rather than
        silently stamping a ``"None:..."`` remote spec. ``ssh_user`` stays optional (``None`` allowed).
        """
        push_host = getattr(self.config, "push_host", None)
        scratch_dir = getattr(self.config, "scratch_dir", None)
        if not push_host or not scratch_dir:
            raise ValueError(f"compute backend {self.id!r} has no push_host/scratch_dir bound")
        ssh_user = getattr(self.config, "ssh_user", None)
        return cast("str", push_host), cast("str", scratch_dir), cast("str | None", ssh_user)

    async def is_available(self, session: AsyncSession) -> bool:
        """D-02: True iff THIS backend's bound ``agent_ref`` names an ONLINE compute agent; False when absent.

        Resolves the per-entry binding (``self._agent_ref()`` -> ``Agent.id``) via
        :func:`select_agent_by_id`, reading ``self.config.agent_ref`` per-call (record-don't-rederive).
        An absent / unregistered / offline bound agent degrades to a hold (``NoActiveAgentError`` ->
        ``False``), preserving the cron no-op discipline (T-68-05, D-05). A backend with no ``agent_ref``
        bound fails loud via ``_agent_ref()`` (defense-in-depth) rather than silently holding.
        """
        try:
            await select_agent_by_id(session, self._agent_ref(), kind="compute")
        except NoActiveAgentError:
            return False
        return True

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Upsert the ``cloud_job`` row, THEN PARK the ``push_file`` enqueue -- one txn, no commit.

        D-03 write ordering: the ``cloud_job`` upsert (``backend_id`` set, ``s3_key`` NULL -- compute
        carries no S3 object, ``status=SUBMITTED``) lands in the caller-passed session; the fileserver
        gate runs first so an absent agent is a clean hold with nothing mutated. NEVER commits -- the
        drain owns the single post-loop commit so the ``pg_advisory_xact_lock`` survives the tick
        (Landmine L1). Always returns ``True`` (a genuine stage) -- mirrors ``KueueBackend.dispatch``.

        phaze-s5sz (supersedes phaze-uciu.3): the ``push_file`` enqueue is PARKED
        (:func:`_park_push_file_enqueue`), not fired inline. SAQ's ``PostgresQueue`` enqueue commits the
        job durably + immediately on its OWN psycopg pool, independent of THIS asyncpg session's commit
        boundary -- an inline enqueue made the job (and a FAST rsync push's own ``/pushed`` callback)
        worker-visible BEFORE the drain's post-loop commit landed this SUBMITTED row. ``report_pushed``'s
        ONLY guard is ``cloud_job.status == 'submitted'`` (SC#1/D-12); under READ COMMITTED that fast
        callback saw the row's PREVIOUS committed status (typically 'awaiting'), matched 0 rows, and took
        the idempotent-no-op hold FOREVER -- at the time nothing else owned recovery for an in-flight
        cloud_job (``ComputeAgentBackend.reconcile`` was a documented no-op; ``recover_orphaned_work``
        excludes any file carrying an in-flight ``cloud_job`` row on the premise that the ``/pushed``
        callback owns it -- phaze-j7m18 later added ``reconcile``'s age-bounded
        :meth:`_reap_stranded_submitted` as the backstop for exactly this class of lost callback).
        Parking the enqueue removes it from the transaction entirely, so the drain fires it via
        ``flush_pending_push_file_enqueues`` ONLY AFTER the single post-loop commit -- the worker-visible
        job can never precede the committed row it reads. Because the enqueue is no longer in the
        transaction, the ``session.begin_nested()`` SAVEPOINT phaze-uciu.3 added to protect it is dead
        weight and is dropped here (mirrors the kueue twin, phaze-grzo).
        """
        # Gate on the fileserver agent (the push initiator) BEFORE mutating: absent -> clean hold, nothing written.
        fileserver_agent = await select_active_agent(session, kind="fileserver")

        # D-03: upsert the cloud_job row in the caller's session. Phase 90 (D-09): the paired PUSHING
        # files.state dual-write was removed; the cloud_job (status=SUBMITTED) is authority.
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly (CR-01 defensive; mirrors cloud_staging.py:109).
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=self.id,
            s3_key=None,  # compute has no S3 object -> s3_key nullable (D-08)
            status=CloudJobStatus.SUBMITTED.value,  # single compute in-flight status (D-10)
        )
        stmt = stmt.on_conflict_do_update(
            # id is OUT of set_: the PK is immutable, so a re-dispatch keeps the existing row's id.
            index_elements=["file_id"],
            set_={
                "backend_id": stmt.excluded.backend_id,
                "status": stmt.excluded.status,
                # phaze-7634: CloudJob.updated_at is a client-side ``onupdate=func.now()``
                # (TimestampMixin) that SQLAlchemy does NOT inject into an ON CONFLICT SET (and
                # there is no DB trigger), so a re-dispatch would otherwise leave updated_at
                # frozen at the row's PREVIOUS write (same defect class as the hold-mode upsert
                # above / phaze-c8nz). Stamp it explicitly so a re-dispatch bumps the clock.
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

        # D-02: stamp THIS backend's destination onto the push payload (record-don't-rederive
        # originates at dispatch; NO re-lookup via resolve_compute_backend here -- the bound
        # self.config already holds it). PARK the enqueue rather than firing it (phaze-s5sz).
        push_host, scratch_dir, ssh_user = self._destination()
        push_queue = task_router.queue_for(fileserver_agent.id, lane_for_task("push_file"))
        enqueue_kwargs = _build_push_file_enqueue_kwargs(
            file,
            fileserver_agent.id,
            dest_host=push_host,
            dest_scratch_dir=scratch_dir,
            dest_ssh_user=ssh_user,
        )
        _park_push_file_enqueue(session, _PendingPushFileEnqueue(queue=push_queue, enqueue_kwargs=enqueue_kwargs))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int]:  # noqa: ARG002 -- protocol signature; the cloud_job read itself is compute's only cron work
        """Reap THIS backend's age-stranded SUBMITTED rows (phaze-j7m18); compute has no Job/Workload to reconcile.

        Compute's ONLY in-flight status is SUBMITTED (D-08, D-10) and it is terminalized SOLELY by the
        ``/pushed``/``/mismatch``/``/failed`` agent HTTP callbacks -- there is no Kueue Job/Workload for
        this cron to read against, unlike :meth:`KueueBackend.reconcile`. That made this method a
        documented no-op, on the premise the callback path always eventually fires. It does not: a dead
        fileserver agent host mid-rsync, or an enqueue failure in
        :func:`flush_pending_push_file_enqueues` (its own docstring names the gap), leaves the row
        SUBMITTED forever with no callback ever coming -- permanently consuming a lane cap slot, the
        same "N/N busy with zero real workloads" failure :meth:`KueueBackend._reap_stranded_staging`
        (phaze-ul2v) fixed for the staging half. :meth:`_reap_stranded_submitted` is that mechanism's
        compute twin.
        """
        tally = {"submitted_reaped": 0}
        await self._reap_stranded_submitted(session, tally)
        return tally

    async def _reap_stranded_submitted(self, session: AsyncSession, tally: dict[str, int]) -> None:
        """Spill THIS backend's age-stranded SUBMITTED ``cloud_job`` rows back to awaiting (phaze-j7m18).

        Mirrors :meth:`KueueBackend._reap_stranded_staging` exactly, narrowed to compute's single
        in-flight status and its owning callback:

        0. **The broker-liveness gate, checked FIRST.** Skip any row whose ``push_file:<file_id>``
           broker key is still ``queued``/``active`` in ``saq_jobs`` (:func:`get_live_job_keys`, the
           same probe recovery uses). A live key means the ``push_file`` SAQ job (and its eventual
           ``/pushed``/``/mismatch``/``/failed`` callback) still owns the row regardless of age -- a
           multi-GB rsync over a slow link legitimately runs for hours and bumps no timestamp while it
           transfers, so age alone cannot distinguish a live push from a lost one.
        1. **The age bound** (:attr:`ControlSettings.cloud_submitted_stale_after_sec`). A row is a
           candidate only once ``now - updated_at`` exceeds it. The coarse backstop for when even the
           broker row is gone (a lost/swept job, or an enqueue that never landed one at all).
        2. **The CAS.** The spill goes through the single awaiting writer (:func:`hold_awaiting_cloud`)
           in SPILL mode with ``expect_status=('submitted',)``. A callback that lands between our read
           and our update advances the row out of SUBMITTED, the CAS matches 0 rows, and the reaper
           takes a FULL no-op -- the happy path always wins the race, by construction.

        ``clear_cloud_phase`` stays ``False``: ``cloud_phase`` is a Kueue-only field (queued_behind_quota
        / admitted / running / finished, D-05) that ``ComputeAgentBackend.dispatch`` never sets, so there
        is nothing to clear (mirrors the push-spill branches in ``routers/agent_push.py``, which also
        leave it untouched). No S3 cleanup either -- compute's ``cloud_job`` carries no S3 object
        (``s3_key`` is NULL, D-08); the only durable side effect to undo is the ``push_file:<file_id>``
        scheduling-ledger row, cleared exactly like ``report_push_failed``'s spill.

        Re-drive is bounded exactly like the staging reaper: each reap increments ``cloud_job.attempts``
        (capped at ``cloud_submit_max_attempts``), so a file that strands repeatedly reaches a spent
        budget and ``select_backend`` routes it to local instead of re-stranding on compute.

        Per-row discipline mirrors :meth:`KueueBackend._reap_stranded_staging`: the drain's
        ``pg_advisory_xact_lock`` is taken at the top of each row's unit of work, each row commits on its
        own, and a per-row ``except`` rolls back so one bad row never aborts the tick.
        """
        cfg = cast("ControlSettings", get_settings())
        now = datetime.now(UTC)
        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status == CloudJobStatus.SUBMITTED.value,
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids only: the per-row rollback expires the ORM identity map, so every row is
        # re-read fresh INSIDE the loop against the fresh state (mirrors the staging reaper).
        cloud_job_ids = [row.id for row in rows]

        # phaze-j7m18 (mirrors phaze-31q3): snapshot the live-broker key set ONCE per sweep (degrade-safe
        # -- an empty set on any read failure falls the reaper back to age-only, never raising).
        live_keys = await get_live_job_keys(session)

        for cloud_job_id in cloud_job_ids:
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                # phaze-7lpb discipline: force a real re-read under the lock rather than the sweep-start
                # identity-mapped object (the sessionmaker is expire_on_commit=False).
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None or cloud_job.status != CloudJobStatus.SUBMITTED.value:
                    # A callback terminalized/advanced it since the snapshot -- nothing to reap.
                    await session.rollback()
                    continue
                file_id = cloud_job.file_id
                live_key = f"push_file:{file_id}"
                if live_key in live_keys:
                    # A queued/active push_file job (and its eventual callback) still owns this row.
                    await session.rollback()
                    continue
                # ``updated_at`` is TIMESTAMP WITHOUT TIME ZONE, so asyncpg hands it back NAIVE in
                # production; assume-UTC before subtracting (mirrors the staging reaper's coercion).
                ref = cloud_job.updated_at or cloud_job.created_at
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=UTC)
                age_sec = (now - ref).total_seconds()
                if age_sec < cfg.cloud_submitted_stale_after_sec:
                    # YOUNGER THAN THE BOUND: the callback owns this row. Never fire here.
                    await session.rollback()
                    continue
                # Bounded re-drive: each reap spends one attempt; at the cap select_backend routes local.
                attempts = min(cloud_job.attempts + 1, cfg.cloud_submit_max_attempts)
                file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
                spilled = file is not None and await hold_awaiting_cloud(
                    session,
                    file,
                    attempts=attempts,
                    expect_status=(CloudJobStatus.SUBMITTED.value,),
                )
                if not spilled:
                    # Lost the race to a live callback (or the FK file vanished): FULL no-op.
                    await session.rollback()
                    continue
                await clear_ledger_entry(session, f"push_file:{file_id}")
                await session.commit()
                tally["submitted_reaped"] += 1
                logger.warning(
                    "ComputeAgentBackend.reconcile: stranded SUBMITTED cloud_job reaped -> spilled back to awaiting (lost agent callback)",
                    cloud_job_id=str(cloud_job_id),
                    file_id=str(file_id),
                    backend_id=self.id,
                    age_sec=int(age_sec),
                    bound_sec=cfg.cloud_submitted_stale_after_sec,
                    attempts=attempts,
                )
            except Exception:
                await session.rollback()
                logger.warning(
                    "ComputeAgentBackend.reconcile: stranded SUBMITTED reap failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True
                )


class KueueBackend(_BaseBackend):
    """Kueue-cluster backend -- re-homes today's single-cluster S3-staging + kube submit/reconcile (D-05).

    ``is_available`` probes THIS backend's Kueue LocalQueue with NO compute-agent dependency (D-01a);
    ``dispatch`` picks the D-06 bucket and runs the no-commit S3-staging core; ``reconcile`` re-homes the
    ``reconcile_cloud_jobs`` cron body, made ``backend_id``-aware, under a per-row advisory lock.
    ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).

    Phase 70 (MKUE-01/D-04): every ``kube_staging`` call is threaded THIS backend's own
    ``KubeConfig`` (``self._kube()``) -- one control plane dispatches to N distinct clusters, each with
    its own constructor-time-authed kr8s client (the module-global ``active_kube`` read is retired).
    """

    def _kube(self) -> KubeConfig:
        """Return THIS backend's ``KubeConfig`` (the bound kueue registry entry's ``[kube]`` table, D-04).

        ``self.config`` is the Phase-67 ``KueueBackend`` submodel bound in ``resolve_backends``; its
        ``kube`` field is the per-cluster connection surface every ``kube_staging`` verb now takes.
        Fail-loud (``KubeStagingError``) if a kueue backend somehow has no ``[kube]`` bound -- the
        config validator already guards this, so this is defense-in-depth.
        """
        kube = getattr(self.config, "kube", None)
        if kube is None:
            raise kube_staging.KubeStagingError(f"kueue backend {self.id!r} has no [kube] config bound")
        return cast("KubeConfig", kube)

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; kueue probes the cluster, not a DB agent
        """Probe THIS backend's Kueue LocalQueue -- True iff reachable; False (never raises) on any probe failure (D-01a).

        Re-homes the ``kube_staging.get_local_queue`` reachability probe, now threaded THIS backend's
        ``KubeConfig`` (MKUE-01/03). Deliberately has NO compute-agent dependency (D-01a asymmetry):
        ephemeral Kueue pods have no persistent compute agent. A ``NotFoundError`` (mis-named queue) or
        transient ``ServerError`` (or an unconfigured ``[kube]``) degrades to False rather than raising
        (mirrors the controller's non-fatal catch), preserving the cron no-op discipline.
        """
        try:
            local_queue = await kube_staging.get_local_queue(self._kube())
        except Exception:  # any kube/mesh failure degrades to "unavailable" (T-68-05 no-op discipline)
            logger.info("KueueBackend.is_available: LocalQueue probe failed -> unavailable", backend_id=self.id)
            return False
        return local_queue is not None

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Pick the D-06 bucket, run the no-commit S3-staging core, THEN flip ``file`` to PUSHING (MKUE-02).

        Phase 70 (MKUE-02/D-06): pick the file's staging bucket deterministically over this backend's
        bound bucket set (``self.config.buckets``), resolve its ``BucketConfig``, thread it into the
        shared ``_stage_file_to_s3`` core (which stamps ``staging_bucket`` on the upsert), and RECORD both
        ``backend_id`` AND ``staging_bucket`` in the SAME uncommitted session so this backend's
        ``in_flight_count`` (COUNT WHERE backend_id == self.id) counts the row and every downstream
        presign/cleanup READS the recorded bucket (never re-derives). NEVER commits (the drain owns the
        single post-loop commit -- Landmine L1). Always a genuine stage on the kueue path, so returns ``True``.

        CR-01 (gate-before-mutate, Pitfall 4 limbo guard): the ``FileState -> PUSHING`` flip lands ONLY
        AFTER ``_stage_file_to_s3`` returns successfully -- NOT before it, as ``LocalBackend`` /
        ``ComputeAgentBackend`` also gate their fileserver-agent check before any state mutation.
        ``_stage_file_to_s3`` resolves the fileserver agent FIRST (``select_active_agent(kind="fileserver")``)
        and reads NOTHING from ``file.state``, so a ``NoActiveAgentError`` (or any pre-upsert S3 raise)
        leaves ``file`` completely untouched. Were the flip to precede the call, SQLAlchemy's default
        ``autoflush`` would flush the pending PUSHING change as a side effect of that gate's ``SELECT``,
        and the drain's single post-loop commit would then persist a PUSHING file with no ``cloud_job``
        row -- the exact "limbo row" this ordering forbids.

        phaze-grzo (supersedes phaze-uciu.3): ``_stage_file_to_s3`` no longer fires the ``s3_upload``
        enqueue inline -- it PARKS it on the session and the drain fires it via
        ``flush_pending_s3_enqueues`` AFTER the single post-loop commit. SAQ's ``PostgresQueue`` commits
        the job on its OWN psycopg pool immediately, so an inline enqueue made the job (and its
        ``report_uploaded`` callback) worker-visible BEFORE this asyncpg session committed the UPLOADING
        row -- a fast callback then saw no UPLOADING row and no-op'd, stranding the file. Parking the
        enqueue removes it from the transaction entirely, so this ``dispatch`` no longer raises on an
        enqueue failure (there is no SAVEPOINT to roll back); the drain drops parked enqueues on a tick
        rollback and a post-commit flush failure leaves the committed UPLOADING row for the age-bounded
        ``_reap_stranded_staging`` reaper (phaze-ul2v).
        """
        cfg = cast("ControlSettings", get_settings())
        # D-06: deterministic per-file bucket over this backend's bound set; the returned id is authoritative.
        # Pure/no-DB: pick + resolve mutate nothing, so a resolution failure here is also mutation-free.
        bucket_ids = list(getattr(self.config, "buckets", []) or [])
        bucket_id = s3_staging.pick_bucket(file.id, bucket_ids)
        bucket = s3_staging.resolve_bucket_config(cfg, bucket_id)
        if bucket is None:
            raise s3_staging.S3StagingError(f"kueue backend {self.id!r} bucket {bucket_id!r} is not in the resolved registry")
        # Gate (fileserver agent) + stage BEFORE the state flip: _stage_file_to_s3 reads no file.state, so a
        # NoActiveAgentError / pre-upsert S3 raise touches nothing (CR-01 Pitfall 4 limbo guard).
        await _stage_file_to_s3(session, file, task_router, bucket)
        # Phase 90 (D-09): the PUSHING files.state dual-write was removed; the cloud_job row (updated
        # below with backend_id + staging_bucket) is the sole derived authority now that staging succeeded.
        # Record backend_id + the D-06 staging_bucket in the SAME uncommitted session (MKUE-02/D-01):
        # in_flight_count is backend_id-scoped, and presign/cleanup read staging_bucket authoritatively.
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, staging_bucket=bucket_id))
        return True

    def _staging_stale_bound_sec(self, cfg: ControlSettings, status: str) -> int:
        """Return the staleness bound (seconds) for a staging ``status`` -- the reaper's never-fire-before line."""
        if status == CloudJobStatus.UPLOADING.value:
            return cfg.cloud_uploading_stale_after_sec
        return cfg.cloud_uploaded_stale_after_sec

    async def _reap_stranded_staging(self, session: AsyncSession, tally: dict[str, int]) -> None:
        """Spill THIS backend's AGE-STRANDED {UPLOADING, UPLOADED} ``cloud_job`` rows back to awaiting (phaze-ul2v).

        The missing safety net. ``in_flight_count`` counts {UPLOADING, UPLOADED, SUBMITTED, RUNNING}
        (D-10), so a staging row holds a burst-lane cap slot -- but :meth:`reconcile` only ever selected
        {SUBMITTED, RUNNING}. The staging half is terminalized SOLELY by the two agent HTTP callbacks
        (``report_uploaded`` flips UPLOADING -> UPLOADED + enqueues the submit; ``report_upload_failed``
        re-drives or spills at the cap). When the fileserver agent dies mid-upload, or the ``s3_upload``
        SAQ job is lost after the row was stamped, NEITHER callback ever fires: the row sits UPLOADING
        forever, reconcile scopes away from it, orphan recovery excludes it (it IS in-flight), and the
        slot leaks. Enough leaks and the lane wedges at "N/N in flight" with zero real workloads.

        **The callback path stays PRIMARY -- this only catches LOST callbacks.** Three mechanisms enforce
        that, and all are load-bearing:

        0. **The broker-liveness gate (phaze-31q3, status-keyed since phaze-1k0i).** BEFORE the age bound,
           skip any row whose CURRENT-status-owning broker key is still ``queued``/``active`` in
           ``saq_jobs`` (the same :func:`get_live_job_keys` probe recovery uses). The owning key depends on
           ``observed_status``: an UPLOADING row is owned by ``s3_upload:<file_id>`` (``report_uploaded``'s
           callback flips it to UPLOADED once that job runs); an UPLOADED row is owned by
           ``submit_cloud_job:<file_id>`` -- ``report_uploaded`` enqueues that job in the SAME transaction as
           the UPLOADED flip and the completed ``s3_upload`` job's key is swept from ``saq_jobs``, so checking
           ONLY ``s3_upload:`` (the phaze-31q3 shape) leaves the UPLOADED half of :data:`STAGING` with no live
           check at all: a ``submit_cloud_job`` queued behind a controller-queue backlog (or ``active`` against
           a hung kube API -- kr8s sets no client-side timeout, see ``kube_staging.submit_job``) reads
           UPLOADED with an ``updated_at`` frozen at the flip and gets reaped out from under the live submit,
           deleting the fully staged object it still owns. The age bound alone CANNOT tell a lost callback
           from live work: ``updated_at`` bumps only at dispatch/re-stage, not mid-transfer, and NOT while a
           job waits in a queue backlog -- so a multi-GB upload that legitimately transfers past the bound,
           or a still-queued job whose SAQ clock has not even started, genuinely reads its status with an old
           timestamp and would be reaped mid-flight. A live broker key means the job (and its callback) still
           owns the row regardless of age; never reap it.
        1. **The age bound.** A row is a candidate only when ``now - updated_at`` exceeds its per-status
           bound (:meth:`_staging_stale_bound_sec`). ``updated_at`` moves on EVERY live-path write (the
           initial stage stamp, and ``redrive_upload``'s re-stage -- phaze-2hv9), so an actively-progressing
           burst keeps resetting the clock. It is a coarse backstop for the case where even the broker row is
           gone (a lost/swept job), the liveness gate above being the precise live-vs-lost discriminator.
        2. **The CAS.** The spill goes through the single awaiting writer
           (:func:`hold_awaiting_cloud`) in SPILL mode with ``expect_status`` pinned to the status this
           reaper actually OBSERVED. A callback that lands between our read and our update advances the
           row out of that status, the CAS matches 0 rows, and the reaper takes a FULL no-op (no S3
           cleanup, no ledger clear) -- the happy path always wins the race, by construction.

        The spill mirrors ``report_upload_failed``'s over-cap branch exactly: re-stamp to
        ``status='awaiting'`` (OUT of :data:`IN_FLIGHT`, so the cap slot is released the moment this
        commits), ``clear_cloud_phase=True`` (off the "Running" tile, D-12), abort the multipart + delete
        the staged object so no orphaned upload/object survives (KSTAGE-04), and clear the
        ``s3_upload:<file_id>`` ledger row so the re-drive is not shadowed by a stale entry.

        Re-drive is BOUNDED: each reap increments ``cloud_job.attempts`` (capped at
        ``cloud_submit_max_attempts``). A file that strands repeatedly therefore reaches a SPENT budget,
        at which point ``select_backend`` routes it to local (D-03) instead of stranding on cloud again --
        so this never becomes an infinite cloud re-drive loop.

        Per-row discipline is identical to :meth:`reconcile`: the drain's ``pg_advisory_xact_lock`` is
        taken at the top of each row's unit of work, each row commits on its own, and a per-row
        ``except`` rolls back so one bad row never aborts the tick. Every skip path rolls back too, which
        releases the xact lock rather than holding it across the whole sweep.
        """
        cfg = cast("ControlSettings", get_settings())
        now = datetime.now(UTC)
        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status.in_([status.value for status in STAGING]),
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids only: the per-row rollback expires the ORM identity map (same reason
        # reconcile re-fetches), so every row is re-read fresh INSIDE the loop and the age/CAS decision
        # is made against that fresh read, never a stale snapshot.
        cloud_job_ids = [row.id for row in rows]

        # phaze-31q3: snapshot the live-broker key set ONCE per sweep (degrade-safe: an empty set on any
        # read failure falls the reaper back to age-only, never raising). A row whose ``s3_upload:<file_id>``
        # key is queued/active is live work the callback path still owns -- reaping it would abort a live
        # multipart mid-transfer AND leave the surviving job to shadow the re-drive enqueue.
        live_keys = await get_live_job_keys(session)

        for cloud_job_id in cloud_job_ids:
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                # phaze-7lpb: ``populate_existing=True`` forces a real SELECT under the lock. The snapshot
                # ``select`` above populated the identity map, and the sessionmaker is ``expire_on_commit=False``
                # (database.py), so a plain ``session.get`` for the first row -- and every row after a per-row
                # ``commit`` -- would return the UNEXPIRED cached object WITHOUT emitting SQL, evaluating the
                # age/CAS decision against sweep-start-stale state. A ``report_upload_failed`` -> redrive that
                # re-stages the row back into the SAME status (fresh upload_id + fresh updated_at, phaze-2hv9)
                # would then be judged on the OLD timestamp and reaped live. Re-read fresh at lock acquisition.
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None or cloud_job.status not in {status.value for status in STAGING}:
                    # A callback terminalized/advanced it since the snapshot -- nothing to reap.
                    await session.rollback()
                    continue
                observed_status = cloud_job.status
                # phaze-31q3/phaze-1k0i: broker-liveness gate BEFORE the age bound, status-keyed. An
                # UPLOADING row is owned by ``s3_upload:<file_id>``; an UPLOADED row's ``s3_upload`` job has
                # already completed (its key is swept from saq_jobs) and ownership has moved to
                # ``submit_cloud_job:<file_id>`` -- checking the UPLOADING key for an UPLOADED row would
                # always miss and reap a row a queued/active submit still owns. Either live key means the
                # callback path still owns this row -- never reap it, no matter how old ``updated_at`` looks
                # (a legitimately hours-long transfer, or a job still waiting in a queue backlog, bumps no
                # timestamp). Skip via rollback (releasing the xact lock), exactly like the young-row skip.
                live_key = (
                    f"s3_upload:{cloud_job.file_id}" if observed_status == CloudJobStatus.UPLOADING.value else f"submit_cloud_job:{cloud_job.file_id}"
                )
                if live_key in live_keys:
                    await session.rollback()
                    continue
                # ``updated_at`` is TIMESTAMP WITHOUT TIME ZONE, so asyncpg hands it back NAIVE in
                # production; assume-UTC before subtracting (a bare naive-minus-aware raises TypeError
                # and would abort the whole sweep -- mirrors reenqueue.py's CR-02 coercion).
                ref = cloud_job.updated_at or cloud_job.created_at
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=UTC)
                age_sec = (now - ref).total_seconds()
                bound_sec = self._staging_stale_bound_sec(cfg, observed_status)
                if age_sec < bound_sec:
                    # YOUNGER THAN THE BOUND: the callback owns this row. Never fire here.
                    await session.rollback()
                    continue
                file_id = cloud_job.file_id
                upload_id = cloud_job.upload_id
                staging_bucket = cloud_job.staging_bucket
                # Bounded re-drive: each reap spends one attempt; at the cap select_backend routes local.
                attempts = min(cloud_job.attempts + 1, cfg.cloud_submit_max_attempts)
                file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
                spilled = file is not None and await hold_awaiting_cloud(
                    session,
                    file,
                    attempts=attempts,
                    expect_status=(observed_status,),
                    # phaze-wnp51: pin the spill to the generation we actually observed. ``status`` alone
                    # is not a generation identifier -- redrive_upload can re-stage the row back into the
                    # SAME status with a fresh upload_id between our read and this CAS; requiring the
                    # observed upload_id too makes that re-drive fail the CAS instead of losing its work
                    # to a reap that thinks it is spilling the OLD (dead) generation.
                    expect_upload_id=upload_id,
                    clear_cloud_phase=True,
                )
                if not spilled:
                    # Lost the race to a live callback (or the FK file vanished): FULL no-op -- no S3
                    # cleanup (a live burst may still own the object), no ledger clear.
                    await session.rollback()
                    continue
                # phaze-jwz0: CAS hit -> clear the ledger + COMMIT the spill FIRST, which releases the drain's
                # global ``pg_advisory_xact_lock`` and the idle-in-transaction connection BEFORE any S3 network
                # I/O. The prior structure held that single global lock + an open txn across
                # ``abort_multipart_upload`` / ``delete_staged_object`` -- aioboto3 calls subject to botocore's
                # full connect-timeout x retry cycle (minutes against a hung/unreachable bucket) -- so ONE bad
                # endpoint wedged every ``stage_cloud_window`` drain tick and every reconcile row behind it, and
                # ran DESTRUCTIVE cleanup before the commit (a commit failure then left the DB claiming an upload
                # whose S3 substrate was already gone).
                await clear_ledger_entry(session, f"s3_upload:{file_id}")
                if observed_status == CloudJobStatus.UPLOADED.value:
                    # phaze-2iizn: an UPLOADED row is owned by submit_cloud_job:<file_id> (phaze-1k0i's
                    # status-keyed liveness gate above), not s3_upload:<file_id> -- the s3_upload job
                    # already completed and swept its own broker key by the time the row reached
                    # UPLOADED. The lost job here is submit_cloud_job's, and its before_enqueue-written
                    # ledger row survives this reap untouched unless cleared too: recover_orphaned_work
                    # would otherwise replay it against an already-spilled/terminal file and guarantee-fail
                    # with KubeStagingError. Clearing s3_upload:<file_id> stays -- it is unconditionally
                    # load-bearing (the control side never clears it on the success path) -- this ADDS the
                    # second key rather than swapping it.
                    await clear_ledger_entry(session, f"submit_cloud_job:{file_id}")
                await session.commit()
                tally["staging_reaped"] += 1
                logger.warning(
                    "KueueBackend.reconcile: stranded staging cloud_job reaped -> spilled back to awaiting (lost agent callback)",
                    cloud_job_id=str(cloud_job_id),
                    file_id=str(file_id),
                    backend_id=self.id,
                    observed_status=observed_status,
                    age_sec=int(age_sec),
                    bound_sec=bound_sec,
                    attempts=attempts,
                )
                # phaze-jwz0: S3 cleanup runs AFTER the commit, OUTSIDE the txn/lock. Both ops are idempotent
                # (they swallow NoSuchUpload / absent-object) and the row is already 'awaiting' -> a re-drive
                # re-stages a FRESH multipart, so the old object is irrelevant: a crash or a hung bucket here at
                # worst leaks the OLD object until the next reap/spill re-runs the same idempotent cleanup.
                # Failures are isolated locally so a slow bucket can neither hold the released lock nor turn a
                # durable spill into a per-row rollback that undoes it.
                bucket = s3_staging.resolve_bucket_config(cfg, staging_bucket)
                if bucket is not None:
                    try:
                        if upload_id:
                            await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
                        # phaze-wa9x: re-read the row IMMEDIATELY before the delete, outside the lock we just
                        # released. ``delete_staged_object`` is keyed only by file_id, and the bucket/key are
                        # identical for every staging generation of this file -- once the row is 'awaiting', a
                        # concurrent drain tick can re-dispatch it and stage a FRESH object at the SAME key
                        # while this delete is still in flight (a stalled-but-eventually-successful S3 DELETE).
                        # A new cycle always re-upserts status back to UPLOADING with a FRESH upload_id
                        # (_stage_file_to_s3), so a row still 'awaiting' with the SAME upload_id we observed
                        # proves no new cycle has claimed the key -- only then is the delete safe.
                        current = None
                        try:
                            current = (
                                await session.execute(select(CloudJob.status, CloudJob.upload_id).where(CloudJob.id == cloud_job_id))
                            ).one_or_none()
                        finally:
                            # phaze-a6un6: rollback in a finally, not after the SELECT in the try body --
                            # if the SELECT itself raises (transient DB error), the old placement skipped
                            # this rollback entirely and the outer except below only logged, leaving the
                            # session in an aborted/pending transaction. The NEXT row's advisory-lock
                            # acquire then raised PendingRollbackError and THAT healthy row's reap was
                            # skipped for the whole tick, misleadingly blamed instead of this probe.
                            await session.rollback()  # read-only probe; release its implicit tx either way
                        if current is not None and current.status == CloudJobStatus.AWAITING.value and current.upload_id == upload_id:
                            await s3_staging.delete_staged_object(file_id, bucket)
                    except Exception:
                        logger.warning(
                            "KueueBackend.reconcile: post-commit S3 cleanup of a reaped staging row failed "
                            "(row already spilled to awaiting; old object may leak until the re-drive re-stages)",
                            cloud_job_id=str(cloud_job_id),
                            file_id=str(file_id),
                            exc_info=True,
                        )
            except Exception:
                await session.rollback()
                logger.warning("KueueBackend.reconcile: stranded staging reap failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int]:
        """Reconcile THIS backend's in-flight ``cloud_job`` rows against their Kueue Job/Workload (backend_id-aware).

        Re-homes ``reconcile_cloud_jobs`` (L282-322): iterate ``cloud_job`` rows in {SUBMITTED, RUNNING}
        scoped to ``backend_id == self.id``, delegate each to the shared ``_reconcile_one`` under the
        per-row ``session.rollback()`` guard so one bad row never aborts the tick. ``ctx`` (carrying the
        re-drive ``queue``) is threaded to ``_reconcile_one``; it defaults to ``{}`` for the lay-and-prove
        unit path where no row reaches a re-drive.

        SCHED-02: each per-row unit of work FIRST acquires the drain's ``pg_advisory_xact_lock(5_000_504)``
        so a reconcile row-mutation and a ``stage_cloud_window`` snapshot are mutually exclusive per-row.
        ``_reconcile_one`` commits per row, which auto-releases the xact lock -- that per-row granularity
        is REQUIRED (Pitfall 2: a whole-tick lock would break the load-bearing delete-after-record
        ordering, which commits mid-tick). Reconcile only ever DECREMENTS in-flight (it never claims a
        slot), so this single shared drain lock is provably cap-safe (RESEARCH reconcile-only-decrements
        proof).

        phaze-ul2v: the tick now ALSO runs :meth:`_reap_stranded_staging` first, covering the pre-submit
        {UPLOADING, UPLOADED} half of :data:`IN_FLIGHT` that this Kueue read scopes away from. Those rows
        hold cap slots but have no Kueue object to read, so they are aged out instead. The extra
        ``staging_reaped`` tally key aggregates in the cron via its ``tally.get(key, 0) + value`` fold.
        """
        cfg = cast("ControlSettings", get_settings())
        cap = cfg.cloud_submit_max_attempts
        tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0, "staging_reaped": 0}
        reconcile_ctx = ctx if ctx is not None else {}

        # phaze-ul2v: FIRST sweep the pre-submit half ({UPLOADING, UPLOADED}) that the Kueue read below
        # deliberately does not cover. Independent of the Job/Workload read -- a stranded staging row has
        # no Kueue object to reconcile against, only an age bound.
        await self._reap_stranded_staging(session, tally)

        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids: the per-row guard's rollback expires the ORM identity map, so re-fetch
        # each row fresh inside the loop rather than touching a stale/expired object (verbatim from the cron).
        cloud_job_ids = [row.id for row in rows]

        for cloud_job_id in cloud_job_ids:
            try:
                # SCHED-02: acquire the drain's advisory lock at the TOP of each per-row unit of work
                # (per-row, not whole-tick) so this reconcile row-mutation is mutually exclusive with a
                # ``stage_cloud_window`` snapshot. ``_reconcile_one`` commits per row -> the xact lock
                # auto-releases at that commit, preserving the delete-after-record ordering.
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                # phaze-7lpb: ``populate_existing=True`` forces a real SELECT under the lock (the snapshot
                # ``select`` above populated the identity map and the sessionmaker is ``expire_on_commit=False``,
                # so a plain ``get`` would hand back the sweep-start-stale cached row for the first row and every
                # post-commit row -- and the None-check for a vanished row could never fire on a cached object).
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None:
                    # phaze-c1u7: a concurrent delete_scan cascade (services/scan_deletion.py) can remove
                    # this row between the snapshot above and this fresh read. Mirror
                    # _reap_stranded_staging's identical vanished-row skip (line ~701): roll back to
                    # release the pg_advisory_xact_lock taken above, exactly like every other skip path in
                    # this loop -- a bare ``continue`` leaves the lock held across every subsequent row's
                    # kube I/O (and the rest of the tick, if this was the last row) instead of releasing it
                    # per-row as the design (SCHED-02 / Pitfall 2) relies on.
                    await session.rollback()
                    continue
                tally["reconciled"] += 1
                # MKUE-01/D-04: thread THIS backend's KubeConfig so every get_job/get_workload_for/
                # delete_job inside reconcile targets the file's own cluster.
                await _reconcile_one(reconcile_ctx, session, cloud_job, cap, tally, self._kube())
            except Exception:
                # Per-row guard: a single bad row never aborts the tick; roll back the partial mutation.
                await session.rollback()
                logger.warning("KueueBackend.reconcile: row reconcile failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)
        # SCHED-05: return the per-backend tally so the cron aggregates it (replaces the old global tally).
        return tally


def resolve_backends(settings: ControlSettings) -> list[Backend]:
    """Build one :class:`Backend` impl per registry entry -- N non-local backends supported (Phase 69, SCHED-01).

    Phase 69 (SCHED-01) removes the Phase-68 ``>1``-non-local boot guard: multi-backend simultaneous
    dispatch is exactly this phase's job, so a registry with N non-local entries now resolves to a full
    ``list[Backend]`` of length N (+ any locals). The tiered drain
    (``release_awaiting_cloud.stage_cloud_window``) iterates this list, snapshots each backend's
    ``is_available`` / ``in_flight_count`` once per tick, and routes each candidate via the pure
    ``select_backend`` policy. Each impl binds to its Phase-67 discriminated-union submodel (``config``).

    The historical ``>1``-non-local defense-in-depth is retained ONLY for the non-drain call sites that
    still assume a single non-local kind (pipeline dashboard / backfill, agent_s3) -- it lives in
    :func:`resolved_non_local_kind` (WR-01), which those callers use; the drain no longer consults it.
    """
    resolved: list[Backend] = []
    for entry in settings.backends:
        if entry.kind == "local":
            resolved.append(LocalBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "compute":
            resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "kueue":
            resolved.append(KueueBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))

    return resolved


def resolve_compute_backend(cfg: ControlSettings, backend_id: str | None) -> ComputeBackend | None:
    """Resolve a recorded ``cloud_job.backend_id`` to its ``ComputeBackend`` registry entry (D-06).

    The single AUTHORITATIVE inverse of ``ComputeAgentBackend.dispatch``'s ``backend_id`` stamp: every
    downstream scratch / terminalization reader (the Plan-02 rsync destination, the Plan-03 ``/pushed`` +
    ``/mismatch`` callbacks) resolves the value RECORDED on ``cloud_job.backend_id`` through here rather
    than re-deriving it. Mirrors ``s3_staging.resolve_bucket_config`` exactly: pure + ORM-free, reads only
    ``cfg.backends``.

    Returns ``None`` when ``backend_id`` is ``None`` (an all-local / unstamped row), or when the id names
    no ``kind == "compute"`` entry (a kueue/local id, or an operator-removed backend) so the caller can
    skip the compute op cleanly.
    """
    if backend_id is None:
        return None
    return {backend.id: backend for backend in cfg.backends if backend.kind == "compute"}.get(backend_id)


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived cloud-lane kind: ``"local"`` when all-local, else the non-local kind.

    The single seam the non-drain single-kind callers use (the S3-upload-complete callback
    ``agent_s3.report_uploaded``, the ``/pipeline/stats`` poll ``build_dashboard_context``, and the
    backfill route): they only ask "is the cloud lane kueue?". ``"local"`` when ``cloud_enabled`` is
    False.

    Phase 70 (MKUE-01, sibling of the Pitfall-1 ``active_compute_scratch_dir`` fix): the callers 500'd
    the moment a 2nd Kueue backend was declared, because the old ``>1``-non-local blanket raise fired on
    the literal MKUE-01 scenario. Generalize: when ANY non-local backend is ``"kueue"``, return
    ``"kueue"`` -- this tolerates N Kueue backends AND a local + N-Kueue + 1-compute registry (the
    callers degrade gracefully by construction, no per-site try/except needed). Phase 72 (MCOMP-01,
    D-03) retires the compute-only ``>1`` fail-fast too: the compute-only branch now returns ``"compute"``
    for N compute backends (per-agent dispatch attribution lands in Phase 73). All-local -> ``"local"``,
    single-kueue -> ``"kueue"``, single-compute -> ``"compute"`` stay byte-identical.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    if any(backend.kind == "kueue" for backend in non_local):
        return "kueue"
    # No kueue backend -> compute-only. Phase 72 (D-03) retired the ambiguous >1-compute fail-fast; the
    # compute-only branch returns "compute" for any N compute (per-agent attribution lands in Phase 73).
    return non_local[0].kind


# --- Phase 71 (BEUI-01): read-only backend-lane snapshot for the N-lane UI poll -----------
#
# A pure read over the Phase-67 registry + the ``cloud_job`` in-flight/admission substrate that feeds
# the BEUI-01 N-lane grid on the existing 5s ``/pipeline/stats`` poll (Plan 03 seeds + renders it). Every
# leg is degrade-safe -- a DB hiccup or a hung Kueue probe can NEVER raise into the hot poll (T-71-03) --
# and secret-free: only ``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}`` ever
# leaves this module; a probe-failure log carries ``backend_id`` ONLY, never a SecretStr / kube SA token
# / S3 key (SP-5, T-71-01). This plan builds the DATA path only -- no template, no context wiring.


# D-02/A2: the per-probe availability timeout -- well under the 5s poll, yet tolerant of a slow-healthy
# kr8s ``LocalQueue`` RTT. A hung Kueue cluster times out to "offline" for THAT lane alone (T-71-02).
_PROBE_TIMEOUT_SEC = 1.5

# The zero-admission fallback merged into a lane with no attributed ``cloud_job`` rows (idle/local lanes).
_ZERO_ADMISSION: dict[str, int] = {"quota_wait": 0, "inadmissible": 0}


async def _admission_by_backend_id(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Return per-``backend_id`` admission counts ``{quota_wait, inadmissible}`` via one ``GROUP BY`` (D-03).

    Generalizes the GLOBAL ``pipeline.get_cloud_phase_counts`` (``cloud_phase == QUEUED_BEHIND_QUOTA``) +
    ``pipeline.get_inadmissible_count`` (``inadmissible`` AND ``status IN {SUBMITTED, RUNNING}``) predicates
    to a per-``backend_id`` ``GROUP BY`` so each Kueue lane owns its OWN quota-wait-vs-Inadmissible counts.
    ``cloud_phase`` is NULL for local/compute rows, so they contribute 0 to ``quota_wait``; ``backend_id``
    -NULL (legacy / unattributed) rows are excluded entirely (they belong to no lane). Degrades to ``{}``
    on any DB error with a guarded rollback (mirrors ``pipeline._safe_count``) so it never raises into the
    hot 5s poll (T-71-03).
    """
    try:
        stmt = (
            select(
                CloudJob.backend_id,
                func.count().filter(CloudJob.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value).label("quota_wait"),
                func.count()
                .filter(
                    CloudJob.inadmissible.is_(True),
                    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
                )
                .label("inadmissible"),
            )
            .where(CloudJob.backend_id.is_not(None))
            .group_by(CloudJob.backend_id)
        )
        rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("backend_lane_admission_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_lane_admission_rollback_failed", exc_info=True)
        return {}
    return {backend_id: {"quota_wait": int(quota_wait or 0), "inadmissible": int(inadmissible or 0)} for backend_id, quota_wait, inadmissible in rows}


async def _probe_one(session: AsyncSession, backend: Backend) -> tuple[str, bool]:
    """Probe ONE backend's live availability, bounded + degrade-safe -> ``(backend_id, available)`` (D-02).

    A :class:`LocalBackend` is short-circuited to ``True`` with NO I/O (local dispatch never depends on a
    remote agent). Every other backend's ``is_available`` is awaited under an ``asyncio.wait_for`` bounded
    by ``_PROBE_TIMEOUT_SEC``; a timeout OR any probe exception degrades THAT lane to offline and logs the
    ``backend_id`` ONLY (never a SecretStr / kube token, T-71-01). A single hung Kueue cluster can
    therefore never stall the shared read (T-71-02).

    phaze-ntr8s: a compute probe's DB read (``select_agent_by_id`` -> ``session.execute``) cancelled
    mid-flight by the ``asyncio.wait_for`` timeout can leave the SHARED session unusable for the next
    statement -- SQLAlchemy raises ``PendingRollbackError`` on the following ``execute`` until the
    session is rolled back. ``_probe_availability`` runs every backend's probe SEQUENTIALLY on this ONE
    session (Pitfall 1), so without an immediate roll back HERE that poison outlives this probe: the very
    next backend's probe (or, for a Kueue probe that ignores the session, whichever LATER probe next
    touches it) inherits a broken session and fails immediately -- a single slow compute lane cascading
    every SUBSEQUENT lane in the SAME sweep to a false "offline", which then reports a false "no cloud
    backend reachable" hold reason for a transient DB blip that has nothing to do with reachability. Roll
    back HERE, inside the per-probe except, not just once after the whole fan-out (the pre-existing
    post-fan-out rollback in :func:`get_backend_lane_snapshot` only protected the NEXT poll's snapshot,
    never a sibling lane within THIS one). ``session.rollback()`` is a safe no-op when there is nothing to
    roll back, and any failure rolling back is itself swallowed -- a cleanup failure must never mask the
    real probe failure this branch is already reporting.
    """
    if isinstance(backend, LocalBackend):
        return (backend.id, True)
    try:
        available = await asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC)
    except Exception:
        logger.info("backend_lane_probe_offline", backend_id=backend.id)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_lane_probe_rollback_failed", backend_id=backend.id, exc_info=True)
        return (backend.id, False)
    return (backend.id, bool(available))


async def _probe_availability(session: AsyncSession, backends: list[Backend]) -> dict[str, bool]:
    """Probe every backend SEQUENTIALLY on the one shared session -> ``{backend_id: available}`` (D-02).

    The probes run one at a time in a plain ``for`` loop: each ``_probe_one`` is fully awaited before the
    next begins, so there is NEVER concurrent use of the shared ``AsyncSession`` -- Session-safety
    (Pitfall 1) holds by CONSTRUCTION, guaranteed by the serial control flow. Since Phase 72 (MCOMP-01) retired the
    single-active-compute assumption, N≥2 compute backends are legal and each compute probe touches the
    shared ``session`` via ``select_agent_by_id`` (``session.execute``); serializing the fan-out guarantees
    those ``session.execute`` calls can never overlap (SQLAlchemy forbids concurrent operations on one
    session). Each ``_probe_one`` is individually capped by ``asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)``
    AND rolls the session back itself on a timeout/exception (phaze-ntr8s), so one poisoned probe can
    never cascade to the NEXT backend probed in this same loop. Because the probes now run one at a time
    the worst-case aggregate wait is ``N x _PROBE_TIMEOUT_SEC`` (not the old ``asyncio.gather`` ~1x
    bound) -- a deliberate D-01 trade-off, acceptable because N is small (registry-declared local +
    N-Kueue + N-compute) and session-safety takes priority over probe latency on the 5s
    ``/pipeline/stats`` poll. The post-fan-out ``session.rollback`` in :func:`get_backend_lane_snapshot`
    is retained as a second, harmless line of defense before the ``in_flight_count`` reads. Kueue probes
    ignore the session (kr8s I/O) and local is short-circuited (no I/O).
    """
    results: dict[str, bool] = {}
    for backend in backends:
        backend_id, available = await _probe_one(session, backend)
        results[backend_id] = available
    return results


def _kind_of(backend: Backend) -> str:
    """Derive the lane ``kind`` ("local"/"compute"/"kueue") from the impl class (mirrors resolve_backends)."""
    if isinstance(backend, LocalBackend):
        return "local"
    if isinstance(backend, ComputeAgentBackend):
        return "compute"
    if isinstance(backend, KueueBackend):
        return "kueue"
    return "unknown"


# --- phaze-5c6i2: lane-card queued/working/processed metrics --------------------------------
#
# Replaces the misleading ``{in_flight}/{cap}`` numeral + saturation bar with the operator's four
# numbers: TOTAL QUEUED (analyze, global), QUEUED per lane, WORKING per lane, and PROCESSED per lane
# (24h primary + lifetime caption). See the bead description for the full rationale; the short version:
# ``in_flight`` conflates "enqueued" with "executing" (the scheduling-ledger row exists at ENQUEUE
# time), so a saturated-looking bar could mean nothing is actually running. These reads carry the
# split queued-vs-working sources that already exist (SAQ's own queued/active counts for local, the
# phaze-zyoag staged/analyzing seam for cloud) instead of re-deriving a third definition.

# The processed-count rolling window -- the "24h" half of the operator's "412 (24h) / 545 all time"
# target shape. Not a Settings knob (yet): a fixed, well-known window matching the target shape
# verbatim; promote to config if an operator ever needs a different one.
_PROCESSED_WINDOW = timedelta(hours=24)


async def _safe_count_or_none(session: AsyncSession, stmt: Any, *, node: str) -> int | None:
    """Run a single-scalar COUNT statement, degrading to ``None`` (NOT 0) on any failure (phaze-5c6i2).

    The sibling of :func:`phaze.services.pipeline._safe_count` with a DELIBERATELY different degrade
    value. ``_safe_count``'s 0-degrade is correct for a count whose true zero is itself a real, safe
    state (e.g. 0 in-flight). It is WRONG for a queue-depth-shaped read: a DB hiccup that silently
    renders "queued 0" on a healthy backlog of thousands is a worse lie than the ``{in_flight}/{cap}``
    numeral this bead replaces (acceptance rule 8 / the bead's DEGRADE POSTURE design note). Every
    lane-metric read below uses this wrapper so a failure surfaces to the template as an explicit
    unknown (an em-dash) instead of a fabricated zero. Same SAVEPOINT discipline as ``_safe_count``: the
    nested scope rolls back alone on error, recovering an aborted transaction without expiring the
    caller's already-loaded ORM objects.
    """
    try:
        async with session.begin_nested():
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("lane_metric_degraded", node=node, exc_info=True)
        return None


async def _local_lane_queued_working(session: AsyncSession, app_state: Any) -> tuple[int | None, int | None]:
    """Return the LOCAL lane's ``(queued, working)`` from SAQ's OWN ``analyze`` lane, kept SEPARATE (phaze-5c6i2).

    :func:`phaze.services.pipeline.get_agent_lane_depths` sums ``count("queued") + count("active")``
    into one number per lane; this reads the SAME two SAQ counts on the SAME ``analyze`` lane -- bound
    to the live fileserver agent via :func:`resolve_lane_queue_agent` (the IDENTICAL binding
    :func:`get_lane_queue_depths` uses for the lane-detail pane, so the two panels can never disagree
    about WHICH agent's queue "the local lane" reads) -- but keeps the two counts distinct instead of
    collapsing them. Neither figure is derived from
    :func:`phaze.services.stage_status.inflight_clause` (which cannot distinguish "enqueued" from
    "started" -- the exact defect this bead exists to fix; acceptance rule 3).

    Degrades to ``(None, None)`` -- never ``(0, 0)`` -- when there is no live fileserver agent to read
    (mirrors :data:`NO_FILESERVER_AGENT_NOTE`'s condition), when ``app_state`` itself is absent (callers
    that only need availability/admission, e.g. :func:`derive_cloud_hold_reason`, pass none), or on any
    broker hiccup: a missing agent or a dead broker is not evidence of an empty queue.
    """
    if app_state is None:
        return None, None
    identity = await resolve_lane_queue_agent(session, "local", "local")
    if identity.agent_id is None:
        return None, None
    try:
        queue = app_state.task_router.queue_for(identity.agent_id, "analyze")
        await queue.connect()
        queued = await queue.count("queued")
        working = await queue.count("active")
    except Exception:
        logger.warning("local_lane_queued_working_degraded", agent_id=identity.agent_id, exc_info=True)
        return None, None
    return queued, working


async def _cloud_lane_queued_working(session: AsyncSession, backend_id: str) -> tuple[int | None, int | None]:
    """Return a CLOUD lane's ``(queued, working)``, scoped to ``backend_id`` via the phaze-zyoag seam (phaze-5c6i2).

    ``queued`` = the pre-execution half of the bounded cloud window (:data:`STAGING` plus a
    compute-attributed SUBMITTED row); ``working`` = the executing half (a kueue-attributed SUBMITTED
    row -- admitted-or-queued-behind-quota counts as "in the cloud window, post-submit" under the
    zyoag option-(a) definition -- plus RUNNING). Reuses
    :func:`phaze.services.pipeline._cloud_window_clauses` verbatim (the SAME per-backend-kind split the
    "Staged (pushing)"/"Analyzing (cloud)" cards use) ANDed with ``backend_id`` so this lane's figures
    can never drift from those two cards' definition of the seam -- CONSUMING zyoag's decision rather
    than re-deriving it a third time (the bead's explicit dependency reason; acceptance rule 4).

    Degrades to ``(None, None)`` on any error -- an unknown queue depth must never render as a
    fabricated 0 (acceptance rule 8).
    """
    try:
        staged, analyzing = _cloud_window_clauses()
    except Exception:
        logger.warning("cloud_lane_queued_working_degraded", backend_id=backend_id, exc_info=True)
        return None, None
    queued = await _safe_count_or_none(session, select(func.count(CloudJob.id)).where(staged, CloudJob.backend_id == backend_id), node="lane_queued")
    working = await _safe_count_or_none(
        session, select(func.count(CloudJob.id)).where(analyzing, CloudJob.backend_id == backend_id), node="lane_working"
    )
    return queued, working


async def _cloud_lane_active(session: AsyncSession, backend_id: str, kind: str) -> int | None:
    """Return comparable execution activity only where the backend exposes it.

    Kueue ``RUNNING`` is a reconciled pod-running fact. Compute has no equivalent lifecycle signal:
    its cloud row is ``SUBMITTED`` while pushing and can become ``SUCCEEDED`` before remote analysis
    finishes, so reporting either state as executing would be invented telemetry. Unknown kinds are
    likewise unobservable. The caller uses local SAQ ``working`` directly for the local lane.
    """
    if kind != "kueue":
        return None
    return await _safe_count_or_none(
        session,
        select(func.count(CloudJob.id)).where(CloudJob.backend_id == backend_id, CloudJob.status == CloudJobStatus.RUNNING.value),
        node="lane_active",
    )


async def get_analysis_working_count(session: AsyncSession, app_state: Any) -> int | None:
    """Return the current local + configured-cloud ``working`` total without building lane cards.

    This reuses the exact local SAQ ``active`` and cloud-window ``analyzing`` definitions used by
    :func:`get_backend_lane_snapshot`, but avoids availability probes, capacity reads, and the serial
    per-lane processed-history queries that a Summary numeral does not consume. If either source is
    unreadable, the aggregate is unknown rather than a partial total presented as zero.
    """
    _, local_working = await _local_lane_queued_working(session, app_state)
    try:
        _, cloud_working_clause = _cloud_window_clauses()
        cloud_ids = [backend.id for backend in resolve_backends(cast("ControlSettings", get_settings())) if _kind_of(backend) != "local"]
    except Exception:
        logger.warning("analysis_working_count_degraded", exc_info=True)
        return None
    if cloud_ids:
        cloud_working = await _safe_count_or_none(
            session,
            select(func.count(CloudJob.id)).where(cloud_working_clause, CloudJob.backend_id.in_(cloud_ids)),
            node="analysis_working_total",
        )
    else:
        cloud_working = 0
    if local_working is None or cloud_working is None:
        return None
    return local_working + cloud_working


def _cloud_job_succeeded_for_backend(backend_id: str) -> ColumnElement[bool]:
    """Return ``EXISTS(a SUCCEEDED cloud_job for this file attributed to backend_id)`` (phaze-5c6i2)."""
    return exists(
        select(CloudJob.id).where(
            CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value, CloudJob.backend_id == backend_id
        )
    )


async def _lane_processed_counts(session: AsyncSession, *, backend_id: str | None) -> tuple[int | None, int | None]:
    """Return ``(processed_24h, processed_lifetime)`` for one lane, attributed by EXECUTION (phaze-5c6i2).

    Attribution keys on ``cloud_job.backend_id`` (the lane that EXECUTED the analysis), never on
    ``FileRecord.agent_id`` (the FILESERVER that scanned/owns the file -- a different axis entirely; see
    the bead's ATTRIBUTION design note). ``backend_id`` given -> direct: a completed file with a
    ``succeeded`` cloud_job row attributed to it. ``backend_id=None`` -> the LOCAL negation: completed
    with NO ``succeeded`` cloud_job row at all -- mirroring :meth:`LocalBackend.in_flight_count`'s own
    carve-out, ADAPTED from its live ``IN_FLIGHT``-status negation to a TERMINAL-status one (this reads
    completed history, not a live race).

    This adaptation is explicitly NOT subject to that method's documented compute-timing gap (a
    transient window where a compute row's cloud_job is already SUCCEEDED -- stamped at PUSH time --
    before its remote ``process_file`` has actually STARTED, which can misattribute a LIVE in-flight
    probe): every caller here gates on ``AnalysisResult.analysis_completed_at IS NOT NULL`` first, which
    is stamped only once execution genuinely FINISHES, wherever it ran. By the time that gate opens, a
    SUCCEEDED cloud_job's ``backend_id`` is a settled historical fact, so a completed file can never be
    double-counted or lost between the local and cloud attributions -- the gap cannot reach a PROCESSED
    count the way it can reach a live in-flight one. (No ``compute`` backend is configured in the
    current deployment -- local + kueue -- so this is documented for completeness per the bead's
    instruction to say so explicitly, not because it is presently load-bearing.)

    Degrades to ``(None, None)`` on any error (acceptance rule 8).
    """
    attribution = (
        ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value))
        if backend_id is None
        else _cloud_job_succeeded_for_backend(backend_id)
    )
    base = (
        select(func.count(AnalysisResult.id))
        .select_from(AnalysisResult)
        .join(FileRecord, FileRecord.id == AnalysisResult.file_id)
        .where(
            AnalysisResult.analysis_completed_at.is_not(None),
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            attribution,
        )
    )
    node = f"lane_processed_{backend_id or 'local'}"
    lifetime = await _safe_count_or_none(session, base, node=f"{node}_lifetime")
    cutoff = datetime.now(UTC) - _PROCESSED_WINDOW
    windowed = await _safe_count_or_none(session, base.where(AnalysisResult.analysis_completed_at >= cutoff), node=f"{node}_24h")
    return windowed, lifetime


async def get_analyze_queue_totals(session: AsyncSession, lanes: list[dict[str, Any]]) -> dict[str, int | None]:
    """Return the global "TOTAL QUEUED (analyze)" figure + its unrouted remainder (phaze-5c6i2, acceptance rule 2).

    ``unrouted_queued`` = Stage.ANALYZE's ``not_started`` bucket
    (:func:`phaze.services.pipeline._safe_bucket_counts`) -- files with NO scheduling-ledger row at all
    for analyze, i.e. not yet routed to ANY lane (the ``in_flight`` bucket already counts every
    routed-but-not-yet-done file, local or cloud, per the same ledger-existence-at-enqueue-time read the
    bead's motivation cites). ``total_queued`` sums that with every lane's OWN ``queued`` figure -- work
    assigned to a lane but not yet executing -- so ``total_queued >= sum(lane["queued"] for lane in
    lanes)`` holds by construction and the unrouted remainder is always the visible, non-negative
    difference between the two rendered numbers, never silently dropped.

    Degrades to ``{"total_queued": None, "unrouted_queued": <bucket value>}`` when ANY lane's own
    ``queued`` is itself degraded (``None``): a partial sum that silently omitted an unknown lane would
    UNDERSTATE the total exactly the way a 0-degrade would, so one unknown component propagates to the
    whole total rather than being quietly dropped. ``unrouted_queued`` keeps ``_safe_bucket_counts``'s
    OWN pre-existing degrade discipline (0 on error) unchanged -- it is not a new read this bead adds.
    """
    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)
    unrouted = buckets["not_started"]
    queued_values = [lane.get("queued") for lane in lanes]
    if any(value is None for value in queued_values):
        return {"total_queued": None, "unrouted_queued": unrouted}
    lane_sum = sum(cast("int", value) for value in queued_values)
    return {"total_queued": unrouted + lane_sum, "unrouted_queued": unrouted}


async def get_backend_lane_snapshot(session: AsyncSession, app_state: Any = None) -> list[dict[str, Any]]:
    """Return one rank-ascending, secret-free lane dict per registry backend for the BEUI-01 grid.

    Resolves the Phase-67 registry, then composes one lane per backend from several degrade-safe reads:
    ``_admission_by_backend_id`` (per-``backend_id`` quota_wait/inadmissible, D-03), ``_probe_availability``
    (live bounded is_available probes, D-02), each backend's ``in_flight_count`` (the D-02 cloud_job
    substrate) and, since phaze-5c6i2, the queued/working/processed metrics below. Lanes are sorted
    rank-ascending, tie-broken by ``id`` (D-06), so the Plan-03 template loops them verbatim. A
    :class:`LocalBackend` lane always shows ``in_flight`` 0 and ``available`` True.

    ``app_state`` (phaze-5c6i2) is threaded through ONLY to resolve the local lane's SAQ queue via
    ``app_state.task_router`` (:func:`_local_lane_queued_working`) -- every render caller already has
    ``request.app.state`` at hand (the same object :func:`get_lane_queue_depths` takes). It defaults to
    ``None`` for callers that only need availability/admission (e.g. :func:`derive_cloud_hold_reason`,
    and every pre-phaze-5c6i2 test): the local lane's ``queued``/``working`` degrade to ``None``
    (explicit unknown) rather than requiring every caller to thread a router it does not otherwise need.

    Every lane carries ``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible, queued,
    working, active, processed_24h, processed_lifetime}`` -- no ``config``, no ``SecretStr``, no kube/S3 token
    (T-71-01). ``in_flight`` is UNCHANGED (still the D-02 cloud_job substrate; several other callers key
    off it, e.g. :func:`derive_localqueue_unreachable` / :func:`derive_cloud_hold_reason` /
    ``_lane_detail.html``'s header numeral). ``queued``/``working``/``processed_24h``/
    ``processed_lifetime`` are the phaze-5c6i2 additions the lane cards render INSTEAD of the misleading
    ``{in_flight}/{cap}`` numeral + saturation bar (acceptance rule 1): ``queued``/``working`` come from
    :func:`_local_lane_queued_working` (local) or :func:`_cloud_lane_queued_working` (compute/kueue, the
    phaze-zyoag seam), ``processed_24h``/``processed_lifetime`` from :func:`_lane_processed_counts`. Each
    of the four is ``int | None`` -- ``None`` means degraded/unknown (never a fabricated 0, acceptance
    rule 8) and the template renders an em-dash for it. Any top-level exception degrades the WHOLE
    snapshot to ``[]`` with a guarded rollback so it can NEVER raise into the hot 5s ``/pipeline/stats``
    poll (SP-1, T-71-03) -- unchanged from before this bead.
    """
    try:
        backends = resolve_backends(cast("ControlSettings", get_settings()))
        admission = await _admission_by_backend_id(session)
        availability = await _probe_availability(session, backends)
        # T-71-02 per-lane isolation: a compute ``is_available`` probe can fail at the DB layer
        # (not just time out), poisoning the shared session. Clear it after the fan-out -- the
        # snapshot does no writes, so a rollback here is safe -- so one bad lane degrades to
        # ``available=False`` (via ``_probe_one``) instead of poisoning the subsequent
        # ``in_flight_count`` reads and collapsing the WHOLE grid to the ``[]`` degrade panel.
        await session.rollback()
        lanes: list[dict[str, Any]] = []
        for backend in backends:
            kind = _kind_of(backend)
            if kind == "local":
                queued, working = await _local_lane_queued_working(session, app_state)
                active = working
                processed_24h, processed_lifetime = await _lane_processed_counts(session, backend_id=None)
            else:
                queued, working = await _cloud_lane_queued_working(session, backend.id)
                active = await _cloud_lane_active(session, backend.id, kind)
                processed_24h, processed_lifetime = await _lane_processed_counts(session, backend_id=backend.id)
            lanes.append(
                {
                    "id": backend.id,
                    "kind": kind,
                    "rank": backend.rank,
                    "cap": backend.cap,
                    "in_flight": await backend.in_flight_count(session),
                    "available": availability.get(backend.id, False),
                    "queued": queued,
                    "working": working,
                    "active": active,
                    "processed_24h": processed_24h,
                    "processed_lifetime": processed_lifetime,
                    **admission.get(backend.id, _ZERO_ADMISSION),
                }
            )
        lanes.sort(key=lambda lane: (lane["rank"], lane["id"]))
    except Exception:
        logger.warning("backend_lane_snapshot_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_lane_snapshot_rollback_failed", exc_info=True)
        return []
    return lanes


def derive_localqueue_unreachable(lanes: list[dict[str, Any]]) -> bool:
    """Return True iff ANY kueue lane in ``lanes`` is unreachable -- the K8s LocalQueue amber alert (D-05).

    Phase 56/70 (KDEPLOY-04, MKUE-01/03) originally drove this from a cross-process Redis flag the
    controller's startup probe wrote once at boot (D-05/D-06). phaze-6r39 retired that mechanism: the
    flag was a boot-time SNAPSHOT with no TTL and no other writer, so it (a) never cleared once
    connectivity was restored (the reported bug) and (b) never appeared at all for an outage that began
    AFTER boot -- the alert was structurally incapable of firing for the exact class of event it exists
    to surface. Every 5s ``/pipeline/stats`` poll already probes the SAME LocalQueue live via
    :func:`get_backend_lane_snapshot` -> ``_probe_availability`` -> ``KueueBackend.is_available``, so
    this derives the flag from that snapshot instead of a second, staler read.

    Preserves the original aggregate semantic: unreachable iff ANY configured kueue backend is
    unreachable (reachable == ALL-reachable). Zero kueue lanes (all-local / compute-only, the WR-01
    case) makes ``any(...)`` False for free -- no special-casing needed, and no Redis key is read or
    written on this path at all any more. ``lanes`` is already degrade-safe (``get_backend_lane_snapshot``
    -> ``[]`` on any error), so a fully degraded snapshot silently reports "reachable" rather than
    surfacing a false alarm -- the same fail-silent posture the retired Redis-flag reader
    (``get_localqueue_unreachable``, removed by this change) used to provide on a Redis hiccup.
    """
    return any(lane["kind"] == "kueue" and not lane["available"] for lane in lanes)


# The neutral, no-causal-claim copy any unexpected error in derive_cloud_hold_reason degrades to --
# the card must never assert a specific blocker it has not actually confirmed.
_HOLD_REASON_DEGRADED = "held"


async def _get_backend_routing_snapshot(session: AsyncSession, cfg: ControlSettings) -> list[dict[str, Any]] | None:
    """Return only the availability/capacity state the cloud hold gate consumes.

    Unlike :func:`get_backend_lane_snapshot`, this does not read admission buckets, queue depths, or
    processed history. ``None`` means degraded; an empty list is an observed empty registry.
    """
    try:
        backends = resolve_backends(cfg)
        availability = await _probe_availability(session, backends)
        await session.rollback()
        return [
            {
                "id": backend.id,
                "kind": _kind_of(backend),
                "cap": backend.cap,
                "in_flight": await backend.in_flight_count(session),
                "available": availability.get(backend.id, False),
            }
            for backend in backends
        ]
    except Exception:
        logger.warning("backend_routing_snapshot_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_routing_snapshot_rollback_failed", exc_info=True)
        return None


async def derive_cloud_hold_reason(session: AsyncSession) -> str:
    """Return the Cloud Routing card's truthful sub-caption, mirroring the drain's own gate order.

    The routing-only snapshot uses the SAME backend implementations and ``{available, cap,
    in_flight}`` inputs as :func:`get_backend_lane_snapshot`, without paying for unrelated lane-card
    metrics. This checks the SAME gates ``release_awaiting_cloud.stage_cloud_window`` checks, in order:
    cloud disabled -> :func:`get_route_control` force-local -> no lane reachable -> every reachable
    lane full -> no fileserver agent online -> else genuinely queued with free capacity. A prior
    incarnation of this card hardcoded "no compute agent online" regardless of the real blocker
    (T-83-hold-reason-bug); this derivation can only ever name a gate it has actually observed.

    Every read here is individually degrade-safe (``get_route_control`` / the routing snapshot
    never raise), but the surrounding ``try/except`` is the belt: ANY unexpected exception -- including
    one from ``get_settings()`` or the fileserver probe -- collapses to the neutral
    :data:`_HOLD_REASON_DEGRADED` copy with NO causal claim, so the hot 5s poll can never 500 on a
    hiccup here (mirrors the T-71-03 idiom this module already applies throughout).
    """
    try:
        cfg = cast("ControlSettings", get_settings())
        if not cfg.cloud_enabled:
            return "cloud routing disabled"
        if await get_route_control(session):
            return "held — cloud routing paused (force-local)"

        lanes = await _get_backend_routing_snapshot(session, cfg)
        if lanes is None:
            return _HOLD_REASON_DEGRADED
        # phaze-g4fh: restrict reachability/capacity math to CLOUD lanes. A local lane is always
        # `available=True` with `in_flight=0` (LocalBackend.is_available/in_flight_count), so
        # including it here made `available_lanes` never empty and `free_slots` always ≥1 -- both
        # the "no cloud backend reachable" and "all lanes at capacity" branches below were dead code
        # even when every real cloud lane was online-but-full. select_backend gates local behind
        # `cloud_spill_to_local_after_seconds` staleness (D-01/D-03), so local is a delayed safety
        # net the drain would NOT dispatch to next tick -- it is not free cloud capacity, and this
        # derivation must mirror exactly what the drain would do next tick (T-83 anti-goal).
        available_lanes = [lane for lane in lanes if lane["kind"] != "local" and lane["available"]]
        if not available_lanes:
            return "held — no cloud backend reachable"

        total_cap = sum(lane["cap"] for lane in available_lanes)
        total_in_flight = sum(lane["in_flight"] for lane in available_lanes)
        free_slots = sum(max(0, lane["cap"] - lane["in_flight"]) for lane in available_lanes)
        if free_slots <= 0:
            return f"held — all lanes at capacity ({total_in_flight}/{total_cap} slots busy)"

        try:
            await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            return "held — no fileserver agent online"

        return f"queued — {free_slots} free slots, dispatching on next drain tick (~5 min)"
    except Exception:
        logger.warning("cloud_hold_reason_degraded", exc_info=True)
        return _HOLD_REASON_DEGRADED


# --- Phase 88 (88-02, DRILL-01): degrade-safe lane-detail data helpers ---------------------
#
# Two bounded, read-only, secret-free reads that feed the `GET /pipeline/lanes/{backend_id}` body
# (_lane_detail.html). Both degrade to [] / 0 on any error so they can NEVER 500 the drill-in pane's
# own 5s tick (D-00b / PERF-01). Neither exposes any config/SecretStr/kube token -- only the CloudJob
# status/timestamp/file_id scalars and the broker depth counts.

# D-07: the fixed last-N cap on the recent-completions list -- predictable render cost under any
# throughput (no whole-corpus scan per poll). Newest-first, bounded by this LIMIT.
LANE_RECENT_N = 20


@dataclasses.dataclass(frozen=True)
class LaneCompletion:
    """One recent-completion row for a lane's recent-completions panel (phaze-2u8v.3).

    Fixes three defects found in the same panel:

    * ``label`` is the file's own name (``COALESCE(original_filename_repaired, original_filename)``),
      never a bare identifier. The panel previously rendered ``CloudJob.file_id.hex[:8]`` unlabelled and
      Robert could not tell what it was -- confirmed (ground truth query against the live archive) that
      it is a PREFIX OF THE FILE's UUID PRIMARY KEY (``files.id``), NOT a sha256 prefix as presumed --
      ``files.sha256_hash`` is a wholly separate column this code never touched. ``id`` is kept below so
      a caller/template can still compose an explicitly-labelled id fallback (never a bare hash/hex).
    * ``completed_at`` is the TRUE analysis completion time -- ``AnalysisResult.analysis_completed_at``,
      stamped synchronously by the ``/api/internal/agent/analysis/{file_id}`` callback at the instant
      analysis actually finished. For a kueue lane this is NOT the same instant as the previous
      ``CloudJob.updated_at`` this code used to render: ``cloud_job.status`` only flips to ``succeeded``
      the NEXT time ``reconcile_cloud_jobs`` runs, and that cron is registered
      ``CronJob(reconcile_cloud_jobs, cron="*/5 * * * *")`` (``tasks/controller.py``) -- a fixed-minute
      schedule, not an interval since the triggering event. So every kueue completion's ``updated_at``
      lands on the tick that happened to notice it, which is always exactly :00/:05/:10/.../:55 past the
      hour (confirmed against the live archive: rows read e.g. completed_at 20:05:21 but
      cloud_job.updated_at 20:10:00.03 -- the very next 5-minute tick) -- explaining the reported
      clustering. ``completed_at`` falls back to ``CloudJob.updated_at`` only in the defensive case where
      no ``AnalysisResult`` row is found (should not happen for a row this code selects as succeeded).
    """

    id: uuid.UUID
    label: str
    completed_at: datetime | None


def _completion_label(filename: str | None, file_id: uuid.UUID) -> str:
    """Return the human-legible completion label: the filename, or an explicitly-labelled id fallback.

    ``FileRecord.original_filename`` is a NOT NULL column, so the fallback below is defense-in-depth
    (an empty string, or a row this helper reaches through some future join this docstring doesn't
    anticipate) rather than the expected path. It is spelled ``file <hex> (id, not a hash)`` -- never a
    bare hex string -- so a fallback can never be mistaken for the sha256 the original bug presumed.
    """
    if filename:
        return filename
    return f"file {file_id.hex[:8]} (id, not a hash)"


# Two separately-typed base queries (kept apart, not a shared variably-shaped `stmt`, so each stays a
# single concrete `Select[...]` type -- mypy flags an in-place reassignment across an if/else with
# different column tuples). Both are `.limit()`-completed at the call site (the local one takes no
# extra WHERE; the cloud one is additionally scoped by backend_id/status there).
_LOCAL_RECENT_COMPLETIONS_SQL = (
    select(
        FileRecord.id,
        func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
        AnalysisResult.analysis_completed_at,
    )
    .select_from(FileRecord)
    .join(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
    .where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        AnalysisResult.analysis_completed_at.isnot(None),
        ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id)),
    )
    .order_by(AnalysisResult.analysis_completed_at.desc(), FileRecord.id.desc())
)

_CLOUD_RECENT_COMPLETIONS_SQL = (
    select(
        CloudJob.id,
        CloudJob.file_id,
        func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("label"),
        AnalysisResult.analysis_completed_at,
        CloudJob.updated_at,
    )
    .select_from(CloudJob)
    .join(FileRecord, FileRecord.id == CloudJob.file_id)
    .outerjoin(AnalysisResult, AnalysisResult.file_id == CloudJob.file_id)
    .order_by(CloudJob.updated_at.desc(), CloudJob.id.desc())
)


async def get_lane_recent_completions(session: AsyncSession, backend_id: str, kind: str, limit: int = LANE_RECENT_N) -> list[LaneCompletion]:
    """Return up to ``limit`` most-recent completions for ANY lane kind, newest-first (D-07, phaze-2u8v.3).

    A ``local`` lane no longer returns ``[]`` unconditionally (Open Question 1's "omit, don't fabricate"
    resolution masked a real defect: the archive genuinely completes local work continuously -- 1501
    locally-completed files with no ``cloud_job`` row confirmed live -- and the panel showed "No
    completions" throughout). A :class:`LocalBackend` still writes NO ``cloud_job`` row (synchronous,
    no cron read), so local completions are read straight off ``files`` + ``analysis`` instead: a music/
    video file whose analysis has landed (``analysis_completed_at IS NOT NULL``) AND that carries no
    ``cloud_job`` row at all (mirrors the same local/cloud discriminator :meth:`LocalBackend.in_flight_count`
    already established -- the one documented gap there, a compute file's brief post-push window, is
    equally out of scope here). For compute/kueue lanes the query is unchanged in shape (``backend_id`` +
    ``status='succeeded'``, D-07 LIMIT) but now outer-joins ``analysis`` for the TRUE completion instant
    (see :class:`LaneCompletion`) and joins ``files`` for the display name. Any query error degrades to
    ``[]`` with a guarded rollback so it can never raise into the hot 5s tick (D-00b). Secret-free: only
    filename/timestamp/id scalars leave here.

    Ordering keeps its existing, already-regression-tested tiebreaker shape: local orders by
    ``analysis_completed_at`` DESC + ``FileRecord.id`` DESC; compute/kueue orders by ``CloudJob.updated_at``
    DESC + ``CloudJob.id`` DESC (unchanged from before this fix -- a monotonic-enough proxy for recency
    since reconcile ticks themselves run in increasing chronological order). Either way a partial ORDER BY
    alone would leave boundary ties in ANY order (heap order, which shifts with page layout, vacuum, and
    plan choice); appending the unique id makes the order TOTAL, so the LIMIT boundary is deterministic
    across repeated calls (mirrors the paging contract's mandatory unique tiebreaker).
    """
    try:
        if kind == "local":
            local_rows = (await session.execute(_LOCAL_RECENT_COMPLETIONS_SQL.limit(limit))).all()
        else:
            cloud_rows = (
                await session.execute(
                    _CLOUD_RECENT_COMPLETIONS_SQL.where(
                        CloudJob.backend_id == backend_id,
                        CloudJob.status == CloudJobStatus.SUCCEEDED.value,
                    ).limit(limit)
                )
            ).all()
    except Exception:
        logger.warning("lane_recent_completions_degraded", backend_id=backend_id, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("lane_recent_completions_rollback_failed", backend_id=backend_id, exc_info=True)
        return []

    if kind == "local":
        return [
            LaneCompletion(id=row_id, label=_completion_label(label, row_id), completed_at=completed_at) for row_id, label, completed_at in local_rows
        ]
    return [
        LaneCompletion(id=cloud_job_id, label=_completion_label(label, file_id), completed_at=(analysis_completed_at or cloud_job_updated_at))
        for cloud_job_id, file_id, label, analysis_completed_at, cloud_job_updated_at in cloud_rows
    ]


# phaze-2u8v.1: the operator-facing copy for each way a lane can legitimately have NO per-tier SAQ
# figure. These are rendered INSTEAD of "analyze 0 · meta 0 · io 0" -- a fabricated
# zero row is indistinguishable from a genuinely idle agent, which is precisely how a saturated lane
# came to read as idle on every panel. Say WHY there is no number; never invent one.
KUEUE_NO_SAQ_QUEUE_NOTE = "Not applicable — a Kueue lane runs k8s Jobs, not SAQ agent-queue work."
NO_FILESERVER_AGENT_NOTE = "Unavailable — no live fileserver agent to read lane queues from."


@dataclasses.dataclass(frozen=True)
class LaneQueueIdentity:
    """WHICH agent's SAQ lane queues carry a compute-lane's work -- or WHY the lane has none (phaze-2u8v.1)."""

    agent_id: str | None
    note: str | None = None


@dataclasses.dataclass(frozen=True)
class LaneQueueDepths:
    """A lane's per-tier SAQ depths plus the identity they were read from (phaze-2u8v.1).

    ``depths is None`` means the lane has NO SAQ agent queue at all and ``note`` says why; it does NOT
    mean "zero". Callers must render the note, never a zero row.
    """

    agent_id: str | None
    depths: dict[str, int] | None
    note: str | None = None


async def resolve_lane_queue_agent(session: AsyncSession, backend_id: str, kind: str) -> LaneQueueIdentity:
    """Return the AGENT whose ``phaze-agent-<agent_id>-<lane>`` queues carry ``backend_id``'s work (phaze-2u8v.1).

    THE BUG THIS EXISTS TO CLOSE. ``AgentTaskRouter.queue_for``'s first parameter is an AGENT id, and a
    lane's registry ``id`` is NOT one. phaze-tbps established that for ``kind == "compute"`` (resolve the
    bound ``agent_ref``) but deliberately left local/kueue passing the raw ``backend_id`` through -- so a
    registry of ``[local, kueue vox]`` built ``phaze-agent-local-*`` and ``phaze-agent-vox-*``, queues no
    producer writes and no worker consumes. SAQ's ``count`` returns 0 for a queue that does not exist
    (not an error), so BOTH lane panels rendered "analyze 0 · meta 0 · io 0" while the
    agent-detail aggregate over the SAME work read the real figure off ``phaze-agent-<fileserver>-*``.
    A fully saturated lane was indistinguishable from an idle one.

    The resolution mirrors what each backend's ``dispatch`` ACTUALLY enqueues to -- that is the only
    definition of "this lane's queue" that cannot drift:

    * **local** -- :meth:`LocalBackend.dispatch` resolves ``select_active_agent(kind="fileserver")`` and
      enqueues ``process_file`` to ``queue_for(agent.id, ...)``. So a local lane's queues are the LIVE
      FILESERVER AGENT's queues, and a local lane legitimately reports the same per-tier figures as that
      agent's own detail pane -- they are the same queues, read twice. With no live fileserver agent
      there is nothing to read: report :data:`NO_FILESERVER_AGENT_NOTE`, not zeros (a dead fileserver is
      the one moment "0 queued" would be most dangerously wrong).
    * **compute** -- :meth:`ComputeAgentBackend` / ``routers/agent_push.py`` enqueue to the bound
      ``agent_ref``; unchanged from phaze-tbps, including its raw-``backend_id`` fallback when the
      registry read hiccups or the id names no compute entry.
    * **kueue** -- a Kueue lane enqueues NOTHING onto a ``phaze-agent-*`` queue. ``dispatch`` stages to
      S3 and submits a k8s Job; the analysis runs in that Job, which consumes no SAQ queue. Its ONE SAQ
      job (``s3_upload``) rides the FILESERVER agent's shared ``io`` lane alongside every other kueue
      lane's, so it is not attributable to one cluster -- keying a kueue lane off the fileserver would
      double-count it across N clusters AND paste the fileserver's local ``analyze`` backlog onto a
      cluster that is not running it. ``KueueBackend.agent_ref`` is NOT an escape hatch either: it names
      a bearer-token callback Agent row for the ``job_runner`` pods (phaze-ifcr) that no producer ever
      enqueues to, so keying off it would restore the exact silent-zero shape this fixes. The honest
      answer is :data:`KUEUE_NO_SAQ_QUEUE_NOTE`.

    Degrade-safe (D-00b): the local branch's ``select_active_agent`` read runs inside a SAVEPOINT so an
    error rolls back the NESTED scope ALONE -- recovering the aborted transaction WITHOUT expiring the
    caller's already-loaded lane/``CloudJob`` rows (a plain ``session.rollback()`` would) -- and returns
    a note. This never raises into the lane pane's 5s tick.
    """
    if kind == "local":
        try:
            # SAVEPOINT degrade (CR-01 / D-00b): a NoActiveAgentError (or any DB hiccup) rolls back the
            # nested scope alone. The read is read-only, so the rollback discards nothing.
            async with session.begin_nested():
                agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            return LaneQueueIdentity(agent_id=None, note=NO_FILESERVER_AGENT_NOTE)
        except Exception:
            logger.warning("lane_queue_identity_degraded", backend_id=backend_id, kind=kind, exc_info=True)
            return LaneQueueIdentity(agent_id=None, note=NO_FILESERVER_AGENT_NOTE)
        return LaneQueueIdentity(agent_id=agent.id)

    if kind == "kueue":
        return LaneQueueIdentity(agent_id=None, note=KUEUE_NO_SAQ_QUEUE_NOTE)

    # compute (and any future agent-backed kind): phaze-tbps, verbatim.
    queue_key = backend_id
    try:
        cfg = cast("ControlSettings", get_settings())
        compute_backend = resolve_compute_backend(cfg, backend_id)
        if compute_backend is not None and compute_backend.agent_ref:
            queue_key = compute_backend.agent_ref
    except Exception:
        logger.warning("lane_queue_agent_ref_resolution_degraded", backend_id=backend_id, exc_info=True)
    return LaneQueueIdentity(agent_id=queue_key)


async def get_lane_queue_depths(session: AsyncSession, app_state: Any, backend_id: str, kind: str) -> LaneQueueDepths:
    """Return per-lane-tier queue depth ``{analyze, meta, io}`` for a lane's backing agent.

    Mirrors the ``get_queue_activity`` idiom (services/pipeline.py): each tier's depth is
    ``count("queued") + count("active")`` on the ``phaze-agent-<agent_id>-<lane>`` Queue of the agent
    :func:`resolve_lane_queue_agent` binds to this lane (read that docstring -- the binding IS the fix).
    Only the ``queued`` / ``active`` kinds are read (scheduled/cron jobs excluded). Every tier is isolated
    in its own ``try/except -> 0`` so a missing ``app.state.task_router`` (the test lifespan-skip) or a
    broker hiccup degrades that tier to 0 and NEVER 500s the 5s tick (D-00b). Bounded: one count pair per
    tier, no corpus scan.

    A lane with NO SAQ agent queue (kueue; or local with no live fileserver) returns ``depths=None`` and a
    ``note`` -- NOT a zero row. ``queue_for`` is not called at all in that case, so no phantom queue name
    is ever constructed.

    phaze-en7s7: connect-before-count (#217), the same fix applied to the sibling reader
    :func:`phaze.services.pipeline.get_agent_lane_depths`. ``queue_for`` constructs the lane's
    ``PostgresQueue`` with its psycopg pool ``open=False``; unless something else (the dashboard
    poll, an API-side enqueue on that exact lane) has already connected it, ``count()`` raises
    ``PoolClosed`` and the per-tier ``except`` below silently degrades it to 0 -- this docstring
    already claimed to "mirror the get_queue_activity idiom", which stopped being true the moment
    #217 added ``connect()`` there and not here. A lane never touched by an API-side enqueue is
    GUARANTEED to construct a virgin closed pool, so this was not a rare race but the common case
    for a lane :func:`resolve_lane_queue_agent` binds fresh. ``connect()`` is idempotent (SAQ
    guards on ``self._connected``).
    """
    identity = await resolve_lane_queue_agent(session, backend_id, kind)
    if identity.agent_id is None:
        return LaneQueueDepths(agent_id=None, depths=None, note=identity.note)

    depths: dict[str, int] = dict.fromkeys(LANES, 0)
    for lane in LANES:
        try:
            queue = app_state.task_router.queue_for(identity.agent_id, lane)
            await queue.connect()
            depths[lane] = await queue.count("queued") + await queue.count("active")
        except Exception:
            depths[lane] = 0
            logger.warning("lane_queue_depth_degraded", backend_id=backend_id, agent_id=identity.agent_id, lane=lane, exc_info=True)
    return LaneQueueDepths(agent_id=identity.agent_id, depths=depths)
