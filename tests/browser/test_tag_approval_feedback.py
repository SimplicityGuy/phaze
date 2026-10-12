"""The rendered tag approval must explain guarded refusals without replacing review selections."""

from __future__ import annotations

from typing import Any
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderSourceObservation
from phaze.models.tag_write_log import TagWriteLog
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services.local_source_import import decide_local_source, import_local_source
from tests.browser.helpers import open_shell


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("changed", ["metadata", "source"])
async def test_stale_tag_approval_explains_refusal_and_requires_refresh(page: Any, seed: Any, changed: str) -> None:
    file = await seed.bulk_eligible_tag_change()
    metadata = await seed.session.scalar(select(FileMetadata).where(FileMetadata.file_id == file.id))
    if changed == "source":
        metadata.raw_tags = {"comments": "01. Example Artist - First\n02. Example Artist - Second\n"}
        await seed.session.commit()
        imported = await import_local_source(seed.session, ImportLocalSource(media_id=file.id, embedded_channel="comments"))
        observation = await seed.session.get(ProviderSourceObservation, imported.tracklist_observation_id)
        await decide_local_source(
            seed.session,
            SourceDecision(
                media_id=file.id,
                observation_id=observation.id,
                kind="tracklist",
                decision_id=uuid.uuid4(),
                expected_revision=observation.revision,
                expected_source_sha256=file.sha256_hash,
                expected_media_sha256=file.sha256_hash,
            ),
            actor="synthetic-reviewer",
        )
        await seed.session.commit()

    sibling = await seed.proposal(filename="<track-02>.mp3")
    await open_shell(page, "/s/rename")
    checkbox = page.get_by_label("Select <track-02>.mp3")
    await checkbox.check()
    sibling_row = page.locator(f"#rename-row-{sibling.id}")
    await sibling_row.get_by_role("button", name="EDIT", exact=True).click()
    await sibling_row.locator('input[name="proposed"]').fill("Unsaved reviewed title.mp3")
    row = page.locator(f"#tagwrite-row-{file.id}")
    approve = row.get_by_role("button", name="APPROVE", exact=True)
    old_token = await approve.get_attribute("hx-vals")
    await row.evaluate("node => window.__tagRow = node")
    if changed == "source":
        metadata.raw_tags = {"comments": "01. Changed Artist - New recording\n"}
    else:
        metadata.album = "New enrichment after review"
    await seed.session.commit()

    async with page.expect_response(lambda response: response.url.split("?", 1)[0].endswith(f"/tags/{file.id}/write")) as result:
        await approve.click()
    assert (await result.value).status == 409
    alert = row.get_by_role("alert")
    await alert.wait_for(state="visible", timeout=5_000)
    assert "refresh" in (await alert.inner_text()).lower()
    assert await approve.is_disabled()
    assert await approve.get_attribute("hx-vals") == old_token
    assert await checkbox.is_checked()
    assert await sibling_row.locator('input[name="proposed"]').input_value() == "Unsaved reviewed title.mp3"
    assert await row.evaluate("node => node === window.__tagRow")
    assert await seed.session.scalar(select(func.count()).select_from(TagWriteLog)) == 0

    await alert.get_by_role("button", name="Refresh review evidence").click()
    await page.wait_for_function("() => !window.__tagRow")
    await row.get_by_role("button", name="APPROVE", exact=True).wait_for()
    assert await row.get_by_role("button", name="APPROVE", exact=True).get_attribute("hx-vals") != old_token
    assert await checkbox.is_checked(), "refresh must retain selection carried in the review URL"
    assert await seed.session.scalar(select(func.count()).select_from(TagWriteLog)) == 0


@pytest.mark.parametrize("failure", ["server", "network"])
async def test_tag_approval_transport_failure_is_bounded_and_visible(page: Any, seed: Any, failure: str) -> None:
    file = await seed.bulk_eligible_tag_change()
    await open_shell(page, "/s/rename")
    row = page.locator(f"#tagwrite-row-{file.id}")

    async def fail(route: Any) -> None:
        if failure == "server":
            await route.fulfill(status=503, content_type="text/plain", body="synthetic private failure detail")
        else:
            await route.abort("failed")

    await page.route(f"**/tags/{file.id}/write*", fail)
    await row.get_by_role("button", name="APPROVE", exact=True).click()
    alert = row.get_by_role("alert")
    await alert.wait_for(state="visible", timeout=5_000)
    assert "could not be confirmed" in (await alert.inner_text()).lower()
    assert "synthetic private failure detail" not in await alert.inner_text()
    assert len(await alert.inner_text()) < 250
    assert await row.get_by_role("button", name="APPROVE", exact=True).is_disabled()
    assert await seed.session.scalar(select(func.count()).select_from(TagWriteLog)) == 0
