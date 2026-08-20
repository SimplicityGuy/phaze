"""Pure, ordered cross-entry policies for the control-plane backend registry.

Pydantic owns parsing and per-entry validation in :mod:`phaze.config_backends`; this module
owns only deterministic checks that need the complete registry.  These helpers intentionally
perform no I/O and know nothing about database-backed agent check-ins.  In particular, a
compute ``agent_ref`` that has not checked in yet is legal at boot; only duplicate static
bindings are rejected here.

The call order in ``ControlSettings._validate_registry`` is part of the operator-facing
contract because it determines which exact configuration error wins when several invariants
are broken at once.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from phaze.config_backends import ComputeBackend, KueueBackend


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.config_backends import BackendConfig, BucketConfig


ClusterSpecificReferences = dict[str, list[str]]


def validate_non_empty_registry(backends: Sequence[BackendConfig]) -> None:
    """Reject a present-but-empty registry before every other cross-entry check."""
    if not backends:
        raise ValueError("backend registry resolved to empty — refusing to start (REG-04)")


def validate_unique_registry_ids(backends: Sequence[BackendConfig], buckets: Sequence[BucketConfig]) -> None:
    """Reject duplicate bucket ids first, then duplicate backend ids.

    Both downstream lookup surfaces build id-keyed dictionaries and would otherwise silently
    select the last entry.  Bucket ids deliberately win over backend ids when both are broken,
    matching the validator's established order.
    """
    bucket_dupes = sorted(bucket_id for bucket_id, count in Counter(bucket.id for bucket in buckets).items() if count > 1)
    if bucket_dupes:
        raise ValueError(f"duplicate bucket ids in registry: {bucket_dupes} — each [[buckets]] id must be unique (REG-05)")

    backend_dupes = sorted(backend_id for backend_id, count in Counter(backend.id for backend in backends).items() if count > 1)
    if backend_dupes:
        id_kinds = {backend_id: sorted(backend.kind for backend in backends if backend.id == backend_id) for backend_id in backend_dupes}
        raise ValueError(f"duplicate backend ids in registry: {backend_dupes} (kinds: {id_kinds}) — each [[backends]] id must be unique")


def validate_unique_compute_agent_refs(backends: Sequence[BackendConfig]) -> None:
    """Reject two compute backends statically bound to the same non-null agent ref.

    This is intentionally not an existence check.  Agents register dynamically, so a unique
    ref naming an agent that has not checked in yet must remain legal and degrade to a runtime
    hold rather than a controller boot failure.
    """
    compute_agent_refs = [backend.agent_ref for backend in backends if isinstance(backend, ComputeBackend) and backend.agent_ref is not None]
    agent_dupes = sorted(agent_ref for agent_ref, count in Counter(compute_agent_refs).items() if count > 1)
    if not agent_dupes:
        return
    collisions = {
        agent_ref: sorted(backend.id for backend in backends if isinstance(backend, ComputeBackend) and backend.agent_ref == agent_ref)
        for agent_ref in agent_dupes
    }
    raise ValueError(
        f"duplicate compute agent_ref(s) {agent_dupes} bound by backends {collisions} — each compute backend must bind a distinct agent_ref (D-04)"
    )


def validate_backend_bucket_lists(
    backends: Sequence[BackendConfig],
    buckets: Sequence[BucketConfig],
) -> ClusterSpecificReferences:
    """Validate each kueue bucket list in backend order and collect D-09 references.

    Within each backend the established failure order is unknown ids, duplicate entries, then
    an empty resolved set.  The returned mapping preserves first bucket-reference order and
    backend order so the later sharing-cardinality error text remains byte-for-byte stable.
    """
    bucket_by_id = {bucket.id: bucket for bucket in buckets}
    cluster_specific_refs: ClusterSpecificReferences = {}
    for backend in backends:
        if not isinstance(backend, KueueBackend):
            continue
        missing = [bucket_id for bucket_id in backend.buckets if bucket_id not in bucket_by_id]
        if missing:
            raise ValueError(f"backend {backend.id!r} references unknown bucket ids {missing} (D-08)")
        within_backend_dupes = sorted(bucket_id for bucket_id, count in Counter(backend.buckets).items() if count > 1)
        if within_backend_dupes:
            raise ValueError(
                f"backend {backend.id!r} lists duplicate bucket ids {within_backend_dupes} in its own buckets list — each id must appear once"
            )
        resolved = [bucket_by_id[bucket_id] for bucket_id in backend.buckets]
        if not resolved:
            raise ValueError(f"backend {backend.id!r} (kueue) resolves to an empty bucket set (D-08)")
        for bucket in resolved:
            if bucket.scope == "cluster-specific":
                cluster_specific_refs.setdefault(bucket.id, []).append(backend.id)
    return cluster_specific_refs


def validate_cluster_specific_sharing(cluster_specific_refs: ClusterSpecificReferences) -> None:
    """Reject a cluster-specific bucket referenced by more than one kueue backend."""
    for bucket_id, refs in cluster_specific_refs.items():
        if len(refs) > 1:
            raise ValueError(
                f"bucket {bucket_id!r} is scope=cluster-specific but referenced by {len(refs)} kueue backends {refs} — at most one allowed (D-09)"
            )
