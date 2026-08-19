"""Narrow every-minute in-flight K8s reconcile cron (Phase 54, Plan 06 -- KSUBMIT-02..06, D-01..D-08).

THE SAFETY NET THAT OWNS THE KUEUE JOB LIFECYCLE. ``submit_cloud_job`` (Plan 05) does ONE fast kube
POST and returns; the one-shot pod runs the analysis and PUTs its result back through the existing
``/api/internal/agent/analysis/{file_id}`` callback -- which is the SOLE authoritative result writer
(KSUBMIT-03). This cron NEVER writes an analysis result. It is a cron-only POLL (D-01): every tick it
re-reads the in-flight Jobs/Workloads; there is NO live kube watch stream.

Iteration source (D-02): ``SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)`` -- the durable
``cloud_job`` sidecar is the in-flight registry, NOT a kube watch and NOT ``recover_orphaned_work``.
For each row it reads the Job (succeeded/failed -- the most direct terminal signal) and, when the Job
is not yet terminal, the paired Kueue Workload (admission state: Pending vs Inadmissible vs Evicted vs
Admitted) and maps the ``(type, status, reason)`` condition tuples to an outcome (RESEARCH
§Status->Outcome Mapping).

The load-bearing correctness property is the delete-after-record ordering (D-04): on a terminal
outcome phaze records the result in the DB and COMMITS *before* it deletes the Job, so the status read
can never lose to GC -- ``ttlSecondsAfterFinished`` (900s) is only the never-reconciled backstop. On a
no-callback terminal (Failed/Evicted) it also deletes the staged S3 object (D-05); the success path
does NOT (the callback already deleted it inline). A no-callback terminal under the cap re-drives a
fresh ``submit_cloud_job`` (D-08); at the cap the cloud_job sidecar is re-stamped ``status='awaiting'`` via
the single spill-mode writer (``hold_awaiting_cloud``, D-04/D-12) with NO ``FileRecord.state`` write, so the
next drain tick routes it to the local safety net (``attempts >= cap``) rather than hard-failing.
Inadmissible (operator misconfig) holds indefinitely + alerts and NEVER consumes
the cap (D-06/D-07); healthy Pending is silent.

phaze-32wz: the re-drive (D-08) only ENQUEUES the fresh ``submit_cloud_job`` -- it does not (cannot)
confirm the re-submitted Job exists in the same transaction, since the actual kube POST runs later on
the controller queue. The row's state model explicitly distinguishes three things a ``None`` Job read
can mean, rather than collapsing them: **pending confirmation** (a resubmit was just enqueued and
hasn't run yet -- recorded by clearing ``kueue_workload``, held quietly with NO attempt charged),
**confirmed vanished** (pending confirmation that outlived the pending-submit bound with still no Job --
NOW a genuine no-callback terminal), and **terminal** (a real Failed/Evicted signal from the Job or
Workload itself). Only "confirmed vanished" and "terminal" burn a re-drive attempt.

phaze-1q4g -- A NODE-LOSS RE-DRIVE IS BOUNDED, NOT FREE. ``cloud_job.attempts`` is the file's ANALYZE
retry budget; a re-drive taken because the pod died WITH ITS NODE does not charge it (an
infrastructure fault is not the file's fault). "Not charged" had silently become "NOT BOUNDED": one
pathological file produced EIGHT pods over five days against a cap of three, taking the burst node
down on every one (spike ``phaze-wcrb`` §5). That path now spends its OWN counter,
``cloud_job.node_loss_redrives``, against its OWN (tightest) ceiling ``cloud_node_loss_max_redrives``
-- so the cases stay distinguishable on the row itself, and the total pods one row can ever produce
is ``1 + cloud_submit_max_attempts + cloud_node_loss_max_redrives``. At the node-loss ceiling the row
takes the SAME terminal as the attempts cap (spill to ``'awaiting'`` with ``attempts=cap``): out of
IN_FLIGHT, cloud-ineligible, drain-owned, routed to the local safety net -- never left SUBMITTED or
RUNNING, which is the shape that strands. The other half of the same defect is in
``kube_staging.build_job_manifest``: ``backoffLimit: 0`` bounds *counted failures*, not *pod
creations*, so the default ``podReplacementPolicy: TerminatingOrFailed`` was free to mint replacement
pods for a pod stuck Terminating on a dead node -- a re-drive phaze never made and could not count.
``podReplacementPolicy: Failed`` closes it.

AUDIT (phaze-1q4g acceptance -- can any OTHER branch re-drive without charging a budget?). Every
re-drive path in the system was walked; findings, including the negatives:

* ``_reconcile_one`` phantom-row hold (``kueue_workload IS NULL``, fresh) -- holds WITHOUT charging,
  by design, but creates NO pod and cannot loop: it is bounded by
  :data:`PENDING_SUBMIT_CONFIRMATION_SECONDS`, after which it terminalizes through the charged path.
  NOT a bypass.
* ``_handle_no_callback_terminal`` still-terminating deferral -- returns with NO budget charged and NO
  pod created; the next tick re-decides. NOT a bypass. (phaze-mwbz3: it DOES stash the classified
  node-loss verdict, if any, onto ``cloud_job.node_loss_pending`` so the eventual re-drive still spends
  the right counter -- that write is a durable RECORD of the verdict, not a charge against either
  budget.)
* ``Inadmissible`` hold (D-06/D-07) -- deliberately never consumes the cap, and deliberately creates
  no pod: it holds one Job that is not admitting. NOT a bypass.
* ``KueueBackend._reap_stranded_staging`` -- charges ``min(attempts + 1, cap)``. Bounded.
* ``submit_cloud_job`` SAQ retries -- idempotent (deterministic Job name + 409->refresh + a CAS'd
  upsert), so N retries converge on ONE Job. Not a pod multiplier.
* **A REAL SECOND BYPASS, found here and FIXED by phaze-2mwyo (migration 055).** ``attempts`` lived only
  on the ``cloud_job`` row, and ``routers/agent_analysis``'s D-14 reaper DELETES that row
  (``DELETE FROM cloud_job WHERE file_id = ... AND status = 'awaiting'``) on BOTH analyze-terminal
  seams -- the analysis-result PUT and the analysis-failure POST. A file that spent its cloud budget,
  spilled to local, and then failed locally therefore lost its entire attempt history: a later
  re-analysis of the same file started a FRESH chain with ``attempts = 0``, and the ceiling this bead
  bounds reset with it. That is consistent with the production evidence -- ``713a368e``'s eight pods
  are two chains of four, 07-24..07-25 and 07-29, with a four-day gap between them, while the other
  three files show exactly one chain of four each. Fixing it was NOT a one-line WHERE-clause change:
  the reaper exists to stop ``ix_cloud_job_awaiting`` scanning a monotonically growing dead set at
  200K rows, and simply retaining budget-spent rows recreates the phaze-9sqa head-of-line poison the
  drain was paginated to walk past. So the reaper is UNCHANGED and the budget moved instead: the
  ``cloud_budget`` table (``models/cloud_budget.py``) is a durable per-file ledger the reaper has no
  reason to touch, folded exactly once per chain by ``hold_awaiting_cloud`` -- including the at-ceiling
  spill below -- and read by ``select_backend`` alongside this row's ``attempts``. This file's ceiling
  still bounds ONE chain; that ledger bounds how many chains there may be.

phaze-202e -- NO WALL CLOCK MAY KILL A RUN. There is no ``activeDeadlineSeconds`` on the Job by
default and no age-based terminal for a Job-backed row anywhere in this file. A wedged row is found by
POD STATE (:func:`_pod_wedge_reason` -> ``kube_staging.classify_job_pods``): a fatal container waiting
reason, scheduling that has failed past a probe, or an un-suspended Job with no pod at all. A pod that
is Running is NEVER terminalized, at any age. The one surviving age rule bounds SUBMIT machinery, not
work: a row with no ``kueue_workload`` has no pod anywhere, so
:data:`PENDING_SUBMIT_CONFIRMATION_SECONDS` can only reclaim bookkeeping, never kill an analyze. This
reverses phaze-1b39's deadline-and-slack design, which killed every 2-6 h concert-set analyze at
exactly 3h and burned each file's whole cloud attempt budget (incident 2026-07-28); the wedged-pod
protection 1b39 wanted is preserved, the collateral damage is not.

CONTROL-ONLY: needs PostgreSQL (``ctx["async_session"]``) + the controller queue (``ctx["queue"]``) for
the re-drive enqueue, and the kube surface via ``kube_staging`` -- exactly like ``stage_cloud_window`` /
``recover_orphaned_work``. Register ONLY in ``phaze.tasks.controller`` (``tests/shared/core/test_task_split.py``
enforces the agent worker stays free of it). FastAPI-free: imports neither ``fastapi`` nor
``phaze.routers``. DO NOT re-add a general auto-advance / ``recover_orphaned_work`` cron here -- this is
narrow, in-flight K8s reconcile ONLY (mirror the ``controller.py`` cron-scope guard comments).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import kr8s
from sqlalchemy import select
import structlog

from phaze.config import get_settings
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from phaze.tasks.submit_cloud_job import submit_cloud_job_key


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import KubeConfig


logger = structlog.get_logger(__name__)


# The Kueue Workload condition vocabulary the loop matches (RESEARCH §Status->Outcome Mapping,
# verified against Context7 /kubernetes-sigs/kueue). Matching the exact (type, status, reason) tuples
# is what keeps healthy Pending from being mistaken for a fault (Pitfall 3).
_TYPE_QUOTA_RESERVED = "QuotaReserved"
_TYPE_ADMITTED = "Admitted"
_TYPE_EVICTED = "Evicted"
_REASON_PENDING = "Pending"
_REASON_INADMISSIBLE = "Inadmissible"

# phaze-202e: how long a row may sit with NO ``kueue_workload`` recorded -- the pending-confirmation
# bound, and the ONLY remaining age-based rule in this file. It is NOT a bound on a run: a row with no
# Job name has no pod anywhere doing work, so nothing can be killed by it. It bounds the SUBMIT
# machinery (a submit that crashed between the row insert and the workload stamp, or a re-drive whose
# freshly-enqueued ``submit_cloud_job`` never executed). Sized above the submit job's own SAQ budget
# (worker_job_timeout 600s x the retry budget) so a merely-retrying submit is never stolen.
PENDING_SUBMIT_CONFIRMATION_SECONDS = 3600

# phaze-202e: how long an UN-SUSPENDED, non-terminal Job may report zero pods before it counts as a
# wedge. This is the "Workload wedged Admitted with no pod" shape phaze-1b39 also had to cover. Safe
# without a run clock because it fires ONLY when the Job itself reports no active pod AND the pod list
# is empty -- i.e. nothing is running to kill. Measured on the Job's ``status.startTime`` (when Kueue
# un-gated it), never on how long an analysis has run. The 15-minute wall-clock bound is preserved
# across the every-minute reconcile cadence (15 ticks; phaze-i3pkb.1).
NO_POD_PROBE_SECONDS = 900


class Wedge(NamedTuple):
    """A terminalizing pod verdict: WHY the pod is dead, and whether its NODE is what killed it.

    phaze-1q4g split this off the bare ``str`` :func:`_pod_wedge_reason` used to return, because the
    two causes must be budgeted differently. ``node_loss=True`` means the pod died WITH ITS NODE
    (:attr:`kube_staging.PodLiveness.NODE_LOST`) -- an infrastructure fault that is not the file's
    fault, so it charges :data:`CloudJob.node_loss_redrives` against ``cloud_node_loss_max_redrives``
    instead of ``attempts`` against ``cloud_submit_max_attempts``. ``node_loss=False`` is an ordinary
    dead-before-start / unschedulable / zero-pod wedge and is charged exactly as before.
    """

    reason: str
    node_loss: bool


async def _terminal_node_loss_reason(name: str, kube: KubeConfig) -> str | None:
    """Return a node-loss reason when the Job's own pods say the NODE took them, else None (phaze-1q4g).

    The Job-terminal branches (a Job reading Failed, a Workload reading Evicted) know only THAT the
    work ended, never WHY -- with ``backoffLimit: 0`` a pod disrupted by its node reads exactly like a
    pod whose analysis crashed. This asks the pods, which do carry the distinction
    (``status.reason=NodeShutdown/NodeLost/...`` or a ``DisruptionTarget`` condition).

    One extra list call, on the TERMINAL path only (never on the healthy per-tick read), and it MUST
    NOT raise: a classification is a refinement of a terminal the caller has ALREADY decided on, so
    letting a kube hiccup escape would abort that terminal, hand the row to the per-row rollback guard,
    and leave it in-flight holding a burst-lane slot -- trading a bounded retry for a wedged row.
    ``contextlib.suppress`` therefore degrades any failure to None, i.e. "cannot prove node loss" ->
    charge the ordinary attempt budget, which is exactly the pre-phaze-1q4g behaviour. Being wrong in
    that direction costs one retry; being wrong the other way would hand a node-killing file the looser
    of the two budgets, which is the whole defect this bead closes.
    """
    pods: list[Any] = []
    with contextlib.suppress(Exception):
        pods = await kube_staging.list_pods_for_job(name, kube)
    if kube_staging.classify_job_pods(pods) is not kube_staging.PodLiveness.NODE_LOST:
        return None
    return f"node_lost ({kube_staging.describe_job_pods(pods)})"


def _row_age_seconds(cloud_job: CloudJob) -> float:
    """Seconds since the row's last state transition (``updated_at``).

    ``updated_at`` carries ``onupdate=func.now()``, which fires ONLY on a real UPDATE -- and every
    in-flight branch below mutates only when a field actually changes -- so a row re-affirmed RUNNING
    tick after tick keeps the timestamp of the transition INTO RUNNING.

    phaze-202e narrowed the ONE caller of this to the pending-confirmation (no ``kueue_workload``)
    branch. It is deliberately NOT consulted for a row that has a Job: a Job-backed row's liveness is
    decided by POD STATE (:func:`_pod_wedge_reason`), because a wall clock cannot tell a legitimate
    2-6 h concert-set analyze from a hang -- and when phaze-1b39 asked it to, it killed every long
    recording at exactly 3h.

    models/base.py declares the timestamp columns without ``timezone=True``, so ``create_all`` yields
    naive datetimes while a TIMESTAMPTZ migration column hands asyncpg tz-aware ones. Match the row's
    awareness (assume-UTC, the scan_reaper / release_awaiting_cloud convention) so the subtraction can
    never raise inside the cron.
    """
    now = datetime.now(UTC)
    updated = cloud_job.updated_at
    if updated.tzinfo is None:
        now = now.replace(tzinfo=None)
    return (now - updated).total_seconds()


def _job_counter(job: Any, key: str) -> int:
    """Read an integer ``status`` counter (``succeeded``/``failed``) off a Job, defaulting to 0.

    kr8s exposes ``.status`` as a dict; with ``backoffLimit: 0`` a non-zero ``succeeded``/``failed`` is
    the most direct terminal signal (the Job is the source of truth for succeeded-vs-failed).
    """
    status = getattr(job, "status", None) or {}
    try:
        return int(status.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _job_has_true_condition(job: Any, cond_type: str) -> bool:
    """Return whether the Job carries a ``(cond_type, status=True)`` entry in ``status.conditions``."""
    status = getattr(job, "status", None) or {}
    return any(cond.get("type") == cond_type and cond.get("status") == "True" for cond in status.get("conditions", []) or [])


def _workload_condition(workload: Any, cond_type: str) -> dict[str, Any] | None:
    """Return the first ``status.conditions`` entry of ``cond_type`` on a Kueue Workload, or None."""
    status = getattr(workload, "status", None) or {}
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == cond_type:
            return cast("dict[str, Any]", cond)
    return None


async def _job_gone(name: str | None, kube: KubeConfig) -> bool:
    """Return whether the Job ``name`` is gone (deleted) on ``kube``'s cluster -- ``get_job`` returns None or 404s.

    The re-drive race guard (D-08): after ``delete_job`` we confirm the prior Job is GONE before
    enqueuing the fresh ``submit_cloud_job``. If it is still terminating, the deterministic-name
    409->refresh inside ``submit_job`` would re-acquire the still-present Failed Job and the next tick
    would re-see Failed and burn an extra attempt. A real ``get_job`` raises ``NotFoundError`` on a 404
    (the desired end state); the fake-kube seam returns None.

    phaze-1b39: a phantom row (``kueue_workload IS NULL``) has no Job to wait on -- nothing can
    409->refresh under us -- so it is trivially gone.
    """
    if name is None:
        return True
    try:
        job = await kube_staging.get_job(name, kube)
    except kr8s.NotFoundError:
        return True
    return job is None


async def _pod_wedge_reason(job: Any, name: str, kube: KubeConfig) -> Wedge | None:
    """Return a :class:`Wedge` when ``job``'s pod is PROVABLY not working, else None (phaze-202e).

    THE REPLACEMENT FOR THE WALL CLOCK. phaze-1b39 answered "is this row wedged?" with
    ``activeDeadlineSeconds + slack``, which cannot distinguish a 4h concert-set analyze from a hang --
    so in production it killed every long recording at exactly 3h and burned the file's whole cloud
    attempt budget (incident 2026-07-28). This asks the pod instead, and only ever terminalizes on
    positive proof that no work is happening:

    * **the pod died WITH ITS NODE** (phaze-1q4g -- ``PodLiveness.NODE_LOST``: a node-scoped
      ``status.reason`` or a ``DisruptionTarget`` condition). Returned with ``node_loss=True`` so the
      caller charges the SEPARATE, tighter ``cloud_node_loss_max_redrives`` budget rather than the
      file's analyze ``attempts`` -- see :func:`_handle_no_callback_terminal`;
    * a container waiting in a fatal reason (ImagePullBackOff / ErrImagePull / InvalidImageName /
      CreateContainerConfigError -- a bad image, or the missing operator ConfigMap/Secret that was
      1b39's motivating wedge);
    * scheduling that has been failing past the scheduling probe (``PodScheduled=False/Unschedulable``);
    * an un-suspended, non-terminal Job with NO pod at all past :data:`NO_POD_PROBE_SECONDS`. This one
      stays ``node_loss=False``: an empty pod list cannot tell "the node took the pod" from "a pod was
      never created", and inferring node loss from an ABSENCE is exactly how a label drift would hand
      the whole burst lane the wrong budget.

    **A Running pod returns None, always.** :func:`kube_staging.classify_job_pods` short-circuits on
    ALIVE before it looks at any clock, so no age, no cluster, and no config can terminalize genuine
    work through this path.

    The zero-pod probe is guarded three ways so a pod-label drift cannot mass-terminalize a healthy
    burst lane: the classifier must not have said ALIVE, ``list_pods_for_job`` must have returned
    EMPTY (not merely un-alive), the Job must independently report ``status.active == 0``, must be
    un-suspended, and must carry a readable ``status.startTime`` older than the probe. Any of those
    being unreadable holds the row instead (an in-flight row that is merely un-observable is left for
    a later tick -- the pre-1b39 behaviour, minus the permanence).
    """
    pods = await kube_staging.list_pods_for_job(name, kube)
    verdict = kube_staging.classify_job_pods(pods)
    if verdict is kube_staging.PodLiveness.ALIVE:
        return None
    if verdict in (kube_staging.PodLiveness.NODE_LOST, kube_staging.PodLiveness.DEAD_BEFORE_START, kube_staging.PodLiveness.UNSCHEDULABLE):
        return Wedge(f"{verdict.value} ({kube_staging.describe_job_pods(pods)})", verdict is kube_staging.PodLiveness.NODE_LOST)
    if pods or _job_counter(job, "active") != 0 or kube_staging.job_is_suspended(job):
        return None
    started = kube_staging.job_started_at(job)
    if started is None or (datetime.now(UTC) - started).total_seconds() <= NO_POD_PROBE_SECONDS:
        return None
    return Wedge("no_pod (job un-suspended with zero active pods past the probe)", False)


async def _analysis_completed(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Return whether the file's analysis result already landed (``analysis_completed_at IS NOT NULL``).

    phaze-2o8p: the ``/api/internal/agent/analysis/{file_id}`` callback (KSUBMIT-03) stamps
    ``analysis_completed_at`` and deletes the staged S3 object, but NEVER advances ``cloud_job.status``.
    A callback-completed file therefore sits SUBMITTED/RUNNING until reconcile next reads its Job. If
    the reconcile lag exceeds ``ttlSecondsAfterFinished`` (900s) the succeeded Job is GC'd, so the
    vanished-Job path would misclassify a DONE file as a no-callback terminal and re-drive it against a
    staged object the callback already deleted. This lets that path recognise the success instead.
    """
    completed_at = (await session.execute(select(AnalysisResult.analysis_completed_at).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    return completed_at is not None


async def _enqueue_resubmit(ctx: dict[str, Any], file_id: uuid.UUID) -> None:
    """Enqueue a fresh ``submit_cloud_job`` on the controller queue with the deterministic dedup key.

    The re-drive (D-08) is a fresh submit on the controller queue (where the kube creds live);
    ``submit_cloud_job_key`` collapses a still-live submit to a no-op (mirrors the staging-cron dedup).
    Writes NO scheduling-ledger row (KSUBMIT-06) -- the re-drive routes the SAME ``submit_cloud_job``
    that the cloud_job sidecar tracks, never a ``process_file`` ledger seed.
    """
    queue = ctx["queue"]
    await queue.connect()
    await queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))


async def _record_success(session: AsyncSession, cloud_job: CloudJob, name: str | None, tally: dict[str, int], kube: KubeConfig) -> None:
    """Succeeded Job: record SUCCEEDED + COMMIT, THEN delete the Job on ``kube``'s cluster (D-04). No S3 delete, no result.

    The analysis result already landed via the ``/api/internal/agent/*`` callback (KSUBMIT-03), which
    also deleted the staged S3 object inline (D-05) -- so the success path makes ZERO S3 calls and
    NEVER writes an analysis result. Recording + committing before the delete means the status read can
    never lose to GC.
    """
    cloud_job.status = CloudJobStatus.SUCCEEDED.value
    cloud_job.inadmissible = False  # CR-01: a transiently-Inadmissible row that then succeeds must clear the alert flag.
    cloud_job.cloud_phase = CloudPhase.FINISHED.value  # D-04: admission progression terminus (orthogonal to the fault flag).
    await session.commit()
    if name is not None:  # phaze-1b39: a phantom row (kueue_workload IS NULL) has no Job to delete.
        await kube_staging.delete_job(name, kube)
    tally["succeeded"] += 1


async def _handle_no_callback_terminal(
    ctx: dict[str, Any],
    session: AsyncSession,
    cloud_job: CloudJob,
    name: str | None,
    cap: int,
    tally: dict[str, int],
    kube: KubeConfig,
    *,
    node_loss_reason: str | None = None,
) -> None:
    """Failed/Evicted (no-callback terminal): bounded re-drive under cap, spill the sidecar to 'awaiting' at cap (D-08/SCHED-03).

    At cap (``attempts + 1 > cloud_submit_max_attempts``) the ordering is the load-bearing terminal
    sequence (MKUE-04 clean-before-flip, D-01/D-03/D-04): capture the OLD (backend_id, staging_bucket)
    identity, ``delete_staged_object`` the old object UNDER the still-held per-row advisory lock (before
    the spill commit) -> re-stamp the cloud_job sidecar ``status='awaiting'`` via the single spill-mode
    writer (``hold_awaiting_cloud``, D-04/D-12 -- reconcile writes NO ``FileRecord.state``) + clear
    ``staging_bucket`` + COMMIT (which releases the lock) -> ``delete_job`` (post-commit). Deleting the old
    object before the commit that makes the file a drain candidate closes Pitfall 9: a concurrent drain
    tick cannot re-dispatch + re-stage a new object under the same ``file_id`` key until this txn commits,
    so the trailing delete can never destroy the new owner's object. The file is NOT hard-failed on cloud
    flakiness (SCHED-03): because ``cloud_job.attempts`` already equals ``cap``, the next drain tick's
    ``select_backend`` excludes every cloud backend (``attempts >= cap``) and routes the file to the local
    safety net -- ANALYSIS_FAILED then comes only from a local failure (D-04), never from this branch. The
    spilled kueue file stays at its prior ``PUSHED`` state (reconcile no longer touches ``FileRecord``),
    which satisfies the loosened pushed/pushing shadow invariants -- fixing the HARD ``state=AWAITING_CLOUD``
    + ``cloud_job.status=FAILED`` shadow violation that is live on ``main`` today.

    Under cap it is a re-drive: delete the prior Job and CONFIRM it is gone (the race guard) BEFORE
    incrementing ``attempts`` + committing and enqueuing the fresh ``submit_cloud_job``. If the prior
    Job is still terminating the re-drive is deferred to a later tick with NO BUDGET charged -- so no
    extra attempt is burned and the deterministic-name 409->refresh cannot latch onto the dying Job.
    phaze-mwbz3: the deferral DOES persist ``cloud_job.node_loss_pending`` (the classified verdict, if
    any) across the wait -- see :data:`CloudJob.node_loss_pending` -- so a Job that finally vanishes
    between ticks still re-drives against the right counter instead of losing the verdict to the
    vanished-Job branch's blind spot. The staged S3 object is PRESERVED on the re-drive path (the
    re-submitted Job still needs it); it is deleted only on the genuinely-terminal at-cap path.

    phaze-32wz (pending-vs-vanished, the TOCTOU this closes): ``_enqueue_resubmit`` only ENQUEUES the
    fresh ``submit_cloud_job`` -- the actual re-create-and-stamp is asynchronous and runs later, on the
    controller queue. Between this commit and that later run, ``cloud_job`` reads ``status=SUBMITTED``
    with NO Job under its (deterministic, unchanged) name -- indistinguishable from a genuinely-vanished
    terminal unless something records "a resubmit is in flight". Rather than inferring that purely from
    elapsed time, this clears ``cloud_job.kueue_workload`` to ``None`` in the SAME commit as the attempts
    bump: a cleared ``kueue_workload`` on a SUBMITTED/RUNNING row is the durable, explicit
    pending-confirmation record (mirrors ``NULL`` s3_key et al -- state IS the record here, no extra
    column/migration needed). The very next ``_reconcile_one`` tick then reads ``name is None`` and takes
    the SAME phantom-row branch already built for "a submit that crashed between insert and workload
    stamp, or is mid-flight right now" (below) -- which holds quietly (no attempt charged) while fresh,
    and only escalates to ``_handle_no_callback_terminal`` again (a NEW, independently-charged attempt)
    once the pending resubmit has been silent past ``PENDING_SUBMIT_CONFIRMATION_SECONDS``, i.e. is now CONFIRMED vanished
    rather than merely pending. Once the enqueued ``submit_cloud_job`` actually runs, its upsert
    re-stamps ``kueue_workload`` to the fresh Job name (unchanged deterministic string) -- exiting the
    pending state and resuming the normal get_job read on the next tick. No new attempt is burned for
    the SAME re-drive merely waiting on its own enqueue to execute.

    phaze-1q4g -- THE SECOND BUDGET. ``node_loss_reason`` (set when the pod died WITH ITS NODE:
    ``PodLiveness.NODE_LOST``) switches WHICH counter this re-drive spends: ``node_loss_redrives``
    against ``cloud_node_loss_max_redrives``, instead of ``attempts`` against
    ``cloud_submit_max_attempts``. Everything else about the branch -- the delete-then-confirm-gone
    race guard, the ``kueue_workload=None`` pending-confirmation record, the enqueue, and the
    at-ceiling terminal below -- is IDENTICAL, because the two cases differ in whose fault it is, not
    in what has to happen next.

    Why a SEPARATE counter rather than making node loss charge ``attempts``: a node reboot is not the
    file's fault, and spending the file's analyze budget on infrastructure is the mistake phaze-1b39
    made in the other direction (a wall clock burned every long recording's whole cloud budget). Why a
    counter AT ALL, when this path used to charge nothing: "not charged" had silently become "not
    bounded", and a node-loss re-drive is the single case most likely to RECUR -- whatever killed the
    node is still there and the same file is about to meet it again. One file used that to produce
    EIGHT pods over five days against a cap of three, taking the burst node down every time (spike
    ``phaze-wcrb`` §5). The ceiling is deliberately the tightest of the three budgets (default 1): one
    free retry for the genuinely-transient case, and no more. Total pods a row can ever produce is
    therefore ``1 + cloud_submit_max_attempts + cloud_node_loss_max_redrives``.

    **The at-ceiling terminal is the SAME terminal as the at-cap one, deliberately** -- spill the
    sidecar to ``'awaiting'`` with ``attempts=cap`` (the budget-spent marker), taking the row OUT of
    ``IN_FLIGHT`` (releasing its burst-lane slot) and making it a drain candidate ``select_backend``
    can only route to LOCAL. It is specifically NOT a hard analyze failure (reconcile writes no
    ``FileRecord.state`` and no analysis result -- D-04/KSUBMIT-03: cloud flakiness must not fail a
    file), and specifically NOT a hold: leaving the row SUBMITTED/RUNNING is the shape that STRANDS,
    because no writer but this cron can advance it and this cron would only re-drive it again.
    """
    cfg = cast("ControlSettings", get_settings())
    file_id = cloud_job.file_id
    # phaze-mwbz3: the still-terminating deferral below commits with NO other DB mutation, so a FRESH
    # node-loss verdict computed for THIS call (``node_loss_reason`` not None) must be persisted across
    # it -- otherwise it dies with this stack frame and, once the Job finally vanishes, the NEXT tick
    # re-enters through the vanished-Job branch (:631) with no pods left to classify and no
    # ``node_loss_reason`` argument at all, silently charging ``attempts`` instead of
    # ``node_loss_redrives`` (the tighter, deliberately-1 ceiling phaze-1q4g exists to enforce).
    # ``cloud_job.node_loss_pending`` is that durable record, one row per file, scoped to the CURRENT
    # Job (cleared below whenever this call actually spends a budget or the row leaves in-flight). A
    # fresh classification always wins; when this call's own classification is unavailable (``None`` --
    # either a genuinely non-node-loss cause, or a caller that structurally cannot classify), fall back
    # to whatever an earlier deferral on this SAME Job already stashed.
    effective_node_loss_reason = node_loss_reason if node_loss_reason is not None else cloud_job.node_loss_pending
    # phaze-1q4g: pick the budget this re-drive spends. The counter, its ceiling and its log label move
    # together; every line below is otherwise cause-agnostic.
    if effective_node_loss_reason is not None:
        next_attempt, ceiling, budget = cloud_job.node_loss_redrives + 1, cfg.cloud_node_loss_max_redrives, "node_loss_redrives"
    else:
        next_attempt, ceiling, budget = cloud_job.attempts + 1, cap, "attempts"

    if next_attempt > ceiling:
        # SCHED-03/D-04: at the cloud cap DO NOT hard-fail. Re-stamp the cloud_job sidecar to 'awaiting'
        # ('awaiting' is NOT in IN_FLIGHT, so the row drops out of ``in_flight_count`` -- the
        # reconcile-only-decrements invariant) and write NO FileRecord.state (D-04, the whole point of the
        # cutover). ``cloud_job.attempts`` already equals ``cap`` here (the last under-cap re-drive set it),
        # so the next drain tick's ``select_backend`` sees ``attempts >= cap`` and routes the file to local
        # (the guaranteed safety net) -- do NOT increment attempts again here (avoids a double-count). Local
        # failure, not cloud flakiness, is the only terminal into ANALYSIS_FAILED (D-04). The re-stamped
        # ``updated_at`` on the spill gives a fresh lane-entry clock (desirable).
        #
        # phaze-1q4g: the NODE-LOSS ceiling lands here too, and on purpose. ``attempts`` may well still be
        # 0 on that path (node loss never charged it), so the ``attempts=cap`` stamp below is doing real
        # work there rather than re-affirming a value: it is what makes ``select_backend`` stop offering
        # this file to cloud at all. Cloud is finished with this row either way -- once because the
        # analysis kept failing, once because the node kept dying under it -- and the row must land in the
        # one state the drain can still act on.
        #
        # MKUE-04 clean-before-flip (D-01/D-03, Pitfall 9 -- the crux): the OLD (backend_id, staging_bucket)
        # staged object MUST be deleted WHILE the per-row ``pg_advisory_xact_lock(5_000_504)`` is still held
        # (acquired at the TOP of this ``KueueBackend.reconcile`` per-row unit, backends.py) -- i.e. BEFORE
        # the ``session.commit()`` that persists the 'awaiting' re-stamp (making the file a drain candidate)
        # and thus RELEASES the lock. The re-dispatch reuses the SAME ``file_id``-scoped S3 key; if D-06
        # lands the re-stage on the same bucket, a delete that ran AFTER the lock released would race the
        # new stage and destroy the object the new pod needs. Deleting before the flip guarantees the old
        # object is gone before any re-stage can occur (the drain holds the same lock across its whole
        # candidate claim, so it physically cannot pick up the file until this txn commits).
        #
        # Capture the OLD identity into locals BEFORE any mutation, resolve the RECORDED staging bucket
        # (never re-derive -- Pitfall 4/T-70-04-04), and delete it UNDER the lock. The delete is best-effort
        # (D-03): ``contextlib.suppress(Exception)`` so a slow/failed/absent S3 delete never blocks the spill
        # nor pins the lock beyond one network timeout (the per-bucket TTL is the backstop). A bucketless row
        # (no staged object) resolves to None and skips the delete cleanly.
        old_bucket_id = cloud_job.staging_bucket  # captured pre-mutation -- the authoritative old identity.
        bucket = s3_staging.resolve_bucket_config(cfg, old_bucket_id)
        with contextlib.suppress(Exception):
            if bucket is not None:
                await s3_staging.delete_staged_object(file_id, bucket)  # MKUE-04: under the still-held lock, BEFORE the commit.
        # D-04/D-12: swap the retired FileRecord.state write + FAILED pre-mutation for the SINGLE go-forward
        # awaiting writer in spill mode (reconcile is its FOURTH caller, alongside agent_s3/agent_push). The
        # rowcount-guarded CAS OWNS the status write (UPDATE cloud_job ... WHERE status IN (SUBMITTED,RUNNING)),
        # so we do NOT pre-mutate cloud_job.status here -- an autoflush of a dirty status would make the CAS
        # miss its own row (RESEARCH Landmine 3). ``attempts=cap`` is the budget-spent MARKER (a set, NOT an
        # increment), so the next drain tick's ``select_backend`` sees ``attempts >= cap`` and routes the file
        # to local; ``clear_cloud_phase=True`` nulls cloud_phase (WR-01, off the "Running" tile). Unlike the
        # agent_s3/agent_push siblings (which KEEP the gated FileRecord dual-write, 83 D-00c), reconcile writes
        # NO FileRecord.state at all (D-04): the spilled kueue file stays at its prior PUSHED state, which
        # satisfies the loosened pushed/pushing shadow invariants -- fixing the HARD state=AWAITING_CLOUD +
        # cloud_job.status=FAILED shadow violation live on main today.
        from phaze.services.backends import hold_awaiting_cloud  # noqa: PLC0415 -- deferred to break the backends<->reconcile_cloud_jobs import cycle

        # The helper's spill-mode CAS dereferences file.id (it does NOT write file.state); load the FileRecord.
        # The FK files.id <- cloud_job.file_id guarantees the row exists, so None is unreachable in practice --
        # the guard is for mypy (scalar_one_or_none is Optional) and defense-in-depth (a None file skips the CAS
        # cleanly, matching the agent_s3/agent_push no-op).
        file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
        if file is not None:
            await hold_awaiting_cloud(
                session,
                file,
                attempts=cap,
                expect_status=(CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value),
                clear_cloud_phase=True,
            )
        cloud_job.inadmissible = False  # terminal row must not keep the operator alert lit (helper does not stamp it).
        cloud_job.staging_bucket = None  # clear so no pre-repurpose reader is misled about the (now-gone) object.
        cloud_job.node_loss_pending = None  # phaze-mwbz3: row is leaving in-flight -- no verdict left to carry.
        await session.commit()  # releases the per-row lock -- the old object is ALREADY gone (clean-before-flip).
        if name is not None:  # phaze-1b39: a phantom row (kueue_workload IS NULL) has no Job to delete.
            await kube_staging.delete_job(name, kube)  # Job delete stays POST-commit (D-04 status-read-vs-GC; cleanup only).
        tally["failed"] += 1
        logger.warning(
            "reconcile_cloud_jobs: submit cap reached -> cloud_job re-stamped 'awaiting' + spill to local",
            file_id=str(file_id),
            attempt=next_attempt,
            cap=ceiling,
            budget=budget,  # phaze-1q4g: WHICH ceiling ran out -- 'attempts' (the file kept failing) or
            node_loss_reason=effective_node_loss_reason,  # 'node_loss_redrives' (the node kept dying under it).
        )
        return

    # Under the ceiling -> re-drive. Delete the prior Job, then confirm it is gone before re-submitting.
    if name is not None:  # phaze-1b39: a phantom row (kueue_workload IS NULL) has no Job to delete.
        await kube_staging.delete_job(name, kube)
    if not await _job_gone(name, kube):
        # phaze-nq3c: COMMIT before returning. This deferral path makes no OTHER DB mutation worth persisting
        # (only a kube-side delete_job ran), but the per-row unit acquired pg_advisory_xact_lock(5_000_504) at
        # the top of KueueBackend.reconcile and the design (SCHED-02 / Pitfall 2) RELIES on _reconcile_one
        # committing per row to auto-release that transaction-scoped lock at row granularity. Returning without
        # a commit was the ONLY non-committing exit in this file -- it leaked the lock past the row boundary
        # until some later row's commit (or session close if this was the last in-flight row), stalling a
        # concurrent stage_cloud_window drain tick that blocks on the same key. Commit to end the txn and
        # release the lock, matching every other no-op path here (lines with 'release the per-row advisory
        # lock (Pitfall 2)').
        #
        # phaze-mwbz3: DO persist the node-loss verdict, though -- ``effective_node_loss_reason`` is the ONLY
        # durable copy of "this terminal's cause was NODE_LOST", and it dies with this stack frame otherwise.
        # If the Job finally vanishes before the NEXT tick, ``_reconcile_one`` re-enters through the
        # vanished-Job branch, which has no pods left to classify and passes no ``node_loss_reason`` argument
        # at all -- without this, that re-entry would silently charge ``attempts`` instead of the tighter
        # ``node_loss_redrives`` ceiling (the whole defect phaze-1q4g exists to prevent). A ``None`` here
        # (ordinary, non-node-loss cause) correctly clears any stale marker from an earlier, unrelated Job
        # under this same deterministic name.
        cloud_job.node_loss_pending = effective_node_loss_reason
        await session.commit()
        logger.info(
            "reconcile_cloud_jobs: prior Job still terminating; deferring re-drive",
            file_id=str(file_id),
            kueue_workload=name,
            node_loss_reason=effective_node_loss_reason,
        )
        return
    # phaze-1q4g: charge the budget this cause spends -- and ONLY that one. A node-loss re-drive leaves
    # ``attempts`` untouched (the file has not failed an analysis), so the two causes stay separable on
    # the row forever; an ordinary re-drive leaves ``node_loss_redrives`` untouched for the same reason.
    if effective_node_loss_reason is not None:
        cloud_job.node_loss_redrives = next_attempt
    else:
        cloud_job.attempts = next_attempt
    cloud_job.status = CloudJobStatus.SUBMITTED.value
    cloud_job.inadmissible = False  # CR-01: re-driving a failed Job clears any stale Inadmissible flag.
    cloud_job.node_loss_pending = None  # phaze-mwbz3: verdict spent -- the NEXT Job under this name starts fresh.
    # phaze-32wz: clear the (now-deleted) Job's name so the NEXT tick reads this row as "pending
    # confirmation" (the phantom-row branch's fresh-hold path), not as a fresh no-callback terminal
    # against the OLD, already-confirmed-gone name -- this is what stops the enqueue-time attempt bump
    # above from being immediately re-charged before the re-submitted Job even exists.
    cloud_job.kueue_workload = None
    await session.commit()
    await _enqueue_resubmit(ctx, file_id)
    tally["redriven"] += 1
    logger.info(
        "reconcile_cloud_jobs: re-driving submit_cloud_job",
        file_id=str(file_id),
        attempt=next_attempt,
        budget=budget,  # phaze-1q4g: which of the two re-drive budgets this one spent, and (when it is
        node_loss_reason=effective_node_loss_reason,  # the node-loss one) the pod evidence that classified it.
    )


async def _reconcile_one(ctx: dict[str, Any], session: AsyncSession, cloud_job: CloudJob, cap: int, tally: dict[str, int], kube: KubeConfig) -> None:
    """Reconcile a single in-flight ``cloud_job`` row against its Job + Kueue Workload on ``kube``'s cluster.

    Phase 70 (MKUE-01/D-04): ``kube`` is THIS row's owning backend ``KubeConfig`` (threaded from
    ``KueueBackend.reconcile``), so every ``get_job`` / ``get_workload_for`` / ``delete_job`` targets the
    file's own cluster.
    """
    name = cloud_job.kueue_workload
    if not name:
        # phaze-1b39 / phaze-32wz: this row has PENDING CONFIRMATION, not a Job to read, so no terminal
        # signal can arrive for it yet -- the pre-1b39 behaviour (warn + skip) re-logged the same line
        # every tick forever while the row kept its burst-lane cap slot: a permanent phantom. Two
        # equally-legitimate ways a row lands here with status in {SUBMITTED, RUNNING}: (a) the FIRST
        # submit crashed between the row insert and the workload stamp, or is mid-flight right now; (b) a
        # no-callback-terminal RE-DRIVE just cleared ``kueue_workload`` (phaze-32wz, above) after
        # confirming the OLD Job gone, and its freshly-enqueued ``submit_cloud_job`` has not yet run to
        # re-stamp a new one. Both are "pending confirmation", explicitly recorded by the cleared column
        # rather than inferred from elapsed time -- bound ONLY the "how long is too long" question by
        # age. Fresh -> hold quietly, so a live/pending submit is never stolen and NO attempt is charged
        # for a submit that simply hasn't run yet. Past the pending-submit bound the pending resubmit is
        # CONFIRMED VANISHED (not merely pending) -> terminalize through the SAME no-callback path
        # everything else uses (bounded re-drive under cap -- a NEW, independently-charged attempt --
        # spill to local at cap), which is what returns the cap slot without operator surgery.
        if _row_age_seconds(cloud_job) <= PENDING_SUBMIT_CONFIRMATION_SECONDS:
            logger.warning("reconcile_cloud_jobs: cloud_job missing kueue_workload; skipping", cloud_job_id=str(cloud_job.id))
            await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).
            return
        logger.warning(
            "reconcile_cloud_jobs: cloud_job missing kueue_workload past the pending-submit bound -- terminalizing phantom row",
            cloud_job_id=str(cloud_job.id),
            file_id=str(cloud_job.file_id),
            age_seconds=int(_row_age_seconds(cloud_job)),
        )
        if await _analysis_completed(session, cloud_job.file_id):
            # The callback landed anyway (it keys off file_id, not the Job) -- finalize as the success it
            # is rather than re-driving an already-analyzed file. name=None: there is no Job to delete.
            await _record_success(session, cloud_job, None, tally, kube)
            return
        await _handle_no_callback_terminal(ctx, session, cloud_job, None, cap, tally, kube)
        return

    # WR-01: a vanished Job (real kube 404 -> NotFoundError; fake seam -> None) on an in-flight row is a
    # no-callback terminal, NOT a transient error. Route it to the bounded re-drive / at-cap spill-back
    # handler instead of letting NotFoundError bubble to the per-row guard, where it would be rolled back
    # and skipped every tick -- leaving the row stuck in-flight forever (e.g. a Failed Job GC'd by
    # ttlSecondsAfterFinished before reconcile read it, or an enqueue that raised after the attempt commit).
    try:
        job = await kube_staging.get_job(name, kube)
    except kr8s.NotFoundError:
        job = None
    if job is None:
        # phaze-2o8p: distinguish a callback-completed-then-TTL-GC'd Job from a genuine no-callback
        # terminal. If the analysis result already landed (analysis_completed_at IS NOT NULL), the
        # vanished Job is a SUCCESS whose Job was reaped by ttlSecondsAfterFinished before this lagging
        # tick read it -- finalize it (record SUCCEEDED + delete Job) instead of re-driving an
        # already-analyzed file against a staged object the success callback already deleted.
        if await _analysis_completed(session, cloud_job.file_id):
            await _record_success(session, cloud_job, name, tally, kube)
            return
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally, kube)
        return

    # 1. Job terminal signals first -- the Job is the source of truth for succeeded-vs-failed.
    if _job_counter(job, "succeeded") >= 1 or _job_has_true_condition(job, "Complete"):
        await _record_success(session, cloud_job, name, tally, kube)
        return
    if _job_counter(job, "failed") >= 1 or _job_has_true_condition(job, "Failed"):
        # phaze-73sv: mirror the vanished-Job guard (line 412). A Job can read Failed AFTER its success
        # callback landed -- activeDeadlineSeconds firing just after the callback PUT completed, an
        # OOM/preempt in the post-callback teardown window -- because the /analysis callback records the
        # result + deletes the staged object (D-05) but never advances cloud_job.status. Re-driving such a row
        # (_handle_no_callback_terminal) re-submits a pod that 404s its now-deleted staged object
        # (EXIT_DOWNLOAD) and re-fails, burning the whole cap. If the analysis already completed,
        # finalize it as the success it is instead of re-driving an already-analyzed file.
        if await _analysis_completed(session, cloud_job.file_id):
            await _record_success(session, cloud_job, name, tally, kube)
            return
        # phaze-1q4g: with ``backoffLimit: 0`` a Job reads Failed for BOTH "the analysis died" and "the
        # node took the pod", and the two must not share a retry budget. The Job cannot tell them apart;
        # its pods can. Ask them once, here on the terminal path only.
        await _handle_no_callback_terminal(
            ctx, session, cloud_job, name, cap, tally, kube, node_loss_reason=await _terminal_node_loss_reason(name, kube)
        )
        return

    # 2. Not terminal -> read the paired Kueue Workload for admission state (D-02 by job-uid).
    uid = str(getattr(getattr(job, "metadata", None), "uid", "") or "")
    workload = await kube_staging.get_workload_for(uid, kube) if uid else None
    if workload is None:
        # Admission state unreadable this tick (label miss + owner-ref miss) -> stay in-flight, no-op.
        await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).
        return

    # Evicted/deactivated -> no-callback terminal (re-drive under cap).
    evicted = _workload_condition(workload, _TYPE_EVICTED)
    if evicted is not None and evicted.get("status") == "True":
        # phaze-73sv: same guard as the Job-Failed branch above. A Kueue eviction under quota pressure
        # can land AFTER the pod's success callback PUT completed (the /analysis callback stamps the
        # result + deletes the staged object but never advances cloud_job.status). Re-driving then re-submits a
        # pod that 404s the deleted staged object and re-fails, burning the cap. Finalize a
        # callback-completed row as the success it is rather than re-driving an already-analyzed file.
        if await _analysis_completed(session, cloud_job.file_id):
            await _record_success(session, cloud_job, name, tally, kube)
            return
        # phaze-1q4g: same question as the Job-Failed branch. A Kueue eviction is usually quota pressure
        # (ordinary), but a node going down also evicts -- and the pods say which.
        await _handle_no_callback_terminal(
            ctx, session, cloud_job, name, cap, tally, kube, node_loss_reason=await _terminal_node_loss_reason(name, kube)
        )
        return

    quota_reserved = _workload_condition(workload, _TYPE_QUOTA_RESERVED)

    # Inadmissible (operator misconfig): loud + hold, NEVER consumes the cap (D-06/D-07).
    if quota_reserved is not None and quota_reserved.get("status") == "False" and quota_reserved.get("reason") == _REASON_INADMISSIBLE:
        if not cloud_job.inadmissible:
            cloud_job.inadmissible = True
        await session.commit()  # WR-01: commit unconditionally (no-op when already flagged) to release the lock.
        tally["inadmissible"] += 1
        logger.warning(
            "reconcile_cloud_jobs: Workload Inadmissible -- K8s Jobs not admitting; check LocalQueue config",
            cloud_job_id=str(cloud_job.id),
            file_id=str(cloud_job.file_id),
            kueue_workload=name,
        )
        return

    # Healthy Pending: silent hold, waits indefinitely -- no cap, no alert (D-07, Pitfall 3).
    if quota_reserved is not None and quota_reserved.get("status") == "False" and quota_reserved.get("reason") == _REASON_PENDING:
        if cloud_job.inadmissible:  # CR-01: the misconfig was fixed -- Workload is back to a healthy quota wait.
            cloud_job.inadmissible = False
        if cloud_job.cloud_phase != CloudPhase.QUEUED_BEHIND_QUOTA.value:  # D-04: behind quota, waiting for admission.
            cloud_job.cloud_phase = CloudPhase.QUEUED_BEHIND_QUOTA.value
        # WR-01: commit unconditionally (a clean no-op when neither field changed) to release the per-row lock.
        await session.commit()
        tally["pending"] += 1
        return

    # Admitted / QuotaReserved=True -> in-flight running; advance SUBMITTED -> RUNNING.
    admitted = _workload_condition(workload, _TYPE_ADMITTED)
    admitted_true = admitted is not None and admitted.get("status") == "True"
    quota_true = quota_reserved is not None and quota_reserved.get("status") == "True"
    if admitted_true or quota_true:
        # phaze-202e wedge detection, BEFORE the RUNNING re-affirm. Admission state alone says nothing
        # about progress: an admitted Workload whose pod never runs (ImagePullBackOff /
        # CreateContainerConfigError from a missing operator ConfigMap/Secret) leaves the Job
        # non-terminal, so the branch below would stamp RUNNING and return -- every tick, forever --
        # while the row holds its burst-lane cap slot. That is the phaze-1b39 failure, and it is still
        # covered here.
        #
        # What changed is HOW. phaze-1b39 answered it with a wall clock (activeDeadlineSeconds + slack)
        # and phaze-uui9 then had to bolt on a "only if already observed RUNNING" gate to stop that
        # clock from killing a pod the instant it was admitted after a long healthy quota wait. Both
        # were fighting the same unfixable ambiguity: elapsed time cannot distinguish a 2-6 h concert-set
        # analyze from a hang. In production it resolved that ambiguity the wrong way -- every long
        # recording SIGTERM'd at exactly 3h, the whole cloud attempt budget burned, 14 files permanently
        # barred from Kueue (incident 2026-07-28).
        #
        # ``_pod_wedge_reason`` asks the POD instead. It returns a reason ONLY on positive proof that no
        # work is happening (fatal container waiting reason, persistent unschedulable, or an un-suspended
        # Job with no pod at all) and returns None for a Running pod at ANY age. No wall clock bounds a
        # run, so the uui9 status gate is no longer needed and is GONE: pod state is equally valid on a
        # row's first admitted tick and on its thousandth.
        #
        # The analysis-result guard stays: the callback (KSUBMIT-03) keys off file_id, so a row whose
        # result already landed is finalized by the normal terminal paths, never re-driven.
        wedge = await _pod_wedge_reason(job, name, kube)
        if wedge is not None and not await _analysis_completed(session, cloud_job.file_id):
            logger.warning(
                "reconcile_cloud_jobs: in-flight Job's pod is provably not working -- terminalizing",
                cloud_job_id=str(cloud_job.id),
                file_id=str(cloud_job.file_id),
                kueue_workload=name,
                wedge_reason=wedge.reason,
                node_loss=wedge.node_loss,  # phaze-1q4g: which budget the re-drive below will spend.
            )
            await _handle_no_callback_terminal(
                ctx, session, cloud_job, name, cap, tally, kube, node_loss_reason=wedge.reason if wedge.node_loss else None
            )
            return
        # D-04 admission progression (ORTHOGONAL to the status advance): Admitted=True means the pod
        # is un-gated and running -> RUNNING; QuotaReserved-only (quota granted, not yet un-suspended)
        # is the intermediate ADMITTED phase. The cloud_job ``status`` axis still advances to RUNNING
        # in both cases (unchanged).
        next_phase = CloudPhase.RUNNING.value if admitted_true else CloudPhase.ADMITTED.value
        if cloud_job.status != CloudJobStatus.RUNNING.value or cloud_job.inadmissible or cloud_job.cloud_phase != next_phase:
            cloud_job.status = CloudJobStatus.RUNNING.value
            cloud_job.inadmissible = False  # CR-01: an admitted Workload is no longer Inadmissible -- clear the alert.
            cloud_job.cloud_phase = next_phase
        # WR-01: commit unconditionally (a clean no-op when already RUNNING in the target phase) to release the lock.
        await session.commit()
        tally["running"] += 1
        return

    # Unknown in-flight condition set -> leave the row untouched for a later tick.
    await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).


async def reconcile_cloud_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Reconcile every backend's in-flight ``cloud_job`` rows per-backend; return an aggregate tally.

    The every-minute cron body (D-01/D-03; phaze-i3pkb.1), Phase-69 SCHED-05 form: dispatch reconcile PER-BACKEND
    (``for b in resolve_backends(cfg): await b.reconcile(session, ctx)``) instead of a single global
    ``select(CloudJob WHERE status IN {SUBMITTED, RUNNING})`` query. Removing that global un-scoped query
    closes the double-owner vector: a compute ``cloud_job`` row's PRIMARY terminalization stays its
    ``/pushed``/``/mismatch``/``/failed`` callback path -- ``ComputeAgentBackend.reconcile`` only reaps
    the AGE-STRANDED rows those callbacks never reach (phaze-j7m18); ``LocalBackend.reconcile`` stays a
    genuine no-op (local completion is synchronous). The Kueue rows are owned by
    ``KueueBackend.reconcile`` (backend_id-scoped, per-row advisory-locked). Each backend's tally is
    aggregated into the cron's return dict (same shape); the per-row guard + delete-after-record
    ordering + "never raise out of the cron" discipline live inside each backend's ``reconcile``
    (KSUBMIT-03: still never writes a result).

    ``resolve_backends`` is imported FUNCTION-LOCALLY (deferred) because ``services.backends`` does a
    module-top ``from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`` -- a module-top import
    here would be a ``backends -> reconcile_cloud_jobs -> backends`` collection-time ImportError.
    """
    from phaze.services.backends import resolve_backends  # noqa: PLC0415 -- deferred to break the backends<->reconcile_cloud_jobs import cycle

    cfg = cast("ControlSettings", get_settings())
    tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0}

    async with ctx["async_session"]() as session:
        for backend in resolve_backends(cfg):
            backend_tally = await backend.reconcile(session, ctx)
            # Kueue and Compute return per-backend tallies; Local's reconcile is a genuine no-op (None).
            if backend_tally:
                for key, value in backend_tally.items():
                    tally[key] = tally.get(key, 0) + value

    logger.info("reconcile_cloud_jobs complete", **tally)
    return tally
