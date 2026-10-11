"""Real Alpine/HTMX regression: closing a detail must remove the row, not its X button."""

from collections.abc import Iterator
from typing import Any

import pytest

from tests.browser.conftest import live_server as standard_live_server
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


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Use the real server/database harness with configured, idle cluster backends."""
    registry = tmp_path_factory.mktemp("reopen-registry") / "backends.toml"
    registry.write_text(_REGISTRY)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(registry))
        yield from standard_live_server.__wrapped__()


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


async def test_header_counts_all_configured_lanes_including_idle_clusters(page: Any) -> None:
    await open_shell(page, "/s/agents")
    await settled(page)
    assert await page.locator("header").evaluate("el => getComputedStyle(el).display") == "flex", "build the stylesheet with just tailwind"
    inventory = page.get_by_role("link", name="3 configured lanes; 2 configured clusters", exact=True)
    await inventory.wait_for(state="visible")
    assert "Lanes" in await inventory.inner_text()
    assert "Clusters" in await inventory.inner_text()
    # A poll must retain inventory totals even though both clusters have no running jobs.
    async with page.expect_response(lambda response: "/pipeline/stats" in response.url and response.status == 200):
        pass
    await inventory.wait_for(state="visible")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.wait_for_function("() => window.innerWidth === 390")
    geometry = await page.locator("header").evaluate("el => ({width: el.scrollWidth, viewport: window.innerWidth})")
    assert geometry["width"] <= geometry["viewport"], f"inventory badges must fit a phone header: {geometry}"
