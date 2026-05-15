"""Tests for agent-side execute_approved_batch progress POSTs (Phase 28 D-03, D-15).

Wave 0 stub — the agent-side task body changes (one `api.post_exec_batch_progress`
per proposal at terminal state, `sub_batch_terminal=true` on the last item, idempotent
`request_id` persisted in SAQ state) land in Plan 28-05. This stub anchors the file
path so Nyquist sampling can resolve test ID 28-V-25.
"""

from __future__ import annotations

import pytest


pytest.skip("Wave 0 stub — implementation lands in Plan 28-05", allow_module_level=True)
