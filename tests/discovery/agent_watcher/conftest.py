"""Shared fixtures for phaze.agent_watcher tests (Phase 27 D-22).

These fixtures land in Wave 0 so subsequent waves (Plan 05) can write
``test_debouncer.py``, ``test_observer.py``, and ``test_main.py`` without
having to introduce new pytest scaffolding.

Import-boundary invariant: this module MUST NOT import ``phaze.database``,
``phaze.tasks.session``, ``sqlalchemy.ext.asyncio``, or ``phaze.agent_watcher``
at module scope. ``phaze.agent_watcher`` does not yet exist (Plan 05 creates
it); fixtures must remain lazy until then.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from phaze.services.agent_client import PhazeAgentClient


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def tmp_watcher_root(tmp_path: Path) -> Path:
    """Isolated filesystem root for watcher tests.

    Returns ``tmp_path`` directly; pytest provides per-test isolation by
    convention. Subsequent waves use this as the watchdog Observer root.
    """
    return tmp_path


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Monkeypatch ``time.monotonic()`` to a controllable value.

    Returns a ``set_clock(t)`` callable that mutates the internal cell.
    Required by ``test_debouncer.py`` (Plan 05) to drive the settle-period
    state machine deterministically.

    Usage:
        def test_x(fake_clock):
            fake_clock(0.0)
            ...   # call site sees time.monotonic() == 0.0
            fake_clock(11.0)
            ...   # settle period elapsed
    """
    cell: list[float] = [0.0]

    monkeypatch.setattr(time, "monotonic", lambda: cell[0])

    def _set_clock(t: float) -> None:
        cell[0] = t

    return _set_clock


@pytest.fixture
def mock_api_client() -> AsyncMock:
    """In-memory replacement for :class:`PhazeAgentClient`.

    Stubs the two methods the watcher / scan_directory pipeline calls --
    ``upsert_files`` and ``patch_scan_batch`` -- as AsyncMock so tests can
    assert call counts and payloads. Other methods are auto-stubbed by
    ``spec=PhazeAgentClient``.

    Required by ``test_main.py`` and ``test_observer.py`` (Plan 05).
    """
    client = AsyncMock(spec=PhazeAgentClient)
    client.upsert_files = AsyncMock()
    # patch_scan_batch is a Phase 27 Plan 02/03 addition; pre-stub on the
    # AsyncMock for forward-compat (the spec= argument auto-creates it as a
    # MagicMock attr; re-binding to AsyncMock makes await-syntax usage clean).
    client.patch_scan_batch = AsyncMock()
    return client
