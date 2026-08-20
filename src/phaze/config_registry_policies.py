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

    # phaze-1sgee: the backend-id Counter below is deliberately KIND-AGNOSTIC — keyed on `backend.id`
    # alone, NOT on `(id, kind)`. `resolve_compute_backend` (services/backends.py) builds a
    # `{backend.id: backend}` dict over the full registry — the exact silently-collapses-to-LAST shape
    # the bucket-id Counter above guards against — and every backend's cap accounting scopes
    # `COUNT(cloud_job WHERE backend_id == self.id)`, so two entries sharing an id double-count each
    # other's in-flight rows. A compute and a kueue backend sharing an id is the NASTIEST variant,
    # because `resolve_compute_backend`'s `kind == "compute"` filter and the drain snapshot then resolve
    # genuinely inconsistent views of "the backend named <id>". Scoping the Counter to same-kind
    # collisions only — a plausible "fix" for a perceived false positive — would silently reopen this.
    # Report both the offending ids and their kinds so the operator can find the copy-paste.
    backend_dupes = sorted(backend_id for backend_id, count in Counter(backend.id for backend in backends).items() if count > 1)
    if backend_dupes:
        id_kinds = {backend_id: sorted(backend.kind for backend in backends if backend.id == backend_id) for backend_id in backend_dupes}
        raise ValueError(f"duplicate backend ids in registry: {backend_dupes} (kinds: {id_kinds}) — each [[backends]] id must be unique")


def validate_unique_compute_agent_refs(backends: Sequence[BackendConfig]) -> None:
    """Reject two compute backends statically bound to the same non-null agent ref.

    This is intentionally not an existence check.  Agents register dynamically, so a unique
    ref naming an agent that has not checked in yet must remain legal and degrade to a runtime
    hold rather than a controller boot failure.

    D-04: this is a STATIC check only — a Counter over config values, mirroring the bucket-id and
    backend-id idioms above — so it opens no DB session.  Skip ``agent_ref is None`` so the
    per-variant ``ComputeBackend._require_dispatch_fields`` "requires an agent_ref" message is never
    masked by this container-level guard.  ``ComputeBackend`` already requires a non-null
    ``agent_ref`` under normal validation, which makes the filter look redundant — it would only ever
    matter for an instance built via ``model_construct`` that bypasses that per-variant check.  That
    is exactly why it stays: without it, two such null-``agent_ref`` entries would collide in the
    Counter and raise a confusing "duplicate compute agent_ref(s): [None]" here instead of letting
    the clearer per-entry "requires an agent_ref" error surface first.
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
        # phaze-ru9oe: fail fast on a duplicate bucket id WITHIN one backend's own `buckets` list (a
        # copy-paste duplicate, not a cross-backend share). Left unchecked, resolving `backend.buckets`
        # positionally below appends `backend.id` once per LIST ENTRY into `cluster_specific_refs`, so a
        # single backend listing the same cluster-specific bucket twice falsely trips the D-09
        # cross-backend cardinality guard in `validate_cluster_specific_sharing` — it reports the SAME
        # backend id twice as if two distinct backends shared the bucket. For scope=shared the same
        # duplicate silently double-weights the bucket in `pick_bucket`'s candidates.
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
