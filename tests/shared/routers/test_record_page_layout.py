"""phaze-x1qr3.8: the full page's two-column layout, its sidebar, and drawer parity.

The bead's acceptance is discharged here, criterion by criterion, against the REAL endpoints
rather than against the templates' source: the full page renders a sidebar carrying the eight
facts and the harmonic-journey wheel, the drawer renders the set panel and the tracklist index
and NO sidebar, and every control the record already had -- the five stage trace triggers, the
force-skip dialogs on the three ENRICH stages only, the Changes Review link carrying
``status=needs_review``, and the history list -- survives the re-layout in both presentations.

That last clause is the blast-radius statement's proof obligation. The re-layout moved every
section of a working production path (the record is the only per-file view in the product) and
folded three of them into ``<details>``; a fold is markup, not a removal, so the controls are
still in the response body and are still asserted here by the same routes and ids the pipeline
router swaps against.

Consumes the DB fixtures, so ``conftest.py`` auto-marks this module ``integration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.set_profile import SetProfile
from phaze.services.record_facts import ABSENT
from phaze.services.set_glyph_colors import camelot_hue
from phaze.services.set_projection import flicker_filtered_key_runs


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


# The synthetic journey every wheel assertion below reads, one fine window per entry.
#
# It is built to exercise all three behaviours at once: "3A" is a ONE-window blip between two
# runs of "9A" and must be filtered out entirely (it is neither a node nor two transitions);
# "8A" -> "9A" is wheel-adjacent (same letter, one semitone step); "9A" -> "5A" and "5A" ->
# "10B" are jumps. The final pair is deliberately the "10B"/"5A" shape the acceptance names,
# read backwards, so the caption contains a literal "10B → 5A".
_JOURNEY_KEYS = ("8A", "8A", "9A", "9A", "3A", "9A", "9A", "5A", "5A", "10B", "10B", "5A", "5A")
_WINDOW_SEC = 30.0

# The eight sidebar rows, in the order build_record_facts emits them.
_EXPECTED_FACT_LABELS = ["Format", "Duration", "sha256", "Lane", "Windows", "Median BPM", "Modal key", "Mood · style"]

# The three ENRICH stages carry a force-skip control; the three downstream stages must not
# (approval-bypass hazard, D-10).
_ENRICH_STAGES = ("metadata", "analyze")
_DOWNSTREAM_STAGES = ("propose", "review", "apply")
_ALL_STAGES = (*_ENRICH_STAGES, *_DOWNSTREAM_STAGES)


async def _seed_keyed_windows(session: AsyncSession, file: FileRecord) -> list[AnalysisWindow]:
    """Seed the synthetic key journey as fine windows carrying a stored ``camelot`` code."""
    windows = [
        AnalysisWindow(
            id=uuid.uuid4(),
            file_id=file.id,
            tier="fine",
            window_index=index,
            start_sec=index * _WINDOW_SEC,
            end_sec=(index + 1) * _WINDOW_SEC,
            bpm=128.0,
            camelot=code,
        )
        for index, code in enumerate(_JOURNEY_KEYS)
    ]
    session.add_all(windows)
    await session.commit()
    return windows


def _soup(body: str) -> BeautifulSoup:
    return BeautifulSoup(body, "html.parser")


@pytest.mark.asyncio
async def test_the_full_page_renders_the_sidebar_with_the_eight_facts_and_the_wheel(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
) -> None:
    """Acceptance 1a: the page's right sidebar carries the eight facts and the harmonic wheel."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    await _seed_keyed_windows(session, file)

    page = _soup((await client.get(f"/files/{file.id}")).text)

    sidebar = page.select_one("[data-record-sidebar]")
    assert sidebar is not None, "the full page must render the right sidebar"
    assert [row.get_text(" ", strip=True) for row in sidebar.select("[data-record-fact] dt")] == _EXPECTED_FACT_LABELS
    assert sidebar.select_one("[data-harmonic-journey]") is not None
    assert sidebar.select_one("[data-more-like-this]") is not None, "the 'more like this set' slot is part of the layout"

    # The facts are real readings, not placeholders: the seeded file is an mp3 analyzed locally
    # with three fine windows at 128/129/130 BPM, and the digest row abbreviates the real hash.
    values = {
        row.select_one("dt").get_text(strip=True): row.select_one("dd").get_text(" ", strip=True)  # type: ignore[union-attr]
        for row in sidebar.select("[data-record-fact]")
    }
    assert values["Format"] == "mp3"
    assert values["Lane"] == "🖥️ local"
    assert values["Duration"] == "6:30", "the analyzed extent of the seeded windows, as h:mm:ss"
    assert values["sha256"].startswith(file.sha256_hash[:12])
    assert values["Mood · style"] == "energetic · techno"
    assert values["Median BPM"] != ABSENT


@pytest.mark.asyncio
async def test_the_drawer_renders_the_set_panel_and_index_and_no_sidebar(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
) -> None:
    """Acceptance 1b: the drawer keeps the shared content and grows no sidebar of its own."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    await _seed_keyed_windows(session, file)

    drawer = _soup((await client.get(f"/record/{file.id}", headers={"HX-Request": "true"})).text)

    assert drawer.select_one("[data-set-panel]") is not None
    assert drawer.select_one("[data-tracklist-index]") is not None
    assert drawer.select_one("[data-record-sidebar]") is None, "the drawer has no room for a sidebar and renders none"
    assert drawer.select_one("[data-harmonic-journey]") is None, "the wheel is a sidebar component, page-only"
    assert drawer.select_one("[data-more-like-this]") is None
    # The facts themselves are NOT lost with the sidebar -- they move into the drawer's own
    # header block, which is what keeps the lane badge (phaze-lljfx) on this route.
    assert drawer.select_one("[data-record-facts]") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", ["page", "drawer"])
async def test_both_presentations_keep_every_control_the_relayout_moved(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
    presentation: str,
) -> None:
    """Acceptance 1c / the blast-radius statement: nothing the record already did was dropped.

    Stage trace triggers, enrich-only force-skip, the Changes Review link with
    ``status=needs_review``, and the history list -- asserted on BOTH routes, because the
    re-layout touched the shared partial that feeds them both.
    """
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename="Artist - Event - Title (2024).mp3",
            proposed_path="/organized/Artist/Event/Artist - Event - Title (2024).mp3",
            confidence=0.94,
            status=ProposalStatus.PENDING.value,
        )
    )
    await session.commit()

    url = f"/files/{file.id}" if presentation == "page" else f"/record/{file.id}"
    response = await client.get(url, headers={"HX-Request": "true"} if presentation == "drawer" else None)
    assert response.status_code == 200
    body = response.text
    soup = _soup(body)

    # Five stage trace triggers, each still a real <button> hx-getting its own trace endpoint
    # into its own reveal target.
    for stage in _ALL_STAGES:
        trigger = soup.select_one(f'button[hx-get="/pipeline/files/{file.id}/trace/{stage}"]')
        assert trigger is not None, f"the {stage} trace trigger is missing"
        assert trigger.get("hx-target") == f"#trace-{stage}-{file.id}"
        assert soup.select_one(f"#trace-{stage}-{file.id}") is not None

    # Force-skip on the ENRICH stages only. The dialog names its stage in its own POST url, so
    # the presence of that url is what distinguishes "has a skip control" from "does not".
    for stage in _ENRICH_STAGES:
        assert f"/skip/{stage}" in body, f"the {stage} force-skip control is missing"
    for stage in _DOWNSTREAM_STAGES:
        assert f"/skip/{stage}" not in body, f"{stage} must carry no skip affordance (D-10)"

    # The Changes Review link, still carrying the canonical queue's status filter.
    review_link = soup.select_one('a[href="/s/rename?status=needs_review"]')
    assert review_link is not None
    assert soup.select_one("[data-review-banner]") is not None

    # The history list. Nothing has executed for this file, so the empty branch is what renders
    # -- inside the fold, and still in the response body, which is the point of asserting it.
    history = soup.select_one("#history")
    assert history is not None
    assert history.name == "details", "history folds"
    assert "No recorded events yet." in history.get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_the_wheel_plots_the_flicker_filtered_runs_with_dashed_amber_jumps(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
) -> None:
    """Acceptance 2: node count = filtered key runs; jump edges dashed and amber; caption names them.

    The node count is asserted against ``flicker_filtered_key_runs`` rather than against a
    hand-counted literal on purpose: that function is also what
    ``set_projection.harmonic_discipline`` counts, so this pins the wheel to the SAME filtered
    sequence the percentage in its own caption is computed from.
    """
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    windows = await _seed_keyed_windows(session, file)
    expected_runs = flicker_filtered_key_runs(windows)

    page = _soup((await client.get(f"/files/{file.id}")).text)
    wheel = page.select_one("[data-harmonic-journey]")
    assert wheel is not None

    nodes = wheel.select("[data-journey-node]")
    assert len(nodes) == len(expected_runs)
    assert [node["data-camelot-code"] for node in nodes] == [run.code for run in expected_runs]
    assert "3A" not in [node["data-camelot-code"] for node in nodes], "a one-window blip is not a key run"

    # Dwell sizes the nodes: the run the set held longest draws the largest circle, and the
    # shortest run draws the smallest, so the radii are not a constant.
    radii = [float(node["r"]) for node in nodes]
    dwells = [run.dwell_sec for run in expected_runs]
    assert radii[dwells.index(max(dwells))] == max(radii)
    assert radii[dwells.index(min(dwells))] == min(radii)

    # Twelve tinted sectors, and every tint is the SAME Camelot hue the set glyph renders.
    sectors = wheel.select("[data-wheel-sector]")
    assert len(sectors) == 12
    for sector in sectors:
        number = int(str(sector["data-camelot-number"]))
        assert f"hsl({camelot_hue(number)}," in str(sector["fill"])

    # Adjacent moves in the accent, jumps dashed and in the review amber -- the same amber rung
    # the Changes Review banner is drawn in, so "a jump" and "needs your attention" read alike.
    edges = wheel.select("[data-journey-edge]")
    assert len(edges) == len(expected_runs) - 1
    jumps = [edge for edge in edges if edge["data-edge-kind"] == "jump"]
    adjacent = [edge for edge in edges if edge["data-edge-kind"] == "adjacent"]
    assert jumps and adjacent, "the synthetic journey has both kinds of move"
    for edge in jumps:
        assert edge.get("stroke-dasharray"), "a jump is dashed, so it is distinguishable without colour"
        assert "stroke-amber-500" in edge.get("class", [])
    for edge in adjacent:
        assert edge.get("stroke-dasharray") is None
        assert "stroke-cyan-700" in edge.get("class", [])
    banner = page.select_one("[data-review-banner]")
    assert banner is not None
    assert any("amber-500" in name for name in banner.get("class", [])), "the jump stroke names the banner's own amber"

    # The caption names the adjacent share and every jump by code pair and elapsed time.
    caption = wheel.select_one("[data-harmonic-caption]")
    assert caption is not None
    caption_text = caption.get_text(" ", strip=True)
    assert f"{len(expected_runs)} key runs" in caption_text
    assert "wheel-adjacent" in caption_text
    assert "10B → 5A at 5:30" in caption_text, "each jump is named by code pair and by when it lands"


@pytest.mark.asyncio
async def test_a_file_with_no_key_data_renders_an_empty_wheel_with_a_text_alternative(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    seed_file_with_windows,
) -> None:
    """Acceptance 2 (the empty half): no key data is a real answer, not a missing component."""
    # seed_file_with_windows stores no `camelot` on any window, which is exactly the shape of a
    # file analyzed before the projection existed.
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-02>.mp3")

    page = _soup((await client.get(f"/files/{file.id}")).text)
    wheel = page.select_one("[data-harmonic-journey]")
    assert wheel is not None

    assert wheel.select("[data-journey-node]") == []
    assert wheel.select("[data-journey-edge]") == []
    assert len(wheel.select("[data-wheel-sector]")) == 12, "the wheel is the frame of reference and is still drawn"

    alternative = wheel.select_one("[data-harmonic-journey-empty]")
    assert alternative is not None
    assert "no harmonic journey" in alternative.get_text(" ", strip=True).lower()
    svg = wheel.select_one("[data-harmonic-wheel]")
    assert svg is not None and "no analyzed key data" in str(svg["aria-label"]).lower()

    # The Modal key fact degrades the same way rather than inventing one.
    values = {
        row.select_one("dt").get_text(strip=True): row.select_one("dd").get_text(" ", strip=True)  # type: ignore[union-attr]
        for row in page.select("[data-record-sidebar] [data-record-fact]")
    }
    assert values["Modal key"] == ABSENT


@pytest.mark.asyncio
async def test_the_sidebar_stacks_under_the_main_column_below_the_large_breakpoint(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    seed_file_with_windows,
) -> None:
    """Acceptance 4: below 1024px the two columns become one, in source order.

    Asserted on the rendered grid rather than in a browser: the layout is a CSS grid whose
    column track is declared only inside the ``lg:`` variant (Tailwind's 1024px breakpoint), so
    every narrower viewport falls back to the base ``grid-cols-1`` and the sidebar -- which
    follows the main column in source order -- stacks beneath it. ``tests/shared/core/
    test_cross_workspace_responsive_a11y.py`` holds the rest of the responsive baseline
    (docs/design/0009-responsive-accessibility-baseline.md) over this template with everything
    else in the product.
    """
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")

    page = _soup((await client.get(f"/files/{file.id}")).text)
    sidebar = page.select_one("[data-record-sidebar]")
    assert sidebar is not None
    grid = sidebar.parent
    assert grid is not None

    classes = grid.get("class", [])
    assert "grid" in classes
    assert "grid-cols-1" in classes, "one column is the base, so every viewport under lg stacks"
    assert any(name.startswith("lg:grid-cols-") for name in classes), "two columns exist only at and above lg"
    assert not any(name.startswith("grid-cols-") and name != "grid-cols-1" for name in classes)

    # Source order is reading order: the main column precedes the sidebar, so the stacked layout
    # needs no visual re-ordering and the tab order is already correct.
    children = grid.find_all(recursive=False)
    assert children[0].name == "article"
    assert children[1] is sidebar


# ---------------------------------------------------------------------------
# phaze-x1qr3.11: the "more like this set" slot's real content (its `[data-more-like-this]`
# presence is already covered above; these two tests cover what fills it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_more_like_this_says_no_similar_sets_yet_with_nothing_to_compare(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    seed_file_with_windows,
) -> None:
    """`seed_file_with_windows` seeds no `SetProfile` row, so there is nothing to rank against --
    the honest empty state, never a silently empty list."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")

    page = _soup((await client.get(f"/files/{file.id}")).text)
    slot = page.select_one("[data-more-like-this]")
    assert slot is not None
    assert slot.select_one("[data-similar-sets]") is None
    assert slot.get_text(" ", strip=True).endswith("No similar sets yet.")


@pytest.mark.asyncio
async def test_more_like_this_renders_real_neighbours_with_glyphs_links_and_scoring_lines(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
) -> None:
    """A file with a usable profile and at least one comparable candidate gets a real list: each
    row carries the candidate's own glyph (the shared `ui.set_glyph` macro), a link to its own
    record, and the deterministic scoring line."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    neighbour, _neighbour_result, _neighbour_windows = await seed_file_with_windows(original_filename="<set-02>.mp3")

    mean_vector = [0.5] * 11
    arc = [0.4] * 64
    glyph_cells = [{"camelot_number": 8, "energy": 0.5}, {"camelot_number": 9, "energy": 0.6}]
    # `seed_file_with_windows` already gives both files an `AnalysisResult` with `bpm=128.0` --
    # equal BPM (and everything else below) is deliberate, the same duplicate-rip shape
    # `test_set_similarity.py` scores at the service level.
    session.add(SetProfile(file_id=file.id, mean_vector=mean_vector, arc=arc, camelot_modal="8A", glyph=glyph_cells))
    session.add(SetProfile(file_id=neighbour.id, mean_vector=list(mean_vector), arc=list(arc), camelot_modal="8A", glyph=glyph_cells))
    await session.commit()

    page = _soup((await client.get(f"/files/{file.id}")).text)
    slot = page.select_one("[data-more-like-this]")
    assert slot is not None
    rows = slot.select("[data-similar-set]")
    assert len(rows) == 1, "the only OTHER file with a usable profile"

    row = rows[0]
    link = row.select_one("a")
    assert link is not None
    assert link["href"] == f"/files/{neighbour.id}"
    assert row.select_one("[data-set-glyph]") is not None, "the candidate's own glyph, via the shared macro"
    assert row.select_one("[data-similar-set-title]").get_text(strip=True) == "<set-02>.mp3"  # type: ignore[union-attr]
    scoring_line = row.select_one("[data-similar-set-line]").get_text(strip=True)  # type: ignore[union-attr]
    assert scoring_line.startswith("arc 0.00"), "identical arcs and mean_vector: the duplicate-rip shape"
    assert scoring_line.endswith("8A")
