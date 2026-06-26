"""Nyquist Wave 0 stubs for the process_file scratch read-path (CLOUDPIPE-03 / -04).

Scaffolding tests reserved by Phase 50 Plan 00 for the ephemeral-scratch analysis path:
the compute agent reads a pushed file from a scratch dir, verifies its sha256 off the
event loop, and cleans the scratch file up on every exit path. These selectors are owned
by Plan 50-04 (disjoint from ``tests/test_push_pipeline.py`` so the two Wave 2 plans never
write the same file in parallel).

Selectors reserved here (50-04):
  * ``-k sha256``  → sha256 mismatch deletes the scratch file, reports a clean failure,
                     and is re-pushable
  * ``-k cleanup`` → scratch file is deleted in a ``finally`` on success AND on every
                     terminal failure path

Imports are restricted to stdlib + pytest: the scratch read-path in ``process_file`` does
not exist yet, so importing it would break collection. Plan 50-04 replaces each
``pytest.skip`` with real assertions.
"""

from __future__ import annotations

import pytest


_WAVE0_REASON = "Wave 0 stub — implemented in 50-04"


def test_sha256_mismatch_deletes_scratch_and_reports() -> None:
    # CLOUDPIPE-03: an off-event-loop sha256 verify that mismatches deletes the scratch
    # file, reports a clean failure (no partial analysis persisted), and is re-pushable.
    pytest.skip(_WAVE0_REASON)


def test_scratch_cleanup_finally_on_all_exit_paths() -> None:
    # CLOUDPIPE-04: the scratch file is removed in a finally block on the success path
    # AND on every terminal failure path (no scratch-dir DoS from orphaned files).
    pytest.skip(_WAVE0_REASON)
