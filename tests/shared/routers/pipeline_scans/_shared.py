"""Shared prelude for the split `tests/shared/routers/pipeline_scans/test_*.py` suite (phaze-1i0h6.6).

`tests/shared/routers/test_pipeline_scans.py` (2,438 lines, 84 tests) was Repowise's
repository-wide fix-first directive (10,414 weighted deficit, top-3% change entropy), and its
production counterpart, `src/phaze/routers/pipeline_scans.py`, co-changes with it directly and was
decomposed the same bead (`trigger_scan` split into named phase helpers: path normalization/
validation, agent/root authorization, RUNNING batch insertion + duplicate translation, enqueue
outcome reconciliation, HTMX error rendering). This package mirrors that split by contract:

- `test_path_and_auth.py` -- path normalization/validation + agent/root authorization (the first
  two `trigger_scan` phases), including the 422-vs-200 envelope boundary.
- `test_duplicate_and_state.py` -- RUNNING batch insertion, the partial-unique duplicate guard, the
  no-refresh/`expire_on_commit=False` assumption, and the elapsed/stall timer helpers that compute
  a batch's displayed state (plus their repo-wide naive-tz antipattern guard).
- `test_enqueue_outcomes.py` -- the enqueue reconciliation phase: definite failure (mark FAILED)
  vs. ambiguous failure (leave RUNNING).
- `test_rendering.py` -- every other read/delete/render endpoint this file covers: agent-roots
  swap, dashboard, the Recent Scans partial + its sortable-header contract, stats OOB counts,
  DELETE + cascade, and the Discover workspace mount.

This module holds everything that ISN'T a `def test_*`: the shared imports and every `_make_*` /
`_count_*` / `_seed_*` / `_assert_*` helper and guard the split files import from. The `smoke`
client fixture itself lives in this package's `conftest.py` instead (pytest auto-discovers it
there without an explicit per-file import; several split files request it as a directly-named
fixture parameter, which would otherwise collide with an `import smoke` in the same module --
ruff F811). Nothing here was rewritten -- every symbol is a verbatim carry-over from the original
file, so no test's behavior changed as a side effect of the split.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
from typing import TYPE_CHECKING
import unicodedata  # noqa: F401 -- re-exported for test_path_and_auth.py
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient  # noqa: F401 -- re-exported: conftest.py + test_path_and_auth.py
import pytest  # noqa: F401 -- re-exported: every split test file imports `pytest` from here
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent  # noqa: F401 -- re-exported: conftest.py + test_path_and_auth.py
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import pipeline, pipeline_scans, shell
from phaze.routers.response_shape import RENDERABLE_ALERT_STATUS
from phaze.services.agent_task_router import AmbiguousEnqueueError  # noqa: F401 -- re-exported for test_enqueue_outcomes.py


_ROUTERS_DIR = Path(__file__).parent.parent.parent.parent.parent / "src" / "phaze" / "routers"

# Keep exemptions at the exceptional call site so their rationale moves with the code.
# The required text after ``--`` prevents a bare suppression from disguising why matching
# the database row's timezone awareness is correct. This remains router-scoped: the guard's
# contract protects router handling of production TIMESTAMPTZ values. Repo-wide enforcement
# would conflate that hazard with deliberate schema-bound normalization in task code and
# should be introduced, if wanted, as its own policy with its own migration.
_NAIVE_TZ_EXEMPTION = re.compile(r"#\s*phaze:\s*allow-naive-tz\s*--\s*\S.{9,}$")


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a smoke FastAPI app mounting pipeline_scans + pipeline routers.

    Returns the app AND the AsyncMock installed at ``app.state.task_router``
    so happy-path tests can assert against ``enqueue_for_agent`` call args.
    """
    app = FastAPI(title="pipeline-scans-smoke", version="test")
    app.include_router(pipeline_scans.router)
    app.include_router(pipeline.router)
    app.include_router(shell.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    # The pipeline router's existing trigger endpoints reference app.state.queue;
    # install a benign mock to keep the dashboard handler import-safe even
    # though dashboard tests do not exercise the queue.
    app.state.queue = AsyncMock()
    return app, mock_router


async def _count_batches(session: AsyncSession) -> int:
    """Count ScanBatch rows in the test session."""
    rows = (await session.execute(select(ScanBatch))).scalars().all()
    return len(rows)


def _naive_now_offenders(routers_dir: Path) -> list[str]:
    """Find every `<expr>.replace(tzinfo=None)` call site under ``routers_dir``.

    Shared by the real guard test below and its seeded-mutation proofs, so the
    detection logic under test is the SAME code the proofs exercise -- a copy
    pasted into the mutation tests could silently drift from what actually
    runs in CI.

    Matches ANY ``.replace(tzinfo=None)`` call, not only the directly-chained
    ``datetime.now(UTC).replace(tzinfo=None)`` shape. An earlier version of
    this helper required the receiver to itself be a `.now(...)` call
    expression, which caught the chained form but FAILED OPEN on the
    assign-then-strip form (`dt = datetime.now(UTC); dt = dt.replace(tzinfo=None)`)
    -- confirmed absent from today's tree only by `grep`, not by this check,
    which is exactly the "reads as coverage but silently misses it" failure
    mode a guard with branching logic must not have (phaze-7l8jh). Stripping
    tzinfo via `.replace(tzinfo=None)` is the antipattern regardless of how
    the datetime it strips was produced, so the receiver's shape is no longer
    part of the match.
    """
    offenders: list[str] = []
    for py in routers_dir.rglob("*.py"):
        text = py.read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and any(kw.arg == "tzinfo" and isinstance(kw.value, ast.Constant) and kw.value.value is None for kw in node.keywords)
            ):
                source_line = lines[node.lineno - 1]
                if not _NAIVE_TZ_EXEMPTION.search(source_line):
                    offenders.append(f"{py.relative_to(routers_dir)}:{node.lineno}")
    return offenders


def _copy_routers(tmp_path: Path) -> Path:
    destination = tmp_path / "routers"
    shutil.copytree(_ROUTERS_DIR, destination)
    return destination


def _make_discovered_file() -> FileRecord:
    """Build a standalone FileRecord in the DISCOVERED state (counts toward stats.discovered)."""
    path = f"/data/music/{uuid.uuid4().hex}.mp3"
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=2048,
    )


def _make_batch_file(batch_id: uuid.UUID, suffix: str) -> FileRecord:
    """Build a FileRecord belonging to a batch (unique path)."""
    path = f"/data/music/{uuid.uuid4().hex}-{suffix}.mp3"
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=2048,
        batch_id=batch_id,
    )


def _running_batch(last_progress_at: object) -> ScanBatch:
    """Build an unsaved RUNNING ScanBatch with the given last_progress_at."""
    b = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    b.last_progress_at = last_progress_at  # type: ignore[assignment]
    return b


def _assert_swappable_alert(response: object, expected_fragment: str) -> None:
    """Assert a trigger_scan failure response is one htmx will actually swap AND render.

    Two obligations, both required (phaze-u1gf):

    1. **Swappable.** The status must be in htmx's default swap range (``[23]..``).
       ``RENDERABLE_ALERT_STATUS`` is the single spelling of that decision.
    2. **Carries the alert.** The body must be the ``role="alert"`` card with the
       operator-facing prose. A status-only assertion passes against a handler that
       returns 200 with an empty body -- which is the very failure (blank
       ``#scan-submit-result``) this bead exists to fix.
    """
    status_code = response.status_code  # type: ignore[attr-defined]
    body = response.text  # type: ignore[attr-defined]
    assert status_code == RENDERABLE_ALERT_STATUS, f"htmx will not swap status {status_code}; body dropped: {body!r}"
    assert 200 <= status_code < 300, f"status {status_code} is outside htmx's default swap range"
    assert 'role="alert"' in body, f"alert card missing from swapped body: {body!r}"
    assert expected_fragment in body, f"operator-facing message missing: {body!r}"


async def _seed_scan_batches(session: AsyncSession) -> None:
    """Seed three terminal batches whose path order is the exact REVERSE of the default order.

    The inversion is load-bearing, not decoration. The default order is ``created_at DESC``, so
    seeding paths in ASCENDING creation order makes "path asc" (alpha, bravo, charlie) and the
    default (charlie, bravo, alpha) disagree in EVERY position -- most importantly the first one.

    An earlier version of this helper seeded the paths in descending creation order, which made
    path-ascending and the default agree on the first row. Every ordering assertion below then
    passed against a deliberately broken implementation: the poll-survival test in particular was
    checking a value that was identical whether or not the sort survived. If you change the seed,
    re-verify by breaking the template on purpose and watching these tests fail.
    """
    from datetime import datetime, timedelta

    # NAIVE on purpose: the test schema comes from create_all, which emits TIMESTAMP WITHOUT TIME
    # ZONE, so a tz-aware value is rejected on INSERT. Production is tz-aware; the sort under test
    # is ordering, not tz handling (elapsed_seconds owns that and has its own tz-aware unit test).
    base = datetime(2026, 1, 1)
    for index, path in enumerate(["/data/music/alpha", "/data/music/bravo", "/data/music/charlie"]):
        session.add(
            ScanBatch(
                id=uuid.uuid4(),
                agent_id="test-agent",
                scan_path=path,
                status=ScanStatus.COMPLETED.value,
                total_files=10,
                processed_files=10,
                created_at=base + timedelta(hours=index),
            )
        )
    await session.commit()


def _path_order(html: str) -> list[str]:
    """Return the seeded scan paths in the order they appear in the rendered table."""
    return re.findall(r"/data/music/(?:alpha|bravo|charlie)", html)[::2] or re.findall(r"/data/music/(?:alpha|bravo|charlie)", html)
