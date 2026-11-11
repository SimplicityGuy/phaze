"""CUE-review scenarios moved from ``tests/shared/core/test_review_apply_workspaces.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_cue_workspace_lists_legacy_tracklists_as_display_only_cards(
    client: AsyncClient,
    seed_cue_set: Callable[..., Awaitable[object]],
) -> None:
    """REVIEW-04 after ADR-0027 (retire legacy timestamp qualification).

    Legacy tracklists -- timed or not -- are listed as gated, display-only cards: no ``.cue``
    preview, no generation control, no per-tracklist POST. Generation lives on the selected-source
    sections the workspace loads beside them.
    """
    timed = await seed_cue_set(timestamped=True)
    untimed = await seed_cue_set(timestamped=False)

    frag = await client.get("/s/cue", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert "Cue sheets" in body and "Legacy stored tracklists" in body
    for _file, tracklist, _version in (timed, untimed):  # type: ignore[misc]
        assert f'id="cue-card-{tracklist.id}"' in body
        assert f"/cue/{tracklist.id}/generate" not in body
    assert body.count("display only") >= 2
    assert "<pre" not in body, "a legacy card renders no .cue preview"
    assert "Generate cue sheet" not in body and 'hx-post="/cue/' not in body
    assert "Open source record" in body and "Tracklist workspace" in body
    assert "/pipeline/local-source-sets?kind=cue" in body, "the selected-source CUE sections still load"


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
