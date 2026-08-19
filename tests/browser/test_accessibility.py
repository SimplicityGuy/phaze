"""phaze-8p1uq: the automated accessibility gate.

Read ``tests/browser/axe.py``'s docstring first -- in particular the paragraph on what this is not.
This file provides the CAPABILITY the operator deferred here on 2026-08-17; the recorded human
multi-viewport / dark-theme / screen-reader pass ADR-0009 requires is separate work and is not
duplicated here.

Every scan runs against SEEDED state. That is the point of running it in this bead rather than in
``phaze-tzy6s.14``: an empty workspace has no data-bearing rows, no status badges, no toasts and no
disabled controls, which is where nearly all of a dense admin UI's contrast and naming risk
actually lives. An axe pass over five empty states is a pass over the chrome and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.axe import CONTRAST_RULES, describe, run_axe
from tests.browser.helpers import open_shell, settled
from tests.browser.test_command_palette_search import HALCYON, NORWOOD


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


async def _populate(seed: Seeder) -> None:
    """A corpus that lights up the states an empty render cannot reach.

    Deliberately mixed: pending AND approved AND rejected proposals (three different badge
    treatments), a duplicate group (keeper/archive tags, which WCAG 1.4.1 forbids conveying by hue
    alone), and a failed scan batch (the red error surface, the highest-risk contrast pairing in the
    product).
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3", confidence=0.95)
    await seed.proposal(status=ProposalStatus.APPROVED, filename="<set-02>.mp3", confidence=0.42)
    await seed.proposal(status=ProposalStatus.REJECTED, filename="<set-03>.mp3", confidence=None)
    await seed.duplicate_group(count=3)
    from datetime import timedelta

    from phaze.models.scan_batch import ScanStatus

    await seed.scan_batch(status=ScanStatus.FAILED, error_message="Scan root is unreadable: permission denied", age=timedelta(minutes=5))
    await seed.scan_batch(status=ScanStatus.COMPLETED, age=timedelta(minutes=30))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("stage", ["summary", "rename", "apply", "dedupe", "discover"])
async def test_a_populated_workspace_has_no_automated_accessibility_violations(open_page: Any, seed: Seeder, stage: str, theme: str) -> None:
    """The gate: each populated workspace, in both themes, at desktop width.

    Both themes are scanned rather than one because several of these rules resolve differently under
    the dark class -- and scanning under the default "auto" theme would assert whatever the CI
    runner's OS preference happens to be, which is not a property of the product.
    """
    await _populate(seed)

    page = await open_page("desktop", theme=theme)
    await open_shell(page, f"/s/{stage}")
    await page.wait_for_function("t => document.documentElement.classList.contains('dark') === (t === 'dark')", arg=theme)

    violations = await run_axe(page)
    assert violations == [], f"/s/{stage} ({theme} theme) has automated accessibility violations:\n{describe(violations)}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN, PRE-EXISTING: the palette violates WCAG AA 4.5:1 in both themes -- muted greys on panel "
        "backgrounds, the cyan link colour, and white-on-emerald buttons. Measured 2026-08-17; see "
        "tests/browser/axe.py CONTRAST_RULES for the numbers. Repainting design tokens is out of scope for a "
        "test bead. strict=True on purpose: when the palette IS fixed this test fails as XPASS, which is the "
        "signal to delete this marker and fold color-contrast into RULES above."
    ),
)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("stage", ["summary", "rename", "apply", "dedupe", "discover"])
async def test_a_populated_workspace_meets_wcag_aa_contrast(open_page: Any, seed: Seeder, stage: str, theme: str) -> None:
    """The computed-contrast check ADR-0009 asked for -- RUN on every invocation, recorded as failing.

    This is deliberately not a disabled rule or a commented-out assertion. Both of those decay into
    "we have an accessibility checker" while checking nothing, and neither leaves anything behind
    that says how bad it currently is. An ``xfail(strict=True)`` runs the real scan every time, keeps
    the real numbers in the failure output for whoever picks up the palette work, and -- because it
    is strict -- turns into a hard failure the moment the product gets BETTER, which is the only
    reliable way a known-failure marker ever gets removed.

    A count-based baseline was the obvious alternative and is worse: it goes stale on any unrelated
    markup change and teaches people to bump the number.
    """
    await _populate(seed)

    page = await open_page("desktop", theme=theme)
    await open_shell(page, f"/s/{stage}")
    await page.wait_for_function("t => document.documentElement.classList.contains('dark') === (t === 'dark')", arg=theme)

    violations = await run_axe(page, rules=CONTRAST_RULES)
    assert violations == [], f"/s/{stage} ({theme} theme) has contrast violations:\n{describe(violations)}"


@pytest.mark.parametrize("viewport", ["phone", "tablet", "desktop"])
async def test_the_shell_chrome_is_accessible_at_every_viewport(open_page: Any, seed: Seeder, viewport: str) -> None:
    """The rail/drawer, header and palette trigger, across the three width bands.

    The chrome is the part that CHANGES with width -- the rail becomes an off-canvas drawer below
    ``lg`` and the header grows a trigger for it -- so it is the part whose accessible naming can
    be correct at one width and absent at another. A desktop-only scan cannot see that, and it is
    exactly the pre-.13 icon-rail defect's shape.
    """
    await _populate(seed)

    page = await open_page(viewport)
    await open_shell(page, "/s/summary")

    violations = await run_axe(page)
    assert violations == [], f"the shell at {viewport} width has automated accessibility violations:\n{describe(violations)}"


async def test_the_open_command_palette_is_accessible(page: Any, seed: Seeder) -> None:
    """The palette is hand-rolled ARIA, so it gets its own scan with results on screen.

    ``cmdk_modal.html`` implements a combobox over a listbox by hand: ``role="combobox"`` on the
    input, ``role="listbox"`` on the results, ``role="option"`` rows, and ``aria-activedescendant``
    tracking the active one. ``aria-required-children`` and ``aria-valid-attr-value`` are precisely
    the rules that catch that construction coming apart -- an ``aria-activedescendant`` pointing at
    an id that no longer exists after a swap is a real, silent screen-reader failure and is
    machine-detectable, which is a rare combination worth gating on.

    Scanned with results rendered, not empty: an empty listbox trivially satisfies its children
    requirement.

    ``aria-required-children`` used to be excluded here and held as its own strict xfail, because
    the ``#cmdk-command-result`` live region rendered INSIDE the listbox. phaze-jng72 hoisted it out
    to a sibling of ``#cmdk-results``, so the exclusion and the xfail are both gone and the full
    ``RULES`` gate now covers the palette.
    """
    # Word-shaped names, because /search/ is Postgres full-text -- see the NORWOOD/HALCYON comment
    # in test_command_palette_search.py for why a `<track-NN>.mp3` name is unsearchable.
    await seed.file(filename=NORWOOD)
    await seed.file(filename=HALCYON)

    await open_shell(page)
    await page.click("#cmdk-trigger")
    await page.locator("#cmdk-dialog").wait_for(state="visible")
    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_function(
        """expected => document.getElementById('cmdk-results').textContent.includes(expected)""",
        arg=NORWOOD,
        timeout=15_000,
    )

    violations = await run_axe(page, include="#cmdk-dialog")
    assert violations == [], f"the open command palette has automated accessibility violations:\n{describe(violations)}"


async def test_the_execute_confirmation_dialog_is_accessible(page: Any, seed: Seeder) -> None:
    """The product's last safety boundary, scanned while open.

    Reachable only with approved proposals seeded, which is why no automated check has ever run
    against it. A confirmation dialog whose buttons lose their accessible names is a dialog a screen
    reader user cannot safely answer -- and the two answers here are "irreversibly move files" and
    "do nothing".
    """
    await seed.approved_proposals(2)

    await open_shell(page, "/s/apply")
    await page.locator('button[aria-label^="Review and execute"]').click()
    await page.locator("#apply-confirm").wait_for(state="visible")
    await settled(page)

    violations = await run_axe(page, include="#apply-confirm")
    assert violations == [], f"the Execute confirmation dialog has automated accessibility violations:\n{describe(violations)}"
