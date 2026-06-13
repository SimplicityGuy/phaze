"""Render + topology tests for the Phase-35 pipeline DAG canvas (35-05).

Three layers:

- ``topology`` / ``render`` (pure Jinja render, no DB — runs everywhere): the SVG edge
  layer is anchor-derived and EDGE-HONEST (only Metadata+Analyze converge into Proposals;
  Fingerprint and the tracklist subgraph do NOT), all 9 node ids render, the Scan/Search
  node uses the literal em-dash denominator with no determinate bar, every node carries a
  ``dark:`` class, counts use ``tabular-nums``, and the full-page store seeds mirror the
  35-04 ``dag-seed-<key>`` OOB ids.
- ``gating`` (pure Jinja render): triggers POST only to the existing endpoints, gate on the
  LOCKED ``:disabled`` predicates (Fingerprint on ``discovered``), surface the LOCKED
  disabled-reason + state-pill copy, and the stacked ``<ol>`` fallback is the text equivalent.
- ``integration`` (DB-backed via the shared ``client`` fixture): GET /pipeline renders the
  canvas with no legacy ``stage_cards``/``processing_card`` markers, GET /pipeline/stats still
  emits the per-node OOB seeds, and the two legacy partial files are gone.
"""

from __future__ import annotations

import itertools
from pathlib import Path
import re
from typing import TYPE_CHECKING

from fastapi.templating import Jinja2Templates
import pytest
from starlette.requests import Request


if TYPE_CHECKING:
    from httpx import AsyncClient


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "phaze" / "templates"
PARTIALS_DIR = TEMPLATES_DIR / "pipeline" / "partials"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The per-node store sub-keys carried in the `dag` context (35-04 contract + the 6
# Phase-38 stage-control keys added in 38-03 — metadata/analyze/fingerprint Paused/Priority —
# which ride the same dag.items() seed/OOB loop).
_DAG_KEYS = (
    "metadataDone",
    "metadataTotal",
    "fingerprintDone",
    "fingerprintTotal",
    "analyzeDone",
    "analyzeTotal",
    "analyzeActive",
    "tracklistDone",
    "scrapeDone",
    "scrapeTotal",
    "matchDone",
    "matchTotal",
    "proposalsDone",
    "proposalsTotal",
    "approved",
    "executedDone",
    "executedTotal",
    # Phase-38 (38-03) per-stage control keys.
    "metadataPaused",
    "metadataPriority",
    "analyzePaused",
    "analyzePriority",
    "fingerprintPaused",
    "fingerprintPriority",
)

# The three agent stages that carry the Phase-38 pause/resume + priority controls.
_AGENT_STAGES = ("metadata", "analyze", "fingerprint")


def _stage_control_fragment(html: str, stage: str) -> str:
    """Slice the rendered ``stage_controls(stage)`` macro fragment for assertions.

    Runs from the control container id to its LOCKED inline error copy (one per stage),
    capturing the full macro output (including the error reveal) without bleeding into the
    next stage's controls or the enqueue button (which precedes the controls in the chip).
    """
    start = html.index(f'id="stage-controls-{stage}"')
    end = html.index("Couldn't update. Retry.", start)
    return html[start : end + len("Couldn't update. Retry.")]


# All 9 DAG node ids (topological order).
_NODE_IDS = (
    "node-discovery",
    "node-metadata",
    "node-analyze",
    "node-fingerprint",
    "node-scan_search",
    "node-proposals",
    "node-scrape",
    "node-execute",
    "node-match",
)


def _fake_request() -> Request:
    """Minimal Starlette Request stub for templates that reference ``request``."""
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


def _render_canvas(
    *,
    discovered: int = 11428,
    analyzed: int = 27,
    metadata_extracted: int = 11428,
    agent_busy: int = 0,
    controller_busy: int = 0,
    dag: dict[str, int] | None = None,
) -> str:
    """Render dag_canvas.html with a representative dashboard context."""
    if dag is None:
        dag = dict.fromkeys(_DAG_KEYS, 0)
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/dag_canvas.html",
        context={
            "stats": {
                "discovered": discovered,
                "analyzed": analyzed,
                "metadata_extracted": metadata_extracted,
            },
            "agent_busy": agent_busy,
            "controller_busy": controller_busy,
            "dag": dag,
        },
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# topology — anchor-derived, edge-honest SVG
# ---------------------------------------------------------------------------


def test_topology_edge_list_is_honest() -> None:
    """The edge-list source declares Metadata+Analyze→Proposals but NOT Fingerprint/tracklist."""
    src = (PARTIALS_DIR / "dag_canvas.html").read_text(encoding="utf-8")
    edge_block = src[src.index("set EDGES") : src.index("] %}", src.index("set EDGES"))]
    assert '["metadata", "proposals"]' in edge_block
    assert '["analyze", "proposals"]' in edge_block
    # Edge honesty: no Fingerprint→Proposals and no tracklist-node→Proposals edges.
    assert '["fingerprint", "proposals"]' not in edge_block
    assert '["scan_search", "proposals"]' not in edge_block
    assert '["scrape", "proposals"]' not in edge_block
    assert '["match", "proposals"]' not in edge_block


def test_topology_renders_anchor_derived_bezier_paths() -> None:
    """Edges render as cubic-bézier <path d="M..C..> strings derived from the layout map."""
    html = _render_canvas()
    # One path per edge; nine edges in the authoritative list.
    paths = re.findall(r'<path d="M [\d., ]+C [\d., ]+"', html)
    assert len(paths) == 9, f"expected 9 anchor-derived edges, found {len(paths)}"


def test_topology_column_one_chips_do_not_overlap() -> None:
    """Regression (UAT 35 + Phase 38): the 4 stacked column-1 chips must be spaced by at least a
    real chip height, so a content-bearing chip cannot paint over the chip below it.

    The original layout gave metadata/fingerprint a "compact" h:76 even though they render a
    trigger button (~154px tall), so each overlapped the next chip by ~55px. Phase 38 added the
    per-stage control row (pause/resume + priority stepper + hint) to the 3 agent chips, growing
    them to ~250px — so the guard's min_chip_height is bumped from 150 to 240 (the new measured
    agent-chip height) and the NODE_LAYOUT col-1 gutter widened to 276px. Node chips are
    content-height (the div sets only left/top/width), so this guards the y-spacing in the
    NODE_LAYOUT map against the smallest height a control-bearing chip actually renders at; the
    old 182px gutters would now FAIL this assertion.
    """
    html = _render_canvas()
    # Minimum rendered height of a column-1 agent chip carrying the enqueue button + the Phase-38
    # control row (measured ~250px); the old 182px gutters fail at this threshold.
    min_chip_height = 240
    tops = {}
    for node in ("metadata", "analyze", "fingerprint", "scan_search"):
        m = re.search(rf'id="node-{node}".*?top:\s*(\d+)px', html, re.DOTALL)
        assert m, f"could not find top position for node {node}"
        tops[node] = int(m.group(1))
    ordered = ["metadata", "analyze", "fingerprint", "scan_search"]
    for upper, lower in itertools.pairwise(ordered):
        gap = tops[lower] - tops[upper]
        assert gap >= min_chip_height, (
            f"column-1 chips overlap: {upper} (top {tops[upper]}) -> {lower} (top {tops[lower]}) "
            f"spaced only {gap}px, need >= {min_chip_height}px for a button chip"
        )


def test_topology_canvas_has_aria_group_and_decorative_svg() -> None:
    """Canvas is role=group/aria-label and the SVG edge layer is aria-hidden."""
    html = _render_canvas()
    assert html.count('aria-label="Pipeline stage graph"') >= 1
    assert 'role="group"' in html
    assert '<svg aria-hidden="true"' in html


# ---------------------------------------------------------------------------
# render — node chips, counts, bars, seeds
# ---------------------------------------------------------------------------


def test_render_all_nine_node_ids_present() -> None:
    """All 9 DAG node chips render with stable ids."""
    html = _render_canvas()
    for node_id in _NODE_IDS:
        assert f'id="{node_id}"' in html, f"missing node {node_id}"


def test_render_scan_search_uses_em_dash_no_determinate_bar() -> None:
    """Scan/Search renders a literal em-dash denominator and no done/total %% bar width."""
    html = _render_canvas()
    scan = html[html.index('id="node-scan_search"') : html.index('id="node-proposals"')]
    assert "/ —" in scan, "Scan/Search must render the literal em-dash denominator"
    # No determinate progress fill (no :style width) inside the Scan/Search node.
    assert ":style" not in scan, "Scan/Search must NOT compute a determinate bar width"


def test_render_counts_use_tabular_nums() -> None:
    """Every count uses tabular-nums so digits don't jitter as they tick."""
    html = _render_canvas()
    # 9 nodes each render one tabular-nums count.
    assert html.count("tabular-nums") >= len(_NODE_IDS)


def test_render_every_node_has_dark_class() -> None:
    """Every node chip carries a dark: class (dark-mode mandatory)."""
    html = _render_canvas()
    for node_id in _NODE_IDS:
        start = html.index(f'id="{node_id}"')
        # Slice to the next node (or end) and assert a dark: utility is present.
        chunk = html[start : start + 1400]
        assert "dark:" in chunk, f"node {node_id} missing a dark: class"


def test_render_full_page_seeds_mirror_dag_oob_ids() -> None:
    """Full-page in-place seeds exist for every per-node key (mirroring dag-seed-<key>)."""
    html = _render_canvas(dag={k: i for i, k in enumerate(_DAG_KEYS)})
    for key in _DAG_KEYS:
        assert f'id="dag-seed-{key}"' in html, f"missing in-place seed for {key}"
        assert f"$store.pipeline.{key} =" in html


def test_render_phase34_gating_keys_seeded_in_place() -> None:
    """The Phase-34 gating keys are seeded in-place under the OOB-target ids."""
    html = _render_canvas(discovered=42, analyzed=10, metadata_extracted=30, agent_busy=3, controller_busy=1)
    assert "$store.pipeline.discovered = 42" in html
    assert "$store.pipeline.analyzed = 10" in html
    assert "$store.pipeline.agentBusy = 3" in html
    assert "$store.pipeline.controllerBusy = 1" in html


def test_discovery_node_has_no_rescan_anchor() -> None:
    """REQ-38-3: the dead "Rescan Files" scroll anchor is removed from the Discovery node.

    Scanning is initiated solely from the Trigger Scan card (POST /pipeline/scans); the
    Discovery chip ends at its node_bar with no action element, so neither the "Rescan Files"
    label nor the in-page scroll target href="#trigger-scan-heading" may appear in the canvas.
    """
    html = _render_canvas()
    assert "Rescan Files" not in html
    assert 'href="#trigger-scan-heading"' not in html


# ---------------------------------------------------------------------------
# stage controls (38-02) — per-stage pause/resume + priority steppers
# ---------------------------------------------------------------------------


def test_controls_render_pause_resume_static_hx_post_per_agent_stage() -> None:
    """REQ-38-1: each agent chip renders TWO x-show-gated static-hx-post Pause/Resume buttons."""
    html = _render_canvas()
    for stage in _AGENT_STAGES:
        frag = _stage_control_fragment(html, stage)
        # Pause — static hx-post, shown only while NOT paused.
        assert f'hx-post="/pipeline/stages/{stage}/pause"' in frag
        assert f'x-show="!$store.pipeline.{stage}Paused"' in frag
        # Resume — static hx-post, shown only while paused.
        assert f'hx-post="/pipeline/stages/{stage}/resume"' in frag
        assert f'x-show="$store.pipeline.{stage}Paused"' in frag
        # Label flip (color is never the only signal).
        assert ">Pause</button>" in frag
        assert ">Resume</button>" in frag


def test_controls_render_priority_steppers_per_agent_stage() -> None:
    """REQ-38-2: ▲ Higher posts {delta:-10}; ▼ Lower posts {delta:+10}; value binds <stage>Priority."""
    html = _render_canvas()
    for stage in _AGENT_STAGES:
        frag = _stage_control_fragment(html, stage)
        assert f'hx-post="/pipeline/stages/{stage}/priority"' in frag
        # ▲ Higher decrements the raw number (runs sooner); disabled at the floor.
        assert "hx-vals='{\"delta\": -10}'" in frag
        assert f"$store.pipeline.{stage}Priority <= 0" in frag
        assert "▲ Higher" in frag
        # ▼ Lower increments; disabled at the ceiling.
        assert "hx-vals='{\"delta\": 10}'" in frag
        assert f"$store.pipeline.{stage}Priority >= 100" in frag
        assert "▼ Lower" in frag
        # Raw value bound with tabular-nums so it doesn't reflow as it steps.
        assert f'x-text="$store.pipeline.{stage}Priority"' in frag
        assert "tabular-nums" in frag


def test_controls_are_authoritative_only_and_store_driven() -> None:
    """Controls use hx-swap=none + a JSON-parse after-request store writer (no optimistic mutation)."""
    html = _render_canvas()
    for stage in _AGENT_STAGES:
        frag = _stage_control_fragment(html, stage)
        # Every control is hx-swap=none + self-disabling for the request duration.
        assert frag.count('hx-swap="none"') == 4  # pause + resume + ▲ + ▼
        assert frag.count('hx-disabled-elt="this"') == 4
        # Authoritative store write from the server JSON (paused coerced to int 0/1).
        assert "JSON.parse($event.detail.xhr.response)" in frag
        assert f"$store.pipeline.{stage}Priority = r.priority" in frag
        assert f"$store.pipeline.{stage}Paused = r.paused ? 1 : 0" in frag


def test_controls_are_not_agentbusy_gated() -> None:
    """Controls read ONLY $store.pipeline.<stage>Paused/Priority — never nodes.<stage>.blocked / agentBusy."""
    html = _render_canvas()
    for stage in _AGENT_STAGES:
        frag = _stage_control_fragment(html, stage)
        assert ".blocked" not in frag, f"{stage} controls must not gate on nodes.{stage}.blocked"
        assert "agentBusy" not in frag, f"{stage} controls must not gate on agentBusy"


def test_controls_carry_dark_class_and_grid_aligned_spacing() -> None:
    """Every control fragment carries a dark: variant; steppers use px-1 + min-h-[28px] (never px-1.5)."""
    html = _render_canvas()
    for stage in _AGENT_STAGES:
        frag = _stage_control_fragment(html, stage)
        assert "dark:" in frag, f"{stage} controls missing a dark: class"
        assert "min-h-[28px]" in frag
        assert "px-1.5" not in frag, "stepper padding must be the grid-aligned px-1, not px-1.5"


def test_controls_render_priority_hint_once_per_agent_stage() -> None:
    """The static 'lower number runs first' hint appears once per agent node (3 total)."""
    html = _render_canvas()
    assert html.count("lower number runs first") == len(_AGENT_STAGES)


def test_controls_only_on_agent_stages_not_other_nodes() -> None:
    """Only the 3 agent chips carry controls — no stage_controls fragment for non-agent nodes."""
    html = _render_canvas()
    assert html.count('id="stage-controls-') == len(_AGENT_STAGES)
    for non_agent in ("discovery", "scan_search", "proposals", "scrape", "execute", "match"):
        assert f'id="stage-controls-{non_agent}"' not in html


# ---------------------------------------------------------------------------
# gating — triggers, LOCKED predicates/copy, the <ol> fallback
# ---------------------------------------------------------------------------


def test_gating_triggers_post_only_to_existing_endpoints() -> None:
    """Every POST target is an existing endpoint — the 4 enqueue triggers + the Phase-38 controls.

    INTENTIONAL Phase-38 contract change (38-02), NOT an accidental loosening of the original
    "exactly 4 hx-post" guard: the per-stage pause/resume/priority controls add 12 new
    ``/pipeline/stages/...`` posts (4 per agent stage). The enqueue-trigger surface is still
    pinned to exactly its 4 existing endpoints (no net-new trigger surface — T-35-10), and a
    separate assertion pins the stage-control surface to the known endpoints.
    """
    html = _render_canvas()
    targets = re.findall(r'hx-post="(/pipeline/[^"]+)"', html)

    # The 4 enqueue triggers POST only to the existing Phase-34 endpoints — unchanged.
    enqueue_targets = sorted(t for t in targets if not t.startswith("/pipeline/stages/"))
    assert enqueue_targets == [
        "/pipeline/analyze",
        "/pipeline/extract-metadata",
        "/pipeline/fingerprint",
        "/pipeline/proposals",
    ], enqueue_targets

    # The Phase-38 stage controls POST only to the existing /pipeline/stages/* endpoints:
    # pause + resume + priority (x2 steppers) for each of the 3 agent stages = 12 posts.
    stage_targets = sorted(t for t in targets if t.startswith("/pipeline/stages/"))
    expected_stage = sorted(f"/pipeline/stages/{stage}/{action}" for stage in _AGENT_STAGES for action in ("pause", "resume", "priority", "priority"))
    assert stage_targets == expected_stage, stage_targets

    # No POST target outside the enqueue + stage-control surfaces.
    assert html.count('hx-post="/pipeline/') == len(enqueue_targets) + len(stage_targets)


def test_gating_fingerprint_gates_on_discovered_not_metadata_extracted() -> None:
    """Fingerprint's gate reads store.discovered, NOT metadataExtracted (UI-SPEC L243)."""
    src = (PARTIALS_DIR / "dag_canvas.html").read_text(encoding="utf-8")
    # Inspect ONLY the fingerprint mk(...) call line (not the explanatory comment, which
    # legitimately names metadataExtracted to document the topology correction).
    start = src.index("fingerprint: mk(")
    fp_line = src[start : src.index("\n", start)]
    assert "s.discovered === 0" in fp_line
    assert "metadataExtracted" not in fp_line


def test_gating_locked_disabled_reason_copy_present() -> None:
    """Every LOCKED disabled-reason string appears verbatim."""
    html = _render_canvas()
    for reason in (
        "No files discovered",
        "Agent busy",
        "Controller busy",
        "Waiting on Analyze",
        "Needs proposals",
        "Needs tracklist",
    ):
        assert reason in html, f"missing LOCKED reason '{reason}'"


def test_gating_locked_state_pill_copy_present() -> None:
    """Every LOCKED state-pill string appears verbatim ({N} ACTIVE rendered as ' ACTIVE')."""
    html = _render_canvas()
    for pill in ("DONE", "READY", "WAITING", "GATED", "ACTIVE"):
        assert pill in html, f"missing LOCKED pill '{pill}'"


def test_gating_predicates_use_busy_and_dependency_gates() -> None:
    """The gate predicates read agentBusy / controllerBusy / analyzed / approved."""
    src = (PARTIALS_DIR / "dag_canvas.html").read_text(encoding="utf-8")
    # The parent x-data `nodes` getter ends just before the "Pipeline Graph" heading.
    nodes_block = src[src.index("get nodes()") : src.index("Pipeline Graph")]
    assert "s.agentBusy > 0" in nodes_block  # metadata / analyze / fingerprint
    assert "s.controllerBusy > 0" in nodes_block  # proposals
    assert "s.analyzed === 0" in nodes_block  # proposals dependency gate
    assert "s.approved === 0" in nodes_block  # execute gate


def test_gating_stacked_ol_is_text_equivalent() -> None:
    """The stacked <ol> exists, is sr-only at >= sm, and lists all 9 stages in order."""
    html = _render_canvas()
    assert "sm:sr-only" in html
    ol = html[html.index("<ol") : html.index("</ol>")]
    assert ol.count("<li") == 9, "the <ol> must carry all 9 stages"
    # Topological order: Discovery first, Execute last.
    assert ol.index("Discovery") < ol.index("Analyze") < ol.index("Approve")


def test_gating_buttons_keep_response_slot_and_inline_error() -> None:
    """Each enqueue trigger keeps its HTMX response slot + the LOCKED inline error copy."""
    html = _render_canvas()
    for slot in ("analyze-response", "extract-metadata-response", "fingerprint-response", "proposals-response"):
        assert f'id="{slot}"' in html, f"missing response slot {slot}"
    assert "Couldn't enqueue. Retry." in html


def test_gating_execute_is_navigational_link() -> None:
    """Approve → Execute is a navigational <a href=/proposals/> (no enqueue POST)."""
    html = _render_canvas()
    assert 'href="/proposals/"' in html
    # Execute never POSTs — it only routes to the human review queue.
    execute = html[html.index('id="node-execute"') : html.index('id="node-match"')]
    assert "hx-post" not in execute


# ---------------------------------------------------------------------------
# integration — DB-backed render via the shared `client` fixture (auto-marked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_dashboard_renders_dag_canvas(client: AsyncClient) -> None:
    """GET /pipeline renders the DAG canvas with all 9 node labels and no legacy markers."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    body = response.text
    assert 'aria-label="Pipeline stage graph"' in body
    for label in ("Discovery", "Metadata", "Analyze", "Fingerprint", "Scan / Search", "Proposals", "Scrape", "Execute", "Match"):
        assert label in body, f"missing node label '{label}'"
    # D-01: the Phase-34 stage-cards heading + processing card are gone.
    assert "Pipeline Actions" not in body
    assert 'id="processing-card"' not in body


@pytest.mark.asyncio
async def test_integration_dashboard_edge_honesty(client: AsyncClient) -> None:
    """The rendered SVG converges only Metadata+Analyze into Proposals (edge honesty)."""
    response = await client.get("/pipeline/")
    body = response.text
    # Nine anchor-derived bézier edges; none originate at fingerprint/scrape/match into proposals.
    paths = re.findall(r'<path d="M [\d., ]+C [\d., ]+"', body)
    assert len(paths) == 9
    src = (PARTIALS_DIR / "dag_canvas.html").read_text(encoding="utf-8")
    edge_block = src[src.index("set EDGES") : src.index("] %}", src.index("set EDGES"))]
    assert '["metadata", "proposals"]' in edge_block
    assert '["analyze", "proposals"]' in edge_block
    assert '["fingerprint", "proposals"]' not in edge_block


@pytest.mark.asyncio
async def test_integration_dashboard_scan_search_em_dash(client: AsyncClient) -> None:
    """The rendered Scan/Search node shows the literal em-dash denominator."""
    response = await client.get("/pipeline/")
    body = response.text
    scan = body[body.index('id="node-scan_search"') : body.index('id="node-proposals"')]
    assert "/ —" in scan


@pytest.mark.asyncio
async def test_integration_stats_poll_still_emits_per_node_oob_seeds(client: AsyncClient) -> None:
    """GET /pipeline/stats still emits the per-node OOB seeds (35-04 contract preserved)."""
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    body = response.text
    for key in _DAG_KEYS:
        assert f'id="dag-seed-{key}" hx-swap-oob="true"' in body, f"missing OOB seed for {key}"


def test_integration_legacy_partials_removed() -> None:
    """The Phase-34 stage_cards.html and processing_card.html files no longer exist."""
    assert not (PARTIALS_DIR / "stage_cards.html").exists()
    assert not (PARTIALS_DIR / "processing_card.html").exists()
