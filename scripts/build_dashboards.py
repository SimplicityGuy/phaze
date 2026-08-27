#!/usr/bin/env python
"""Generate the committed Grafana dashboard JSON under ``dashboards/``.

phaze-m1drf.4. The JSON is COMMITTED (it is the artifact an operator imports, and it must
survive a rebuild and be reviewable in a diff) and it is GENERATED (four dashboards of
hand-written Grafana JSON is thousands of lines of near-identical panel scaffolding, where
a typo in one panel's datasource reference is invisible in review and fatal on import).
``tests/shared/telemetry/test_dashboards.py`` regenerates and compares, so the two can
never drift.

**THE IMPORTABILITY REQUIREMENT IS STRUCTURAL, NOT COSMETIC.** Per the operator's
2026-08-26 decision these dashboards must import into a RUNNING Grafana that phaze does not
control, whose Prometheus has a datasource uid phaze cannot know. So every panel and every
target references ``${datasource}`` -- a dashboard-level TEMPLATE VARIABLE of type
``datasource`` -- and no hard-coded uid appears anywhere. :func:`_panel` is the only place a
datasource reference is written, which is what makes that a property of the file rather
than a habit.

**No local identifiers.** Committed dashboard JSON is a tracked file. Every query here is
written against label names and bounded label VALUES from
``phaze/telemetry/catalogue.py``; no archive filename, path, digest or file UUID appears in
any panel, query, description or example, and none can, because file identity is not a
metric label in the first place.

Every metric name below was READ OFF A REAL COLLECTOR (see
``docs/telemetry/metric-catalogue.md`` §5), not derived from the OTLP -> Prometheus naming
rules on paper. That is how ``phaze_saq_queue_depth`` was found to be exported as
``phaze_saq_queue_depth_ratio`` under a bare ``unit="1"``, and how two metrics were found to
be silently dropped entirely by a reserved-label collision.

    uv run python scripts/build_dashboards.py            # write dashboards/*.json
    uv run python scripts/build_dashboards.py --check    # verify the committed files match
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboards"

#: The datasource template variable every panel points at. Grafana's import flow prompts
#: for it, so an operator picks their own Prometheus and nothing needs editing.
DATASOURCE_REF: dict[str, str] = {"type": "prometheus", "uid": "${datasource}"}

_TEMPLATING: dict[str, Any] = {
    "list": [
        {
            "current": {},
            "hide": 0,
            "includeAll": False,
            "label": "Prometheus",
            "multi": False,
            "name": "datasource",
            "options": [],
            "query": "prometheus",
            "refresh": 1,
            "regex": "",
            "skipUrlSync": False,
            "type": "datasource",
        },
        {
            "current": {},
            "hide": 0,
            "includeAll": False,
            # phaze-m1drf.8 acceptance 6 asks for a path from a metrics panel to the
            # corresponding trace. A hard-coded Tempo uid would break the importability
            # requirement exactly as a hard-coded Prometheus uid would, so the trace store is
            # picked at import time the same way. A Grafana with no trace datasource leaves
            # this unset and the link is simply inert -- the panels are unaffected.
            "label": "Traces",
            "multi": False,
            "name": "tracesource",
            "options": [],
            "query": "tempo",
            "refresh": 1,
            "regex": "",
            "skipUrlSync": False,
            "type": "datasource",
        },
        {
            # Lets one Grafana show several phaze deployments without editing a query.
            # `job` is the Prometheus label the collector derives from `service.name`.
            "current": {},
            "datasource": DATASOURCE_REF,
            "definition": "label_values(phaze_analysis_run_duration_seconds_count, job)",
            "hide": 0,
            "includeAll": True,
            "label": "Service",
            "multi": True,
            "name": "job",
            "options": [],
            "query": {"query": "label_values(phaze_analysis_run_duration_seconds_count, job)", "refId": "job"},
            "refresh": 2,
            "regex": "",
            "skipUrlSync": False,
            "sort": 1,
            "type": "query",
        },
    ]
}


def _target(expr: str, legend: str, ref: str = "A", *, instant: bool = False) -> dict[str, Any]:
    return {
        "datasource": DATASOURCE_REF,
        "editorMode": "code",
        "expr": expr,
        "instant": instant,
        "range": not instant,
        "legendFormat": legend,
        "refId": ref,
    }


def _panel(
    panel_id: int,
    title: str,
    kind: str,
    targets: list[dict[str, Any]],
    grid: tuple[int, int, int, int],
    *,
    description: str = "",
    unit: str = "",
    options: dict[str, Any] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    custom: dict[str, Any] | None = None,
    decimals: int | None = None,
) -> dict[str, Any]:
    """One panel. THE only place a datasource reference is written.

    ``grid`` is ``(x, y, w, h)`` on Grafana's 24-column grid.
    """
    x, y, w, h = grid
    panel: dict[str, Any] = {
        "id": panel_id,
        "title": title,
        "type": kind,
        "datasource": DATASOURCE_REF,
        "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": custom or {}, **({"decimals": decimals} if decimals is not None else {})},
            "overrides": overrides or [],
        },
        "options": options or {},
    }
    return panel


def _row(panel_id: int, title: str, y: int) -> dict[str, Any]:
    return {"id": panel_id, "title": title, "type": "row", "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False, "panels": []}


#: Metrics -> traces, as a dashboard link rather than an exemplar. MEASURED 2026-08-27: phaze
#: DOES attach exemplars -- an `InMemoryMetricReader` shows a valid trace_id and span_id on
#: `phaze.analysis.model.inference.duration` recorded inside an active span -- but they do not
#: survive the collector's Prometheus exposition, which returned zero exemplar markers even
#: under OpenMetrics content negotiation. So the link is the fallback acceptance 6 allows, and
#: the exemplar finding is recorded in docs/design/0017-telemetry-export-topology.md section 7c
#: because the missing half is on the collector's side, not phaze's.
_TRACE_LINK: dict[str, Any] = {
    "title": "Find the trace for one file",
    "type": "link",
    "icon": "external link",
    "tooltip": "Per-FILE and per-CHUNK detail lives only in traces -- metrics carry no file dimension by construction.",
    "url": "/explore?left=" + '{"datasource":"${tracesource}","queries":[{"query":"{ resource.service.name = "phaze-analysis" }"}]}',
    "targetBlank": True,
    "asDropdown": False,
    "keepTime": True,
    "includeVars": False,
    "tags": [],
}


def _dashboard(uid: str, title: str, description: str, panels: list[dict[str, Any]], tags: list[str], *, trace_link: bool = False) -> dict[str, Any]:
    return {
        # No `id`, and `version` 0: an import into an unrelated Grafana must not carry this
        # instance's numbering with it.
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["phaze", *tags],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 0,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": _TEMPLATING,
        "links": [_TRACE_LINK] if trace_link else [],
        "panels": panels,
    }


# ---------------------------------------------------------------------------
# Shared query fragments
# ---------------------------------------------------------------------------
#
# `$job` scopes every query to the selected service(s); `$__rate_interval` is Grafana's own
# scrape-aware window, so these work unchanged against a homelab Prometheus whose scrape
# interval phaze does not know.
J = '{job=~"$job"}'


def _histogram_avg(metric: str, selector: str = J, by: str = "") -> str:
    """Mean of a histogram over the range -- sum-rate over count-rate.

    Deliberately NOT a quantile for the per-model panels: with 34 models the interesting
    comparison is total and mean cost per inference, and a quantile over a bucket ladder
    chosen for a five-decade range would read as bucket boundaries rather than as data.
    """
    grouping = f" by ({by})" if by else ""
    return f"sum(rate({metric}_sum{selector}[$__rate_interval])){grouping} / sum(rate({metric}_count{selector}[$__rate_interval])){grouping}"


def _histogram_share(metric: str, selector: str = J, by: str = "") -> str:
    """Seconds of work per second of wall clock -- the shape a stacked cost breakdown needs."""
    grouping = f" by ({by})" if by else ""
    return f"sum(rate({metric}_sum{selector}[$__rate_interval])){grouping}"


def _quantile(metric: str, q: float, selector: str = J, by: str = "") -> str:
    grouping = ", ".join(filter(None, ["le", by]))
    return f"histogram_quantile({q}, sum(rate({metric}_bucket{selector}[$__rate_interval])) by ({grouping}))"


_TS = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]}, "tooltip": {"mode": "multi", "sort": "desc"}}
_TS_STACK = {"lineWidth": 1, "fillOpacity": 60, "stacking": {"mode": "normal", "group": "A"}, "showPoints": "never"}
_TS_LINE = {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never"}
_STAT = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "textMode": "auto", "colorMode": "value"}


# ---------------------------------------------------------------------------
# 1. Analysis pipeline -- what is happening right now
# ---------------------------------------------------------------------------


def analysis_pipeline() -> dict[str, Any]:
    panels = [
        _row(1, "Right now", 0),
        _panel(
            2,
            "Windows completed / min",
            "timeseries",
            [_target('sum by (tier) (rate(phaze_analysis_windows_total{job=~"$job", outcome="analyzed"}[$__rate_interval])) * 60', "{{tier}}")],
            (0, 1, 12, 8),
            description=(
                "The signal that did not exist before this epic. The UI progress channel is fine-tier-only by design, "
                "so a running analysis reported nothing at all for the 94.69% of its wall clock spent in the coarse tier."
            ),
            unit="/min",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            3,
            "Model sweeps completed / min",
            "timeseries",
            [_target(f"sum(rate(phaze_analysis_model_sweep_duration_seconds_count{J}[$__rate_interval])) * 60", "sweeps")],
            (12, 1, 12, 8),
            description=(
                "One sweep is one of the 34 graphs run across every buffer of one chunk. The coarse tier can go many "
                "minutes without completing a WINDOW while completing sweeps steadily -- this is the finer liveness read."
            ),
            unit="/min",
            options=_TS,
            custom=_TS_LINE,
        ),
        _row(4, "Coverage and failures", 9),
        _panel(
            5,
            "Windows skipped / min (per-window failure isolation firing)",
            "timeseries",
            [_target('sum by (tier) (rate(phaze_analysis_windows_total{job=~"$job", outcome="skipped"}[$__rate_interval])) * 60', "{{tier}}")],
            (0, 10, 12, 8),
            description=(
                "A skip is a window dropped rather than a file failed -- a decode that produced no audio, a model that "
                "killed the window, a derivation that raised. Steady non-zero here is coverage quietly eroding."
            ),
            unit="/min",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            6,
            "Analysis outcomes / hour",
            "timeseries",
            [_target(f"sum by (outcome) (rate(phaze_analysis_run_duration_seconds_count{J}[$__rate_interval])) * 3600", "{{outcome}}")],
            (12, 10, 12, 8),
            unit="/h",
            options=_TS,
            custom=_TS_STACK,
        ),
        _row(7, "Memory -- the D-07 / D-09 invariant, made watchable", 18),
        _panel(
            8,
            "Peak RSS at chunk boundaries",
            "timeseries",
            [
                _target(_quantile("phaze_analysis_chunk_peak_rss_bytes", 0.99, by="tier"), "p99 {{tier}}", "A"),
                _target(_histogram_avg("phaze_analysis_chunk_peak_rss_bytes", by="tier"), "mean {{tier}}", "B"),
            ],
            (0, 19, 16, 8),
            description=(
                "A FLAT line is the invariant holding. A line with SLOPE is duration-linear growth, which is a BUG and "
                "never a sizing input -- phaze-b2qs9 measured +0.31 GiB per fine chunk before the D-09 fix, reaching "
                "10.28 GiB at 12 h against a 4 GiB pod limit. Do not raise a limit to flatten this panel."
            ),
            unit="bytes",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            9,
            "Chunks completed / min",
            "timeseries",
            [_target(f"sum by (tier) (rate(phaze_analysis_chunks_total{J}[$__rate_interval])) * 60", "{{tier}}")],
            (16, 19, 8, 8),
            unit="/min",
            options=_TS,
            custom=_TS_LINE,
        ),
    ]
    return _dashboard(
        "phaze-analysis-pipeline",
        "phaze / Analysis pipeline (live)",
        "What an analysis is doing right now, including the coarse tier that used to be invisible from outside the process.",
        panels,
        ["analysis"],
        trace_link=True,
    )


# ---------------------------------------------------------------------------
# 2. Analysis cost breakdown -- the phaze-8ifq8 dashboard
# ---------------------------------------------------------------------------


def analysis_cost() -> dict[str, Any]:
    coarse = '{job=~"$job", tier="coarse"}'
    panels = [
        _row(1, "Where the coarse tier goes -- phaze-8ifq8's first question", 0),
        _panel(
            2,
            "Coarse tier: seconds of work per second of wall clock, by phase",
            "timeseries",
            [
                _target(_histogram_share("phaze_analysis_chunk_decode_duration_seconds", coarse), "chunk decode", "A"),
                _target(_histogram_share("phaze_analysis_model_graph_build_duration_seconds"), "graph construction", "B"),
                _target(_histogram_share("phaze_analysis_model_inference_duration_seconds"), "inference proper", "C"),
                _target(_histogram_share("phaze_analysis_chunk_derive_duration_seconds", coarse), "derive + assemble", "D"),
            ],
            (0, 1, 24, 10),
            description=(
                "THE panel this dashboard exists for. phaze-8ifq8 asks how the coarse tier splits between decode, graph "
                "construction, inference and assembly; before this epic that question needed hand-querying Postgres and "
                "reading pod log timestamps. Stacked because the interesting reading is the SHARE, and the values are "
                "concurrent work-seconds per wall-second -- above 1 means several analyses are running at once."
            ),
            unit="s",
            options=_TS,
            custom=_TS_STACK,
        ),
        _row(3, "Per-model cost by classifier_type -- phaze-8ifq8's second question", 11),
        _panel(
            4,
            "Mean inference per window, by model",
            "barchart",
            [
                _target(
                    _histogram_avg("phaze_analysis_model_inference_duration_seconds", by="model_name, model_variant, classifier_type"),
                    "{{model_name}} / {{model_variant}} ({{classifier_type}})",
                    instant=True,
                )
            ],
            (0, 12, 14, 12),
            description=(
                "34 models: 11 characteristic sets x 3 variants, plus the genre model. musicnn, vggish and "
                "effnet_discogs do not cost the same, and this is where that becomes a number rather than a belief. "
                "The WINDOW is not a label here -- it would be unbounded across a corpus whose longest file has 241 "
                "coarse windows -- so this is the mean over every window the range covers."
            ),
            unit="s",
            options={"orientation": "horizontal", "legend": {"showLegend": False}, "xTickLabelRotation": 0},
        ),
        _panel(
            5,
            "Total inference time by classifier_type",
            "piechart",
            [
                _target(
                    f"sum by (classifier_type) (rate(phaze_analysis_model_inference_duration_seconds_sum{J}[$__rate_interval]))",
                    "{{classifier_type}}",
                )
            ],
            (14, 12, 10, 12),
            description="Where the sweep's wall clock actually lands, aggregated to the three inference families.",
            unit="s",
            options={
                "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]},
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        ),
        _row(6, "What the D-07 chunking costs", 24),
        _panel(
            7,
            "Graph construction vs inference",
            "timeseries",
            [
                _target(_histogram_share("phaze_analysis_model_graph_build_duration_seconds"), "graph build", "A"),
                _target(_histogram_share("phaze_analysis_model_graph_release_duration_seconds"), "graph release", "B"),
                _target(_histogram_share("phaze_analysis_model_inference_duration_seconds"), "inference", "C"),
            ],
            (0, 25, 12, 8),
            description=(
                "D-07 chunking pays 34 graph constructions per CHUNK instead of per file -- the deliberate price for a "
                "peak that does not scale with duration. This is that price, next to the work it protects."
            ),
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            8,
            "Fine vs coarse tier",
            "timeseries",
            [_target(_histogram_share("phaze_analysis_tier_duration_seconds", by="tier"), "{{tier}}")],
            (12, 25, 12, 8),
            description=(
                "phaze-zaf2l measured 5.31% fine / 94.69% coarse on one production run by differencing log timestamps. "
                "This is the same split, continuously and without reading a pod log."
            ),
            unit="s",
            options=_TS,
            custom=_TS_STACK,
        ),
        _panel(
            9,
            "Chunk decode duration",
            "timeseries",
            [
                _target(_quantile("phaze_analysis_chunk_decode_duration_seconds", 0.95, by="tier"), "p95 {{tier}}", "A"),
                _target(_histogram_avg("phaze_analysis_chunk_decode_duration_seconds", by="tier"), "mean {{tier}}", "B"),
            ],
            (0, 33, 12, 8),
            description="MonoLoader cannot seek, so every chunk decodes from byte 0 up to its own gate. This is what that costs.",
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            10,
            "Fine window measurement (RhythmExtractor2013 + KeyExtractor)",
            "timeseries",
            [
                _target(_quantile("phaze_analysis_fine_window_duration_seconds", 0.95), "p95", "A"),
                _target(_histogram_avg("phaze_analysis_fine_window_duration_seconds"), "mean", "B"),
            ],
            (12, 33, 12, 8),
            description=(
                "The fine tier's counterpart to a model sweep. phaze-zaf2l measured 2.379-2.694 s per fine window on "
                "four of five production files and a 5.845 s outlier it could not explain from the database, because "
                "`metadata` stores no sample rate. This panel is where that outlier becomes visible while it happens."
            ),
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
    ]
    return _dashboard(
        "phaze-analysis-cost",
        "phaze / Analysis cost breakdown",
        "How the coarse tier splits between decode, graph construction, inference and assembly, and what each of the 34 models costs. Built for phaze-8ifq8.",
        panels,
        ["analysis", "cost"],
        trace_link=True,
    )


# ---------------------------------------------------------------------------
# 3. Throughput and backlog
# ---------------------------------------------------------------------------


def throughput_backlog() -> dict[str, Any]:
    panels = [
        _row(1, "Throughput", 0),
        _panel(
            2,
            "Audio-hours per wall-hour",
            "stat",
            [_target('sum(rate(phaze_analysis_audio_duration_seconds_total{job=~"$job", outcome="ok"}[$__rate_interval]))', "audio-h / wall-h")],
            (0, 1, 6, 6),
            description=(
                "Audio-seconds admitted per wall-second, which is the same number as audio-hours per wall-hour. "
                "phaze-zaf2l measured 2.7183 across 410 completions over 7 days by joining analysis_completed_at to "
                "metadata.duration in psql. This is that figure, live."
            ),
            options=_STAT,
            decimals=4,
        ),
        _panel(
            3,
            "Wall clock per second of audio",
            "stat",
            [
                _target(
                    f"sum(rate(phaze_analysis_run_duration_seconds_sum{J}[$__rate_interval])) / sum(rate(phaze_analysis_audio_duration_seconds_total{J}[$__rate_interval]))",
                    "x duration",
                )
            ],
            (6, 1, 6, 6),
            description=(
                "The 1.4951x phaze-zaf2l measured at the production operating point (cap = 4 concurrent pods on the "
                "burst node). Note this counts one file's own wall clock, so it rises with concurrency by design -- "
                "phaze-8r6t4 priced W=4 at +83.6% per-file wall against W=2."
            ),
            options=_STAT,
            decimals=4,
        ),
        _panel(
            4,
            "Files completed / hour",
            "stat",
            [_target('sum(rate(phaze_analysis_run_duration_seconds_count{job=~"$job", outcome="ok"}[$__rate_interval])) * 3600', "files/h")],
            (12, 1, 6, 6),
            description="phaze-zaf2l measured 2.4480 files/hour over a 7-day window, and noted it is the LESS stable of the two throughput reads because it is blind to the mix of durations.",
            options=_STAT,
            decimals=4,
        ),
        _panel(
            5,
            "Analysis failure share",
            "stat",
            [
                _target(
                    f'sum(rate(phaze_analysis_run_duration_seconds_count{{job=~"$job", outcome="error"}}[$__rate_interval])) / clamp_min(sum(rate(phaze_analysis_run_duration_seconds_count{J}[$__rate_interval])), 1e-12)',
                    "failed",
                )
            ],
            (18, 1, 6, 6),
            description="Measured baseline: 4 hard failures against 4,383 completed analyses (0.0913%) at the time of phaze-zaf2l -- 3 AnalysisDecodeError, 1 AnalysisProbeError.",
            unit="percentunit",
            options=_STAT,
        ),
        _panel(
            6,
            "Throughput over time",
            "timeseries",
            [
                _target(
                    'sum(rate(phaze_analysis_audio_duration_seconds_total{job=~"$job", outcome="ok"}[$__rate_interval]))', "audio-h / wall-h", "A"
                ),
                _target('sum(rate(phaze_analysis_run_duration_seconds_count{job=~"$job", outcome="ok"}[$__rate_interval])) * 3600', "files / h", "B"),
            ],
            (0, 7, 24, 9),
            options=_TS,
            custom=_TS_LINE,
        ),
        _row(7, "Backlog -- a settled operator decision, not a fault", 16),
        _panel(
            8,
            "Files waiting, by stage",
            "timeseries",
            [_target(f"max by (backlog) (phaze_pipeline_backlog{J})", "{{backlog}}")],
            (0, 17, 16, 9),
            description=(
                "OPERATOR DECISION 2026-08-26, bead phaze-m1drf.5: asked how the 8,079-row awaiting backlog should be "
                "handled, the operator chose the option labelled 'Accept the drain rate' (durable record: repowise "
                "decision e1e3374e; the question as put is quoted in docs/telemetry/alerting.md). So backlog DEPTH is "
                "explicitly NOT a fault condition and nothing alerts on it. "
                "CAVEAT: these series are POLL-DRIVEN -- they are sampled by the admin UI's own /pipeline/stats read, "
                "so they go stale when no admin tab is open. Read them here; never alert on them."
            ),
            unit="short",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            9,
            "Days to drain the awaiting queue at the current rate",
            "stat",
            [
                _target(
                    'max(phaze_pipeline_backlog{job=~"$job", backlog="awaiting_cloud"}) / clamp_min(sum(rate(phaze_analysis_run_duration_seconds_count{job=~"$job", outcome="ok"}[$__rate_interval])) * 86400, 1e-12)',
                    "days",
                )
            ],
            (16, 17, 8, 9),
            description=(
                "phaze-zaf2l computed 137.5 days (band 94-156) for 8,079 awaiting rows at 2.4480 files/hour. "
                "Informational: the drain rate is a settled decision, so a large number here is expected, not a fault."
            ),
            unit="d",
            options=_STAT,
            decimals=1,
        ),
    ]
    return _dashboard(
        "phaze-throughput-backlog",
        "phaze / Throughput and backlog",
        "How fast the archive is draining and how much is left -- the figures phaze-zaf2l had to obtain by hand-querying Postgres.",
        panels,
        ["throughput"],
    )


# ---------------------------------------------------------------------------
# 4. Service health
# ---------------------------------------------------------------------------


def service_health() -> dict[str, Any]:
    panels = [
        _row(1, "HTTP", 0),
        _panel(
            2,
            "Request duration p95, by route",
            "timeseries",
            [_target(f"topk(10, {_quantile('phaze_http_server_request_duration_seconds', 0.95, by='http_route')})", "{{http_route}}")],
            (0, 1, 16, 9),
            description=(
                "Routes are TEMPLATES -- '/record/{file_id}', never a real file uuid -- and anything matching no route "
                "reports the literal '__unmatched__'. That is what keeps a 404 scan from minting one series per probed path. "
                "Measured baselines (phaze-zaf2l section 4): /pipeline/stats 534.0 ms fired every 5 s by every open admin tab, "
                "/pipeline/tracklist-drain-status 1,378.6 ms."
            ),
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            3,
            "Requests / s by status class",
            "timeseries",
            [
                _target(
                    f"sum by (http_status_class) (rate(phaze_http_server_request_duration_seconds_count{J}[$__rate_interval]))",
                    "{{http_status_class}}",
                )
            ],
            (16, 1, 8, 9),
            unit="reqps",
            options=_TS,
            custom=_TS_STACK,
        ),
        _panel(
            4,
            "Handler seconds per wall second, by route",
            "timeseries",
            [_target(f"topk(10, sum by (http_route) (rate(phaze_http_server_request_duration_seconds_sum{J}[$__rate_interval])))", "{{http_route}}")],
            (0, 10, 16, 9),
            description=(
                "Duty cycle, not latency -- the read that exposes a cheap-looking endpoint on a fast timer. "
                "phaze-zaf2l measured /pipeline/stats at a 10.68% continuous duty cycle: 384.5 s of handler time per "
                "wall hour for a browser tab left open."
            ),
            unit="s",
            options=_TS,
            custom=_TS_STACK,
        ),
        _panel(
            5,
            "Requests in flight",
            "timeseries",
            [_target(f"sum(phaze_http_server_active_requests{J})", "in flight")],
            (16, 10, 8, 9),
            unit="short",
            options=_TS,
            custom=_TS_LINE,
        ),
        _row(6, "SAQ", 19),
        _panel(
            7,
            "SAQ rows queued and active, by pipeline stage",
            "timeseries",
            [_target(f"sum by (stage, status) (phaze_pipeline_stage_inflight{J})", "{{stage}} / {{status}}")],
            (0, 20, 12, 8),
            description=(
                "phaze-zaf2l measured production depth holding at 9 across 28 samples and filed NO bead "
                "against SAQ: a local burst drained 5,000 jobs at 318.3/s against a production load of "
                "0.0939 jobs/s, i.e. 0.03% of capacity. "
                "Labelled by STAGE, not by queue -- the sampler groups by SAQ function name, and calling "
                "that a queue would be mislabelled data. POLL-DRIVEN: sampled by the admin UI's own "
                "/pipeline/stats read, so it goes stale when no tab is open."
            ),
            unit="short",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            8,
            "Job duration p95, by function",
            "timeseries",
            [_target(f"topk(10, {_quantile('phaze_saq_job_duration_seconds', 0.95, by='saq_function')})", "{{saq_function}}")],
            (12, 20, 12, 8),
            description="Labelled `saq_function`, not `job`: `job` is reserved by Prometheus for the target derived from service.name, and a metric that collides with it is dropped ENTIRELY by the collector.",
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            9,
            "Job outcomes / min",
            "timeseries",
            [_target(f"sum by (outcome) (rate(phaze_saq_jobs_total{J}[$__rate_interval])) * 60", "{{outcome}}")],
            (0, 28, 12, 8),
            unit="/min",
            options=_TS,
            custom=_TS_STACK,
        ),
        _panel(
            10,
            "Pipeline stage transitions / min",
            "timeseries",
            [
                _target(
                    f"sum by (stage, transition) (rate(phaze_pipeline_stage_transitions_total{J}[$__rate_interval])) * 60", "{{stage}} {{transition}}"
                )
            ],
            (12, 28, 12, 8),
            description="Scheduling-ledger transitions: a stage SCHEDULED for a file, and that row RESOLVED. The ledger is the durable record recovery reads.",
            unit="/min",
            options=_TS,
            custom=_TS_LINE,
        ),
        _row(11, "Database", 36),
        _panel(
            12,
            "Statement duration p95, by operation",
            "timeseries",
            [_target(_quantile("phaze_db_statement_duration_seconds", 0.95, by="db_operation"), "{{db_operation}}")],
            (0, 37, 12, 8),
            description="Only the leading keyword is ever a label. The statement TEXT is unbounded AND carries operator data -- a WHERE clause over this archive is a local identifier that would be stored forever.",
            unit="s",
            options=_TS,
            custom=_TS_LINE,
        ),
        _panel(
            13,
            "Statements / s by operation",
            "timeseries",
            [_target(f"sum by (db_operation) (rate(phaze_db_statements_total{J}[$__rate_interval]))", "{{db_operation}}")],
            (12, 37, 12, 8),
            description="Divided by request rate this is per-request fan-out -- the cost phaze-zaf2l had to measure with pg_stat_user_tables deltas against a duration-matched idle control.",
            unit="ops",
            options=_TS,
            custom=_TS_STACK,
        ),
    ]
    return _dashboard(
        "phaze-service-health",
        "phaze / Service health",
        "HTTP, SAQ and database health for the api and worker processes.",
        panels,
        ["service"],
    )


DASHBOARDS: dict[str, Any] = {
    "phaze-analysis-pipeline.json": analysis_pipeline,
    "phaze-analysis-cost.json": analysis_cost,
    "phaze-throughput-backlog.json": throughput_backlog,
    "phaze-service-health.json": service_health,
}


def render(name: str) -> str:
    """Serialize one dashboard.

    ``sort_keys=True`` and ``indent=2`` are not stylistic: they are what this repo's
    ``pretty-format-json`` pre-commit hook (``--autofix --indent=2``) produces. Emitting
    anything else means the hook silently rewrites every generated file on commit and the
    generator's ``--check`` mode then reports drift against the hook's own output forever.
    """
    return json.dumps(DASHBOARDS[name](), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the committed JSON differs from what this script generates")
    args = parser.parse_args()

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for name in DASHBOARDS:
        rendered = render(name)
        path = DASHBOARD_DIR / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                drift.append(name)
        else:
            path.write_text(rendered, encoding="utf-8")
    if args.check and drift:
        sys.stderr.write(f"dashboards out of date: {drift}\nrun: uv run python scripts/build_dashboards.py\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
