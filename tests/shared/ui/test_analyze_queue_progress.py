"""The Running now progress cell shows each analysis tier independently."""

from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path(__file__).resolve().parents[3] / "src/phaze/templates"


def _render(*, fine_done: int | None, fine_total: int | None, coarse_done: int | None, coarse_total: int | None, oob: bool = False) -> BeautifulSoup:
    run = SimpleNamespace(
        label="<set-01>",
        lane_kind="local",
        lane="Local",
        started_at=None,
        heartbeat_at=None,
        heartbeat_lost=False,
        fine_done=fine_done,
        fine_total=fine_total,
        coarse_done=coarse_done,
        coarse_total=coarse_total,
    )
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    html = environment.get_template("pipeline/partials/_analyze_queue.html").render(
        running_analyses=[run], analyze_running_total=1, total_queued_analyze=0, queue_now=None, oob=oob
    )
    return BeautifulSoup(html, "html.parser")


def test_fine_and_coarse_bars_advance_independently() -> None:
    soup = _render(fine_done=101, fine_total=141, coarse_done=3, coarse_total=20, oob=True)
    bars = soup.select('[role="progressbar"]')

    assert soup.select_one("#analyze-queue")["hx-swap-oob"] == "true"
    assert len(bars) == 2
    assert [bar["aria-label"] for bar in bars] == ["Fine windows analyzed for <set-01>", "Coarse windows analyzed for <set-01>"]
    assert [(bar["aria-valuemin"], bar["aria-valuenow"], bar["aria-valuemax"]) for bar in bars] == [
        ("0", "101", "141"),
        ("0", "3", "20"),
    ]
    assert [bar.select_one("div")["style"] for bar in bars] == ["width: 71%", "width: 15%"]
    assert "101/141 · 71%" in soup.get_text(" ", strip=True)
    assert "3/20 · 15%" in soup.get_text(" ", strip=True)


def test_unknown_and_zero_totals_do_not_claim_completion() -> None:
    soup = _render(fine_done=5, fine_total=None, coarse_done=0, coarse_total=0)
    fine, coarse = soup.select('[role="progressbar"]')

    assert "aria-valuenow" not in fine.attrs
    assert "aria-valuemax" not in fine.attrs
    assert fine["aria-valuetext"] == "Sizing windows"
    assert fine.select_one("div")["style"] == "width: 0%"
    assert coarse["aria-valuemin"] == "0"
    assert coarse["aria-valuenow"] == "0"
    assert coarse["aria-valuemax"] == "0"
    assert coarse.select_one("div")["style"] == "width: 0%"
    assert "5/— · sizing…" in soup.get_text(" ", strip=True)
    assert "0/0 · no windows" in soup.get_text(" ", strip=True)


def test_known_total_with_unknown_count_is_indeterminate() -> None:
    soup = _render(fine_done=None, fine_total=141, coarse_done=None, coarse_total=None)
    fine, coarse = soup.select('[role="progressbar"]')

    assert fine["aria-valuemax"] == "141"
    assert "aria-valuenow" not in fine.attrs
    assert fine["aria-valuetext"] == "Progress unavailable"
    assert "—/141 · unavailable" in soup.get_text(" ", strip=True)
    assert coarse["aria-valuetext"] == "Sizing windows"
