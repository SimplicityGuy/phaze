"""Full records and their 404 shell must receive the shared stats poll without losing the snapshot."""

from __future__ import annotations

from typing import Any
import uuid

import pytest


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("missing", [False, True], ids=["record", "not-found"])
async def test_record_page_receives_multiple_poll_ticks(page: Any, seed: Any, missing: bool) -> None:
    file_id = uuid.uuid4() if missing else (await seed.file()).id
    await page.add_init_script("""document.addEventListener('htmx:oobErrorNoTarget', event => {
        window.__missingPollTargets = (window.__missingPollTargets || []).concat(event.detail.content.id);
    });""")
    response = await page.goto(f"/files/{file_id}", wait_until="domcontentloaded")
    assert response.status == (404 if missing else 200)
    await page.wait_for_function("() => window.Alpine && Alpine.store('pipeline')")
    await page.evaluate("window.__recordSnapshot = document.getElementById('record-main')")

    for index in range(2):
        await seed.file(filename=f"<track-{index + 1:02d}>.mp3")
        expected = index + 1 + (0 if missing else 1)
        await page.wait_for_function(
            "count => Alpine.store('pipeline').seedKnown && Alpine.store('pipeline').discovered === count",
            arg=expected,
            timeout=20_000,
        )
        assert await page.locator('a[data-rail-stage="discover"] span[aria-label]').get_attribute("aria-label") == f"{expected} files discovered"
        assert await page.evaluate("window.__missingPollTargets || []") == []
        assert await page.evaluate("window.__recordSnapshot === document.getElementById('record-main')")
        assert await page.locator("#pipeline-stats").count() == 1
        duplicates = await page.evaluate("""() => {
            const ids = Array.from(document.querySelectorAll('[id]'), node => node.id);
            return ids.filter((id, index) => ids.indexOf(id) !== index);
        }""")
        assert duplicates == []
