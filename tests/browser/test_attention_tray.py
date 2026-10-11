"""Lazy nonmodal health tray keyboard behavior at every supported viewport and theme."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.backends.lane_detail import claim_overdue_seconds
from tests._saq_jobs_seed import now_ms, seed_job
from tests.browser.test_shell_contract import _open


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("viewport", ["phone", "tablet", "desktop"])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_health_tray_lazy_keyboard_focus_and_layout(page_at: Any, viewport: str, theme: str) -> None:
    async with page_at(viewport=viewport, theme=theme) as page:
        loads: list[str] = []
        page.on("request", lambda request: loads.append(request.url) if "/shell/attention" in request.url else None)
        await _open(page, "/s/files")
        assert loads == [], "attention items loaded before the tray was opened"
        assert await page.evaluate("document.documentElement.classList.contains('dark')") is (theme == "dark")
        if viewport == "phone":
            assert await page.evaluate("window.innerWidth") == 390
        assert await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1
        pill = page.locator("#attention-pill")
        assert await pill.get_attribute("aria-expanded") == "false"
        assert await pill.get_attribute("title") == await pill.get_attribute("aria-label")
        await pill.focus()
        await page.keyboard.press("Enter")
        tray = page.locator("#attention-tray")
        await tray.wait_for(state="visible")
        await page.wait_for_function("document.getElementById('attention-tray-content').textContent.indexOf('Loading attention items') === -1")
        assert len(loads) == 1
        assert await pill.get_attribute("aria-expanded") == "true"
        assert await tray.get_attribute("role") != "dialog"
        assert await tray.get_attribute("aria-modal") is None
        await page.wait_for_function("document.activeElement.id === 'attention-tray-heading'")
        assert await tray.evaluate("node => node.scrollWidth - node.clientWidth") <= 1
        bounds = await tray.bounding_box()
        assert bounds is not None and bounds["x"] >= 0
        assert bounds["x"] + bounds["width"] <= await page.evaluate("window.innerWidth")
        await page.keyboard.press("Tab")
        assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == "Close attention tray"
        await page.keyboard.press("Escape")
        await tray.wait_for(state="hidden")
        await page.wait_for_function("document.activeElement.id === 'attention-pill'")
        assert await pill.get_attribute("aria-expanded") == "false"
        # A closed tray's global Escape handler must leave another control's focus alone.
        await page.locator("#cmdk-trigger").focus()
        await page.keyboard.press("Escape")
        assert await page.evaluate("document.activeElement.id") == "cmdk-trigger"
        await pill.focus()
        await page.keyboard.press("Enter")
        await tray.wait_for(state="visible")
        await page.wait_for_function("document.getElementById('attention-tray-content').textContent.indexOf('Loading attention items') === -1")
        await tray.get_by_role("link", name="Open Summary", exact=True).click()
        await tray.wait_for(state="hidden")
        await page.wait_for_function("document.activeElement.id === 'attention-pill'")
        assert "/s/summary" in page.url


@pytest_asyncio.fixture(loop_scope="session")
async def incident_claim(seed: Any, redis_url: str) -> AsyncGenerator[None]:
    """Own and remove the synthetic claim: the browser harness preserves SAQ tables between cases."""
    agent = await seed.agent()
    router = AgentTaskRouter(
        queue_url=seed.session.bind.url.render_as_string(hide_password=False).replace("postgresql+asyncpg", "postgresql"),
        cache_redis_url=redis_url,
    )
    queue = str(router.queue_for(agent.id, "analyze").name)
    clock = await now_ms(seed.session)
    key = "process_file:incident-claim"
    try:
        await seed_job(
            seed.session,
            queue,
            key=key,
            status="active",
            blob={"started": clock - (claim_overdue_seconds() + 120) * 1000, "touched": clock, "timeout": 0, "heartbeat": 120},
        )
        await seed.session.commit()
        yield
    finally:
        await seed.session.rollback()
        await seed.session.execute(text("DELETE FROM saq_jobs WHERE key = :key"), {"key": key})
        await seed.session.commit()
        await router.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_incident_rows_and_recovery_safety_on_phone(page_at: Any, seed: Any, theme: str, tmp_path: Path, incident_claim: None) -> None:
    for index in range(8):
        record = await seed.file(filename=f"failed-{index}.mp3")
        analysis = await seed.analysis(record, completed=False)
        analysis.failed_at = datetime.now(UTC)
    orphan = await seed.file(filename="orphan.mp3")
    seed.session.add(SchedulingLedger(key=f"process_file:{orphan.id}", function="process_file", routing="agent", payload={"file_id": str(orphan.id)}))
    await seed.session.commit()
    async with page_at(viewport="phone", theme=theme) as page:
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        await _open(page, "/s/analyze")
        card = page.locator("#analysis-health-card")
        await card.locator('[data-incident-id="analysis-failed"]').wait_for(timeout=30000)
        await card.locator('[data-incident-id="pipeline-orphans"]').wait_for(timeout=30000)
        await card.locator('[data-incident-id="lane:local:stuck"]').wait_for(timeout=30000)
        expected = await card.locator("[data-incident-id]").evaluate_all(
            "rows => rows.map(row => [row.dataset.incidentId, row.dataset.incidentTone])"
        )
        assert len(expected) == len({item[0] for item in expected})
        assert await card.locator('[data-incident-id="analysis-failed"]').count() == 1
        assert "8 file(s) reached terminal failure" in await card.inner_text()
        recover = page.get_by_role("button", name="Recover orphaned work", exact=True)
        await page.wait_for_function("document.querySelector('[hx-post=\"/pipeline/recover\"]').disabled")
        assert await recover.is_disabled()
        assert "across all pipeline stages" in await page.locator("#analyze-recover-gate-copy").inner_text()
        await recover.evaluate("node => node.click()")
        await card.locator("summary").focus()
        await page.keyboard.press("Enter")
        page.on("dialog", lambda dialog: dialog.dismiss())
        await page.locator("#analysis-retry-failed").click()
        assert posts == [], "disabled recovery or cancelled retry sent a mutation"
        assert await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1
        await page.locator("#attention-pill").click()
        await page.locator('#attention-tray-content [data-incident-id="analysis-failed"]').wait_for()
        tray_rows = await page.locator("#attention-tray-content [data-incident-id]").evaluate_all(
            "rows => rows.map(row => [row.dataset.incidentId, row.dataset.incidentTone])"
        )
        assert tray_rows == expected
        await page.keyboard.press("Escape")
        artifacts = Path(os.environ.get("PHAZE_INCIDENT_ARTIFACT_DIR", str(tmp_path / "phaze-wzfzo-5")))
        artifacts.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(artifacts / f"incidents-phone-{theme}.png"), full_page=True)
        await page.locator("#attention-pill").click()
        await page.locator("#attention-tray-content").get_by_role("link", name="Open Summary", exact=True).click()
        await page.locator('[data-summary-overview] [data-incident-id="analysis-failed"]').wait_for()
        summary_rows = await page.locator("[data-summary-overview] [data-incident-id]").evaluate_all(
            "rows => rows.map(row => [row.dataset.incidentId, row.dataset.incidentTone])"
        )
        assert summary_rows == expected
        assert "Analysis failures" not in await page.locator('section[aria-labelledby="next-action-title"]').inner_text()
        recommendation = page.get_by_role("link", name="Review incidents", exact=True)
        assert await recommendation.get_attribute("hx-get") is None
        await recommendation.click()
        await page.screenshot(path=str(artifacts / f"incidents-summary-phone-{theme}.png"), full_page=True)
        await (
            page.locator('[data-summary-overview] [data-incident-id="analysis-failed"]')
            .get_by_role("link", name="Open failed files", exact=True)
            .click()
        )
        await page.wait_for_url("**/s/files?stage=analyze&bucket=failed&scope=media")
        assert "failed-0.mp3" in await page.locator("#stage-workspace").inner_text()
