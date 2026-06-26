"""Nyquist Wave 0 stubs for the file-server push pipeline (CLOUDPIPE-02 / -04).

These are scaffolding tests reserved by Phase 50 Plan 00. They establish the
``-k`` selectors that Plan 50-03 verifies against (rsync argv construction, exit-code
handling, and the compute-only startup janitor) before any production code in
``push.py`` exists. Every body skips with a reason so the suite stays green and pytest
collection never errors on a not-yet-existing import.

Selectors reserved here (50-03):
  * ``-k argv``     → rsync argv list (no shell; pinned known_hosts + StrictHostKeyChecking)
  * ``-k exit_code``→ non-zero/partial rsync exit → job fails, no callback, re-drivable
  * ``-k janitor``  → orphaned scratch swept on compute-worker start, in-flight skipped

Imports are deliberately restricted to stdlib + pytest: the push module and its
read-path do not exist yet, so importing them would break collection. Plan 50-03
replaces each ``pytest.skip`` with real assertions.
"""

from __future__ import annotations

import pytest


_WAVE0_REASON = "Wave 0 stub — implemented in 50-03"


def test_rsync_argv_no_shell_pinned_known_hosts() -> None:
    # CLOUDPIPE-02: push_file builds an argv list (shell=False) with
    # StrictHostKeyChecking=yes and a pinned known_hosts file — no shell injection surface.
    pytest.skip(_WAVE0_REASON)


def test_rsync_exit_code_nonzero_raises_no_callback() -> None:
    # CLOUDPIPE-02/-05: a non-zero or partial rsync exit fails the job, fires no success
    # callback, and leaves the work re-drivable (deterministic key collapses a retry).
    pytest.skip(_WAVE0_REASON)


def test_startup_janitor_compute_only_sweep() -> None:
    # CLOUDPIPE-04: on compute-worker start, orphaned scratch files are swept while
    # in-flight pushes are skipped; the janitor only runs on the compute agent.
    pytest.skip(_WAVE0_REASON)
