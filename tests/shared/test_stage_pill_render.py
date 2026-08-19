"""Jinja-render tests for the Phase 87 stage-status pill + matrix (87-04, UI-01 / D-01 / D-08).

Renders ``pipeline/partials/_stage_pill.html`` and ``_stage_matrix.html`` through FastAPI's
``Jinja2Templates`` (the SAME safe wrapper Phaze uses in production, so autoescape matches prod
exactly) and asserts the UI-SPEC five-bucket token contract:

* every bucket carries a distinct GLYPH shape + a human WORD + an aria-label + a ``dark:`` class
  (colour is never the sole channel -- WCAG 1.4.1);
* the matrix renders EXACTLY five pills in order Meta · Analyze · Prop · Appr · Exec with the
  7-stage -> 6-pill remap (Appr reads the ``review`` bucket, Exec reads ``apply``; ``tracklist`` is
  never shown) -- the RESEARCH landmine;
* the skipped pill is visually unlike done (violet + ``⊘`` + dashed ring, D-08 honesty).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub -- the partials don't use ``request`` but Jinja2Templates wraps it in."""
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


def _render_pill(*, stage_label: str, bucket: str) -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/_stage_pill.html",
        context={"stage_label": stage_label, "bucket": bucket},
    )
    return response.body.decode()


def _render_matrix(*, buckets: dict[str, str] | None = None, legend: bool = False, legend_only: bool = False) -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/_stage_matrix.html",
        context={"buckets": buckets, "legend": legend, "legend_only": legend_only},
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# _stage_pill.html -- the five buckets (glyph + word + aria-label + semantic tone)
# ---------------------------------------------------------------------------

# (bucket, glyph, visible word fragment, aria-label suffix, a required tint, a required text tone)
_BUCKET_CASES = [
    ("done", "✓", "done", "Meta: done", "bg-green-100", "text-ok"),
    ("in_flight", "●", "in flight", "Meta: in flight", "bg-blue-100", "text-info"),
    ("not_started", "—", "not started", "Meta: not started", "bg-gray-100", "text-muted"),
    ("failed", "✗", "failed", "Meta: failed", "bg-red-100", "text-danger"),
    ("skipped", "⊘", "skipped", "Meta: skipped (force-completed)", "bg-violet-100", "dark:text-violet-300"),
]


def test_every_bucket_has_glyph_word_arialabel_and_semantic_tone() -> None:
    """All five buckets combine redundant semantics with a theme-resolved text tone."""
    for bucket, glyph, word, aria, tint, text_tone in _BUCKET_CASES:
        html = _render_pill(stage_label="Meta", bucket=bucket)
        assert glyph in html, f"{bucket}: missing glyph {glyph!r}"
        assert word in html, f"{bucket}: missing word {word!r}"
        assert f'aria-label="{aria}"' in html, f"{bucket}: missing aria-label {aria!r}"
        assert tint in html, f"{bucket}: missing tint {tint!r}"
        assert "dark:" in html, f"{bucket}: no dark: class at all"
        assert text_tone in html, f"{bucket}: missing text tone {text_tone!r}"
        # The pill geometry token is the project-wide pill recipe.
        assert "text-xs font-semibold px-2 py-0.5 rounded-full" in html


def test_in_flight_pill_pulses() -> None:
    """in_flight carries animate-pulse (the ● dot pulses -- UI-SPEC non-colour affordance)."""
    assert "animate-pulse" in _render_pill(stage_label="Meta", bucket="in_flight")


def test_skipped_pill_is_visually_unlike_done() -> None:
    """skipped = violet + ⊘ + dashed ring, and carries NONE of done's solid-green check (D-08 honesty)."""
    html = _render_pill(stage_label="Meta", bucket="skipped")
    assert "ring-1 ring-dashed ring-violet-400/60" in html
    assert "⊘" in html
    # A forced skip must never read as genuine completion.
    assert "✓" not in html
    assert "bg-green-100" not in html


def test_unknown_bucket_falls_back_to_not_started() -> None:
    """An unknown/empty bucket degrades to the muted not_started token (never a blank cell)."""
    html = _render_pill(stage_label="Meta", bucket="")
    assert 'aria-label="Meta: not started"' in html
    assert "—" in html


# ---------------------------------------------------------------------------
# _stage_matrix.html -- 6-pill row + the 7->6 remap landmine
# ---------------------------------------------------------------------------


def test_matrix_renders_exactly_five_pills() -> None:
    """The matrix renders exactly 6 pills -- one aria-label per pill, no more, no fewer."""
    buckets = {
        "metadata": "done",
        "analyze": "done",
        "propose": "done",
        "review": "done",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    assert html.count("aria-label=") == 5


def test_matrix_pill_order_is_meta_analyze_prop_appr_exec() -> None:
    """The 5 pills render in the fixed stage order Meta · Analyze · Prop · Appr · Exec."""
    buckets = {
        "metadata": "done",
        "analyze": "done",
        "propose": "done",
        "review": "done",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    positions = [html.find(f'aria-label="{label}:') for label in ("Meta", "Analyze", "Prop", "Appr", "Exec")]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), f"pills out of order: {positions}"


def test_matrix_remap_appr_reads_review_exec_reads_apply() -> None:
    """LANDMINE: Appr must read the ``review`` bucket and Exec the ``apply`` bucket (never swapped)."""
    # Distinct buckets so a swap would flip the observed labels.
    buckets = {
        "metadata": "done",
        "analyze": "not_started",
        "propose": "failed",
        "review": "skipped",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    assert 'aria-label="Appr: skipped (force-completed)"' in html
    assert 'aria-label="Exec: done"' in html
    # A swap would have produced these instead:
    assert 'aria-label="Appr: done"' not in html
    assert 'aria-label="Exec: skipped (force-completed)"' not in html


def test_matrix_omits_tracklist() -> None:
    """``tracklist`` is one of the 7 Stage members but is NEVER shown as a pill."""
    buckets = {
        "metadata": "done",
        "analyze": "done",
        "propose": "done",
        "review": "done",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    assert "Tracklist" not in html
    assert "racklist" not in html


def test_matrix_legend_renders_all_five_buckets() -> None:
    """With legend=True the one-line legend names all five buckets."""
    buckets = dict.fromkeys(("metadata", "analyze", "propose", "review", "apply"), "done")
    html = _render_matrix(buckets=buckets, legend=True)
    for fragment in ("✓ done", "● in-flight", "— not-started", "✗ failed", "⊘ skipped"):
        assert fragment in html, f"legend missing {fragment!r}"


def test_matrix_legend_only_renders_legend_without_pills() -> None:
    """legend_only=True renders the shared legend and NO pills (one legend per surface -- D-01)."""
    html = _render_matrix(buckets=None, legend_only=True)
    assert "✓ done" in html
    # No pills -> no aria-labels.
    assert "aria-label=" not in html
