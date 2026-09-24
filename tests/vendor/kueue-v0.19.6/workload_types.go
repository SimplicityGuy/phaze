// Vendored (phaze-pe41d), NOT compiled -- reference material only.
//
// Source:  https://github.com/kubernetes-sigs/kueue
// Tag:     v0.19.6  (published 2026-09-24T13:28:08Z)
// Commit:  f0e95cea51b5591e0177fbe0fa06a299f36db548
// Path:    apis/kueue/v1beta1/workload_types.go  (condition-type block) and
//          apis/kueue/v1beta2/workload_types.go  (WorkloadPending -- see note below)
//
// Fetched via:
//   gh api "repos/kubernetes-sigs/kueue/contents/apis/kueue/v1beta1/workload_types.go?ref=v0.19.6" -q '.download_url'
//   curl -sL https://raw.githubusercontent.com/kubernetes-sigs/kueue/v0.19.6/apis/kueue/v1beta1/workload_types.go
//
// This is the AUTHORITATIVE source phaze's Kueue reason literals are pinned against
// (tests/analyze/services/backends/_kueue_k8s_reason_vocabulary.py,
// tests/analyze/services/backends/test_kueue_k8s_reason_vocabulary.py). Only the excerpts that
// phaze's reconciler (src/phaze/tasks/cloud_reconcile_observation.py,
// src/phaze/tasks/reconcile_cloud_jobs.py) actually branches on are kept; the full file has many more
// condition/reason constants unrelated to phaze's classification.
//
// v1beta1 vs v1beta2 note: the string VALUES below are identical in both API versions (re-verified
// directly against apis/kueue/v1beta2/workload_types.go at the same tag/commit) -- Kueue's own docs
// (phaze's docs/k8s-burst.md "apiVersion lockstep" section) state the admission-condition fields
// phaze reads are unchanged between the two, and this vendor snapshot confirms it at the string
// level. ``WorkloadPending`` is declared only in the v1beta1 package as
// ``kueue.WorkloadPending`` via the v1beta2 alias used internally by pkg/controller/core -- fetched
// separately from apis/kueue/v1beta2/workload_types.go at the same tag/commit and inlined below for
// completeness.

const (
	// WorkloadAdmitted means that the Workload has reserved quota and all the admissionChecks
	// defined in the ClusterQueue are satisfied.
	WorkloadAdmitted = "Admitted"

	// WorkloadQuotaReserved means that the Workload has reserved quota a ClusterQueue.
	WorkloadQuotaReserved = "QuotaReserved"

	// WorkloadEvicted means that the Workload was evicted. The possible reasons
	// for this condition are:
	// - "Preempted": the workload was preempted
	// - "PodsReadyTimeout": the workload exceeded the PodsReady timeout
	// - "AdmissionCheck": at least one admission check transitioned to False
	// - "ClusterQueueStopped": the ClusterQueue is stopped
	// - "Deactivated": the workload has spec.active set to false
	// When a workload is preempted, this condition is accompanied by the "Preempted"
	// condition which contains a more detailed reason for the preemption.
	WorkloadEvicted = "Evicted"
)

// Reasons for the QuotaReserved=False condition.
const (
	// WorkloadInadmissible means that the Workload can't reserve quota
	// due to LocalQueue or ClusterQueue doesn't exist or inactive.
	WorkloadInadmissible = "Inadmissible"

	// WorkloadPending indicates that the workload is pending evaluation or scheduling.
	// LEGACY / deprecated (SA1019) fallback reason -- what a default install still emits, because the
	// more granular reasons below are gated behind the UnadmittedWorkloadsObservability feature.
	WorkloadPending = "Pending"
)

// pkg/controller/core/workload_controller.go's schedulerSetReasons -- the GRANULAR QuotaReserved=False
// reasons introduced by KEP 10852 ("inadmissible workloads observability"), returned INSTEAD OF
// WorkloadInadmissible/WorkloadPending only when the UnadmittedWorkloadsObservability feature gate is
// enabled (site/data/featuregates/versioned_feature_list.yaml: Alpha, default=false at v0.19). THIS IS
// THE DRIFT phaze-pe41d's bead worries about, already shipped upstream and one feature-gate flip away
// from being the default:
//
//	WorkloadQuotaReservedReasonWaitingForQuota
//	WorkloadQuotaReservedReasonNoMatchingFlavor
//	WorkloadQuotaReservedReasonExceedsMaxQuota
//	WorkloadQuotaReservedReasonTopologyPlacementFailed
//	WorkloadQuotaReservedReasonWaitingForPreemptedWorkloads
//	WorkloadQuotaReservedReasonWaitingForPodsReady
//	WorkloadQuotaReservedReasonPendingEvaluation
//	WorkloadQuotaReservedReasonSuspended
//	WorkloadQuotaReservedReasonMisconfigured
//
// pkg/workload/workload.go:
//
//	func UnadmittedWorkloadReasonWithFallback(granularReason, fallback string) string {
//		if features.Enabled(features.UnadmittedWorkloadsObservability) {
//			return granularReason
//		}
//		return fallback
//	}
