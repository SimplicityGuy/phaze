"""Nyquist Wave 0 stubs for the "stay one ahead" staging cron (CLOUDPIPE-01 / -05).

Scaffolding tests reserved by Phase 50 Plan 00 for the bounded-window controller cron
that keeps at most N files in flight to the compute agent. The window math:

  * N=2, window full          → 0 newly staged
  * N=2, one slot free         → exactly 1 newly staged
  * no compute agent online    → no-op (nothing staged)
  * ordering                   → FIFO, oldest AWAITING_CLOUD first

These selectors run against the default file (no ``-k``); Plan 50-06 replaces each
``pytest.skip`` with real assertions. Imports are stdlib + pytest only — the staging-cron
controller does not exist yet, so importing it would break collection.
"""

from __future__ import annotations

import pytest


_WAVE0_REASON = "Wave 0 stub — implemented in 50-06"


def test_window_full_stages_zero() -> None:
    # CLOUDPIPE-01/-05: with N already in flight, the cron stages 0 new files.
    pytest.skip(_WAVE0_REASON)


def test_one_free_slot_stages_one() -> None:
    # CLOUDPIPE-01/-05: with N-1 in flight, exactly one AWAITING_CLOUD file is staged.
    pytest.skip(_WAVE0_REASON)


def test_no_compute_agent_is_noop() -> None:
    # CLOUDPIPE-01: with no compute agent online the cron is a no-op — nothing is staged.
    pytest.skip(_WAVE0_REASON)


def test_fifo_oldest_awaiting_cloud_first() -> None:
    # CLOUDPIPE-01: staging order is FIFO — the oldest AWAITING_CLOUD file goes first.
    pytest.skip(_WAVE0_REASON)
