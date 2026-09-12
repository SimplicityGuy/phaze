"""Assertion-free shared setup for execution orchestration contract tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

from phaze.config import AgentSettings
from phaze.services.agent_client import AgentApiServerError


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_api_client_mock() -> AsyncMock:
    """Mock PhazeAgentClient with all 4 methods used by execute_approved_batch (Phase 28)."""
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _make_job_mock(initial_meta: dict[str, str] | None = None) -> MagicMock:
    """Mock SAQ Job with a writeable ``meta`` dict and an async ``update`` method.

    phaze-ebb46: ``update`` now has a ``side_effect`` that actually applies ``meta=...`` onto
    ``job.meta``, mirroring real SAQ (``saq.queue.base.Queue.update`` does ``setattr(job, k, v)``
    for every kwarg before persisting). Without this the mock's ``job.meta`` stayed frozen at
    whatever ``initial_meta`` was, so code that reads ``job.meta`` again later in the SAME batch
    (as this bead's per-proposal "moved" flag does, right after `_load_or_seed_uuids` already
    wrote the UUID keys) would see a stale, incomplete dict instead of the real cumulative state.
    """
    job = MagicMock()
    job.meta = dict(initial_meta or {})

    async def _update(**kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(job, key, value)

    job.update = AsyncMock(side_effect=_update)
    return job


def _seed_files(tmp_path: Path, count: int) -> tuple[list[Path], list[Path]]:
    """Create ``count`` orig files under ``tmp_path/orig`` and target paths under ``tmp_path/new``."""
    orig_paths: list[Path] = []
    proposed_paths: list[Path] = []
    for i in range(count):
        o = tmp_path / "orig" / f"track{i}.mp3"
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_bytes(f"audio-content-{i}".encode())
        n = tmp_path / "new" / f"track{i}.mp3"
        orig_paths.append(o)
        proposed_paths.append(n)
    return orig_paths, proposed_paths


def _patch_settings(monkeypatch: pytest.MonkeyPatch, scan_roots: list[str]) -> None:
    """Stub ``get_settings()`` to return an AgentSettings-shaped mock with given scan_roots."""
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


def _payload_from_call(call: object) -> object:
    """Extract the progress payload passed positionally or by keyword."""
    args = getattr(call, "args", ()) or ()
    kwargs = getattr(call, "kwargs", {}) or {}
    if len(args) >= 2:
        return args[1]
    return kwargs["payload"]


def _fail_only_telemetry_posts() -> AsyncMock:
    """Fail only non-terminal progress telemetry posts."""

    async def _side_effect(_batch_id: uuid.UUID, body: object) -> None:
        if not getattr(body, "sub_batch_terminal", False):
            raise AgentApiServerError("progress endpoint down")

    return AsyncMock(side_effect=_side_effect)
