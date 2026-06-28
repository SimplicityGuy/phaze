"""Shared fake-kube test substrate (Phase 54, Plan 03 -- Layer-1 logic fakes).

The reconcile/submit state-machine tests (Plans 05/06) drive the kube seam by
monkeypatching ``kube_staging`` and returning canned status objects from these
plain factories -- exercising every Kueue admission/terminal transition with ZERO
HTTP (the high-value coverage lives here). kr8s surfaces ``.status``/``.spec`` as
dicts and ``.metadata`` as an attribute object, so the fakes mirror that shape with
``SimpleNamespace`` rather than dragging real kr8s objects into logic tests.

The named constants (``PENDING`` / ``INADMISSIBLE`` / ``ADMITTED`` / ``EVICTED`` /
``QUOTA_RESERVED``) are the exact ``(type, status, reason)`` Workload-condition tuples
from RESEARCH Status -> Outcome Mapping; importing them keeps the logic tests pinned to
the verified Kueue condition vocabulary.
"""

from __future__ import annotations

from types import SimpleNamespace


def fake_workload(*conditions: tuple[str, str, str], owner_uid: str | None = None, name: str = "phaze-analyze-job-wl") -> SimpleNamespace:
    """Return a canned Kueue Workload stand-in.

    ``conditions`` are ``(type, status, reason)`` tuples placed under
    ``.status["conditions"]`` exactly as Kueue reports them. ``owner_uid`` (when set)
    stamps ``.metadata.ownerReferences`` with a single ``{"uid": owner_uid}`` entry so the
    ``get_workload_for`` owner-reference fallback path (A2 de-risk) can be exercised; an
    unset ``owner_uid`` yields an empty ``ownerReferences`` list.
    """
    return SimpleNamespace(
        status={"conditions": [{"type": t, "status": s, "reason": r} for (t, s, r) in conditions]},
        metadata=SimpleNamespace(
            name=name,
            ownerReferences=[{"uid": owner_uid}] if owner_uid else [],
        ),
    )


def fake_job(succeeded: int = 0, failed: int = 0, suspend: bool = False, uid: str = "uid-1", name: str = "phaze-analyze-fake") -> SimpleNamespace:
    """Return a canned batch/v1 Job stand-in.

    ``.status`` carries ``succeeded``/``failed`` counters (the primary terminal signals
    with ``backoffLimit: 0``); ``.spec["suspend"]`` reflects whether Kueue has un-gated the
    Job; ``.metadata`` exposes the ``uid`` (the Workload-discovery key) and ``name``.
    """
    return SimpleNamespace(
        status={"succeeded": succeeded, "failed": failed},
        spec={"suspend": suspend},
        metadata=SimpleNamespace(uid=uid, name=name),
    )


# Canned Workload condition sets -- the exact (type, status, reason) tuples from
# RESEARCH Status -> Outcome Mapping (verified against Context7 /kubernetes-sigs/kueue).
PENDING = fake_workload(("QuotaReserved", "False", "Pending"))
INADMISSIBLE = fake_workload(("QuotaReserved", "False", "Inadmissible"))
ADMITTED = fake_workload(("QuotaReserved", "True", ""), ("Admitted", "True", ""))
EVICTED = fake_workload(("Evicted", "True", "WorkloadInactive"))
QUOTA_RESERVED = fake_workload(("QuotaReserved", "True", ""))
