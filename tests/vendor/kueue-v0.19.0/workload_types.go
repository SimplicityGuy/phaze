// Vendored (phaze-pe41d), NOT compiled -- reference material only.
//
// *** DEPLOYED KUEUE RELEASE: CONFIRMED. *** Operator-authorized read-only kubectl on the real
// cluster (recorded as a comment on phaze-pe41d and phaze-tkkor): image
// registry.k8s.io/kueue/kueue:v0.19.0; k8s Server Version v1.36.2+k0s (matches this bead's
// Kubernetes anchor); API served [v1beta2, v1beta1], preferred v1beta2 (phaze's own
// [backends.kube].workload_api_version explicitly requests v1beta1, so it gets v1beta1-shaped
// objects regardless of the server's preference -- the string VALUES on the wire are identical
// either way, see the v1beta1/v1beta2 note below); feature gates: DisableWaitForPodsReady=true
// ONLY, so UnadmittedWorkloadsObservability is OFF, confirming the "Pending"/"Inadmissible" legacy
// reasons below are what production actually emits, not merely the documented default.
//
// This snapshot is anchored to v0.19.0 -- the CONFIRMED deployed tag, not the latest stable
// (v0.19.6, checked earlier in this bead's history and kept as corroborating evidence below).
//
// v0.19.0 vs v0.19.6 diff (re-verified directly, phaze-pe41d):
//   - apis/kueue/v1beta1/workload_types.go: BYTE-IDENTICAL (`diff` exit 0) -- every string this
//     snapshot pins is unchanged.
//   - apis/kueue/v1beta2/workload_types.go: ONE constant added (WorkloadDRAResourcesResolved =
//     "DRAResourcesResolved", unrelated to anything phaze reads) -- no change to any string phaze
//     pins.
//   - pkg/scheduler/scheduler.go's `condReason := workload.UnadmittedWorkloadReasonWithFallback(
//     e.quotaReservedReason, "Pending")` -- IDENTICAL at both tags (the "Pending" fallback literal
//     is unchanged).
//   - pkg/features/kube_features.go's UnadmittedWorkloadsObservability entry: `Default: false` at
//     BOTH tags (the observed behaviour is identical); only its PreRelease stage label differs
//     (Beta at v0.19.0, Alpha at v0.19.6) -- a documentation/classification change, not a behaviour
//     change, and irrelevant to phaze since Default stayed false either way.
//
// Source:  https://github.com/kubernetes-sigs/kueue
// Tag:     v0.19.0
// Commit:  911a822a49bcfd99c9c62203a009efa4130ad604
// Path:    apis/kueue/v1beta1/workload_types.go  (condition-type block) and
//          apis/kueue/v1beta2/workload_types.go  (WorkloadPending -- see note below)
//
// Fetched via:
//   gh api repos/kubernetes-sigs/kueue/git/refs/tags/v0.19.0 -q '.object'                 # -> annotated tag object
//   gh api repos/kubernetes-sigs/kueue/git/tags/7b3cad14a540cd256dc9c658be5a23fff809ff68 -q '.object.sha'   # -> commit sha above
//   curl -sL https://raw.githubusercontent.com/kubernetes-sigs/kueue/v0.19.0/apis/kueue/v1beta1/workload_types.go
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
