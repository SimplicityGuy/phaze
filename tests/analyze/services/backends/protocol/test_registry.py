"""Tests for `services/backends/registry.py` (split from test_backends.py, phaze-7l8jh).

`resolve_backends` / `resolve_compute_backend` / `resolved_non_local_kind` -- `services/backends/registry.py`.
"""

from __future__ import annotations

from tests.analyze.services.backends.protocol._shared import (
    _LOCAL_2KUEUE_HEAD,
    _TWO_BUCKETS,
    Any,
    backends,
)


# === resolved_non_local_kind: N-Kueue-safe (any-kueue) + compute-only fail-fast ===========


def test_resolved_non_local_kind_returns_compute_for_multiple_compute_only(backends_toml_env: Any) -> None:
    """The compute-only ``>1`` fail-fast is RETIRED (D-03): two COMPUTE backends (no kueue) return "compute".

    Phase 70 (MKUE-01) generalized ``resolved_non_local_kind`` to tolerate N Kueue backends; Phase 72
    (MCOMP-01, D-03) generalizes the compute-only branch the same way -- N compute backends resolve to
    "compute" with NO raise (per-agent dispatch attribution lands in Phase 73). The discretion
    confirmation that the compute-only branch still yields "compute" for N compute.
    """
    from phaze.config import ControlSettings

    backends_toml_env(
        """
        [[backends]]
        kind = "compute"
        id = "compute-a"
        rank = 10
        cap = 2
        agent_ref = "agent-a"
        scratch_dir = "/scratch/a"
        push_host = "a.push"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 2
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        push_host = "b.push"
        """
    )
    settings = ControlSettings()
    assert settings.cloud_enabled is True
    # D-03: the compute-only >1 fail-fast is retired; N compute resolves to "compute" without raising.
    assert backends.resolved_non_local_kind(settings) == "compute"


def test_resolved_non_local_kind_returns_kueue_for_n_kueue(backends_toml_env: Any) -> None:
    """MKUE-01: ANY-kueue registry resolves to "kueue" with NO raise -- the literal N-cluster scenario.

    A local + 2-Kueue registry (the milestone target) previously 500'd every ``resolved_non_local_kind``
    call site (report_uploaded / build_dashboard_context / backfill) via the old blanket ``>1`` raise.
    The generalized helper returns "kueue" (the callers only ask "is the cloud lane kueue"). Adding a
    compute backend to the mix STILL returns "kueue" (any-kueue wins).
    """
    from phaze.config import ControlSettings

    backends_toml_env(_LOCAL_2KUEUE_HEAD + _TWO_BUCKETS)
    settings = ControlSettings()
    assert backends.resolved_non_local_kind(settings) == "kueue"

    compute_block = """
    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
    push_host = "oci-a1.push.example"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()
    assert backends.resolved_non_local_kind(settings) == "kueue"


# === SCHED-01: resolve_backends supports N non-local backends (Phase-69 guard removal) ====


def test_resolve_backends_returns_all_non_local(backends_toml_env: Any) -> None:
    """SCHED-01: a registry of 2+ non-local backends resolves to a list of that length -- no ValueError.

    Phase 69 removed the Phase-68 ``>1``-non-local boot guard from :func:`resolve_backends` (multi-backend
    simultaneous dispatch is exactly this phase's job). The registry must now resolve cleanly to N
    ``Backend`` impls so the tiered drain can snapshot + route across all of them. The single-kind
    fail-fast survives only in :func:`resolved_non_local_kind` (asserted above), never here.
    """
    from phaze.config import ControlSettings

    backends_toml_env(
        """
        [[backends]]
        kind = "compute"
        id = "compute-a"
        rank = 10
        cap = 2
        agent_ref = "agent-a"
        scratch_dir = "/scratch/a"
        push_host = "a.push"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 3
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        push_host = "b.push"

        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 4
        """
    )
    settings = ControlSettings()
    resolved = backends.resolve_backends(settings)

    # All three entries resolve (2 non-local + 1 local) -- no ValueError on the 2 non-local backends.
    assert len(resolved) == 3
    non_local = [b for b in resolved if not isinstance(b, backends.LocalBackend)]
    assert len(non_local) == 2
    assert {b.id for b in non_local} == {"compute-a", "compute-b"}


# === D-06: resolve_compute_backend inverse-lookup (backend_id -> ComputeBackend) ==========


def test_resolve_compute_backend(backends_toml_env: Any) -> None:
    """D-06: the authoritative inverse-lookup returns the compute entry by id; None for miss/non-compute.

    resolve_compute_backend(cfg, None) -> None; an unknown id -> None; a real compute id -> that
    ComputeBackend; a kueue/local id -> None (only kind==compute entries are considered). Every
    downstream scratch/terminalization reader resolves a recorded cloud_job.backend_id through this.
    """
    from phaze.config import ControlSettings

    compute_block = """
    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
    push_host = "oci-a1.push.example"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()

    assert backends.resolve_compute_backend(settings, None) is None
    assert backends.resolve_compute_backend(settings, "does-not-exist") is None
    hit = backends.resolve_compute_backend(settings, "oci-a1")
    assert hit is not None
    assert hit.id == "oci-a1"
    assert hit.kind == "compute"
    assert hit.push_host == "oci-a1.push.example"
    # A kueue id and the local id are NOT compute entries -> None (kind-filtered).
    assert backends.resolve_compute_backend(settings, "kueue-a") is None
    assert backends.resolve_compute_backend(settings, "local") is None


# === MCOMP-03: per-file compute scratch resolution under local + 2 Kueue + 1 compute ======


def test_resolve_compute_backend_scratch_under_local_2kueue_1compute(backends_toml_env: Any) -> None:
    """MCOMP-03: local + 2 Kueue + 1 compute resolves the compute scratch_dir per file -- no global accessor.

    The transitional ``active_compute_scratch_dir`` global was RETIRED in Phase 73: scratch is now
    resolved PER FILE from the recorded ``cloud_job.backend_id`` via ``resolve_compute_backend`` (the
    ``/pushed`` reader was rewired in Plan 03). This pins the milestone's target deploy (≥2 non-local
    backends) resolving cleanly to the sole compute backend's ``scratch_dir`` through the per-file path;
    a kueue id resolves to None (only ``kind == "compute"`` entries are considered).
    """
    from phaze.config import ControlSettings

    compute_block = """
    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
    push_host = "oci-a1.push.example"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()
    backend = backends.resolve_compute_backend(settings, "oci-a1")
    assert backend is not None
    assert backend.scratch_dir == "/srv/scratch"
    # A kueue id is not a compute entry -> None (per-file resolution never mis-attributes to a cluster).
    assert backends.resolve_compute_backend(settings, "kueue-a") is None
