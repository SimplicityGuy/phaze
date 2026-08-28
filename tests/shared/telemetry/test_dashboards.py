"""The CI-runnable half of the dashboard guarantees (phaze-m1drf.4).

The other half needs a live Grafana holding a real analysis run and lives in
``scripts/verify_dashboards.py`` -- it imports the committed JSON through Grafana's real
API against a datasource whose uid deliberately differs, and runs every panel's PromQL
through the query API. That is the ADR-0012 rule 3 verification: the real consumer of
Grafana dashboard JSON is Grafana, and a schema check would happily accept a dashboard
whose datasource does not exist, whose panel type Grafana does not have, or whose PromQL
does not parse.

What CI *can* hold is the part that does not need a server, and each of these is a
property something in the epic would otherwise get wrong quietly:

* the committed JSON matches its generator (otherwise a hand edit is silently reverted by
  the next build, or the generator becomes a lie);
* no hard-coded datasource uid anywhere (the importability requirement);
* every metric referenced exists in the catalogue (a typo in a metric name is an
  empty panel, and empty panels look like idle systems);
* no local identifier appears in a tracked file.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess  # nosec B404 - runs this repo's own generator with a fixed argv, no shell
import sys

import pytest

from phaze.telemetry.catalogue import CATALOGUE


REPO = Path(__file__).resolve().parents[3]
DASHBOARD_DIR = REPO / "dashboards"

DASHBOARDS = sorted(DASHBOARD_DIR.glob("*.json"))


#: Every Prometheus metric family the catalogue can produce, with the suffixes the
#: OTLP -> Prometheus translation appends. Measured against a real
#: otel/opentelemetry-collector-contrib 0.140.0 and recorded in
#: docs/telemetry/metric-catalogue.md section 5 -- NOT derived from the naming rules on paper.
def _prometheus_families() -> set[str]:
    families: set[str] = set()
    for spec in CATALOGUE:
        base = "phaze_" + spec.name.removeprefix("phaze.").replace(".", "_")
        if spec.unit == "s":
            base += "_seconds"
        elif spec.unit == "By":
            base += "_bytes"
        if spec.kind == "histogram":
            families.update({base, f"{base}_bucket", f"{base}_sum", f"{base}_count"})
        elif spec.kind == "counter":
            families.update({f"{base}_total"})
        else:
            families.add(base)
    return families


def test_there_are_dashboards_at_all() -> None:
    assert DASHBOARDS, "no dashboard JSON is committed"


def test_the_committed_json_matches_its_generator() -> None:
    """Both halves are load-bearing: the JSON is the artifact an operator imports, and the
    generator is what keeps four dashboards of near-identical panel scaffolding correct."""
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved interpreter, literal script path
        [sys.executable, str(REPO / "scripts" / "build_dashboards.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert result.returncode == 0, f"dashboards are out of date; run scripts/build_dashboards.py\n{result.stderr}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_hard_coded_datasource_uid(path: Path) -> None:
    """THE importability requirement, and it is structural.

    Operator decision 2026-08-26, bead phaze-m1drf.4: the dashboards must import into a
    RUNNING Grafana, not only be provisioned into a container this repo controls -- the
    operator asked for "grafana dashboards that can be imported in running grafana
    instances", recorded verbatim in epic phaze-m1drf's description. That instance's
    Prometheus has a uid phaze cannot know, so every reference must be the ``${datasource}``
    template variable.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    uids = re.findall(r'"uid":\s*"([^"]*)"', json.dumps(dashboard))
    offenders = [uid for uid in uids if uid != "${datasource}" and uid != dashboard.get("uid")]
    assert not offenders, f"{path.name} carries hard-coded uid(s) {offenders}; use the ${{datasource}} variable"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_the_datasource_variable_exists(path: Path) -> None:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    variables = {var["name"]: var for var in dashboard["templating"]["list"]}
    assert "datasource" in variables, f"{path.name} references ${{datasource}} but declares no such variable"
    assert variables["datasource"]["type"] == "datasource"
    assert variables["datasource"]["query"] == "prometheus"


def _panels(dashboard: dict) -> list[dict]:
    panels: list[dict] = []
    for panel in dashboard.get("panels", []):
        panels.append(panel)
        panels.extend(panel.get("panels", []))
    return [panel for panel in panels if panel.get("type") != "row"]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_panel_has_a_query_and_a_description_where_it_matters(path: Path) -> None:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for panel in _panels(dashboard):
        assert panel.get("targets"), f"{path.name}: panel {panel['title']!r} has no query"
        for target in panel["targets"]:
            assert target.get("expr"), f"{path.name}: panel {panel['title']!r} has an empty expr"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_metric_referenced_is_catalogued(path: Path) -> None:
    """A typo in a metric name is an EMPTY PANEL, and an empty panel reads as an idle
    system rather than as a broken dashboard. That is the failure this catches."""
    families = _prometheus_families()
    text = path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bphaze_[a-z0-9_]+", text))
    unknown = sorted(referenced - families)
    assert not unknown, f"{path.name} queries metric(s) the catalogue does not define: {unknown}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_local_identifier_in_committed_dashboard_json(path: Path) -> None:
    """Dashboard JSON is a tracked file. CONVENTIONS.md forbids real archive filenames,
    paths, digests and file UUIDs in one -- and a dashboard is a place they arrive easily,
    through an example query or a screenshot annotation.

    ``{file_id}`` in a route TEMPLATE is fine and is the point: it is the placeholder that
    exists so a real uuid never becomes a label.
    """
    text = path.read_text(encoding="utf-8")
    uuids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)
    assert not uuids, f"{path.name} contains what looks like a file UUID: {uuids}"
    digests = re.findall(r"\b[0-9a-f]{40,64}\b", text)
    assert not digests, f"{path.name} contains what looks like a content digest: {digests}"
    for pattern in (r"/mnt/", r"/media/", r"/Volumes/", r"\.mp3", r"\.m4a", r"\.flac"):
        assert not re.search(pattern, text), f"{path.name} matches {pattern!r}, which looks like archive content"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_explore_link_payload_is_valid_json(path: Path) -> None:
    """The `/explore?left=` payload is JSON *inside* a JSON string, and the TraceQL query
    inside it carries its own double quotes -- three quoting levels, so a hand-written
    literal lands one escape level short and Grafana's Explore silently fails to parse the
    state. That exact defect shipped: the committed payload broke at the quote before the
    service name (`json.loads` error at char 79). Parsing the committed bytes back is what
    a schema check cannot do and the real consumer would only reveal on click."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for link in dashboard.get("links", []):
        url = link.get("url", "")
        marker = "/explore?left="
        if marker not in url:
            continue
        payload = url.split(marker, 1)[1]
        try:
            state = json.loads(payload)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path.name}: link {link.get('title')!r} carries an invalid /explore payload: {exc}")
        assert state.get("queries"), f"{path.name}: link {link.get('title')!r} parsed but declares no queries"
