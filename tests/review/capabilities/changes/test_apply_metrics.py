"""phaze-nwmsu: the Apply status strip is the shared ``ui.metric`` strip, and no data reads as ``—``."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_apply_with_no_proposals_shows_an_em_dash_not_zero_percent(client: AsyncClient) -> None:
    """An empty archive has no average confidence to report: ``0%`` would assert a measurement that does not exist."""
    response = await client.get("/s/apply", headers={"HX-Request": "true"})

    assert response.status_code == 200
    match = re.search(r"Avg confidence</dt>\s*<dd[^>]*>([^<]*)</dd>", response.text)
    assert match is not None, "the Avg confidence tile must render through ui.metric (a <dt>/<dd> pair)"
    assert match.group(1).strip() == "—"
    assert "0%" not in match.group(0)


@pytest.mark.asyncio
async def test_apply_metrics_use_the_shared_framed_mono_strip(client: AsyncClient) -> None:
    """The six status counts + average confidence are ``ui.metric`` tiles in a ``ui.metric_strip`` frame."""
    response = await client.get("/s/apply", headers={"HX-Request": "true"})

    assert 'aria-label="Proposal status counts"' in response.text
    section = response.text.split('aria-label="Proposal status counts"', 1)[1].split("</section>", 1)[0]
    labels = re.findall(r'<dt class="font-jura[^"]*">([^<]+)</dt>', section)
    assert labels == ["Total", "Needs Review", "Approved", "Executed", "Blocked", "Rejected", "Avg confidence"]
    values = re.findall(r'<dd class="mt-1 font-mono[^"]*">', section)
    assert len(values) == len(labels), "every value is mono (ui.metric), none falls back to Inter"
