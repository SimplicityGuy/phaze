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

import hashlib
import json
from pathlib import Path
import uuid

from bs4 import BeautifulSoup
from fastapi.templating import Jinja2Templates
import pytest
from starlette.requests import Request

from phaze.web.template_globals import register_format_filters


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_format_filters(_templates.env)


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


def _render_pill(*, stage_label: str, bucket: str, variant: str | None = None) -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/_stage_pill.html",
        context={"stage_label": stage_label, "bucket": bucket, **({"pill_variant": variant} if variant is not None else {})},
    )
    return response.body.decode()


def _render_matrix(*, buckets: dict[str, str] | None = None, legend: bool = False, legend_only: bool = False) -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/_stage_matrix.html",
        context={"buckets": buckets, "legend": legend, "legend_only": legend_only},
    )
    return response.body.decode()


# _stage_pill.html -- the five buckets (glyph + word + aria-label + semantic tone)

# (bucket, glyph, visible word fragment, aria-label suffix, a required tint, a required text tone)
_BUCKET_CASES = [
    ("done", "✓", "done", "Meta: done", "bg-green-100", "text-ok"),
    ("in_flight", "●", "in flight", "Meta: in flight", "bg-blue-100", "text-info"),
    ("queued", "◌", "queued", "Meta: queued", "bg-slate-100", "text-slate-700"),
    ("missing", "✕", "missing", "Meta: file missing on disk", "bg-red-100", "text-danger"),
    ("not_started", "—", "not started", "Meta: not started", "bg-gray-100", "text-muted"),
    ("failed", "✕", "failed", "Meta: failed", "bg-red-100", "text-danger"),
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
        assert "inline-flex items-center gap-1.5 text-xs font-semibold uppercase px-2 py-0.5 rounded-full" in html


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


# _stage_matrix.html -- 6-pill row + the 7->6 remap landmine


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


def test_matrix_legend_renders_all_seven_buckets() -> None:
    """With legend=True the one-line legend names all five buckets."""
    buckets = dict.fromkeys(("metadata", "analyze", "propose", "review", "apply"), "done")
    html = _render_matrix(buckets=buckets, legend=True)
    for fragment in ("✓ done", "● in-flight", "◌ queued", "— not-started", "✕ failed", "! / ✕ missing on disk", "⊘ skipped"):
        assert fragment in html, f"legend missing {fragment!r}"


def test_matrix_legend_only_renders_legend_without_pills() -> None:
    """legend_only=True renders the shared legend and NO pills (one legend per surface -- D-01)."""
    html = _render_matrix(buckets=None, legend_only=True)
    assert "✓ done" in html
    # No pills -> no aria-labels.
    assert "aria-label=" not in html


@pytest.mark.parametrize(("bucket", "glyph", "word", "aria", "tint", "tone"), _BUCKET_CASES)
@pytest.mark.parametrize("variant", ["word", "glyph"])
def test_all_seven_buckets_preserve_accessible_meaning_in_both_variants(
    bucket: str, glyph: str, word: str, aria: str, tint: str, tone: str, variant: str
) -> None:
    pill = BeautifulSoup(_render_pill(stage_label="Meta", bucket=bucket, variant=variant), "html.parser").select_one("[aria-label]")
    assert pill is not None
    assert pill["aria-label"] == aria
    assert tint in pill["class"] and tone in pill["class"]
    expected_glyph = "!" if bucket == "missing" and variant == "glyph" else glyph
    assert pill.select_one('[aria-hidden="true"]').get_text() == expected_glyph
    if variant == "glyph":
        assert pill.get_text(strip=True) == expected_glyph
        assert pill["title"] == aria
        assert pill["data-status-variant"] == "glyph"
    else:
        assert word in pill.get_text()
        assert not pill.has_attr("data-status-variant")


@pytest.mark.parametrize("bucket", [case[0] for case in _BUCKET_CASES] + [""])
def test_word_variant_is_byte_identical_to_pre_c1_render(bucket: str) -> None:
    baseline = json.loads((TEMPLATES_DIR.parents[2] / "tests/fixtures/stage_pill_default_sha256.json").read_text())
    for variant in (None, "word"):
        assert hashlib.sha256(_render_pill(stage_label="Meta", bucket=bucket, variant=variant).encode()).hexdigest() == baseline["renders"][bucket]


def test_legend_is_accessible_and_names_each_bucket_once() -> None:
    legend = BeautifulSoup(_render_matrix(legend_only=True), "html.parser").select_one("[data-stage-legend]")
    assert legend is not None
    assert not legend.has_attr("aria-hidden") and legend.find_parent(attrs={"aria-hidden": "true"}) is None
    assert {item["data-stage-bucket"] for item in legend.select("[data-stage-bucket]")} == {case[0] for case in _BUCKET_CASES}
    assert len(legend.select("[data-stage-bucket]")) == 7
    missing = legend.select_one('[data-stage-bucket="missing"]')
    assert "!" in missing.get_text() and "✕" in missing.get_text()


@pytest.mark.parametrize("bucket", [case[0] for case in _BUCKET_CASES])
def test_namespaced_stage_oob_matches_files_and_record_variants(bucket: str) -> None:
    from phaze.routers.pipeline._common import _stage_pill_oob

    file_id = uuid.uuid4()
    for prefix, surface, variant in (("files-stage-pill", "table", "glyph"), ("files-mobile-stage-pill", "mobile", "word")):
        first = _templates.env.get_template("pipeline/partials/_files_stage_control.html").render(
            file_id=file_id, key="analyze", stage_label="Analyze", bucket=bucket, surface=surface, oob=False
        )
        pushed = _stage_pill_oob(file_id, "analyze", bucket, id_prefix=prefix)
        a = BeautifulSoup(first, "html.parser").select_one("[aria-label]")
        b = BeautifulSoup(pushed, "html.parser").select_one("[aria-label]")
        assert str(a) == str(b)
        assert (b.get("data-status-variant") == "glyph") == (variant == "glyph")
    record = BeautifulSoup(_stage_pill_oob(file_id, "analyze", bucket), "html.parser").select_one("[aria-label]")
    assert not record.has_attr("data-status-variant")
