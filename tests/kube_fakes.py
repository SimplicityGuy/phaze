"""Shared fake-kube test substrate (Phase 54, Plan 03 -- Layer-1 logic fakes).

The reconcile/submit state-machine tests (Plans 05/06) drive the kube seam by
monkeypatching ``kube_staging`` and returning canned status objects from these
plain factories -- exercising every Kueue admission/terminal transition with ZERO
HTTP (the high-value coverage lives here). kr8s surfaces ``.status``/``.spec`` as
dicts and ``.metadata`` as an attribute object, so the fakes mirror that shape with
``SimpleNamespace`` rather than dragging real kr8s objects into logic tests.

The named constants (``PENDING`` / ``INADMISSIBLE`` / ``ADMITTED`` / ``EVICTED`` /
``QUOTA_RESERVED``) are the exact ``(type, status, reason)`` Workload-condition tuples
from RESEARCH Status -> Outcome Mapping; importing them keeps the logic tests pinned to
the verified Kueue condition vocabulary.
"""

from __future__ import annotations

from types import SimpleNamespace


def fake_workload(*conditions: tuple[str, str, str], owner_uid: str | None = None, name: str = "phaze-analyze-job-wl") -> SimpleNamespace:
    """Return a canned Kueue Workload stand-in.

    ``conditions`` are ``(type, status, reason)`` tuples placed under
    ``.status["conditions"]`` exactly as Kueue reports them. ``owner_uid`` (when set)
    stamps ``.metadata.ownerReferences`` with a single ``{"uid": owner_uid}`` entry so the
    ``get_workload_for`` owner-reference fallback path (A2 de-risk) can be exercised; an
    unset ``owner_uid`` yields an empty ``ownerReferences`` list.
    """
    return SimpleNamespace(
        status={"conditions": [{"type": t, "status": s, "reason": r} for (t, s, r) in conditions]},
        metadata=SimpleNamespace(
            name=name,
            ownerReferences=[{"uid": owner_uid}] if owner_uid else [],
        ),
    )


def fake_job(
    succeeded: int = 0,
    failed: int = 0,
    suspend: bool = False,
    uid: str = "uid-1",
    name: str = "phaze-analyze-fake",
    *,
    active: int = 0,
    start_time: str | None = None,
) -> SimpleNamespace:
    """Return a canned batch/v1 Job stand-in.

    ``.status`` carries ``succeeded``/``failed`` counters (the primary terminal signals
    with ``backoffLimit: 0``); ``.spec["suspend"]`` reflects whether Kueue has un-gated the
    Job; ``.metadata`` exposes the ``uid`` (the Workload-discovery key) and ``name``.

    phaze-202e adds the two fields the zero-pod wedge probe reads: ``active`` (pods in
    Pending/Running -- a Job with a wedged ImagePullBackOff pod still reports ``active=1``)
    and ``start_time`` (``status.startTime``, RFC3339, stamped when Kueue un-suspends the
    Job). Both default to the "cannot prove anything" shape (0 / absent), so every existing
    caller keeps producing a Job that the probe HOLDS rather than terminalizes.
    """
    status: dict[str, object] = {"succeeded": succeeded, "failed": failed, "active": active}
    if start_time is not None:
        status["startTime"] = start_time
    return SimpleNamespace(
        status=status,
        spec={"suspend": suspend},
        metadata=SimpleNamespace(uid=uid, name=name),
    )


def fake_pod(
    phase: str = "Pending",
    *,
    waiting_reason: str | None = None,
    init_waiting_reason: str | None = None,
    unschedulable_since: str | None = None,
    status_reason: str | None = None,
    disruption_target_reason: str | None = None,
    name: str = "phaze-analyze-fake-abcde",
) -> SimpleNamespace:
    """Return a canned v1 Pod stand-in for the phaze-202e pod-state wedge classifier.

    ``phase`` is ``status.phase`` (``Running``/``Succeeded`` are the never-terminalize verdict);
    ``waiting_reason`` / ``init_waiting_reason`` put a container into
    ``state.waiting.reason`` (``ImagePullBackOff``, ``CreateContainerConfigError``, ...) on the
    app or init container respectively; ``unschedulable_since`` stamps an RFC3339
    ``PodScheduled=False/Unschedulable`` condition ``lastTransitionTime`` so the scheduling
    probe can be exercised without patching a clock.

    phaze-1q4g adds the two NODE-LOSS shapes -- a pod that died WITH ITS NODE rather than because the
    analysis failed. ``status_reason`` sets the pod-level ``status.reason`` (``NodeShutdown`` /
    ``NodeLost`` / ``Evicted`` / ...: the node-lifecycle controller's and kubelet's own vocabulary);
    ``disruption_target_reason`` stamps the k8s>=1.26 ``DisruptionTarget=True`` condition with that
    reason (``DeletionByTaintManager`` / ``TerminationByKubelet`` / ...). Either alone is sufficient
    for :attr:`PodLiveness.NODE_LOST`, so both can be exercised independently.
    """
    status: dict[str, object] = {"phase": phase}
    if status_reason is not None:
        status["reason"] = status_reason
    if waiting_reason is not None:
        status["containerStatuses"] = [{"name": "analyze", "state": {"waiting": {"reason": waiting_reason}}}]
    if init_waiting_reason is not None:
        status["initContainerStatuses"] = [{"name": "init", "state": {"waiting": {"reason": init_waiting_reason}}}]
    conditions: list[dict[str, str]] = []
    if unschedulable_since is not None:
        conditions.append({"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": unschedulable_since})
    if disruption_target_reason is not None:
        conditions.append({"type": "DisruptionTarget", "status": "True", "reason": disruption_target_reason})
    if conditions:
        status["conditions"] = conditions
    return SimpleNamespace(status=status, metadata=SimpleNamespace(name=name))


def fake_local_queue(name: str = "phaze-lq", namespace: str = "phaze") -> SimpleNamespace:
    """Return a canned Kueue LocalQueue stand-in (Phase 56, KDEPLOY-04 reachability probe).

    Mirrors ``fake_job``/``fake_workload``: the kr8s LocalQueue surfaces ``.metadata`` as an
    attribute object, so this exposes ``metadata=SimpleNamespace(name=..., namespace=...)`` -- the
    only fields the startup probe touches (it GETs the object; existence == reachable). The probe's
    failure modes (404 -> ``kr8s.NotFoundError``, transient -> other exc) are exercised by
    monkeypatching ``kube_staging.get_local_queue`` to raise, so no ``.status`` shape is needed here.
    """
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
    )


# Canned Workload condition sets -- the exact (type, status, reason) tuples from
# RESEARCH Status -> Outcome Mapping (verified against Context7 /kubernetes-sigs/kueue).
PENDING = fake_workload(("QuotaReserved", "False", "Pending"))
INADMISSIBLE = fake_workload(("QuotaReserved", "False", "Inadmissible"))
ADMITTED = fake_workload(("QuotaReserved", "True", ""), ("Admitted", "True", ""))
EVICTED = fake_workload(("Evicted", "True", "WorkloadInactive"))
QUOTA_RESERVED = fake_workload(("QuotaReserved", "True", ""))
