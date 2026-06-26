"""Nyquist Wave 0 stubs for the Phase 49 routing-seam reshape (CLOUDPIPE-01).

Scaffolding tests reserved by Phase 50 Plan 00. The routing seam must hold a long file in
``AWAITING_CLOUD`` for the bounded staging window to pick up — it must never enqueue
directly to the compute agent, bypassing the ≤N in-flight window. Plan 50-06 replaces each
``pytest.skip`` with real assertions. Imports are stdlib + pytest only — the reshaped
routing seam does not exist yet, so importing it would break collection.
"""

from __future__ import annotations

import pytest


_WAVE0_REASON = "Wave 0 stub — implemented in 50-06"


def test_long_file_routes_to_awaiting_cloud_not_compute() -> None:
    # CLOUDPIPE-01: a long file is parked in AWAITING_CLOUD, not enqueued straight to compute.
    pytest.skip(_WAVE0_REASON)


def test_no_direct_to_compute_enqueue_path() -> None:
    # CLOUDPIPE-01: there is no routing path that enqueues to the compute agent directly,
    # bypassing the bounded staging window.
    pytest.skip(_WAVE0_REASON)
