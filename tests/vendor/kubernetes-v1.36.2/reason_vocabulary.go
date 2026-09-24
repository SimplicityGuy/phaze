// Vendored (phaze-pe41d), NOT compiled -- reference material only.
//
// Source:  https://github.com/kubernetes/kubernetes
// Tag:     v1.36.2
// Commit:  24e2b02af5543d7910c2bb074c7264df5a8f0467
//
// v1.36.2 was chosen because it is the k8s version MEASURED, in production, on host-compute --
// phaze's real Kueue burst node -- in docs/spikes/phaze-zaf2l-where-phaze-spends-time.md and
// docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md ("k0s v1.36.2 ... in the production
// registry and serving real traffic throughout"). k0s's own version numbers track the upstream
// Kubernetes minor/patch verbatim (confirmed: k0s tag v1.36.2+k0s.0 exists), so k0s v1.36.2 means
// Kubernetes v1.36.2. This is host-compute's KUBERNETES version, not phaze's separate Kueue
// installation on it -- Kueue's own deployed release could not be determined from any repo
// config/manifest/doc (checked: pyproject.toml, docs/k8s-burst.md, docs/configuration.md, every
// *.md under docs/ and .planning/ -- none pin an exact Kueue release/image tag, only the API version
// `kueue.x-k8s.io/v1beta1`); the read-only command to confirm it is recorded on phaze-tkkor.
//
// Each block below is fetched from its own file (kubelet/scheduler reason strings are NOT part of
// any single versioned API package -- they are scattered, unexported-by-convention implementation
// literals, which is itself evidence for this bead: unlike the Kueue Workload API's typed, documented
// condition/reason constants, several of the k8s-side strings below carry NO compatibility guarantee
// at all).
//
// This is the AUTHORITATIVE source phaze's k8s reason literals are pinned against
// (tests/analyze/services/backends/_kueue_k8s_reason_vocabulary.py,
// tests/analyze/services/backends/test_kueue_k8s_reason_vocabulary.py).

// --- staging/src/k8s.io/api/batch/v1/types.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/staging/src/k8s.io/api/batch/v1/types.go
const (
	// JobComplete means the job has completed its execution.
	JobComplete JobConditionType = "Complete"
	// JobFailed means the job has failed its execution.
	JobFailed JobConditionType = "Failed"
)

// --- staging/src/k8s.io/api/core/v1/types.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/staging/src/k8s.io/api/core/v1/types.go
const (
	PodScheduled     PodConditionType = "PodScheduled"
	// DisruptionTarget indicates the pod is about to be terminated due to a
	// disruption (preemption, eviction API, or garbage-collection).
	DisruptionTarget PodConditionType = "DisruptionTarget"

	// PodReasonUnschedulable reason in PodScheduled PodCondition means that the scheduler
	// can't schedule the pod right now, for example due to insufficient resources in the cluster.
	PodReasonUnschedulable = "Unschedulable"

	// PodReasonTerminationByKubelet reason in DisruptionTarget pod condition indicates that the
	// termination is initiated by kubelet. NOT filtered on by phaze (only the condition TYPE +
	// status=True are read; see kube_staging.py's _node_lost_by_disruption_condition) -- kept here
	// for completeness since it is the reason kubelet's own graceful-shutdown path stamps (see the
	// nodeshutdown_manager.go excerpt below).
	PodReasonTerminationByKubelet = "TerminationByKubelet"
)

// --- pkg/kubelet/images/types.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/kubelet/images/types.go
// (these are error VALUES, i.e. errors.New("...").Error() == the container waiting reason kubelet
// stamps on state.waiting.reason; extracted as the plain string each carries)
const (
	// ErrImagePullBackOff - Container image pull failed, kubelet is backing off image pull
	ErrImagePullBackOffReason = "ImagePullBackOff"
	// ErrImagePull - General image pull error
	ErrImagePullReason = "ErrImagePull"
	// ErrInvalidImageName - Unable to parse the image name.
	ErrInvalidImageNameReason = "InvalidImageName"
)

// --- pkg/kubelet/kuberuntime/kuberuntime_container.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/kubelet/kuberuntime/kuberuntime_container.go
const ErrCreateContainerConfigReason = "CreateContainerConfigError"

// --- pkg/util/node/node.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/util/node/node.go
const (
	// NodeUnreachablePodReason is the reason on a pod when its state cannot be confirmed as kubelet is unresponsive
	NodeUnreachablePodReason = "NodeLost"
)

// --- pkg/kubelet/eviction/helpers.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/kubelet/eviction/helpers.go
const (
	// Reason is the reason reported back in status (node-pressure eviction: memory/disk).
	EvictionReason = "Evicted"
)

// --- pkg/kubelet/nodeshutdown/nodeshutdown_manager.go (and its pre-split predecessor,
//     nodeshutdown_manager_linux.go, at older tags) ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/<tag>/pkg/kubelet/nodeshutdown/nodeshutdown_manager<_linux>.go
//
// THREE DISTINCT, ALL-REAL reasons across k8s history -- CORRECTED (phaze-pe41d review): an earlier
// version of this file claimed "Shutdown" was NEVER real anywhere, which was WRONG. It was the actual
// KILL reason at v1.20 and v1.21 (confirmed directly against both tags' source below), renamed to
// "Terminated" by kubernetes/kubernetes#102840 (merged 2021-06-16, first shipped in v1.22). The
// PROVENANCE note this file originally cited (commit 86acf3bc, "Fix nodeShutdownReason for node
// shutdown e2e", PR #104540) is REAL but was misread: that PR fixed an E2E TEST that had not been
// updated for #102840's rename, months after the rename shipped -- it is evidence the STRING CHANGED
// in 2021, not evidence "Shutdown" was never real. Lesson: a "some code got a string wrong" citation
// proves the string was wrong AT THAT COMMIT, not across all of history -- check the tags on both
// sides of a rename before concluding a string was never real.
//
//	v1.20.0: nodeShutdownReason = "Shutdown"    (pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go:40)
//	v1.21.0: nodeShutdownReason = "Shutdown"    (same file/line)
//	v1.22.0: nodeShutdownReason = "Terminated"  (pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go:39, PR #102840)
//	v1.36.2: nodeShutdownReason = "Terminated"  (pkg/kubelet/nodeshutdown/nodeshutdown_manager.go, this file's own fetch)
//
// phaze's NODE_LOSS_POD_STATUS_REASONS keeps "Shutdown" as a defensive/historical match (the deployed
// k8s version is v1.36.2, where it no longer fires, but removing a string that WAS real anywhere in
// the fleet's plausible version range buys nothing and risks a real miss) and deliberately does NOT
// add "Terminated" (too generic -- would misclassify any container-level termination as node loss;
// the modern, reason-agnostic DisruptionTarget/TerminationByKubelet condition below is the intended
// v1.22+ replacement signal, and phaze already reads it independently).
const (
	// NodeShutdownNotAdmittedReason: a pod that FAILS ADMISSION because its node is already shutting
	// down. The pod never ran. Real and unchanged at v1.36.2; did not exist yet at v1.20 (this
	// admission-rejection path was added later).
	NodeShutdownNotAdmittedReason = "NodeShutdown"

	// nodeShutdownReason: the status.Reason stamped on a pod kubelet's shutdown manager ACTUALLY
	// KILLS during a graceful node shutdown, AT v1.22+ (see the table above for v1.20/v1.21). phaze
	// does NOT match on this value -- too generic -- relying instead on the DisruptionTarget/
	// TerminationByKubelet condition the same kill also stamps.
	nodeShutdownReason = "Terminated"
)

// --- pkg/scheduler/framework/plugins/names/names.go + pkg/kubelet/lifecycle/predicate.go ---
// Fetched via:
//   curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/scheduler/framework/plugins/names/names.go
//   curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/kubelet/lifecycle/predicate.go
//
// names.go:  NodeAffinity = "NodeAffinity"   (the NodeAffinity filter plugin's registered Name)
// predicate.go's generalFilter() wraps a failed NodeAffinity filter as
// &PredicateFailureError{r.Name, r.Reason} (r.Name == "NodeAffinity"); the admission-failure switch
// then sets `reason = re.PredicateName` -- so a pod kubelet re-admits after a restart, whose node no
// longer satisfies its (bound) node affinity, gets status.Reason == "NodeAffinity".
const NodeAffinityPredicateName = "NodeAffinity"
