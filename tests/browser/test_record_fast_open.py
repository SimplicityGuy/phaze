"""phaze-fak5d: a click on a record opener the instant its row exists must still load the record.

Every record opener pairs an Alpine ``@click`` that opens the drawer (wired the moment the row is
inserted) with an htmx ``hx-get`` that htmx wires only in its deferred SETTLE task, about 20 ms after
the swap. A click inside that gap used to open the drawer and fetch nothing, leaving the skeleton up
forever; it is also what made ``test_record_done_gate`` time out 5-10% of runs, because Playwright
can click inside the window. The click here is issued from the MutationObserver callback that first
sees the row, which is deterministically inside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.browser.helpers import open_shell
from tests.browser.test_record_done_gate import _seed_matched_file, _wait_for_record


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser

_CLICK_ON_INSERT = """() => new Promise((resolve) => {
  window.__recordRequests = [];
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    if (String(url).startsWith('/record/')) window.__recordRequests.push(url);
    return open.apply(this, arguments);
  };
  const observer = new MutationObserver(() => {
    const cell = document.querySelector('#analyze-files-view tr[hx-get^="/record/"] td');
    if (!cell) return;
    observer.disconnect();
    // htmx has not processed this row yet: that is the precondition under test, so state it.
    resolve({ htmxWired: !!cell.closest('tr')['htmx-internal-data'] });
    cell.click();
  });
  observer.observe(document.body, { childList: true, subtree: true });
})"""


async def test_a_click_before_htmx_wires_the_row_still_fetches_the_record_once(page: Any, seed: Seeder) -> None:
    target = await _seed_matched_file(seed)
    await open_shell(page, "/s/summary")
    armed = page.evaluate(_CLICK_ON_INSERT)
    await page.evaluate("() => document.querySelector('a[href=\"/s/analyze\"]').click()")
    precondition = await armed
    assert precondition == {"htmxWired": False}, f"the click was not early enough to exercise the race: {precondition}"

    await _wait_for_record(page, target.id)
    # Exactly one fetch: the fallback must not double up when htmx does wire the row in time.
    await page.wait_for_timeout(500)
    requests = await page.evaluate("() => window.__recordRequests")
    assert requests == [f"/record/{target.id}"], requests
