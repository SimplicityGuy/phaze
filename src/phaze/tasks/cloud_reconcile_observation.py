"""Read-only observation and pure state classification for cloud-job reconciliation.

This module names what the reconciliation controller can observe without deciding or applying any
side effect.  It may read Kubernetes and PostgreSQL state, but it does not mutate ORM rows, commit or
roll back transactions, enqueue work, delete Jobs or staged objects, or invoke backend adapters.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import kr8s
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.services import kube_staging


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config_backends import KubeConfig
    from phaze.models.cloud_job import CloudJob


_TYPE_QUOTA_RESERVED = "QuotaReserved"
_TYPE_ADMITTED = "Admitted"
_TYPE_EVICTED = "Evicted"
_REASON_PENDING = "Pending"
_REASON_INADMISSIBLE = "Inadmissible"

# The only age bound on submission bookkeeping. It is not a run deadline.
PENDING_SUBMIT_CONFIRMATION_SECONDS = 3600

# An un-suspended Job with no active or listed pod is held until this probe elapses.
NO_POD_PROBE_SECONDS = 900


class JobDisposition(StrEnum):
    """Observed Job states in their load-bearing precedence order."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_FLIGHT = "in_flight"


class WorkloadDisposition(StrEnum):
    """Observed Kueue Workload states before any reconciliation effect is selected."""

    EVICTED = "evicted"
    INADMISSIBLE = "inadmissible"
    PENDING = "pending"
    ADMITTED = "admitted"
    QUOTA_RESERVED = "quota_reserved"
    UNKNOWN = "unknown"


class PendingConfirmationDisposition(StrEnum):
    """Whether submission bookkeeping is still inside its confirmation window."""

    FRESH = "fresh"
    EXPIRED = "expired"


class Wedge(NamedTuple):
    """A terminalizing pod verdict and whether node loss caused it."""

    reason: str
    node_loss: bool


def row_age_seconds(cloud_job: CloudJob) -> float:
    """Return seconds since the row's last transition, accepting naive or aware timestamps."""
    now = datetime.now(UTC)
    updated = cloud_job.updated_at
    if updated.tzinfo is None:
        now = now.replace(tzinfo=None)
    return (now - updated).total_seconds()


def job_counter(job: Any, key: str) -> int:
    """Read a Job status counter, defaulting malformed or absent values to zero."""
    status = getattr(job, "status", None) or {}
    try:
        return int(status.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def job_has_true_condition(job: Any, cond_type: str) -> bool:
    """Return whether a Job has a condition of ``cond_type`` with status ``True``."""
    status = getattr(job, "status", None) or {}
    return any(cond.get("type") == cond_type and cond.get("status") == "True" for cond in status.get("conditions", []) or [])


def workload_condition(workload: Any, cond_type: str) -> dict[str, Any] | None:
    """Return the first Kueue Workload condition of ``cond_type``."""
    status = getattr(workload, "status", None) or {}
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == cond_type:
            return cast("dict[str, Any]", condition)
    return None


def condition_true(condition: dict[str, Any] | None) -> bool:
    """Return whether a present Job or Workload condition has status ``True``."""
    return condition is not None and condition.get("status") == "True"


def quota_hold_reason(quota_reserved: dict[str, Any] | None) -> Any:
    """Return the reason of a ``QuotaReserved=False`` condition, otherwise ``None``."""
    if quota_reserved is None or quota_reserved.get("status") != "False":
        return None
    return quota_reserved.get("reason")


def classify_job(job: Any) -> JobDisposition:
    """Classify a Job, preserving callback-compatible success primacy over failure."""
    if job_counter(job, "succeeded") >= 1 or job_has_true_condition(job, "Complete"):
        return JobDisposition.SUCCEEDED
    if job_counter(job, "failed") >= 1 or job_has_true_condition(job, "Failed"):
        return JobDisposition.FAILED
    return JobDisposition.IN_FLIGHT


def classify_workload(workload: Any) -> WorkloadDisposition:
    """Classify Kueue conditions in the controller's established transition order."""
    if condition_true(workload_condition(workload, _TYPE_EVICTED)):
        return WorkloadDisposition.EVICTED

    quota_reserved = workload_condition(workload, _TYPE_QUOTA_RESERVED)
    hold_reason = quota_hold_reason(quota_reserved)
    if hold_reason == _REASON_INADMISSIBLE:
        return WorkloadDisposition.INADMISSIBLE
    if hold_reason == _REASON_PENDING:
        return WorkloadDisposition.PENDING

    if condition_true(workload_condition(workload, _TYPE_ADMITTED)):
        return WorkloadDisposition.ADMITTED
    if condition_true(quota_reserved):
        return WorkloadDisposition.QUOTA_RESERVED
    return WorkloadDisposition.UNKNOWN


def classify_pending_confirmation(age_seconds: float) -> PendingConfirmationDisposition:
    """Classify the only age-bounded state: a row with no confirmed Job name."""
    if age_seconds <= PENDING_SUBMIT_CONFIRMATION_SECONDS:
        return PendingConfirmationDisposition.FRESH
    return PendingConfirmationDisposition.EXPIRED


def classify_pod_wedge(job: Any, pods: list[Any], *, now: datetime) -> Wedge | None:
    """Classify a provably dead pod state without reading external state or causing effects."""
    verdict = kube_staging.classify_job_pods(pods, now=now)
    if verdict is kube_staging.PodLiveness.ALIVE:
        return None
    if verdict in (kube_staging.PodLiveness.NODE_LOST, kube_staging.PodLiveness.DEAD_BEFORE_START, kube_staging.PodLiveness.UNSCHEDULABLE):
        return Wedge(f"{verdict.value} ({kube_staging.describe_job_pods(pods)})", verdict is kube_staging.PodLiveness.NODE_LOST)
    if pods or job_counter(job, "active") != 0 or kube_staging.job_is_suspended(job):
        return None
    started = kube_staging.job_started_at(job)
    if started is None or (now - started).total_seconds() <= NO_POD_PROBE_SECONDS:
        return None
    return Wedge("no_pod (job un-suspended with zero active pods past the probe)", False)


def terminal_node_loss_reason(pods: list[Any]) -> str | None:
    """Classify whether terminal Job pods prove that their node took them."""
    if kube_staging.classify_job_pods(pods) is not kube_staging.PodLiveness.NODE_LOST:
        return None
    return f"node_lost ({kube_staging.describe_job_pods(pods)})"


async def observe_terminal_node_loss_reason(name: str, kube: KubeConfig) -> str | None:
    """Read terminal Job pods best-effort and return a node-loss classification."""
    pods: list[Any] = []
    with contextlib.suppress(Exception):
        pods = await kube_staging.list_pods_for_job(name, kube)
    return terminal_node_loss_reason(pods)


async def observe_job_gone(name: str | None, kube: KubeConfig) -> bool:
    """Return whether the named Job is absent; a phantom row is trivially gone."""
    if name is None:
        return True
    try:
        job = await kube_staging.get_job(name, kube)
    except kr8s.NotFoundError:
        return True
    return job is None


async def observe_pod_wedge_reason(job: Any, name: str, kube: KubeConfig) -> Wedge | None:
    """Read a Job's pods and classify whether positive evidence proves they are not working."""
    pods = await kube_staging.list_pods_for_job(name, kube)
    return classify_pod_wedge(job, pods, now=datetime.now(UTC))


async def observe_analysis_completed(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Return whether the authoritative analysis callback already completed for ``file_id``."""
    completed_at = (await session.execute(select(AnalysisResult.analysis_completed_at).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    return completed_at is not None
