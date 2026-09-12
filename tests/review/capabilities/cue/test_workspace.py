"""CUE-review scenarios moved from ``tests/shared/core/test_review_apply_workspaces.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_cue_gate_and_preview(
    client: AsyncClient,
    seed_cue_set: Callable[..., Awaitable[object]],
) -> None:
    """REVIEW-04 -- Cue Sheets is an artifact workspace with prerequisites, preview, generation, and source actions.

    One eligible set (approved tracklist, EXECUTED file, a timestamped track) and one ineligible set (no
    timestamped track) are seeded. The eligible card renders the in-memory ``.cue`` preview ``<pre>`` and an
    APPROVE that POSTs ``/cue/{id}/generate`` (generate IS approve/write -- there is NO ``/approve`` route);
    the ineligible card is ``opacity-60`` with "awaiting tracklist match…" and NO approve control.
    """
    eligible = await seed_cue_set(eligible=True)
    _gated = await seed_cue_set(eligible=False)
    eligible_tracklist_id = eligible[1].id  # (file, tracklist, version)

    frag = await client.get("/s/cue", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert "Artifacts · Cue sheets" in body and "Generated artifact" in body
    assert "Applied file:" in body and "Tracklist:" in body and "Timestamps:" in body
    assert "<pre" in body, "the eligible card renders the in-memory .cue preview block"
    assert f'hx-post="/cue/{eligible_tracklist_id}/generate"' in body
    assert "/approve" not in body, "there is no /cue/{id}/approve route -- generate IS the write"
    assert "Generate cue sheet" in body and "Open source record" in body and "Tracklist workspace" in body
    assert "Preview and generation are unavailable" in body and "! required" in body
    assert body.count("Generate cue sheet") == 1, "only the eligible card carries a generation control"


@pytest.mark.asyncio
async def test_cue_preview_failure_is_not_reported_as_missing_timestamps(
    client: AsyncClient,
    seed_cue_set: Callable[..., Awaitable[object]],
) -> None:
    """A renderer failure is a red preview error, not an amber prerequisite diagnosis."""
    await seed_cue_set(eligible=True)
    with patch("phaze.services.review.generate_cue_content", side_effect=ValueError("synthetic preview failure")):
        response = await client.get("/s/cue", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "preview failed" in response.text
    assert "timestamps were not classified as missing" in response.text
    assert "Preview and generation are unavailable until" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dedupe", "cue"])
async def test_workspace_declares_no_second_toast_container(client: AsyncClient, stage: str) -> None:
    """phaze-gzrd -- the rendered document carries EXACTLY ONE ``id="toast-container"``.

    ``shell.html`` already declares the single toast container, and every stage workspace mounts
    inside that same document (full page render, or an HX fragment swapped into ``#stage-workspace``
    of the live shell). A workspace that declares its own ``id="toast-container"`` therefore creates
    a DUPLICATE id: htmx resolves an ``hx-swap-oob`` selector via ``querySelectorAll`` (ALL matches,
    unlike ``getElementById``) and appends an independent ``cloneNode(true)`` into EACH one, so every
    OOB toast renders twice with separate Alpine state and separate dismissal timers.

    Same hazard class the shared scaffold already calls out ("don't create duplicate ids that would
    steal the live OOB swap", ``_workspace_scaffold.html``). Asserted on the FULL-PAGE render, which
    is the composed document the browser actually holds.
    """
    page = await client.get(f"/s/{stage}")
    assert page.status_code == 200
    declared = page.text.count('id="toast-container"')
    assert declared == 1, f'/s/{stage} renders {declared} elements with id="toast-container"; the shell owns the only one.'

    # And the bare fragment must not smuggle one back in -- it swaps into a shell that already has it.
    frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    assert 'id="toast-container"' not in frag.text, f"the /s/{stage} fragment must not declare its own toast container"
