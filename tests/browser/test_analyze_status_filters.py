"""Exercise Analyze status changes through the real browser control."""

from datetime import UTC, datetime
from typing import Any

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from tests.browser.helpers import open_shell, swap_settles


pytestmark = pytest.mark.browser


async def test_analyze_filter_changes_request_and_visible_rows(page: Any, seed: Any) -> None:
    active = await seed.file(filename="active.mp3")
    await seed.analysis(active, completed=False)
    failed = await seed.file(filename="failed.mp3")
    analysis = await seed.analysis(failed, completed=False)
    analysis.failed_at = datetime.now(UTC)
    seed.session.add(CloudJob(file_id=failed.id, status=CloudJobStatus.AWAITING.value))
    awaiting = await seed.file(filename="awaiting.mp3")
    seed.session.add(CloudJob(file_id=awaiting.id, status=CloudJobStatus.AWAITING.value))
    await seed.session.commit()

    await open_shell(page, "/s/analyze")
    await page.wait_for_selector("#analyze-filter-status")
    for status, expected, excluded in [
        ("failed", "failed.mp3", ["active.mp3", "awaiting.mp3"]),
        ("in_flight", "active.mp3", ["failed.mp3", "awaiting.mp3"]),
        ("awaiting_cloud", "awaiting.mp3", ["active.mp3"]),
    ]:
        async with (
            page.expect_request(lambda request, selected=status: "/pipeline/analyze-files" in request.url and f"status={selected}" in request.url),
            swap_settles(page),
        ):
            await page.select_option("#analyze-filter-status", status)
        body = await page.locator("#analyze-files-view").inner_text()
        assert expected in body
        assert all(name not in body for name in excluded)
        assert await page.locator("#analyze-filter-status").input_value() == status
        assert "Clear filter" in body
        if status == "in_flight":
            assert await page.locator('#analyze-file-table [aria-label="Analyze: in flight"]').count() == 1
        if status == "failed":
            assert await page.locator('#analyze-file-table [aria-label="Analyze: failed"]').count() == 1
