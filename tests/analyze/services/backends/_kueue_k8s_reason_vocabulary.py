"""Authoritative Kueue/Kubernetes reason vocabulary, pinned to a real, tagged upstream (phaze-pe41d).

Every frozenset below is transcribed directly from the vendored source excerpts under
``tests/vendor/kueue-v0.19.6/`` and ``tests/vendor/kubernetes-v1.36.2/`` -- see those files for the
exact upstream repo, tag, commit sha and the ``curl``/``gh api`` command used to fetch each one.

This module exists so :mod:`test_kueue_k8s_reason_vocabulary` can assert that every literal
``src/phaze/services/kube_staging.py`` and ``src/phaze/tasks/cloud_reconcile_observation.py`` branch
on is a MEMBER of a real, versioned vocabulary -- replacing ``tests/kube_fakes.py``'s previous
"exercising every Kueue admission/terminal transition with ZERO HTTP" proxy, whose condition/reason
constants were hand-copied from research notes (one, ``"WorkloadInactive"``, was never a real Kueue
value at all -- see ``tests/kube_fakes.py``'s ``EVICTED`` fixture).

Kubernetes version: v1.36.2 -- the version MEASURED in production on host-compute (phaze's real Kueue
burst node); see the vendor file's header for the measurement citations. Kueue version: the exact
release deployed in production could not be determined from any repo config/manifest/doc (only the
API version, ``kueue.x-k8s.io/v1beta1``, is pinned in ``backends.toml``'s default and
``docs/k8s-burst.md``) -- v0.19.6 (the latest stable tag at the time this was written) is used as the
best-evidenced anchor. The read-only command to confirm the real deployed Kueue release is recorded
on phaze-tkkor.
"""

from __future__ import annotations


# tests/vendor/kueue-v0.19.6/workload_types.go -- Workload condition TYPES.
KUEUE_WORKLOAD_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "Admitted",
        "QuotaReserved",
        "Evicted",
    }
)

# tests/vendor/kueue-v0.19.6/workload_types.go -- LEGACY reasons for QuotaReserved=False. These are
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

# tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go -- real, CURRENT pod status.reason values that
# mean "this pod is gone because of its node", each traced to its own producing source. Deliberately
# does NOT include "Shutdown" (see the vendor file's PROVENANCE note): no real Kubernetes source at
# v1.36.2 -- or, per the cited 2021 upstream fix, likely ever -- produces that string as a pod
# status.reason.
K8S_NODE_LOSS_POD_STATUS_REASONS: frozenset[str] = frozenset(
    {
        "NodeLost",
        "NodeShutdown",
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
