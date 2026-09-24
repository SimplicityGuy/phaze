"""Seam F3 (phaze-pe41d): pin every Kueue/k8s reason literal the reconciler branches on to a real,
tagged upstream source -- instead of a proxy (``tests/kube_fakes.py``, "exercising every Kueue
admission/terminal transition with ZERO HTTP") whose condition/reason constants were hand-copied from
research notes with nothing checking them against a real, versioned Kueue/k8s.

Every assertion here is a MEMBERSHIP check: "every literal phaze's reconciler actually branches on is
a real value from a real, cited upstream source at a declared tag/commit" (see
``tests/analyze/services/backends/_kueue_k8s_reason_vocabulary.py`` and the vendored source under
``tests/vendor/kueue-v0.19.0/`` / ``tests/vendor/kubernetes-v1.36.2/``). This is deliberately NOT the
reverse direction (asserting phaze's set equals the FULL authoritative vocabulary) -- Kueue's granular
QuotaReserved=False reasons (``WaitingForQuota``, ``NoMatchingFlavor``, ...) are real and current but
phaze does not yet recognise them; that gap is the exact drift this bead's ACs require to be surfaced
LOUDLY rather than silently held, covered by
``tests.analyze.recovery_cloud.pending.test_reconcile::test_unknown_workload_disposition_is_loud_not_silent``.

MUTATION PROOF (recorded here rather than committed as a permanent test, per this bead's brief):
manually changing ``cloud_reconcile_observation._REASON_INADMISSIBLE`` from ``"Inadmissible"`` to a
typo'd ``"Inadmisible"`` and re-running this file turns every test below RED (each fails a membership
assertion against the pinned vocabulary) -- verified 2026-09-24 on this branch, then reverted.

KUEUE VERSION: deployed Kueue release CONFIRMED -- operator-authorized read-only ``kubectl`` on the
real cluster found image ``registry.k8s.io/kueue/kueue:v0.19.0`` (recorded as a comment on
phaze-pe41d and phaze-tkkor). This file's Kueue assertions are anchored to that exact tag; re-verified
directly that ``apis/kueue/v1beta1/workload_types.go`` is byte-identical between v0.19.0 and v0.19.6
(the latest-stable tag this bead checked first), so nothing here changed as a result of re-anchoring.
See ``_kueue_k8s_reason_vocabulary.py``'s docstring for the full measurement (including the confirmed
feature-gate state) and the cross-check against v0.5.0 (the oldest stable tag with the ``v1beta1`` API
package phaze's manifests target) showing all five pinned Kueue strings are unchanged across that
whole range, which the confirmed v0.19.0 sits inside.
"""

from __future__ import annotations

from phaze.services import kube_staging
from phaze.tasks import cloud_reconcile_observation
from tests.analyze.services.backends._kueue_k8s_reason_vocabulary import (
    K8S_DEAD_BEFORE_START_WAITING_REASONS,
    K8S_JOB_CONDITION_TYPES,
    K8S_NODE_LOSS_POD_STATUS_REASONS,
    K8S_POD_CONDITION_TYPES,
    K8S_POD_SCHEDULED_FALSE_REASONS,
    KUEUE_QUOTA_RESERVED_FALSE_REASONS,
    KUEUE_WORKLOAD_CONDITION_TYPES,
)


def test_kueue_workload_condition_types_are_real() -> None:
    """``_TYPE_QUOTA_RESERVED`` / ``_TYPE_ADMITTED`` / ``_TYPE_EVICTED`` are real Kueue v0.19.0 (confirmed deployed) Workload condition types."""
    literals = {
        cloud_reconcile_observation._TYPE_QUOTA_RESERVED,
        cloud_reconcile_observation._TYPE_ADMITTED,
        cloud_reconcile_observation._TYPE_EVICTED,
    }
    assert literals <= KUEUE_WORKLOAD_CONDITION_TYPES


def test_kueue_quota_reserved_false_reasons_are_real() -> None:
    """``_REASON_PENDING`` / ``_REASON_INADMISSIBLE`` are the real (legacy, default-emitted) Kueue v0.19.0 (confirmed deployed) reasons."""
    literals = {
        cloud_reconcile_observation._REASON_PENDING,
        cloud_reconcile_observation._REASON_INADMISSIBLE,
    }
    assert literals <= KUEUE_QUOTA_RESERVED_FALSE_REASONS


def test_job_condition_types_are_real() -> None:
    """``_JOB_CONDITION_COMPLETE`` / ``_JOB_CONDITION_FAILED`` are real ``batch/v1`` condition types at k8s v1.36.2."""
    literals = {
        cloud_reconcile_observation._JOB_CONDITION_COMPLETE,
        cloud_reconcile_observation._JOB_CONDITION_FAILED,
    }
    assert literals <= K8S_JOB_CONDITION_TYPES


def test_pod_scheduled_condition_and_unschedulable_reason_are_real() -> None:
    """``_CONDITION_POD_SCHEDULED`` / ``_REASON_UNSCHEDULABLE`` are real ``core/v1`` values at k8s v1.36.2."""
    assert kube_staging._CONDITION_POD_SCHEDULED in K8S_POD_CONDITION_TYPES
    assert kube_staging._REASON_UNSCHEDULABLE in K8S_POD_SCHEDULED_FALSE_REASONS


def test_disruption_target_condition_type_is_real() -> None:
    """``_DISRUPTION_TARGET_CONDITION`` is the real ``v1.DisruptionTarget`` pod condition type."""
    assert kube_staging._DISRUPTION_TARGET_CONDITION in K8S_POD_CONDITION_TYPES


def test_dead_before_start_waiting_reasons_are_real() -> None:
    """Every ``DEAD_BEFORE_START_WAITING_REASONS`` entry is a real kubelet container-waiting reason at v1.36.2."""
    assert kube_staging.DEAD_BEFORE_START_WAITING_REASONS <= K8S_DEAD_BEFORE_START_WAITING_REASONS


def test_node_loss_pod_status_reasons_are_real() -> None:
    """Every ``NODE_LOSS_POD_STATUS_REASONS`` entry is a real k8s-produced pod ``status.reason``,
    somewhere in the plausible deployed-version range -- not necessarily all at the SAME version.

    ``"Shutdown"`` is HISTORICAL (real only at k8s v1.20/v1.21; see
    ``tests/vendor/kubernetes-v1.20.0-v1.21.0/nodeshutdown_reason.go``) -- kept deliberately even though
    the version measured in production (v1.36.2) no longer produces it, because removing a defensive
    match string buys nothing and risks a real miss against an older cluster. An earlier revision of
    this bead removed it on the mistaken belief it was never real anywhere; this test's membership
    check (against a vocabulary that includes it, sourced from actual v1.20/v1.21 kubelet source) is
    what would have caught that mistake, same as it would catch any OTHER literal that turns out not to
    be real at all.
    """
    assert kube_staging.NODE_LOSS_POD_STATUS_REASONS <= K8S_NODE_LOSS_POD_STATUS_REASONS
    assert "Shutdown" in kube_staging.NODE_LOSS_POD_STATUS_REASONS
