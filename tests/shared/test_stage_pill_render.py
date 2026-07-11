"""Jinja-render tests for the Phase 87 stage-status pill + matrix (87-04, UI-01 / D-01 / D-08).

Renders ``pipeline/partials/_stage_pill.html`` and ``_stage_matrix.html`` through FastAPI's
``Jinja2Templates`` (the SAME safe wrapper Phaze uses in production, so autoescape matches prod
exactly) and asserts the UI-SPEC five-bucket token contract:

* every bucket carries a distinct GLYPH shape + a human WORD + an aria-label + a ``dark:`` class
  (colour is never the sole channel -- WCAG 1.4.1);
* the matrix renders EXACTLY six pills in order Meta · FP · Analyze · Prop · Appr · Exec with the
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
# _stage_pill.html -- the five buckets (glyph + word + aria-label + dark: pair)
# ---------------------------------------------------------------------------

# (bucket, glyph, visible word fragment, aria-label suffix, a required light hue, a required dark class)
_BUCKET_CASES = [
    ("done", "✓", "done", "Meta: done", "bg-green-100", "dark:text-green-400"),
    ("in_flight", "●", "in flight", "Meta: in flight", "bg-blue-100", "dark:bg-blue-950"),
    ("not_started", "—", "not started", "Meta: not started", "bg-gray-100", "dark:text-gray-400"),
    ("failed", "✗", "failed", "Meta: failed", "bg-red-100", "dark:bg-red-950"),
    ("skipped", "⊘", "skipped", "Meta: skipped (force-completed)", "bg-violet-100", "dark:text-violet-300"),
]


def test_every_bucket_has_glyph_word_arialabel_and_dark_pair() -> None:
    """All 5 buckets render a glyph + word + aria-label + a ``dark:`` class (colour never the sole channel)."""
    for bucket, glyph, word, aria, light_hue, dark_class in _BUCKET_CASES:
        html = _render_pill(stage_label="Meta", bucket=bucket)
        assert glyph in html, f"{bucket}: missing glyph {glyph!r}"
        assert word in html, f"{bucket}: missing word {word!r}"
        assert f'aria-label="{aria}"' in html, f"{bucket}: missing aria-label {aria!r}"
        assert light_hue in html, f"{bucket}: missing light hue {light_hue!r}"
        assert "dark:" in html, f"{bucket}: no dark: class at all"
        assert dark_class in html, f"{bucket}: missing dark class {dark_class!r}"
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
    html = _render_pill(stage_label="FP", bucket="")
    assert 'aria-label="FP: not started"' in html
    assert "—" in html


# ---------------------------------------------------------------------------
# _stage_matrix.html -- 6-pill row + the 7->6 remap landmine
# ---------------------------------------------------------------------------


def test_matrix_renders_exactly_six_pills() -> None:
    """The matrix renders exactly 6 pills -- one aria-label per pill, no more, no fewer."""
    buckets = {
        "metadata": "done",
        "fingerprint": "done",
        "analyze": "done",
        "propose": "done",
        "review": "done",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    assert html.count("aria-label=") == 6


def test_matrix_pill_order_is_meta_fp_analyze_prop_appr_exec() -> None:
    """The 6 pills render in the fixed stage order Meta · FP · Analyze · Prop · Appr · Exec."""
    buckets = {
        "metadata": "done",
        "fingerprint": "done",
        "analyze": "done",
        "propose": "done",
        "review": "done",
        "apply": "done",
    }
    html = _render_matrix(buckets=buckets)
    positions = [html.find(f'aria-label="{label}:') for label in ("Meta", "FP", "Analyze", "Prop", "Appr", "Exec")]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), f"pills out of order: {positions}"


def test_matrix_remap_appr_reads_review_exec_reads_apply() -> None:
    """LANDMINE: Appr must read the ``review`` bucket and Exec the ``apply`` bucket (never swapped)."""
    # Distinct buckets so a swap would flip the observed labels.
    buckets = {
        "metadata": "done",
        "fingerprint": "in_flight",
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
        "fingerprint": "done",
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
    buckets = dict.fromkeys(("metadata", "fingerprint", "analyze", "propose", "review", "apply"), "done")
    html = _render_matrix(buckets=buckets, legend=True)
    for fragment in ("✓ done", "● in-flight", "— not-started", "✗ failed", "⊘ skipped"):
        assert fragment in html, f"legend missing {fragment!r}"


def test_matrix_legend_only_renders_legend_without_pills() -> None:
    """legend_only=True renders the shared legend and NO pills (one legend per surface -- D-01)."""
    html = _render_matrix(buckets=None, legend_only=True)
    assert "✓ done" in html
    # No pills -> no aria-labels.
    assert "aria-label=" not in html
