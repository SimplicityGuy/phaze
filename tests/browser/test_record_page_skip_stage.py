"""phaze-iyqhg: "Skip stage…" actually submits on the standalone ``/files/{id}`` page.

``_force_skip_dialog.html`` renders its submit ``disabled`` + ``data-hx-guard`` (phaze-tckiy), and only
the ``htmx:load`` listener releases that guard. Until this bead the listener lived inline in
``shell/shell.html`` alone, and the standalone record page -- which renders the SAME
``record/_record_content.html`` -- carried neither it nor ``#toast-container``. So on ``/files/{id}``
the dialog opened, took a reason, and then offered a submit that could never be clicked; and had it
posted, the OOB success toast had no target. The drawer inside the shell was unaffected, which is why
nothing server-side could see it: the markup is identical on both routes, and only a real browser
running the real page's scripts observes whether the button ever becomes enabled.

This test was run against the pre-fix templates first and failed on the enable wait, as the bead's
acceptance requires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


async def test_skip_stage_submits_and_toasts_on_the_standalone_record_page(page: Any, seed: Seeder) -> None:
    file = await seed.file(filename="<set-01>.mp3")  # metadata never ran: not started, so the control is offered

    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    await page.evaluate("window.__documentAlive = true")

    await page.get_by_role("button", name="Skip metadata for this file").click()
    dialog = page.get_by_role("dialog", name="Skip metadata for this file")
    await dialog.wait_for(state="visible")
    await dialog.locator("textarea[name=reason]").fill("synthetic: unreadable tags")

    submit = dialog.get_by_role("button", name="Skip stage", exact=True)
    # The load-bearing wait: pre-fix, nothing on this page ever removed `disabled`.
    await page.wait_for_function(
        "el => !el.disabled && !el.hasAttribute('data-hx-guard')",
        arg=await submit.element_handle(),
        timeout=5_000,
    )
    await submit.click()

    toast = page.locator("#toast-container [role=status]")
    await toast.first.wait_for(state="visible", timeout=5_000)
    assert "Skipped metadata" in await toast.first.inner_text()
    await dialog.wait_for(state="hidden")
    assert "skipped" in (await page.locator(f"#stage-pill-metadata-{file.id}").inner_text()).lower()
    assert await page.evaluate("window.__documentAlive === true"), "the submit fell through to a native navigation"
    assert "reason=" not in page.url, "the reason leaked into the address bar via a native GET"
