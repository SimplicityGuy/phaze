"""phaze-x6ql9: one macro (``ui.kind_label``) renders every lane / agent kind label.

The same lane used to read ``🖥nox`` / ``❄ vox`` / ``🖥LOCAL · nox`` / ``▪ cloud`` / ``🗄FILE SERVER`` / a green
``🖥 local`` depending on the page, with an icon-text gap on some and not others. Each test below renders
ONE real site through its real template and asserts the form that came out of the shared macro: leading
glyph, the pill's single gap, the kind word where the site shows one, the name verbatim, a neutral tone.
"""

from datetime import UTC, datetime
from pathlib import Path
import re
from types import SimpleNamespace

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest

from phaze.services.record_facts import RecordFact
from phaze.utils.humanize import relative_time


TEMPLATES = Path(__file__).resolve().parents[3] / "src/phaze/templates"

# The tone classes status_pill gives its non-neutral tones. A kind label must carry NONE of them.
_STATUS_HUES = ("bg-blue-100", "bg-amber-100", "bg-green-100", "bg-red-100", "bg-indigo-100", "bg-slate-100", "bg-violet-100")
_GAP = "gap-1.5"


def _env() -> Environment:
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    environment.globals["humanize_relative_time"] = relative_time
    return environment


def _labels(html: str) -> list[Tag]:
    """Every rendered kind-label pill (they all carry an aria-label starting ``Kind: ``)."""
    return BeautifulSoup(html, "html.parser").select('span[aria-label^="Kind: "]')


def _form(pill: Tag) -> tuple[str, str]:
    """(leading glyph, visible text) of a pill, whitespace-normalised."""
    glyph, text = pill.find_all("span", recursive=False)
    return glyph.get_text(strip=True), re.sub(r"\s+", " ", text.get_text()).strip()


def _assert_neutral_and_gapped(pill: Tag) -> None:
    classes = " ".join(pill["class"])
    assert _GAP in classes, "one icon-to-text gap, owned by status_pill"
    assert "bg-gray-100" in classes, "kind is an identity: the neutral tone"
    assert not any(hue in classes for hue in _STATUS_HUES), "never a status colour"
    assert pill.find_all("span", recursive=False)[0]["aria-hidden"] == "true"


def _macro(call: str) -> str:
    return _env().from_string('{% import "ui/primitives.html" as ui %}' + call).render()


@pytest.mark.parametrize(
    ("call", "glyph", "text", "aria"),
    [
        ("{{ ui.kind_label('local', 'nox') }}", "\U0001f5a5️", "LOCAL · nox", "Kind: local lane, nox"),
        ("{{ ui.kind_label('compute', 'a1') }}", "☁️", "COMPUTE · a1", "Kind: compute lane, a1"),
        ("{{ ui.kind_label('kueue', 'vox') }}", "⎈", "KUEUE · vox", "Kind: kueue lane, vox"),
        ("{{ ui.kind_label('kueue', 'vox', show_kind=false) }}", "⎈", "vox", "Kind: kueue lane, vox"),
        ("{{ ui.kind_label('kueue') }}", "⎈", "KUEUE", "Kind: kueue lane"),
        ("{{ ui.kind_label('mystery', 'x') }}", "▪", "MYSTERY · x", "Kind: mystery lane, x"),
        ("{{ ui.kind_label(none, 'cloud', show_kind=false) }}", "▪", "cloud", "Kind: cloud lane, cloud"),
        ("{{ ui.kind_label('fileserver', family='agent') }}", "\U0001f5c4", "FILE SERVER", "Kind: file server"),
        ("{{ ui.kind_label('compute', family='agent') }}", "⚙", "COMPUTE", "Kind: compute"),
        ("{{ ui.kind_label('weird', family='agent') }}", "\U0001f5c4", "FILE SERVER", "Kind: file server"),
    ],
)
def test_macro_renders_every_kind_in_one_form(call: str, glyph: str, text: str, aria: str) -> None:
    (pill,) = _labels(_macro(call))

    assert _form(pill) == (glyph, text)
    assert pill["aria-label"] == aria
    _assert_neutral_and_gapped(pill)


def test_a_lane_name_is_rendered_verbatim_and_escaped() -> None:
    (pill,) = _labels(_macro("{{ ui.kind_label('local', '<b>nox</b>') }}"))

    assert pill.find("b") is None
    assert "&lt;b&gt;nox&lt;/b&gt;" in str(pill)
    assert pill.select_one(".normal-case") is not None, "names are not shouted by the pill's uppercase"


def test_analyze_running_now_row_uses_the_macro() -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    runs = [
        SimpleNamespace(
            label=f"<set-0{i}>",
            lane_kind=kind,
            lane=lane,
            started_at=None,
            heartbeat_at=None,
            heartbeat_lost=False,
            fine_done=1,
            fine_total=2,
            coarse_done=1,
            coarse_total=2,
            coarse_work_percent=None,
        )
        for i, (kind, lane) in enumerate([("local", "nox"), ("kueue", "vox")], start=1)
    ]
    html = (
        _env()
        .get_template("pipeline/partials/_analyze_queue.html")
        .render(running_analyses=runs, analyze_running_total=2, total_queued_analyze=0, queue_now=now)
    )

    forms = [_form(p) for p in _labels(html)]

    assert forms == [("\U0001f5a5️", "nox"), ("⎈", "vox")]
    for pill in _labels(html):
        _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(
    ("kind", "glyph", "word"),
    [("local", "\U0001f5a5️", "LOCAL"), ("compute", "☁️", "COMPUTE"), ("kueue", "⎈", "KUEUE")],
)
def test_lane_card_header_uses_the_macro(kind: str, glyph: str, word: str) -> None:
    lane = {"id": "nox", "kind": kind, "rank": 1, "cap": 4, "in_flight": 1, "available": True, "queued": 0, "working": 1, "active": 1}
    html = _env().get_template("pipeline/partials/_lane_card.html").render(lane=lane, selected_lane=None)

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, f"{word} · nox")
    _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(("kind", "glyph", "word"), [("local", "\U0001f5a5️", "LOCAL"), ("kueue", "⎈", "KUEUE")])
def test_lane_detail_header_uses_the_macro(kind: str, glyph: str, word: str) -> None:
    lane = {
        "id": "vox", "kind": kind, "rank": 2, "cap": 4, "in_flight": 1, "available": True, "active": 1, "queued": 0, "working": 1,
        "quota_wait": 0, "inadmissible": 0,
    }  # fmt: skip
    html = _env().get_template("pipeline/partials/_lane_detail.html").render(lane=lane)

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, f"{word} · vox")
    _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(("kind", "glyph"), [("compute", "☁️"), ("kueue", "⎈")])
def test_compute_lane_detail_header_uses_the_macro(kind: str, glyph: str) -> None:
    lane = {"kind": kind, "backend_id": "vox", "state": "IDLE", "waiting": 0, "available": True}
    html = _env().get_template("admin/partials/_compute_lane_detail.html").render(lane=lane, running_jobs=[])

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, "vox")
    _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(
    ("lane_kind", "lane", "glyph"),
    [("local", "nox", "\U0001f5a5️"), ("kueue", "vox", "⎈"), (None, "cloud", "▪")],
)
def test_file_progress_lane_cell_uses_the_macro(lane_kind: str | None, lane: str, glyph: str) -> None:
    rows = [[{"text": "song.mp3", "mono": True}, {"lane_kind": lane_kind, "lane": lane}]]
    html = (
        _env()
        .get_template("pipeline/partials/_file_table.html")
        .render(columns=["File", "Lane"], rows=rows, row_file_ids=["<uuid-1>"], empty_heading="none", empty_body="none")
    )

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, lane)
    _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(
    ("row_kind", "status_kind", "glyph", "text", "aria"),
    [
        ("fileserver", "agent", "\U0001f5c4", "FILE SERVER", "Kind: file server"),
        ("compute", "agent", "⚙", "COMPUTE", "Kind: compute"),
        ("kueue", "lane", "⎈", "KUEUE", "Kind: kueue lane"),
        ("compute", "lane", "☁️", "COMPUTE", "Kind: compute lane"),
    ],
)
def test_agents_kind_badge_uses_the_macro(row_kind: str, status_kind: str, glyph: str, text: str, aria: str) -> None:
    row = SimpleNamespace(kind=row_kind, status_kind=status_kind)
    html = _env().get_template("admin/partials/_kind_badge.html").render(row=row)

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, text)
    assert pill["aria-label"] == aria
    _assert_neutral_and_gapped(pill)


@pytest.mark.parametrize(
    ("lane_kind", "glyph"),
    [("local", "\U0001f5a5️"), ("compute", "☁️"), ("kueue", "⎈"), ("", "▪")],
)
def test_record_page_lane_fact_uses_the_macro_in_a_neutral_tone(lane_kind: str, glyph: str) -> None:
    facts = [RecordFact(label="Lane", value="nox", lane_kind=lane_kind), RecordFact(label="Format", value="mp3")]
    html = _env().get_template("record/partials/_record_facts.html").render(record_facts=facts)

    (pill,) = _labels(html)

    assert _form(pill) == (glyph, "nox")
    _assert_neutral_and_gapped(pill)
    assert "text-ok" not in html, "the old green 'local' is gone"


def test_lane_card_columns_share_labels_where_the_semantics_match() -> None:
    """Waiting/Running mean the same on every kind; only column 3 (Claimed vs Capacity) is kind-specific."""

    def headers(kind: str) -> list[str]:
        lane = {
            "id": "n",
            "kind": kind,
            "rank": 1,
            "cap": 4,
            "in_flight": 1,
            "available": True,
            "queued": 0,
            "working": 1,
            "active": 1,
            "claimed_unrun": 0,
        }
        html = _env().get_template("pipeline/partials/_lane_card.html").render(lane=lane, selected_lane=None)
        return [dt.get_text(strip=True) for dt in BeautifulSoup(html, "html.parser").select("dl dt")]

    local, kueue, compute = headers("local"), headers("kueue"), headers("compute")

    assert local[:2] == kueue[:2] == compute[:2] == ["Waiting", "Running"]
    assert local[2] == "Claimed"
    assert kueue[2] == compute[2] == "Capacity"
