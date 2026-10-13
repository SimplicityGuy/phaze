"""Execute empty-state navigation and batch exceptions in the actual browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.browser.helpers import open_shell, settled


pytestmark = pytest.mark.browser


@pytest.mark.usefixtures("seed")
async def test_empty_execute_links_into_review_without_an_execution_alarm(page: Any) -> None:
    """The neutral empty state has a working HTMX destination and no dispatch dialog."""
    await open_shell(page, "/s/apply")
    empty = page.locator("[data-execute-empty]")
    assert await empty.is_visible()
    assert await page.locator('button[aria-label^="Review and execute"]').is_disabled()
    assert await page.locator("#apply-confirm").count() == 0
    Path("test-results/screenshots").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path="test-results/screenshots/phaze-wzfzo-6-empty.png", full_page=True)
    await empty.locator("a:has-text('Open Review')").click()
    await settled(page)
    assert "/s/rename?status=needs_review" in page.url
    assert await page.locator("nav[aria-label='Changes Review filters']").is_visible()


async def test_partial_batch_dialog_excludes_only_revoked_approved_work(page: Any, seed: Any) -> None:
    """An approved revoked row remains explicit without inflating the live batch's confirmation."""
    await seed.approved_proposals(1)
    await seed.agent("revoked-seat", revoked=True)
    await seed.approved_proposals(1, agent_id="revoked-seat")
    await seed.proposal(filename="pending.mp3")
    await open_shell(page, "/s/apply")
    workspace = page.locator("#stage-workspace")
    text = (await workspace.inner_text()).casefold()
    assert "will run — 1 operation" in text
    assert "will not run — 1 approved operation excluded" in text
    assert "owned by a revoked agent" in text
    assert "other work outside this approved batch" in text
    await page.locator('button[aria-label^="Review and execute"]').click()
    dialog = page.locator("#apply-confirm")
    await dialog.wait_for(state="visible")
    assert "1 approved operation listed above is excluded" in await dialog.inner_text()
    Path("test-results/screenshots").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path="test-results/screenshots/phaze-wzfzo-6-partial.png", full_page=True)
    await dialog.locator("button:has-text('Cancel')").click()
    await dialog.wait_for(state="hidden")
    assert await page.locator("#apply-execute-response [sse-connect]").count() == 0


async def test_all_revoked_batch_links_agent_recovery_instead_of_more_approval(page: Any, seed: Any) -> None:
    """The zero-runnable boundary preserves the revocation finding and its actual next action."""
    await seed.agent("revoked-seat", revoked=True)
    await seed.approved_proposals(1, agent_id="revoked-seat")
    await open_shell(page, "/s/apply")
    assert await page.locator("[data-execute-empty]").count() == 0
    assert "All approved operations belong to revoked agents" in await page.locator("#stage-workspace").inner_text()
    assert await page.locator('button[aria-label^="Review and execute"]').is_disabled()
    assert await page.locator("#apply-confirm").count() == 0
    Path("test-results/screenshots").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path="test-results/screenshots/phaze-wzfzo-6-all-revoked.png", full_page=True)
    await page.locator('#stage-workspace a:has-text("Open agents")').click()
    await settled(page)
    assert "/s/agents" in page.url
