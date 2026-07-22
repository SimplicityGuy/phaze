"""Tests for ``phaze.routers.shell._analyze_file_count`` -- the RECORD-04 first-run gate reader.

Covers the degrade-safe discipline the docstring documents: on ANY DB error the reader returns
the non-zero sentinel ``1`` (never falsely trips the empty state) and, per CR-01, does so via a
SAVEPOINT (``session.begin_nested()``) rather than a full ``session.rollback()`` -- the shared
request session may already carry ``Agent`` / ``ScanBatch`` ORM rows ``build_dashboard_context``
loaded before this reader runs, and a full rollback would expire them, 500-ing the subsequent
Jinja render on the next lazy load.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers.shell import _analyze_file_count


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` in the fake-session tests.

    ``__aexit__`` returns ``False`` so an exception raised inside the ``async with`` block (the
    COUNT read) propagates out to ``_analyze_file_count``'s degrade ``except`` -- exactly as a
    real SAVEPOINT does after ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_analyze_file_count_happy_path(session: AsyncSession) -> None:
    """The count reflects the real row count on the healthy path."""
    assert await _analyze_file_count(session) == 0

    session.add(
        Agent(id="afc-agent", name="AfcBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver"),
    )
    await session.flush()
    session.add(
        FileRecord(
            id=uuid.uuid4(),
            sha256_hash=uuid.uuid4().hex,
            original_path="/media/afc.mp3",
            original_filename="afc.mp3",
            current_path="/media/afc.mp3",
            file_type="mp3",
            file_size=1234,
            agent_id="afc-agent",
        )
    )
    await session.flush()
    assert await _analyze_file_count(session) == 1


@pytest.mark.asyncio
async def test_analyze_file_count_degrades_to_nonzero_sentinel_on_db_error() -> None:
    """A forced read error degrades to the non-zero sentinel (never falsely trips the empty state).

    The read runs inside a SAVEPOINT (``begin_nested``); the exception propagates out of the nested
    scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is never
    touched with a full ``session.rollback()``).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await _analyze_file_count(_ExplodingSession()) == 1  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_analyze_file_count_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, the reader still degrades
    to the non-zero sentinel rather than propagating."""

    class _DoubleExplodingSession:
        def begin_nested(self) -> object:
            raise RuntimeError("connection already closed")

    assert await _analyze_file_count(_DoubleExplodingSession()) == 1  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_analyze_file_count_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``_render_stage``'s analyze branch runs this AFTER ``build_dashboard_context`` has loaded
    ``Agent`` / ``ScanBatch`` rows into the SAME request session's identity map. A plain
    ``session.rollback()`` in the degrade branch would expire those already-loaded rows, 500-ing the
    subsequent Jinja render on the next lazy load (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush an Agent row, force ONLY the file-count SELECT to fail, then assert
    ``session.get`` still finds the agent afterwards -- proving the outer transaction survived.
    """
    agent = Agent(id="afc-cr01-agent", name="AfcCr01Box", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver")
    session.add(agent)
    await session.flush()

    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    result = await _analyze_file_count(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    assert result == 1
    assert await session.get(Agent, "afc-cr01-agent") is not None
