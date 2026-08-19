"""Registry resolution: ``[[backends]]`` config entries -> :class:`Backend` impls, and the two inverses.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df). This is the
seam the 2026-08-08 bug hunt's duplicate-``[[backends]]``-id silent-last-wins defect lived in, so it
is kept small and separately testable on purpose --
``tests/analyze/services/backends/test_registry_duplicate_ids.py`` characterizes exactly what each of
the three functions does with a duplicated id and where the real guard now sits
(``ControlSettings`` validation, phaze-1sgee).

Three functions, three different callers:

* :func:`resolve_backends` -- the drain / reconcile cron / lane snapshot: ALL backends, N non-local.
* :func:`resolve_compute_backend` -- the authoritative inverse of a recorded ``cloud_job.backend_id``.
* :func:`resolved_non_local_kind` -- the single-kind question the three non-drain callers ask.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from phaze.services.backends.compute_agent import ComputeAgentBackend
from phaze.services.backends.kueue import KueueBackend
from phaze.services.backends.local import LocalBackend


if TYPE_CHECKING:
    from phaze.config import ControlSettings
    from phaze.config_backends import ComputeBackend
    from phaze.services.backends.base import Backend


logger = structlog.get_logger(__name__)


def resolve_backends(settings: ControlSettings) -> list[Backend]:
    """Build one :class:`Backend` impl per registry entry -- N non-local backends supported (Phase 69, SCHED-01).

    Phase 69 (SCHED-01) removes the Phase-68 ``>1``-non-local boot guard: multi-backend simultaneous
    dispatch is exactly this phase's job, so a registry with N non-local entries now resolves to a full
    ``list[Backend]`` of length N (+ any locals). The tiered drain
    (``release_awaiting_cloud.stage_cloud_window``) iterates this list, snapshots each backend's
    ``is_available`` / ``in_flight_count`` once per tick, and routes each candidate via the pure
    ``select_backend`` policy. Each impl binds to its Phase-67 discriminated-union submodel (``config``).

    The historical ``>1``-non-local defense-in-depth is retained ONLY for the non-drain call sites that
    still assume a single non-local kind (pipeline dashboard / backfill, agent_s3) -- it lives in
    :func:`resolved_non_local_kind` (WR-01), which those callers use; the drain no longer consults it.
    """
    resolved: list[Backend] = []
    for entry in settings.backends:
        if entry.kind == "local":
            resolved.append(LocalBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "compute":
            resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "kueue":
            resolved.append(KueueBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))

    return resolved


def resolve_compute_backend(cfg: ControlSettings, backend_id: str | None) -> ComputeBackend | None:
    """Resolve a recorded ``cloud_job.backend_id`` to its ``ComputeBackend`` registry entry (D-06).

    The single AUTHORITATIVE inverse of ``ComputeAgentBackend.dispatch``'s ``backend_id`` stamp: every
    downstream scratch / terminalization reader (the Plan-02 rsync destination, the Plan-03 ``/pushed`` +
    ``/mismatch`` callbacks) resolves the value RECORDED on ``cloud_job.backend_id`` through here rather
    than re-deriving it. Mirrors ``s3_staging.resolve_bucket_config`` exactly: pure + ORM-free, reads only
    ``cfg.backends``.

    Returns ``None`` when ``backend_id`` is ``None`` (an all-local / unstamped row), or when the id names
    no ``kind == "compute"`` entry (a kueue/local id, or an operator-removed backend) so the caller can
    skip the compute op cleanly.
    """
    if backend_id is None:
        return None
    return {backend.id: backend for backend in cfg.backends if backend.kind == "compute"}.get(backend_id)


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived cloud-lane kind: ``"local"`` when all-local, else the non-local kind.

    The single seam the non-drain single-kind callers use (the S3-upload-complete callback
    ``agent_s3.report_uploaded``, the ``/pipeline/stats`` poll ``build_dashboard_context``, and the
    backfill route): they only ask "is the cloud lane kueue?". ``"local"`` when ``cloud_enabled`` is
    False.

    Phase 70 (MKUE-01, sibling of the Pitfall-1 ``active_compute_scratch_dir`` fix): the callers 500'd
    the moment a 2nd Kueue backend was declared, because the old ``>1``-non-local blanket raise fired on
    the literal MKUE-01 scenario. Generalize: when ANY non-local backend is ``"kueue"``, return
    ``"kueue"`` -- this tolerates N Kueue backends AND a local + N-Kueue + 1-compute registry (the
    callers degrade gracefully by construction, no per-site try/except needed). Phase 72 (MCOMP-01,
    D-03) retires the compute-only ``>1`` fail-fast too: the compute-only branch now returns ``"compute"``
    for N compute backends (per-agent dispatch attribution lands in Phase 73). All-local -> ``"local"``,
    single-kueue -> ``"kueue"``, single-compute -> ``"compute"`` stay byte-identical.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    if any(backend.kind == "kueue" for backend in non_local):
        return "kueue"
    # No kueue backend -> compute-only. Phase 72 (D-03) retired the ambiguous >1-compute fail-fast; the
    # compute-only branch returns "compute" for any N compute (per-agent attribution lands in Phase 73).
    return non_local[0].kind
