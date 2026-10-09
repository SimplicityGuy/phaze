"""phaze-gjhbk: every status pill renders through ``ui.status_pill`` -- one geometry, a leading indicator.

Before this bead the app had a dozen hand-rolled ``rounded-full`` pills whose geometry, indicator, text case and
failed glyph had drifted apart (an agent's ``ALIVE`` carried no dot while a lane's ``ACTIVE`` in the same table did;
stage pills were lowercase, lane pills uppercase; failure was ``U+2715`` on one page and ``U+2717`` on another).
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

from fastapi.templating import Jinja2Templates
import pytest

from phaze.web.template_globals import register_format_filters


TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_format_filters(_templates.env)
_MACRO_FILE = TEMPLATES_DIR / "ui" / "primitives.html"

# A pill-shaped span (buttons and links are controls, not status pills): an opening tag (any whitespace, newlines included) that is `rounded-full` AND has horizontal
# or vertical padding. A bare `w-2 h-2 rounded-full` dot or a progress bar has neither, so it is not matched.
_PILL_TAG = re.compile(r"<span\b[^>]*>", re.S)
_PILL_SET = re.compile(r"\{%\s*set\s+\w+\s*=\s*'[^']*rounded-full[^']*'", re.S)

# Files that carry a pill-shaped span the shared macro cannot yet render. Each entry is a follow-up, not a license.
_PENDING_FOLLOW_UP = {
    "record/partials/_record_facts.html": "mood chips (label + percentage with a hue swatch), not a status",
}


def _pill_shaped(markup: str) -> bool:
    return "rounded-full" in markup and bool(re.search(r"\bp[xy]-\d", markup))


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        rel = path.relative_to(TEMPLATES_DIR).as_posix()
        if path == _MACRO_FILE or rel in _PENDING_FOLLOW_UP:
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        for tag in _PILL_TAG.findall(text):
            if _pill_shaped(tag) and "data-not-status-pill=" not in tag:
                found.append(f"{rel}: {' '.join(tag.split())[:110]}")
        found.extend(f"{rel}: {m.group(0)[:110]}" for m in _PILL_SET.finditer(text) if _pill_shaped(m.group(0)))
    return found


def test_no_status_pill_is_hand_rolled_outside_the_shared_macro() -> None:
    """A new ``rounded-full`` pill outside ``ui.status_pill`` fails here. Non-status chips (counts, tags) opt out
    explicitly with ``data-not-status-pill="<reason>"`` on the element, which keeps the exemption greppable."""
    assert _violations() == [], "render this through ui.status_pill, or mark a non-status chip data-not-status-pill"


def test_pending_follow_up_entries_still_need_the_exemption() -> None:
    """An entry here that no longer holds a pill-shaped element is dead weight and hides the next regression."""
    for rel in _PENDING_FOLLOW_UP:
        text = (TEMPLATES_DIR / rel).read_text(encoding="utf-8")
        assert _pill_shaped(text), f"{rel} no longer carries a pill-shaped element; drop it from _PENDING_FOLLOW_UP"


def test_no_template_uses_the_alternate_failed_glyph() -> None:
    offenders = [
        p.relative_to(TEMPLATES_DIR).as_posix()
        for p in TEMPLATES_DIR.rglob("*.html")
        if "✗" in p.read_text(encoding="utf-8") or "&#10007;" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], "failed is U+2715 everywhere"


def _render(source: str, **context: object) -> str:
    return _templates.env.from_string(source).render(**context)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("alive", "Status: alive"),
        ("stale", "Status: stale"),
        ("dead", "Status: dead"),
        ("revoked", "Status: revoked"),
        ("never", "Status: never seen"),
    ],
)
def test_agent_pills_carry_a_leading_dot_like_lane_pills(status: str, label: str) -> None:
    agent = SimpleNamespace(_status=status)
    agent_html = _render('{% include "admin/partials/_status_pill.html" %}', agent=agent)
    lane_html = _render(
        '{% include "admin/partials/_status_pill.html" %}',
        row=SimpleNamespace(status_kind="lane", status="ACTIVE"),
    )
    for html in (agent_html, lane_html):
        assert re.search(r'<span class="w-2 h-2 shrink-0 rounded-full [^"]+" aria-hidden="true"></span><span>', html)
    assert f'aria-label="{label}"' in agent_html
    # Identical geometry: strip the tone classes and compare the structural prefix.
    geometry = "inline-flex items-center gap-1.5 text-xs font-semibold uppercase px-2 py-0.5 rounded-full"
    assert geometry in agent_html and geometry in lane_html


def test_waiting_lane_pill_keeps_its_alert_role() -> None:
    html = _render('{% include "admin/partials/_status_pill.html" %}', row=SimpleNamespace(status_kind="lane", status="WAITING"))
    assert 'role="alert"' in html and 'aria-label="Status: waiting on quota"' in html


def test_status_badge_keeps_its_default_aria_label_and_glyph_indicator() -> None:
    html = _render("""{% import "ui/primitives.html" as ui %}{{ ui.status_badge('Meta', 'in flight', 'accent', '●', true) }}""")
    assert 'aria-label="Meta: in flight"' in html
    assert '<span aria-hidden="true">●</span>' in html and "uppercase" in html and "motion-safe:animate-pulse" in html
