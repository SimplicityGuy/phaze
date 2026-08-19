"""phaze-8p1uq: an automated accessibility checker for the browser suite.

Why this is here and not in phaze-tzy6s.14
=========================================

ADR-0009:85-87 claimed axe and computed-contrast checks belonged to ``.14``. Neither shipped;
``.17`` corrected the ADR text rather than adding the pass, and the operator deferred the checker
itself to this bead (2026-08-17). Before this module NO automated accessibility check ran anywhere
in the epic.

What this is, and what it is NOT
================================

This is a **capability**, deliberately scoped as one. ``phaze-8p1uq`` provides the runner and a
small, high-signal gate; the recorded multi-viewport / dark-theme / screen-reader pass belongs to
``phaze-tzy6s`` sibling work and is not duplicated here. If you are adding "we checked X at width Y
and it looked right", that is the human pass, and it goes there.

axe is also not a substitute for that pass. It catches roughly a third of WCAG issues -- the
machine-checkable third -- and is silent on everything that requires judgment: whether a label
describes its control usefully, whether a focus order is *sensible* rather than merely present,
whether an ``aria-live`` region announces at a useful moment. A green axe run means "no detectable
violation of the rules below", never "accessible".

Why a rule subset rather than the full ruleset
==============================================

Running every axe rule against a dense admin UI produces a large, mixed-confidence finding list on
day one, and a gate that is red on arrival gets a ``continue-on-error`` and then gets ignored --
which is worse than no gate, because it looks like coverage. :data:`RULES` is a subset chosen for
two properties: each rule is machine-decidable with essentially no false positives, and each one
maps to a defect that makes the product unusable rather than merely imperfect. Widen it
deliberately, one rule at a time, with the fixes in the same change.

Loading
=======

axe is fetched once per session from a pinned CDN URL and verified against a pinned SHA-384 digest
BEFORE it is injected, then handed to the page as inline script content. Reaching a CDN is not a new
dependency class for this suite -- it already cannot run without unpkg for htmx and Alpine -- but
three details of how are deliberate:

* **Verified in Python, not by SRI.** Playwright's ``add_script_tag`` has no ``integrity``
  parameter, and hand-writing the tag would put the check in a place whose failure mode is a silent
  no-op script. Checking the bytes here fails loudly, with both digests in the message.
* **Fetched once, cached for the session.** A per-test download would put a network round trip in
  front of every scan -- roughly two dozen of them -- which is the single most likely way this gate
  becomes the flaky test everyone reruns.
* **Not a pyproject dependency.** The browser suite is deliberately outside the default install
  (Playwright itself is an ephemeral ``uv run --with``), so adding axe to the project's dependency
  tree would make every non-browser install pay for it.
"""

from __future__ import annotations

import hashlib
from typing import Any


# Pinned, with the digest that pin was verified against. Bump both together or the pin stops meaning
# anything -- the same reasoning behind the repo's frozen pre-commit hook SHAs.
AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.10.3/axe.min.js"
AXE_SHA384 = "17w4i/EfU4m8vRTTBQs69gAb/fjBpQi0/OOcQTspZXbRlDA0OcGoZX3ZUxQpgTYL"

_SOURCE_CACHE: dict[str, str] = {}


def axe_source() -> str:
    """Fetch (once) and digest-verify the pinned axe-core bundle."""
    if AXE_URL in _SOURCE_CACHE:
        return _SOURCE_CACHE[AXE_URL]

    import base64

    import httpx

    response = httpx.get(AXE_URL, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    digest = base64.b64encode(hashlib.sha384(response.content).digest()).decode()
    if digest != AXE_SHA384:
        raise RuntimeError(
            f"{AXE_URL} does not match its pinned digest.\n  expected sha384-{AXE_SHA384}\n  got      sha384-{digest}\n"
            "Either the CDN served something unexpected or the pin is stale; do not 'fix' this by updating the digest "
            "without checking what changed."
        )
    _SOURCE_CACHE[AXE_URL] = response.text
    return _SOURCE_CACHE[AXE_URL]


# The BLOCKING gate. Every entry is a rule whose violations are unambiguous and whose failure mode
# is "a class of user cannot operate this", not "this could be tidier". Measured clean across five
# workspaces x two themes and three viewports when this landed, so it is a regression gate from day
# one rather than an aspiration.
#
#   image-alt / input-image-alt, label, button-name, link-name, aria-command-name
#                             -- an unnamed control is unusable by a screen reader, full stop. The
#                                icon-only controls in this shell's header and rail are exactly the
#                                shape that loses its name to a refactor.
#   aria-valid-attr-value, aria-required-attr, aria-required-children, aria-allowed-attr
#                             -- broken ARIA is worse than none: it actively misdescribes. This
#                                shell hand-rolls a listbox (the palette) and a dialog (the drawer),
#                                so it is carrying the risk these rules cover.
#   duplicate-id-active       -- a duplicated id on a focusable element breaks label association AND
#                                is the precise defect the dedupe workspace's #toast-container
#                                comments describe htmx cloning OOB swaps into.
#   html-has-lang, document-title, frame-title
#                             -- document-level basics; cheap, and silently lost by a layout change.
RULES = (
    "aria-allowed-attr",
    "aria-command-name",
    "aria-required-attr",
    "aria-required-children",
    "aria-valid-attr-value",
    "button-name",
    "color-contrast",
    "document-title",
    "duplicate-id-active",
    "frame-title",
    "html-has-lang",
    "image-alt",
    "input-image-alt",
    "label",
    "link-name",
)


async def run_axe(page: Any, *, rules: tuple[str, ...] = RULES, include: str | None = None) -> list[dict[str, Any]]:
    """Run axe against ``page`` and return its violations.

    ``include`` scopes the scan to one selector -- useful for asserting a specific dialog or
    workspace rather than the whole document, and necessary for anything mounted in a portal.

    The runner is invoked with an explicit rule list rather than by disabling rules, so adding a
    rule to :data:`RULES` is the only way to widen the gate. A scan that silently grew new rules
    would turn an unrelated change red and teach the next person to disable the check.
    """
    if not await page.evaluate("() => window.axe !== undefined"):
        await page.add_script_tag(content=axe_source())
        await page.wait_for_function("() => window.axe !== undefined")

    context = {"include": [[include]]} if include else None
    result = await page.evaluate(
        """async ({ context, ruleIds }) => {
            const options = {
                runOnly: { type: 'rule', values: ruleIds },
                // Frames are opted out: this app renders none, and axe's frame handling otherwise
                // adds an unbounded wait to every scan for no coverage.
                iframes: false,
            };
            const result = await window.axe.run(context || document, options);
            return result.violations.map(v => ({
                id: v.id,
                impact: v.impact,
                help: v.help,
                nodes: v.nodes.slice(0, 5).map(n => ({ target: n.target, summary: n.failureSummary })),
                count: v.nodes.length,
            }));
        }""",
        {"context": context, "ruleIds": list(rules)},
    )
    return list(result)


def describe(violations: list[dict[str, Any]]) -> str:
    """Render violations as an assertion message that says what to fix and where.

    A bare count is useless in CI output -- the whole cost of an accessibility finding is locating
    it -- so each violation carries its rule id, impact, and up to five offending selectors.
    """
    if not violations:
        return "no violations"
    lines: list[str] = []
    for violation in violations:
        lines.append(f"  [{violation['impact']}] {violation['id']}: {violation['help']} ({violation['count']} node(s))")
        for node in violation["nodes"]:
            lines.append(f"      at {node['target']}")
            summary = (node.get("summary") or "").replace("\n", " ")
            if summary:
                lines.append(f"         {summary[:220]}")
    return "\n".join(lines)
