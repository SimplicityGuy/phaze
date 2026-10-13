"""phaze-vvzam: the Summary flow board in a real browser, at phone and desktop widths, in both themes.

Server-side tests prove the markup; this proves what Chromium lays out and paints from it: the board is a
real visible picture at md+ and absent below it, the card grid carries the complete values on a phone, the
visually hidden text equivalent is present at every width, nothing scrolls the document sideways, the
outlined bottleneck agrees with Needs Attention, and the board's SVG text meets WCAG AA contrast against
its own node fill in the theme that actually painted.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser import seed as _seed
from tests.browser.conftest import THEMES
from tests.browser.helpers import open_shell


pytestmark = pytest.mark.browser


_PROBE_JS = r"""
() => {
    const doc = document.documentElement;
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    // Computed colours arrive in whatever space the stylesheet used (Tailwind 4 emits oklch), so
    // resolve each one through a 1x1 canvas to sRGB bytes rather than parsing the string.
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d', {willReadFrequently: true});
    const rgb = value => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
    const lum = ([r, g, b]) => {
        const c = [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    };
    const ratio = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

    const board = document.querySelector('figure[data-flow-board]');
    const svg = board && board.querySelector('svg');
    const contrasts = [];
    if (svg && visible(svg)) {
        for (const group of svg.querySelectorAll('g[data-flow-node]')) {
            const fill = getComputedStyle(group.querySelector('rect')).fill;
            for (const label of group.querySelectorAll('text')) {
                contrasts.push({node: group.dataset.flowNode, text: label.textContent.trim(), fg: getComputedStyle(label).fill, bg: fill, ratio: ratio(getComputedStyle(label).fill, fill)});
            }
        }
    }
    // Every label must sit inside its own node box: SVG text does not wrap or clip, so a long count
    // would otherwise run across an edge or into the next node.
    const spills = [];
    if (svg && visible(svg)) {
        for (const group of svg.querySelectorAll('g[data-flow-node]')) {
            const box = group.querySelector('rect').getBBox();
            for (const label of group.querySelectorAll('text')) {
                const t = label.getBBox();
                if (t.x < box.x || t.x + t.width > box.x + box.width || t.y < box.y || t.y + t.height > box.y + box.height) {
                    spills.push({node: group.dataset.flowNode, text: label.textContent.trim()});
                }
            }
        }
    }
    const outlined = svg ? [...svg.querySelectorAll('g[data-bottleneck] rect')].map(r => ({
        node: r.parentElement.dataset.flowNode, width: getComputedStyle(r).strokeWidth,
    })) : [];
    const flowSection = document.querySelector('section[aria-labelledby="pipeline-flow-title"]');
    const cards = [...flowSection.querySelectorAll('a[href="/s/metadata"], a[href="/s/analyze"], a[href="/s/propose"]')];
    const text = document.querySelector('[data-flow-text]');
    const attention = document.querySelector('section[aria-labelledby="needs-attention-title"]');
    const topAttention = attention.querySelector('ol li h3');
    return {
        overflow: doc.scrollWidth - doc.clientWidth,
        dark: doc.classList.contains('dark'),
        boardVisible: visible(svg),
        boardRight: svg && visible(svg) ? Math.round(svg.getBoundingClientRect().right) : 0,
        viewport: doc.clientWidth,
        nodeCount: svg ? svg.querySelectorAll('g[data-flow-node]').length : 0,
        edgeCount: svg ? svg.querySelectorAll('path[data-flow-edge]').length : 0,
        contrasts,
        spills,
        outlined,
        cards: cards.map(a => ({visible: visible(a), text: a.innerText.replace(/\s+/g, ' ').trim(), right: Math.round(a.getBoundingClientRect().right)})),
        textItems: text ? [...text.querySelectorAll('li')].map(li => li.textContent.replace(/\s+/g, ' ').trim()) : [],
        textClipped: text ? getComputedStyle(text).clipPath !== 'none' || getComputedStyle(text).clip !== 'auto' : false,
        callout: (document.querySelector('[data-flow-bottleneck]') || {}).textContent || '',
        callVisible: visible(document.querySelector('[data-flow-bottleneck]')),
        topAttention: topAttention ? topAttention.firstChild.textContent.trim() : null,
        attentionUnknown: attention.innerText.includes('Attention status unavailable'),
    };
}
"""


@pytest.mark.parametrize(("viewport", "theme"), [pytest.param(v, t, id=f"{v}-{t}") for v in ("phone", "desktop") for t in THEMES])
async def test_flow_board_holds_its_layout_values_and_contrast(viewport: str, theme: str, page_at: Any, browser_dsn: str) -> None:
    await _seed.reset_dsn(browser_dsn)
    # Terminal metadata + analysis failures: a real attention item, so the bottleneck outline is exercised.
    await _seed.seed_populated(browser_dsn, with_failures=True)

    async with page_at(viewport=viewport, theme=theme) as page:
        await open_shell(page, "/s/summary")
        await page.wait_for_selector("[data-flow-text]", state="attached")
        probe = dict(await page.evaluate(_PROBE_JS))

    where = f"{viewport}/{theme}"
    assert probe["dark"] is (theme == "dark"), f"{where}: the pinned theme did not paint"
    assert probe["overflow"] <= 1, f"{where}: the document scrolls sideways by {probe['overflow']}px"
    assert probe["callVisible"], f"{where}: the bottleneck callout is not visible"
    print(f"[flow-board] {where}: callout={probe['callout'].strip()!r} top={probe['topAttention']!r} unknown={probe['attentionUnknown']}")
    # The callout agrees with whatever Needs Attention actually shows -- the top item, or the unknown state.
    if probe["attentionUnknown"]:
        assert probe["callout"].strip() == "Bottleneck unknown: Inspect unavailable status", f"{where}: {probe['callout']!r}"
        expected_outline: list[dict[str, str]] = []
    else:
        assert probe["topAttention"], f"{where}: the seeded failures produced no Needs Attention item"
        assert probe["callout"].strip().endswith(probe["topAttention"]), (
            f"{where}: callout {probe['callout']!r} disagrees with {probe['topAttention']!r}"
        )
        expected_outline = [{"node": "Metadata", "width": "3px"}]

    # The text equivalent: every node and edge, at every width, visually hidden.
    assert len(probe["textItems"]) == 6 + 7, f"{where}: text equivalent has {probe['textItems']}"
    assert probe["textClipped"], f"{where}: the text equivalent is not visually hidden"

    # The cards remain the links and carry complete values at every width.
    assert probe["cards"] and all(card["visible"] for card in probe["cards"]), f"{where}: stage cards hidden: {probe['cards']}"
    assert all(card["right"] <= probe["viewport"] + 1 for card in probe["cards"]), f"{where}: a stage card overflows: {probe['cards']}"
    assert any("/" in card["text"] for card in probe["cards"]), f"{where}: cards show no done/total values: {probe['cards']}"

    if viewport == "phone":
        assert not probe["boardVisible"], f"{where}: the picture must give way to the card grid below md"
    else:
        assert probe["boardVisible"], f"{where}: the flow board is not visible at md+"
        assert probe["boardRight"] <= probe["viewport"], f"{where}: the board overruns the viewport"
        assert (probe["nodeCount"], probe["edgeCount"]) == (6, 7)
        assert probe["outlined"] == expected_outline, f"{where}: outlined {probe['outlined']}"
        assert not probe["spills"], f"{where}: board text overruns its node box: {probe['spills']}"
        low = [c for c in probe["contrasts"] if c["ratio"] < 4.5]
        assert not low, f"{where}: board text below WCAG AA 4.5:1 -- {low}"
        print(f"[flow-board] {where}: min contrast {min(c['ratio'] for c in probe['contrasts']):.2f}")


@pytest.mark.parametrize("theme", THEMES)
async def test_archive_scale_counts_fit_inside_their_nodes(theme: str, page_at: Any, browser_dsn: str) -> None:
    """The seeded database is small; the real archive is not. Render the board server-side with
    archive-scale counts (six-digit totals, five-digit failure and missing counts, an unmeasured
    denominator) through the real template and the real stylesheet, and check every label still fits.
    """
    from bs4 import BeautifulSoup

    from phaze.routers.shell.stage_maps import templates
    from tests.shared.core.test_summary_flow import _bucket, _derive_summary_overview, _inputs, _progress

    big = _derive_summary_overview(
        _progress(
            total=1_234_567,
            metadata=_bucket(145_057, not_started=0, done=104_156, failed=12_345, missing=28_556),
            analyze=_bucket(145_057, not_started=0, in_flight=0, orphaned=0, done=131_478, failed=1_234, missing=12_345),
            proposals={"done": 99_999, "total": None},
            execute={"done": 88_888, "total": 77_777},
        ),
        _inputs(proposal_pending=123_456, proposal_approved=654_321),
    )
    board = str(
        BeautifulSoup(templates.env.get_template("shell/partials/summary_overview.html").render(summary=big), "html.parser").select_one(
            "figure[data-flow-board]"
        )
    )

    await _seed.reset_dsn(browser_dsn)
    async with page_at(viewport="desktop", theme=theme) as page:
        await open_shell(page, "/s/summary")
        await page.evaluate("html => { document.querySelector('figure[data-flow-board]').outerHTML = html; }", board)
        probe = dict(await page.evaluate(_PROBE_JS))

    assert probe["boardVisible"]
    assert not probe["spills"], f"{theme}: archive-scale text overruns its node box: {probe['spills']}"
    assert probe["overflow"] <= 1
    assert all(c["ratio"] >= 4.5 for c in probe["contrasts"]), probe["contrasts"]
