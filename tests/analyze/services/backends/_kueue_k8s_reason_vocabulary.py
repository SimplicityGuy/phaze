"""Authoritative Kueue/Kubernetes reason vocabulary, pinned to a real, tagged upstream (phaze-pe41d).

Every frozenset below is transcribed directly from the vendored source excerpts under
``tests/vendor/kueue-v0.19.0/``, ``tests/vendor/kubernetes-v1.36.2/`` and
``tests/vendor/kubernetes-v1.20.0-v1.21.0/`` -- see those files for the exact upstream repo, tag,
commit sha and the ``curl``/``gh api`` command used to fetch each one.

This module exists so :mod:`test_kueue_k8s_reason_vocabulary` can assert that every literal
``src/phaze/services/kube_staging.py`` and ``src/phaze/tasks/cloud_reconcile_observation.py`` branch
on is a MEMBER of a real, versioned vocabulary -- replacing ``tests/kube_fakes.py``'s previous
"exercising every Kueue admission/terminal transition with ZERO HTTP" proxy, whose condition/reason
constants were hand-copied from research notes (one, ``"WorkloadInactive"``, was never a real Kueue
value at all -- see ``tests/kube_fakes.py``'s ``EVICTED`` fixture).

Kubernetes version: v1.36.2+k0s -- CONFIRMED (operator-authorized read-only ``kubectl`` on the real
cluster, recorded as a comment on phaze-pe41d and phaze-tkkor: "k8s Server Version: v1.36.2+k0s",
matching this bead's earlier production-measurement anchor from docs/spikes/).

Kueue version: v0.19.0 -- CONFIRMED (same kubectl measurement: image
``registry.k8s.io/kueue/kueue:v0.19.0``; API served ``[v1beta2, v1beta1]``, preferred v1beta2 -- but
phaze's own ``[backends.kube].workload_api_version`` explicitly requests v1beta1, and the wire STRING
values are identical either way, see the vendor file's v1beta1/v1beta2 note; feature gates:
``DisableWaitForPodsReady=true`` ONLY, confirming ``UnadmittedWorkloadsObservability`` is OFF in
production, i.e. the legacy ``"Pending"``/``"Inadmissible"`` reasons below are what production
actually emits today, not merely Kueue's documented default). This module was anchored to the latest
stable tag (v0.19.6) before this measurement came back; re-verified directly (phaze-pe41d) that
``apis/kueue/v1beta1/workload_types.go`` is BYTE-IDENTICAL between v0.19.0 and v0.19.6, so nothing
here changed as a result of re-anchoring -- see the vendor file's diff note for the one, unrelated,
v1beta2-only difference (a new DRA constant) and a feature-gate PreRelease STAGE LABEL change
(Beta->Alpha) that left the actual ``Default: false`` unchanged. "Pinned to the deployed Kueue
version" is now FULLY discharged.

Cross-checked (phaze-pe41d review) against the oldest stable tag with the ``apis/kueue/v1beta1``
package phaze's manifests target, v0.5.0, and three points in between (v0.8.0, v0.12.0/v0.15.0/v0.18.0
sampled) -- every one of the five Kueue string values this module pins
(``Admitted``/``QuotaReserved``/``Evicted``/``Inadmissible``/``Pending``) is IDENTICAL at both ends of
that range:
  - ``WorkloadAdmitted``/``WorkloadQuotaReserved``/``WorkloadEvicted`` are unchanged v0.5.0->v0.19.6.
  - ``"Inadmissible"`` is a real literal reason at v0.5.0 (``pkg/controller/core/workload_controller.go``,
    inline, pre-dating its promotion to a named ``WorkloadInadmissible`` constant at v0.8.0) through
    v0.19.6 -- string value never changed, only its Go declaration form.
  - ``"Pending"`` is the real literal reason for a healthy quota-wait at v0.5.0
    (``pkg/scheduler/scheduler.go``: ``workload.UnsetQuotaReservationWithCondition(e.Obj, "Pending",
    e.inadmissibleMsg)``) through v0.19.6, where it is now the explicit FALLBACK string in
    ``workload.UnadmittedWorkloadReasonWithFallback(e.quotaReservedReason, "Pending")`` -- the KEP 10852
    refactor that added the granular reasons below deliberately preserved this exact literal as its
    off-by-default behaviour, which is strong evidence it is durable, not merely unchanged so far.
  - So: **no known version-range drift** across v0.5.0 through v0.19.6 for the five strings this
    module pins -- and the deployed release, v0.19.0, sits inside that whole verified range. The one
    KNOWN drift risk is not version-range-dependent at all -- it is Kueue's
    ``UnadmittedWorkloadsObservability`` feature gate, CONFIRMED OFF on the real cluster today (see
    above) but enableable by the operator on this exact deployed release without any upgrade,
    replacing ``"Pending"``/``"Inadmissible"`` with granular reasons (``WaitingForQuota``,
    ``NoMatchingFlavor``, ``Suspended``, ``Misconfigured``, ...). That is exactly the drift
    ``test_unknown_workload_disposition_is_loud_not_silent`` (in
    ``tests/analyze/recovery_cloud/pending/test_reconcile.py``) proves is now surfaced loudly rather
    than silently held, should the operator ever flip it.

Kubernetes ``"Shutdown"`` note: real at v1.20.0 and v1.21.0 (confirmed directly against both tags'
``pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go``:
``nodeShutdownReason = "Shutdown"`` at both), renamed to ``"Terminated"`` at v1.22.0
(``pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go``: ``nodeShutdownReason = "Terminated"``,
kubernetes/kubernetes#102840). Kept in ``K8S_NODE_LOSS_POD_STATUS_REASONS`` as a defensive/historical
member even though the confirmed-deployed v1.36.2 no longer produces it -- see
``tests/vendor/kubernetes-v1.20.0-v1.21.0/nodeshutdown_reason.go``.
"""

from __future__ import annotations


# tests/vendor/kueue-v0.19.0/workload_types.go -- Workload condition TYPES.
KUEUE_WORKLOAD_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "Admitted",
        "QuotaReserved",
        "Evicted",
    }
)

# tests/vendor/kueue-v0.19.0/workload_types.go -- LEGACY reasons for QuotaReserved=False. These are
# what a default install (UnadmittedWorkloadsObservability feature gate OFF, its default at v0.19)
# still emits. The granular reasons the same file documents (WaitingForQuota, NoMatchingFlavor, ...)
# are deliberately EXCLUDED from this set: phaze does not recognise them yet, and that is exactly the
# drift test_kueue_k8s_reason_vocabulary's unknown-reason test exercises.
KUEUE_QUOTA_RESERVED_FALSE_REASONS: frozenset[str] = frozenset(
    {
        "Inadmissible",
        "Pending",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- batch/v1 Job condition TYPES.
K8S_JOB_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "Complete",
        "Failed",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- core/v1 Pod condition TYPES phaze reads.
K8S_POD_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "PodScheduled",
        "DisruptionTarget",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- PodScheduled=False reasons phaze matches.
K8S_POD_SCHEDULED_FALSE_REASONS: frozenset[str] = frozenset(
    {
        "Unschedulable",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- kubelet container state.waiting.reason
# values that mean the container can never start without operator action.
K8S_DEAD_BEFORE_START_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go (+ tests/vendor/kubernetes-v1.20.0-v1.21.0/ for
# "Shutdown") -- real pod status.reason values that mean "this pod is gone because of its node", each
# traced to its own producing source and k8s version range. "Shutdown" is HISTORICAL (real at k8s
# v1.20/v1.21 only, renamed to "Terminated" -- deliberately excluded, too generic -- by
# kubernetes/kubernetes#102840 in v1.22); kept for defensive breadth even though the version MEASURED
# in production (v1.36.2) no longer produces it. CORRECTION (phaze-pe41d review): an earlier revision
# of this module excluded "Shutdown" on the mistaken belief it was never a real k8s value anywhere --
# it was, at v1.20/v1.21 (see the vendor file for the direct source citation at both tags).
K8S_NODE_LOSS_POD_STATUS_REASONS: frozenset[str] = frozenset(
    {
        "NodeLost",
        "NodeShutdown",
        "Shutdown",
        "NodeAffinity",
        "Evicted",
    }
)

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- the DisruptionTarget condition's own reason
# vocabulary. NOT filtered on by phaze (only type + status=True are read) -- kept for documentation
# parity with kube_staging.py's docstring, which names these reasons for the operator log.
K8S_DISRUPTION_TARGET_REASONS: frozenset[str] = frozenset(
    {
        "TerminationByKubelet",
        "PreemptionByScheduler",
        # DeletionByTaintManager / DeletionByPodGC are controller-internal literals
        # (pkg/controller/tainteviction, pkg/controller/podgc) -- not part of any versioned API
        # package, and not independently re-fetched for this vendor snapshot since phaze does not
        # filter on them either.
    }
)
