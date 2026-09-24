// Vendored (phaze-pe41d), NOT compiled -- reference material only.
//
// Source: https://github.com/kubernetes/kubernetes
// Path:   pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go (pre-split; the manager lived in one
//         linux-only file at these tags, unlike v1.36.2's nodeshutdown_manager.go)
//
// Confirmed IDENTICAL at both tags:
//   v1.20.0: commit 1833d7d52ad70d4a740796ee2dc6c1eb47666b81 (tag object)
//     curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.20.0/pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go
//   v1.21.0:
//     curl -sL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.21.0/pkg/kubelet/nodeshutdown/nodeshutdown_manager_linux.go
//
// This is the ACTUAL status.Reason kubelet's graceful-node-shutdown manager stamped on a killed pod
// at these two releases -- "Shutdown", not "Terminated" (that rename is kubernetes/kubernetes#102840,
// merged 2021-06-16, first shipped in v1.22.0 -- see tests/vendor/kubernetes-v1.36.2/reason_vocabulary.go
// for the v1.22.0/v1.36.2 side of this history and the correction this file's addition records).
//
// PROVENANCE for kube_staging.py keeping "Shutdown" in NODE_LOSS_POD_STATUS_REASONS (phaze-pe41d,
// restored on review after an earlier revision of this bead removed it on the mistaken belief that
// no k8s version ever produced it): this file IS that proof.
const (
	nodeShutdownReason  = "Shutdown"
	nodeShutdownMessage = "Node is shutting, evicting pods"
)
