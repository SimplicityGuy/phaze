"""Tests for the Phase-35 DAG-canvas store extension + per-node router context (35-04).

Two surfaces are covered:

- The ``$store.pipeline`` extension in ``base.html`` (a pure template-text assertion;
  no DB) — every per-node sub-key is registered AND seeded to 0, with the Phase-34 keys
  preserved (``-k store``).
- The per-node router context built by ``_build_dag_context`` + the OOB seed paragraphs
  emitted by ``stats_bar.html`` on the 5s poll (DB-backed, auto-marked ``integration``):
  DB-truth ``done`` from ``get_stage_progress``, the ``completed``-counter degrade-fallback
  (D-02), and the never-500 degrade when the counter source is unavailable (T-35-09).
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_BASE_HTML = Path(__file__).resolve().parent.parent / "src" / "phaze" / "templates" / "base.html"

# Every per-node store sub-key the UI-SPEC "Store extension" mandates (35-UI-SPEC L294).
_NEW_STORE_KEYS = (
    "metadataDone",
    "metadataTotal",
    "fingerprintDone",
    "fingerprintTotal",
    "analyzeDone",
    "analyzeTotal",
    "analyzeActive",
    "tracklistDone",
    "scrapeDone",
    "scrapeTotal",
    "matchDone",
    "matchTotal",
    "proposalsDone",
    "proposalsTotal",
    "approved",
    "executedDone",
    "executedTotal",
)

# The Phase-34 keys the existing button :disabled gating reads — must NOT be removed.
_PRESERVED_STORE_KEYS = ("discovered", "analyzed", "metadataExtracted", "agentBusy", "controllerBusy")


def _store_literal() -> str:
    """Return the ``Alpine.store('pipeline', { ... });`` object-literal text from base.html."""
    text = _BASE_HTML.read_text(encoding="utf-8")
    match = re.search(r"Alpine\.store\('pipeline',\s*\{(.*?)\}\s*\);", text, re.DOTALL)
    assert match is not None, "Alpine.store('pipeline', {...}) literal not found in base.html"
    return match.group(1)


# ---------------------------------------------------------------------------
# Task 1: $store.pipeline extension (pure template text — no DB, runs everywhere)
# ---------------------------------------------------------------------------


def test_store_seeds_every_new_per_node_key_to_zero() -> None:
    """Every UI-SPEC per-node sub-key is present in the store literal and seeded to 0."""
    literal = _store_literal()
    for key in _NEW_STORE_KEYS:
        assert re.search(rf"\b{key}\s*:\s*0\b", literal), f"store key '{key}' missing or not seeded to 0"


def test_store_preserves_phase34_keys() -> None:
    """The Phase-34 keys the button gating reads are still seeded (not removed)."""
    literal = _store_literal()
    for key in _PRESERVED_STORE_KEYS:
        assert re.search(rf"\b{key}\s*:\s*0\b", literal), f"Phase-34 store key '{key}' was removed"


def test_store_literal_has_no_undefined_seed() -> None:
    """No store key is left undefined — a sample of the new keys read 0, not undefined."""
    literal = _store_literal()
    for key in ("analyzeActive", "metadataDone", "proposalsTotal", "executedDone"):
        assert f"{key}: 0" in literal


# ---------------------------------------------------------------------------
# Task 2: per-node router context (_build_dag_context) — DB-backed
# ---------------------------------------------------------------------------


class _FallbackRedis:
    """Minimal async Redis double exposing only ``mget`` over a seeded ``store`` dict.

    Mirrors a non-``decode_responses`` client (returns ``bytes`` per present key) so the
    ``pipeline_counters._to_int`` bytes-decode path is exercised. ``read_counters`` only
    calls ``mget`` on the enqueued + completed key lists, so that is all we implement.
    """

    def __init__(self, store: dict[str, int]) -> None:
        self.store = store

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [str(self.store[k]).encode() if k in self.store else None for k in keys]


def _idle_activity() -> dict[str, int]:
    """A zero queue-activity dict (no in-flight work) for _build_dag_context."""
    return {
        "agent_queued": 0,
        "agent_active": 0,
        "controller_queued": 0,
        "controller_active": 0,
        "agent_busy": 0,
        "controller_busy": 0,
    }


def _make_file_with_metadata() -> tuple[FileRecord, FileMetadata]:
    """A discovered FileRecord plus a FileMetadata row (drives metadata.done == 1)."""
    uid = uuid.uuid4()
    file_rec = FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.METADATA_EXTRACTED,
    )
    metadata = FileMetadata(file_id=uid, artist="Test", title="Track")
    return file_rec, metadata


@pytest.mark.asyncio
async def test_build_dag_context_carries_every_per_node_key(session: AsyncSession) -> None:
    """_build_dag_context returns a ``dag`` dict carrying every per-node store key."""
    app_state = SimpleNamespace()  # no redis → counters degrade to {} (DB-only)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    dag = ctx["dag"]
    for key in _NEW_STORE_KEYS:
        assert key in dag, f"context dag missing store key '{key}'"
        assert isinstance(dag[key], int), f"dag['{key}'] must be an int for x-init interpolation"


@pytest.mark.asyncio
async def test_dag_done_comes_from_db_truth(session: AsyncSession) -> None:
    """metadata.done reflects the DB metadata row count (get_stage_progress authority)."""
    file_rec, metadata = _make_file_with_metadata()
    session.add(file_rec)
    session.add(metadata)
    await session.flush()

    app_state = SimpleNamespace()
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 1
    assert ctx["dag"]["analyzeActive"] == 0


@pytest.mark.asyncio
async def test_completed_counter_degrade_fallback(session: AsyncSession) -> None:
    """When a node's DB done is 0 AND its mapped completed counter > 0, the counter renders.

    This exercises the maintained ``completed`` counter as a DOCUMENTED degrade-fallback
    (D-02): there are NO metadata rows in the DB (metadata.done == 0), but the maintained
    ``completed`` counter for ``extract_file_metadata`` is 3, so metadataDone falls back to 3.
    """
    fake = _FallbackRedis({"phaze:pipeline:completed:extract_file_metadata": 3})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 3


@pytest.mark.asyncio
async def test_proposals_batch_counter_is_not_a_fallback_done(session: AsyncSession) -> None:
    """WR-03: generate_proposals is a BATCH task, so its completed counter must NOT render as
    proposalsDone (a per-file count). With DB done == 0 and a generate_proposals completed
    counter of 5, proposalsDone stays 0 (DB-truth) rather than the wrong-unit batch count.
    """
    fake = _FallbackRedis({"phaze:pipeline:completed:generate_proposals": 5})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["proposalsDone"] == 0


@pytest.mark.asyncio
async def test_db_truth_wins_over_completed_counter(session: AsyncSession) -> None:
    """When the DB has done > 0, DB-truth wins even if the completed counter differs (D-03)."""
    file_rec, metadata = _make_file_with_metadata()
    session.add(file_rec)
    session.add(metadata)
    await session.flush()

    # completed counter says 99, but the DB has exactly 1 metadata row → DB wins.
    fake = _FallbackRedis({"phaze:pipeline:completed:extract_file_metadata": 99})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 1


@pytest.mark.asyncio
async def test_build_dag_context_never_raises_on_counter_outage(session: AsyncSession) -> None:
    """A counter source that raises degrades to DB-truth — _build_dag_context never raises."""

    class _ExplodingRedis:
        async def mget(self, keys: list[str]) -> list[bytes | None]:  # noqa: ARG002
            raise RuntimeError("redis down")

    app_state = SimpleNamespace(redis=_ExplodingRedis())
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    # Empty DB + dead counters → all-zero, but the function returned cleanly (no 500).
    assert ctx["dag"]["metadataDone"] == 0


# ---------------------------------------------------------------------------
# Task 2: HTTP render — OOB seeds on the poll + dashboard full-page never-500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_poll_emits_oob_seed_for_every_node_key(client: AsyncClient) -> None:
    """GET /pipeline/stats emits an OOB x-init seed paragraph for every per-node store key."""
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    body = response.text
    for key in _NEW_STORE_KEYS:
        assert f'id="dag-seed-{key}"' in body, f"missing OOB seed id for store key '{key}'"
        assert f"$store.pipeline.{key} =" in body, f"missing x-init store write for '{key}'"
    # The seeds are OOB-swapped (only honored during an HTMX swap, i.e. the poll).
    assert body.count('hx-swap-oob="true"') >= len(_NEW_STORE_KEYS)


@pytest.mark.asyncio
async def test_stats_poll_degrades_to_200_without_counter_source(client: AsyncClient) -> None:
    """The 5s poll never 500s when the Redis counter source is absent (T-35-09)."""
    # The test client skips the lifespan, so app.state.redis / controller_queue are absent;
    # the counter read degrades to {} and the poll still renders 200 from DB-truth.
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_full_page_renders_200_with_dag_context(client: AsyncClient) -> None:
    """GET /pipeline/ renders 200 — the dashboard context carries the per-node dag values."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "Pipeline Dashboard" in response.text
