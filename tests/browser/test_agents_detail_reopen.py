"""Real Alpine/HTMX regression: closing a detail must remove the row, not its X button."""

from collections.abc import Iterator
from typing import Any

import pytest

from tests.browser.conftest import serve_live_app
from tests.browser.helpers import open_shell, settled


pytestmark = pytest.mark.browser

_REGISTRY = """
[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 2

[[backends]]
kind = "compute"
id = "compute-a"
rank = 20
cap = 2
agent_ref = "compute-agent"
scratch_dir = "/scratch/compute"
push_host = "127.0.0.1"

[[backends]]
kind = "kueue"
id = "burst-a"
rank = 10
cap = 3
buckets = ["burst-bucket"]

[backends.kube]
api_url = "https://127.0.0.1:1"
namespace = "phaze"
local_queue = "phaze-burst"

[[buckets]]
id = "burst-bucket"
scope = "cluster-specific"
endpoint_url = "https://127.0.0.1:1"
bucket = "phaze-burst"
"""


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A private server and database for this module's backend registry.

    Module-scoped on purpose: a session-scoped override was a SECOND server on the session server's
    database, whose DROP ... WITH (FORCE) severed the first server mid-run and left every later module
    talking to a wiped schema (the epic's browser CI stalled after this module's tests).
    """
    registry = tmp_path_factory.mktemp("reopen-registry") / "backends.toml"
    registry.write_text(_REGISTRY)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(registry))
        patch.setenv("PHAZE_BROWSER_DB_SUFFIX", "_reopen")
        yield from serve_live_app()


@pytest.mark.parametrize(("identity", "prefix"), [("burst-a", "compute-lane"), ("reopen-agent", "agent")])
async def test_details_reopen_after_clicking_close(page: Any, seed: Any, identity: str, prefix: str) -> None:
    await seed.agent("reopen-agent")
    await open_shell(page, "/s/agents")
    await settled(page)
    trigger = page.locator(f"#{prefix}-trigger-{identity}-details")
    detail = page.locator(f"#{prefix}-detail-row-{identity}")

    for _ in range(3):
        await trigger.click()
        await detail.wait_for(state="visible")
        await detail.get_by_role("button", name="Close detail", exact=True).click()
        # Reopen must work immediately, without waiting for the table's five-second poll.
        await detail.wait_for(state="detached", timeout=1000)
        assert await trigger.evaluate("el => el === document.activeElement"), "close must return focus before removing the row"


async def test_header_health_pill_survives_stats_poll_and_fits_phone(page: Any) -> None:
    await open_shell(page, "/s/agents")
    await settled(page)
    assert await page.locator("header").evaluate("el => getComputedStyle(el).display") == "flex", "build the stylesheet with just tailwind"
    pill = page.get_by_role("button").and_(page.locator("#attention-pill"))
    await pill.wait_for(state="visible")
    assert await pill.get_attribute("aria-controls") == "attention-tray"
    assert await pill.get_attribute("aria-expanded") == "false"

    async def assert_health_cue() -> None:
        state = await page.evaluate("() => ({...Alpine.store('pipeline')})")
        # Inventory remains actual configured capacity; it is not the health count.
        assert state["laneCount"] == 3
        assert state["clusterCount"] == 2
        known = state["seedKnown"] and state["attentionKnown"]
        label = await pill.get_attribute("aria-label")
        assert label == await pill.get_attribute("title")
        if known:
            count = str(state["attentionCount"])
            status = "Healthy" if state["attentionCount"] == 0 else "Attention"
            assert " ".join((await pill.inner_text()).split()) == f"{status} {count}"
            assert label == f"{count} attention items; open attention tray"
        else:
            assert " ".join((await pill.inner_text()).split()) == "Health —"
            assert label == "Attention status unavailable; open attention tray"

    await assert_health_cue()
    # Prove the existing real poll retains truthful capacity and the health cue.
    async with page.expect_response(lambda response: "/pipeline/stats" in response.url and response.status == 200):
        pass
    await settled(page)
    await assert_health_cue()
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.wait_for_function("() => window.innerWidth === 390")
    geometry = await page.locator("header").evaluate("el => ({width: el.scrollWidth, viewport: window.innerWidth})")
    assert geometry["width"] <= geometry["viewport"], f"health controls must fit a phone header: {geometry}"
    await pill.focus()
    async with page.expect_response(lambda response: "/shell/attention" in response.url and response.status == 200):
        await page.keyboard.press("Enter")
    tray = page.locator("#attention-tray")
    await tray.wait_for(state="visible")
    assert await pill.get_attribute("aria-expanded") == "true"
    assert await tray.get_attribute("aria-labelledby") == "attention-tray-heading"
    assert await tray.get_attribute("aria-modal") is None
    await page.wait_for_function("document.activeElement.id === 'attention-tray-heading'")
    await page.keyboard.press("Escape")
    await tray.wait_for(state="hidden")
    await page.wait_for_function("document.activeElement.id === 'attention-pill'")
    assert await pill.get_attribute("aria-expanded") == "false"
