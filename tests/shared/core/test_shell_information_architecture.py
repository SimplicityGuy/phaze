"""Application-shell information architecture contracts."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"


def _render(template: str, **context: object) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    return env.get_template(template).render(**context)


def test_navigation_has_four_domain_groups_and_native_enhanced_links() -> None:
    soup = BeautifulSoup(_render("shell/partials/rail.html", stage="summary"), "html.parser")
    headings = [heading.get_text(" ", strip=True) for heading in soup.find_all("h2")]
    assert headings == ["Overview", "Pipeline", "Review", "Operations"]
    links = [link for link in soup.select("a[data-rail-stage]") if isinstance(link, Tag)]
    assert links
    assert all(link.get("href") == link.get("hx-get") for link in links)
    assert all(link.get("hx-target") == "#stage-workspace" for link in links)
    assert all(link.get("hx-push-url") == "true" for link in links)
    assert not soup.select("button[data-rail-stage]")


def test_every_navigation_destination_has_one_current_page_semantic() -> None:
    stages = [
        "summary",
        "files",
        "discover",
        "metadata",
        "analyze",
        "tracklist",
        "propose",
        "rename",
        "dedupe",
        "cue",
        "apply",
        "operations",
        "audit",
        "agents",
    ]
    for stage in stages:
        soup = BeautifulSoup(_render("shell/partials/rail.html", stage=stage), "html.parser")
        current = soup.select('[aria-current="page"]')
        assert len(current) == 1
        assert current[0].get("data-rail-stage") == stage


def test_review_current_page_uses_cyan_location_inside_the_amber_domain() -> None:
    soup = BeautifulSoup(_render("shell/partials/rail.html", stage="rename"), "html.parser")
    review_heading = soup.select_one("#nav-review")
    current = soup.select_one('a[data-rail-stage="rename"][aria-current="page"]')
    unselected = soup.select_one('a[data-rail-stage="dedupe"]')
    assert isinstance(review_heading, Tag)
    assert isinstance(current, Tag)
    assert isinstance(unselected, Tag)
    assert "text-warn" in review_heading.get("class", [])
    assert "text-warn" in unselected.get("class", [])
    classes = set(current.get("class", []))
    assert "aria-[current=page]:bg-blue-500/10" in classes
    assert "aria-[current=page]:text-info" in classes
    assert "aria-[current=page]:shadow-[inset_3px_0_0_var(--color-blue-500)]" in classes
    assert not any("aria-[current=page]" in one and "amber" in one for one in classes)


def test_header_uses_canonical_workers_route_and_read_only_override_warning() -> None:
    normal = _render("shell/partials/header.html", force_local=False)
    forced = _render("shell/partials/header.html", force_local=True)
    assert 'href="/s/agents"' in normal
    assert 'href="/admin/agents"' not in normal
    assert 'hx-post="/pipeline/routing/force-local"' not in normal + forced
    assert normal.count('id="routing-override-warning"') == 1
    assert forced.count('id="routing-override-warning"') == 1
    assert "FORCED&nbsp;LOCAL" in forced
    assert "FORCED&nbsp;LOCAL" not in normal


def test_palette_has_close_control_platform_shortcuts_and_required_groups() -> None:
    modal = (_TEMPLATES / "shell" / "partials" / "cmdk_modal.html").read_text()
    header = (_TEMPLATES / "shell" / "partials" / "header.html").read_text()
    results = _render(
        "search/partials/palette_results.html",
        query="",
        file_results=[],
        tracklist_results=[],
        discogs_results=[],
        artists=[],
    )
    assert 'aria-label="Close command palette"' in modal
    assert "x-trap.inert.noscroll.noreturn" in modal
    assert "'⌘K' : 'Ctrl K'" in header
    assert ">Navigate<" in results
    assert ">Commands<" in results
    command_soup = BeautifulSoup(results, "html.parser")
    # phaze-jng72: the outcome live region belongs to the MODAL, not to this swapped fragment —
    # #cmdk-results is an hx-swap="innerHTML" target, so a live region rendered here is destroyed
    # by the next keystroke (and is an invalid child of role="listbox"). It must still exist, and
    # the command rows must still resolve it by id from inside the swapped subtree.
    assert command_soup.select_one("#cmdk-command-result") is None
    outcome = BeautifulSoup(modal, "html.parser").select_one('#cmdk-command-result[role="status"][aria-live="polite"]')
    assert isinstance(outcome, Tag)
    for command in command_soup.select('button[id^="cmdk-cmd-"]'):
        assert command.get("hx-target") == "#cmdk-command-result"
        assert command.get("hx-swap") == "innerHTML"
        assert command.get("hx-disabled-elt") == "this"
        assert command.has_attr("hx-confirm")
    populated = _render(
        "search/partials/palette_results.html",
        query="song",
        file_results=[{"id": "11111111-1111-4111-8111-111111111111", "title": "song.mp3", "artist": "Artist"}],
        tracklist_results=[],
        discogs_results=[],
        artists=[],
    )
    assert ">Files<" in populated


def test_agents_page_links_use_the_canonical_shell_route() -> None:
    agent_detail = (_TEMPLATES / "admin" / "partials" / "_agent_detail_row.html").read_text()
    lane_detail = (_TEMPLATES / "admin" / "partials" / "_compute_lane_detail_row.html").read_text()
    empty_state = (_TEMPLATES / "pipeline" / "partials" / "empty_state.html").read_text()
    assert "history.replaceState({}, '', '/s/agents?" in agent_detail
    assert "history.replaceState({}, '', '/s/agents?" in lane_detail
    assert 'href="/s/agents?agent={{ agent.id }}"' in empty_state
    assert 'href="/s/agents"' in empty_state
    assert 'href="/admin/agents' not in empty_state
