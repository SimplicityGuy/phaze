"""Lazy nonmodal health tray keyboard behavior at every supported viewport and theme."""

from __future__ import annotations

from datetime import UTC, datetime
import html
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline._common import templates
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.backends.lane_detail import claim_overdue_seconds
from tests._saq_jobs_seed import now_ms, seed_job
from tests.browser.test_shell_contract import _open


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.browser


async def test_polled_incidents_announce_only_meaningful_changes(page: Any, seed: Any) -> None:
    """Observe real HTMX OOB delivery, not source strings or a helper's return value."""
    first = {
        "id": "analysis-failed",
        "title": "Failed analyses",
        "count": 2,
        "tone": "danger",
        "detail": "Retry measured failures.",
        "href": "/s/files",
        "action": "Open failures",
    }
    current = [first]
    measurement_known = True
    polls = 0

    async def poll(route: Any) -> None:
        nonlocal polls
        response = await route.fetch()
        body = await response.text()
        seed = (
            '<div id="incident-announcement-seed" hidden hx-swap-oob="true" data-known="'
            + str(measurement_known).lower()
            + '" data-incidents="'
            + html.escape(json.dumps(current), quote=True)
            + '"></div>'
        )
        body = re.sub(r'<div id="incident-announcement-seed"[^>]*></div>', lambda _: seed, body)
        card = templates.env.get_template("pipeline/partials/analysis_failed_card.html").render(
            oob=True, analysis_incidents_known=measurement_known, analysis_incidents=current, analysis_failed_count=2 if current else 0
        )
        body = re.sub(r'<section id="analysis-health-card".*?</section>', lambda _: card, body, flags=re.S)
        await route.fulfill(response=response, body=body)
        polls += 1

    await seed.file(filename="poll-controls.mp3")  # Mount visible Analyze controls, rather than the first-run hidden hosts.
    await page.route("**/pipeline/stats*", poll)
    await _open(page, "/s/analyze")
    await page.evaluate("""() => {
        window.announcements = [];
        window.incidentPollsSettled = 0;
        const poll = document.getElementById('pipeline-stats');
        poll.setAttribute('hx-trigger', 'refresh');
        htmx.process(poll);
        document.body.addEventListener('htmx:afterSettle', event => {
            if (event.detail.target.id === 'pipeline-stats') window.incidentPollsSettled++;
        });
        const region = document.getElementById('incident-announcement');
        window.announcementObserver = new MutationObserver(() => window.announcements.push(region.textContent));
        window.announcementObserver.observe(region, { childList:true, characterData:true, subtree:true });
    }""")

    async def refresh() -> None:
        before = polls
        settled_before = await page.evaluate("window.incidentPollsSettled")
        await page.evaluate("htmx.trigger(document.getElementById('pipeline-stats'), 'refresh')")
        await page.wait_for_function("before => window.incidentPollsSettled > before", arg=settled_before)
        assert polls > before

    await refresh()
    baseline = await page.evaluate("window.announcements.length")
    assert baseline == 1
    assert await page.locator('#analysis-health-card [data-incident-id="analysis-failed"]').count() == 1
    await page.locator("#analysis-health-summary").click()

    async def retry_failure(route: Any) -> None:
        await route.fulfill(status=503, body="Unavailable")

    await page.route("**/pipeline/analysis-failed/retry", retry_failure)
    page.once("dialog", lambda dialog: dialog.accept())
    await page.locator("#analysis-retry-failed").click()
    await page.locator("#retry-failed-result [data-action-failure]").wait_for()
    await refresh()
    await refresh()
    error = page.locator("#retry-failed-result [data-action-failure]")
    assert await error.is_visible(), "unchanged health polling must retain the originating control's action error"
    assert await error.get_attribute("aria-live") == "off", "retained feedback must not reannounce during an unrelated poll"
    assert await page.locator("#analysis-retry-failed").is_enabled()
    await page.unroute("**/pipeline/analysis-failed/retry", retry_failure)
    page.once("dialog", lambda dialog: dialog.accept())
    async with page.expect_response("**/pipeline/analysis-failed/retry") as retry_response:
        await page.locator("#analysis-retry-failed").click()
    assert (await retry_response.value).status == 200
    await page.locator("#retry-failed-result").get_by_text("No failed files to retry.", exact=True).wait_for()
    assert await error.count() == 0, "the real response must clear the transferred transport error without claiming any work was queued"
    status = page.locator("#retry-failed-result [data-action-status]")
    expires = await status.get_attribute("data-action-expires")
    assert expires is not None
    await page.wait_for_timeout(3000)  # Poll near the original expiry: restarting five seconds would fail the deadline assertion.
    await refresh()
    await refresh()
    assert await status.get_attribute("data-action-expires") == expires
    remaining = await page.evaluate("expires => Number(expires) - Date.now()", expires)
    await status.wait_for(state="detached", timeout=max(1000, remaining + 1000))
    await page.route("**/pipeline/analysis-failed/retry", retry_failure)
    page.once("dialog", lambda dialog: dialog.accept())
    await page.locator("#analysis-retry-failed").click()
    await error.wait_for()
    await refresh()
    await refresh()
    await error.get_by_role("button", name="Dismiss action error").click()
    assert await error.count() == 0, "dismissal must still work after the poll transfers response markup"
    assert await page.evaluate("window.announcements.length") == baseline
    measurement_known = False
    await refresh()
    assert "unavailable" in await page.locator("#incident-announcement").inner_text()
    await refresh()
    assert await page.evaluate("window.announcements.length") == baseline + 1
    measurement_known = True
    await refresh()
    assert "available again" in await page.locator("#incident-announcement").inner_text()
    baseline += 2
    current = [{**first, "count": 3}]
    await refresh()
    assert await page.evaluate("window.announcements.length") == baseline + 1
    current.append({**first, "id": "pipeline-orphans", "title": "Orphaned work", "count": 1})
    await refresh()
    assert await page.evaluate("window.announcements.length") == baseline + 2
    current = []
    await refresh()
    assert await page.evaluate("window.announcements.length") == baseline + 3
    assert "cleared" in await page.locator("#incident-announcement").inner_text()
    assert await page.locator("#analysis-health-card [role=alert], #analysis-health-card [role=status], #analyze-lanes [role=alert]").count() == 0
    assert await page.locator("#incident-announcement").get_attribute("role") == "status"
    assert await page.locator("#incident-announcement").get_attribute("aria-live") == "polite"
    assert await page.locator("#incident-announcement").get_attribute("aria-atomic") == "true"
    current = [first]
    await refresh()
    assert await page.evaluate("window.announcements.length") == baseline + 4


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
