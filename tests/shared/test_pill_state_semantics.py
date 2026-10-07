"""phaze-6ak0q: status pills must report the state the DATA is in, not a plausible-looking one.

Six defects, one family: a pill whose wording/tone was decided by something other than the underlying
state (a failure count overriding activity, requirements styled as met, a queued row called in flight,
a tie called a quality win, a status column that was plain text, an amber card over an all-clear).
Each test renders the real template (or calls the real derivation) and asserts on the state shown.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

from bs4 import BeautifulSoup
from fastapi.templating import Jinja2Templates
import pytest
from starlette.requests import Request

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.shell.summary import _summary_stage_status
from phaze.services.dedup import score_group
from phaze.services.review_dedupe import build_dupe_group_card
from phaze.web.template_globals import register_page_name_globals
from tests.shared.core.test_summary_overview import _derive, _progress


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-yyfax: the shell templates call the `page_name` global, which the app registers at startup.
register_page_name_globals(_templates.env)


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    return Request(scope=scope)  # type: ignore[arg-type]


def _render(name: str, **context: Any) -> str:
    return _templates.TemplateResponse(request=_request(), name=name, context=context).body.decode()  # type: ignore[attr-defined]


def _pills(html: str) -> list[str]:
    """The visible text of every status pill (rounded-full uppercase span) in ``html``."""
    soup = BeautifulSoup(html, "html.parser")
    return _texts(soup, "span.rounded-full.uppercase")


def _texts(soup: BeautifulSoup, selector: str) -> list[str]:
    """Pill WORD only: the last inner span (the leading one is the aria-hidden glyph/dot)."""
    return [s.find_all("span", recursive=False)[-1].get_text(" ", strip=True).lower() for s in soup.select(selector)]


# --- (a) Summary stage state is not overridden by failures ------------------------------------------


def _bucket(**kw: int) -> dict[str, int | None]:
    base = {"not_started": 0, "in_flight": 0, "done": 0, "skipped": 0, "failed": 0, "total": 0}
    base.update(kw)
    return base


def test_running_with_some_failed_reads_in_flight_and_reports_failures_separately() -> None:
    status = _summary_stage_status(_bucket(total=100, in_flight=5, done=80, failed=15))
    assert status["label"] == "in flight"
    assert status["failed"] == 15


def test_mostly_done_with_failures_reads_complete_not_failed() -> None:
    status = _summary_stage_status(_bucket(total=104247, done=104156, failed=91))
    assert status["label"] == "complete"
    assert status["failed"] == 91


def test_idle_partly_done_with_some_failed_reads_partial_with_separate_failures() -> None:
    status = _summary_stage_status(_bucket(total=10, not_started=3, done=5, failed=2))
    assert status["label"] == "partial"
    assert status["failed"] == 2


def test_idle_partly_done_without_failures_reads_partial() -> None:
    status = _summary_stage_status(_bucket(total=145057, not_started=40901, done=104156))
    assert (status["label"], status["failed"]) == ("partial", 0)


def test_idle_with_nothing_done_reads_not_started() -> None:
    assert _summary_stage_status(_bucket(total=8, not_started=8))["label"] == "not started"
    assert _summary_stage_status(_bucket(total=8, not_started=6, failed=2))["label"] == "not started"


def test_running_partly_done_stage_still_reads_in_flight() -> None:
    assert _summary_stage_status(_bucket(total=8, not_started=2, in_flight=2, done=4))["label"] == "in flight"


def test_summary_template_renders_partial_pill_beside_failed_badge() -> None:
    partial = _bucket(total=8, not_started=3, done=4, failed=1)
    summary = _derive(_progress(metadata=partial))
    pills = _pills(_render("shell/partials/summary_overview.html", summary=summary))
    assert "partial" in pills
    assert "1 failed" in pills


def test_everything_failed_is_the_only_overall_failed() -> None:
    assert _summary_stage_status(_bucket(total=4, failed=4))["label"] == "failed"


def test_all_done_has_no_failure_count() -> None:
    status = _summary_stage_status(_bucket(total=4, done=4))
    assert (status["label"], status["failed"]) == ("complete", 0)


def test_summary_template_renders_a_separate_failed_badge() -> None:
    stuck = _bucket(total=2, in_flight=1, failed=1)
    summary = _derive(_progress(metadata=stuck, analyze=stuck))
    html = _render("shell/partials/summary_overview.html", summary=summary)
    pills = _pills(html)
    assert "in flight" in pills
    assert "1 failed" in pills


# --- (b) Cue prerequisites reflect whether they are met ---------------------------------------------


def _cue_html(cards: list[dict[str, Any]]) -> str:
    return _render("pipeline/partials/cue_workspace.html", cue_cards=cards)


def test_cue_prerequisites_unmet_when_no_card_exists() -> None:
    pills = _pills(_cue_html([]))
    assert "needs applied file" in pills
    assert "needs approved tracklist" in pills
    assert "file applied" not in pills
    assert "tracklist approved" not in pills


def test_cue_prerequisites_met_when_every_card_is_eligible() -> None:
    card = {"tracklist_id": "t", "set_name": "s", "eligible": True, "build_error": False, "cue_text": "x", "version_id": "v", "file_id": "f"}
    pills = _pills(_cue_html([card]))
    assert "file applied" in pills
    assert "tracklist approved" in pills
    assert "timestamps present" in pills


def test_cue_timestamps_flagged_when_a_card_is_blocked() -> None:
    blocked = {"tracklist_id": "t", "set_name": "s", "eligible": False, "build_error": False, "cue_text": None, "version_id": None, "file_id": "f"}
    pills = _pills(_cue_html([blocked]))
    assert "timestamps required (1 blocked)" in pills


# --- (c) Queued rows are not "in flight" -------------------------------------------------------------


def _analyze_row(**kw: Any) -> dict[str, Any]:
    base = {
        "file_id": "f1",
        "filename": "a.mp3",
        "path": "/x/a.mp3",
        "awaiting_cloud": False,
        "analysis_failed": False,
        "completed": False,
        "lane": "local",
        "lane_kind": "local",
        "duration": 60,
        "fine_done": 0,
        "fine_total": 0,
    }
    base.update(kw)
    return base


def _analyze_pills(row: dict[str, Any]) -> list[str]:
    page = SimpleNamespace(page=1, page_size=50, has_next=False, status="")
    html = _render("pipeline/partials/_analyze_files.html", analyze_rows=[row], analyze_page=page)
    soup = BeautifulSoup(html, "html.parser")
    return _texts(soup, "tbody span.rounded-full.uppercase")


def test_awaiting_cloud_row_renders_queued_not_in_flight() -> None:
    pills = _analyze_pills(_analyze_row(awaiting_cloud=True))
    assert "queued" in pills
    assert "in flight" not in pills


def test_executing_row_still_renders_in_flight() -> None:
    assert "in flight" in _analyze_pills(_analyze_row(fine_total=10, fine_done=3))


# --- (d) Dedupe only claims "highest quality" on a strict win ----------------------------------------


def _file(i: str, path: str, *, bitrate: int, tags: int) -> dict[str, Any]:
    return {"id": i, "original_path": path, "bitrate": bitrate, "tag_filled": tags, "tag_total": 5, "file_size": 1000, "tag_label": "Partial"}


def _scored_card(files: list[dict[str, Any]]) -> dict[str, Any]:
    group = {"sha256_hash": "h" * 64, "files": files}
    score_group(group)
    return build_dupe_group_card(group)


def test_tied_group_does_not_claim_highest_quality_and_states_the_tie_break() -> None:
    card = _scored_card([_file("a", "/m/aa/x.mp3", bitrate=192000, tags=2), _file("b", "/m/a/x.mp3", bitrate=192000, tags=2)])
    assert card["strictly_better"] is False
    pills = _pills(_render("pipeline/partials/_dupe_group.html", group=card))
    assert "highest quality" not in pills
    assert any("shortest path" in p for p in pills)


def test_strictly_better_keeper_claims_highest_quality() -> None:
    card = _scored_card([_file("a", "/m/aa/x.mp3", bitrate=320000, tags=2), _file("b", "/m/a/x.mp3", bitrate=192000, tags=2)])
    assert card["strictly_better"] is True
    assert "highest quality" in _pills(_render("pipeline/partials/_dupe_group.html", group=card))


def test_unscored_card_never_claims_a_quality_win() -> None:
    card = build_dupe_group_card({"sha256_hash": "h" * 64, "canonical_id": "a", "files": [_file("a", "/m/x.mp3", bitrate=1, tags=1)]})
    assert card["strictly_better"] is False


# --- (e) Tracklist status columns are shared pills ---------------------------------------------------


def test_tracklist_status_columns_render_as_pills() -> None:
    row = {
        "set_name": "set",
        "path": "/x/set.mp3",
        "file_id": "f1",
        "tracklist_state": "matched",
        "tracks_total": 10,
        "tracks_confident": 10,
        "discogs_matched": True,
    }
    page = SimpleNamespace(rows=[row], page=1, page_size=50, has_prev=False, has_next=False, show_pager=False, available=True)
    pills = _pills(_render("pipeline/partials/_tracklist_sets.html", sets_page=page, host_id="h", sort=None))
    assert "file linked" in pills
    assert "10/10" in pills
    assert "matched" in pills


# --- (f) Record review card is neutral when nothing needs review ------------------------------------


async def _seed_record_file(session: AsyncSession) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


def _banner(body: str) -> Any:
    banner = BeautifulSoup(body, "html.parser").select_one("[data-review-banner]")
    assert banner is not None
    return banner


@pytest.mark.asyncio
async def test_review_card_is_not_amber_when_nothing_needs_review(client: AsyncClient, session: AsyncSession) -> None:
    file_id = await _seed_record_file(session)
    banner = _banner((await client.get(f"/record/{file_id}")).text)
    assert banner["data-review-state"] == "clear"
    assert "amber" not in " ".join(banner["class"])
    assert "text-warn" not in str(banner)
    assert "up to date" in banner.get_text()


@pytest.mark.asyncio
async def test_review_card_stays_amber_when_decisions_are_pending(client: AsyncClient, session: AsyncSession) -> None:
    file_id = await _seed_record_file(session)
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="Artist - Title (2024).mp3",
            proposed_path="/organized/Artist/Artist - Title (2024).mp3",
            confidence=0.9,
            status=ProposalStatus.PENDING.value,
        )
    )
    await session.commit()
    banner = _banner((await client.get(f"/record/{file_id}")).text)
    assert banner["data-review-state"] == "pending"
    assert "amber" in " ".join(banner["class"])


@pytest.mark.parametrize("bucket", ["queued"])
def test_stage_pill_knows_the_queued_bucket(bucket: str) -> None:
    html = _render("pipeline/partials/_stage_pill.html", stage_label="Analyze", bucket=bucket)
    assert "queued" in _pills(html)
