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

// --- pkg/kubelet/nodeshutdown/nodeshutdown_manager.go ---
// Fetched via: curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/pkg/kubelet/nodeshutdown/nodeshutdown_manager.go
//
// TWO DISTINCT, BOTH-REAL reasons -- do not conflate them (phaze's old comment did, see below):
const (
	// NodeShutdownNotAdmittedReason: a pod that FAILS ADMISSION because its node is already shutting
	// down. The pod never ran.
	NodeShutdownNotAdmittedReason = "NodeShutdown"

	// nodeShutdownReason: the status.Reason stamped on a pod kubelet's shutdown manager ACTUALLY
	// KILLS during a graceful node shutdown. NOT "Shutdown" (see the 2021 fix below) -- and phaze does
	// NOT match on this value (it is too generic to key node-loss detection on safely); the same kill
	// also stamps a DisruptionTarget=True/TerminationByKubelet condition, which
	// kube_staging.py's _node_lost_by_disruption_condition catches independently, reason-agnostic.
	nodeShutdownReason = "Terminated"
)

// PROVENANCE for removing "Shutdown" from phaze's NODE_LOSS_POD_STATUS_REASONS (phaze-pe41d):
// commit kubernetes/kubernetes@86acf3bc8f3594df1cc284bef7c7830caddec53f, "Fix nodeShutdownReason for
// node shutdown e2e" (merged 2021, PR #104540) changed an in-tree E2E test from asserting
// `pod.Status.Reason != "Shutdown"` to `pod.Status.Reason != nodeShutdownReason` (i.e. "Terminated") --
// evidence that "Shutdown" was never the real value kubelet produced, even when that test was
// originally written; it was a test-author assumption error, not a value that was later renamed.
// Fetched via: gh api repos/kubernetes/kubernetes/commits/86acf3bc8f3594df1cc284bef7c7830caddec53f -q '.files[].patch'

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
