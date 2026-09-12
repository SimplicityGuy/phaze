"""Dedupe-review scenarios moved from ``tests/shared/core/test_review_apply_workspaces.py``."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient

    from phaze.models.file import FileRecord


@pytest.mark.asyncio
async def test_dedupe_keeper_resolve_wiring(
    client: AsyncClient,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    """REVIEW-03/REVIEW-05 -- keeper selection is staged; explicit confirmation resolves and remains undoable.

    A seeded duplicate group (two EXECUTED files sharing one sha256) surfaces as a keeper-select card. The
    radio is a form-local ``canonical_id`` selection with no request behavior. The enclosing form posts the
    VERIFIED ``/duplicates/{sha256}/resolve`` contract only from an explicit confirmation control. POSTing
    the form then returns the resolved state whose UNDO round-trips ``file_states`` to
    ``/duplicates/{sha256}/undo`` (REVIEW-05 -- undo reconstructs prior state FROM that blob).
    """
    files = await seed_duplicate_group(count=2)
    sha = files[0].sha256_hash

    frag = await client.get("/s/dedupe", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert f'<form id="dupe-group-{sha}"' in body
    assert f'hx-post="/duplicates/{sha}/review"' in body, "the choice posts only to the non-mutating review route"
    assert 'type="radio" name="canonical_id"' in body
    radio = re.search(r'<input type="radio"[^>]+>', body)
    assert radio is not None and "hx-post" not in radio.group(), "selection alone must not invoke the resolve endpoint"
    assert "group_id" not in body and "keeper_id" not in body, "the UI-SPEC sketch's group_id/keeper_id must NOT appear"
    assert "pending keeper" in body and "will archive" in body
    assert "Review decision" in body and "Selecting a copy only stages this decision" in body
    assert "bitrate first" in body and "tag completeness" in body and "shortest path" in body
    assert body.count("checked") == 1, "exactly one keeper radio is pre-selected per group"

    # Resolving round-trips file_states on UNDO over the existing resolve_response.html toast (REVIEW-05).
    review = await client.post(f"/duplicates/{sha}/review", data={"canonical_id": str(files[0].id)})
    plan_id = re.search(r'name="plan_id" value="([^"]+)"', review.text)
    assert plan_id is not None
    resolved = await client.post(f"/duplicates/{sha}/resolve", data={"plan_id": plan_id.group(1)})
    assert resolved.status_code == 200
    assert f'hx-post="/duplicates/{sha}/undo"' in resolved.text, "the resolved state's UNDO posts the undo route"
    assert 'name="file_states"' in resolved.text, "UNDO carries the file_states blob for a stateful reversal"


@pytest.mark.asyncio
async def test_dedupe_auto_keep_submits_rendered_group_hashes(
    client: AsyncClient,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    """AUTO-SELECTION submits rendered hashes to a read-only review boundary before bulk resolution.

    Regression for a page/page_size-derived bulk resolve that could silently act on groups the operator
    was never shown. The button now carries NO ``page``/``page_size`` hx-vals; instead it ``hx-include``s
    a hidden ``name="group_hashes"`` input per rendered group, so the write set matches the display set.
    """
    files = await seed_duplicate_group(count=2)
    sha = files[0].sha256_hash

    frag = await client.get("/s/dedupe", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert 'hx-post="/duplicates/review-all"' in body
    assert 'hx-post="/duplicates/resolve-all"' not in body, "the workspace action must not commit before review"
    assert '"page"' not in body, "AUTO-KEEP must not carry a page/page_size re-derivation"
    assert '"page_size"' not in body
    assert 'hx-include="#dedupe-group-hash-inputs"' in body, "AUTO-KEEP must pull the rendered group hashes via hx-include"
    assert f'<input type="hidden" name="group_hashes" value="{sha}">' in body, "the rendered group's hash must be submittable"

    review = await client.post("/duplicates/review-all", data={"group_hashes": [sha]})
    assert review.status_code == 200
    assert "No files have been archived yet" in review.text
    assert 'hx-post="/duplicates/resolve-all"' in review.text
    assert 'name="plan_ids"' in review.text
    assert f'value="{sha}"' not in review.text, "the commit carries opaque plans, not caller-controlled hashes"
    assert "Confirm 1 resolutions" in review.text
