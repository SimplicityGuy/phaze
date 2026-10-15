"""First paint shows REAL counts, never placeholder zeros presented as data (phaze-s8xtd).

``$store.pipeline`` used to boot at literal ``0`` for every count, so each full page load read
"Agents online 0 · Lanes active 0", an empty rail and "No orphaned enrich work detected" until the first
``/pipeline/stats`` poll replaced them. The shell now seeds the store from the server's own numbers
(``routers/shell/store_seed.py``), and anything it cannot measure renders an em-dash.

These tests read the INITIAL HTML of a FULL-document render -- what the browser paints before Alpine or any
poll has run -- against a database holding known, nonzero state.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import text

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers.shell import store_seed, summary as summary_mod
from phaze.services.pipeline import orphans


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_FILES = 3


async def _seed_archive(session: AsyncSession) -> None:
    """One online file-server agent, one online compute agent and ``_FILES`` music files."""
    session.add(Agent(id="fp-fileserver", name="FpFileserver", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver"))
    session.add(Agent(id="fp-compute", name="FpCompute", scan_roots=[], last_seen_at=datetime.now(UTC), kind="compute"))
    await session.flush()
    for index in range(_FILES):
        session.add(
            FileRecord(
                id=uuid.uuid4(),
                sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                original_path=f"/media/fp-{index}.mp3",
                original_filename=f"fp-{index}.mp3",
                current_path=f"/media/fp-{index}.mp3",
                file_type="mp3",
                file_size=1024,
                agent_id="fp-fileserver",
            )
        )
    await session.commit()


def _seed_in_html(body: str) -> dict[str, int]:
    """The object ``shell.html`` merges over the store defaults, parsed back out of the page."""
    match = re.search(r'Object\.assign\(Alpine\.store\("pipeline"\), (\{.*?\})\);', body, re.DOTALL)
    assert match, "the initial HTML must carry the server-side store seed"
    return json.loads(match.group(1))


def _text_of(body: str, pattern: str) -> str:
    match = re.search(pattern, body, re.DOTALL)
    assert match, f"{pattern!r} not found in the initial HTML"
    return match.group(1).strip()


@pytest.mark.asyncio
async def test_initial_html_carries_the_real_counts_not_zero(client: AsyncClient, session: AsyncSession) -> None:
    """Acceptance 1 + 3: a nonzero DB renders its real counts into the header, the rail and the store seed."""
    await _seed_archive(session)

    resp = await client.get("/s/discover")  # no HX-Request: the FULL shell, i.e. a direct load
    assert resp.status_code == 200
    body = resp.text

    seed = _seed_in_html(body)
    assert seed["seedKnown"] == 1
    assert seed["discovered"] == _FILES
    assert seed["agentOnline"] == 2
    assert seed["computeOnline"] == 1
    # None of the three files has been enriched yet, so the whole backlog is "not yet enriched".
    assert seed["notYetEnriched"] == _FILES

    # What the browser paints BEFORE Alpine boots: the static fallback text, not the old "0".
    assert "3 agents · 0 clusters" in body  # Includes the fixture's never-seen fileserver inventory row.
    # Header uses the cached attention snapshot; an unmeasured cache paints unknown.
    assert 'aria-controls="attention-tray"' in body
    assert "attentionKnown: 0" in body
    assert _text_of(body, r'x-text="\$store\.pipeline\.seedKnown \? formatCount\(\$store\.pipeline\.discovered\) : [^>]*>([^<]*)</span>') == str(
        _FILES
    )
    assert (
        _text_of(body, r"""x-text="\$store\.pipeline\.seedKnown \? `\$\{formatCount\(\$store\.pipeline\.analyzeDone\)\}[^>]*>([^<]*)</span>""")
        == f"0 / {_FILES}"
    )


@pytest.mark.asyncio
async def test_unmeasurable_seed_renders_em_dash_placeholders(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 1 (placeholder arm): when the seed cannot be measured nothing shows a numeric 0."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("seed unavailable")

    monkeypatch.setattr("phaze.routers.shell.store_seed.get_stage_progress", _boom)

    resp = await client.get("/s/discover")
    assert resp.status_code == 200  # a seed failure never 500s the page
    body = resp.text

    assert _seed_in_html(body) == {}
    assert "Agents unavailable · Clusters unavailable" in body
    assert _text_of(body, r'x-text="\$store\.pipeline\.seedKnown \? formatCount\(\$store\.pipeline\.discovered\) : [^>]*>([^<]*)</span>') == "—"
    assert (
        _text_of(body, r"""x-text="\$store\.pipeline\.seedKnown \? `\$\{formatCount\(\$store\.pipeline\.analyzeDone\)\}[^>]*>([^<]*)</span>""")
        == "— / —"
    )


@pytest.mark.asyncio
async def test_fragment_swap_does_not_pay_for_the_seed(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a full-document render builds the seed; an htmx rail swap (the poll keeps it live) must not."""
    called = False

    async def _spy(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("phaze.routers.shell.build_pipeline_store_seed", _spy)

    resp = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert called is False


def _discover_orphan_blocks(body: str) -> dict[str, bool]:
    """Which of the three Discover recovery states are VISIBLE in the initial HTML (no inline display:none)."""

    def visible(pattern: str) -> bool:
        tag = re.search(pattern, body)
        assert tag, f"{pattern!r} not found"
        return "display: none" not in tag.group(0)

    return {
        "found": visible(
            r'<div x-show="\$store\.pipeline\.orphanKnown && \$store\.pipeline\.metadataOrphan \+ \$store\.pipeline\.analyzeOrphan > 0"[^>]*>'
        ),
        "none": visible(
            r'<details x-show="\$store\.pipeline\.orphanKnown && \$store\.pipeline\.metadataOrphan \+ \$store\.pipeline\.analyzeOrphan === 0"[^>]*>'
        ),
        "pending": visible(r'<p x-show="!\$store\.pipeline\.orphanKnown"[^>]*>'),
    }


@pytest.mark.asyncio
async def test_discover_never_claims_no_orphans_before_the_count_is_known(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 2: the orphan cache is born as zeros; those zeros are not a measurement."""
    monkeypatch.setattr(orphans, "_orphan_cache", {"metadata": 0, "analyze": 0})
    monkeypatch.setattr(orphans, "_orphan_cache_expires_at", 0.0)

    body = (await client.get("/s/discover")).text

    assert _seed_in_html(body)["orphanKnown"] == 0
    assert _discover_orphan_blocks(body) == {"found": False, "none": False, "pending": True}
    assert "Orphaned-work status not yet known" in body


@pytest.mark.asyncio
async def test_discover_shows_the_real_orphan_state_once_known(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 2 (known arms): a measured zero says none detected; a measured nonzero says orphaned work detected."""
    monkeypatch.setattr(orphans, "_orphan_cache_expires_at", 1.0)

    monkeypatch.setattr(orphans, "_orphan_cache", {"metadata": 0, "analyze": 0})
    clean = (await client.get("/s/discover")).text
    assert _seed_in_html(clean)["orphanKnown"] == 1
    assert _discover_orphan_blocks(clean) == {"found": False, "none": True, "pending": False}

    monkeypatch.setattr(orphans, "_orphan_cache", {"metadata": 91, "analyze": 0})
    dirty = (await client.get("/s/discover")).text
    seed = _seed_in_html(dirty)
    assert (seed["orphanKnown"], seed["metadataOrphan"]) == (1, 91)
    assert _discover_orphan_blocks(dirty) == {"found": True, "none": False, "pending": False}


def test_stage_orphan_counts_known_tracks_the_first_successful_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """The discriminator itself: the born-as-zeros cache is unknown, a stamped one is known."""
    monkeypatch.setattr(orphans, "_orphan_cache_expires_at", 0.0)
    assert orphans.stage_orphan_counts_known() is False
    monkeypatch.setattr(orphans, "_orphan_cache_expires_at", 12.5)
    assert orphans.stage_orphan_counts_known() is True


@pytest.mark.asyncio
async def test_a_stage_without_a_context_builder_still_renders_the_full_shell(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_render_stage`` documents that a stage absent from the builder map keeps the base context -- and still gets the seed.

    Every stage has a builder today, so that arm is otherwise unreachable and its branch sat uncovered (the committed
    branch baseline predates that). Pin it, because the seed is built AFTER the builder merge and must not depend on one.
    """
    monkeypatch.setattr("phaze.routers.shell._STAGE_CONTEXT_BUILDERS", {})

    resp = await client.get("/s/cue")

    assert resp.status_code == 200
    assert _seed_in_html(resp.text)["seedKnown"] == 1


@pytest.mark.asyncio
async def test_seed_reuses_only_this_requests_progress_and_keeps_other_inputs_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh second request must get its own snapshot; no cross-request cache or frozen DAG."""
    reread = AsyncMock(side_effect=AssertionError("healthy Summary progress must not be read twice"))
    dag_read = AsyncMock(side_effect=[{"dag": {"agentOnline": 1}}, {"dag": {"agentOnline": 2}}])
    monkeypatch.setattr(store_seed, "get_stage_progress", reread)
    monkeypatch.setattr(store_seed, "_build_dag_context", dag_read)
    monkeypatch.setattr(store_seed, "_derive_stats", lambda progress: progress["stats"])
    first_progress = {"stats": {"discovered": 3, "analyzed": 1, "metadata_extracted": 2}}
    second_progress = {"stats": {"discovered": 4, "analyzed": 2, "metadata_extracted": 3}}
    state, session = object(), object()
    first = await store_seed.build_pipeline_store_seed(state, session, {"_stage_progress": first_progress})  # type: ignore[arg-type]
    second = await store_seed.build_pipeline_store_seed(state, session, {"_stage_progress": second_progress})  # type: ignore[arg-type]
    assert first == {"discovered": 3, "analyzed": 1, "metadataExtracted": 2, "agentOnline": 1}
    assert second == {"discovered": 4, "analyzed": 2, "metadataExtracted": 3, "agentOnline": 2}
    assert dag_read.await_args_list[0].args == (state, session, {}, first_progress)
    assert dag_read.await_args_list[1].args == (state, session, {}, second_progress)
    reread.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_summary_snapshot_retains_the_independent_seed_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    progress = {"stats": {"discovered": 3, "analyzed": 1, "metadata_extracted": 2}}
    reread = AsyncMock(return_value=progress)
    monkeypatch.setattr(store_seed, "get_stage_progress", reread)
    monkeypatch.setattr(store_seed, "_derive_stats", lambda value: value["stats"])
    monkeypatch.setattr(store_seed, "_build_dag_context", AsyncMock(return_value={"dag": {"seedKnown": 1}}))
    seed = await store_seed.build_pipeline_store_seed(object(), object(), {"_stage_progress": None})  # type: ignore[arg-type]
    assert seed == {"discovered": 3, "analyzed": 1, "metadataExtracted": 2, "seedKnown": 1}
    reread.assert_awaited_once()


@pytest.mark.parametrize("degraded", [False, True])
async def test_full_summary_uses_one_healthy_progress_read_or_retries_a_degraded_one(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, degraded: bool
) -> None:
    """Exercise the real shell consumer and DB, including a fresh subsequent request."""
    await _seed_archive(session)
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    await session.commit()
    actual = summary_mod.get_stage_progress

    async def overview_read(*args: object, **kwargs: object) -> object:
        result = await actual(*args, **kwargs)  # type: ignore[arg-type]
        if degraded:
            kwargs["read_failures"]["metadata_bucket"] = ("synthetic_stage_failure",)  # type: ignore[index]
        return result

    overview = AsyncMock(side_effect=overview_read)
    seed_reread = AsyncMock(wraps=store_seed.get_stage_progress)
    monkeypatch.setattr(summary_mod, "get_stage_progress", overview)
    monkeypatch.setattr(store_seed, "get_stage_progress", seed_reread)
    response = await client.get("/")
    assert response.status_code == 200
    assert _seed_in_html(response.text)["discovered"] == _FILES
    assert overview.await_count == 1
    assert seed_reread.await_count == int(degraded)

    extra_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=extra_id,
            sha256_hash=extra_id.hex * 2,
            original_path="/synthetic/extra.mp3",
            original_filename="extra.mp3",
            current_path="/synthetic/extra.mp3",
            file_type="mp3",
            file_size=1024,
            agent_id="fp-fileserver",
        )
    )
    await session.commit()
    second = await client.get("/")
    assert second.status_code == 200
    assert _seed_in_html(second.text)["discovered"] == _FILES + 1
    assert overview.await_count == 2
    assert seed_reread.await_count == 2 * int(degraded)
