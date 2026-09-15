"""The properties that are cheap in plain Python, plus the catalogue cross-check (phaze-m1drf.5,
phaze-jjjy8).

The other half is `promtool`, the real consumer of both the rules and their unit tests
(``docs/design/0012-verification-fidelity-and-operator-attribution.md`` rule 3):

    docker run --rm -v "$PWD/alerts:/alerts:ro" --entrypoint /bin/promtool \
      prom/prometheus:v3.10.0 test rules /alerts/phaze-alerts.test.yml

phaze-jjjy8 wired that into a gate rather than leaving it a reproduce-by-hand command: `just
alerts-test` runs it (loudly skipping when `promtool` is not on the local PATH) and is a
dependency of `just check-all`; `.github/workflows/code-quality.yml` installs the same
v3.10.0 `promtool` binary and always runs it for real, so CI never merely skips.

What THIS file holds is the set of properties that are cheap to check without a Prometheus
binary at all: what the rules must NOT do (a rule added later, in a hurry, that fires on
something the operator has already settled -- 2026-08-26, bead phaze-m1drf.5, the drain rate
-- or that bounds an analysis by wall clock, phaze-1b39), and, since phaze-jjjy8, that every
`phaze_*` family the rules reference is one the catalogue actually mints -- the alert-side
half of the metric-rename guard that only the dashboards had before (see
`test_every_referenced_metric_family_is_catalogued` below).
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pytest
import yaml

from tests.shared.telemetry._prometheus_translation import prometheus_families


REPO = Path(__file__).resolve().parents[3]
RULES_PATH = REPO / "alerts" / "phaze-alerts.yml"
TESTS_PATH = REPO / "alerts" / "phaze-alerts.test.yml"
DOC_PATH = REPO / "docs" / "telemetry" / "alerting.md"


def _rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


def test_every_referenced_metric_family_is_catalogued() -> None:
    """phaze-jjjy8 acceptance 1: renaming a catalogued metric must redden this half too.

    Before this test, only the dashboards half
    (``test_dashboards.py::test_every_metric_referenced_is_catalogued``) caught a metric
    rename. These alert exprs would have gone on querying the dead family with green CI --
    silently, because an unknown label produces an EMPTY vector, not an error, so the pager
    goes permanently quiet on exactly the family a rename just retired. Reuses the SAME
    (deduplicated, phaze-aa07i item 3) OTLP -> Prometheus translation the dashboards test
    uses, so the two guards cannot quietly drift apart from each other.
    """
    families = prometheus_families()
    text = RULES_PATH.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bphaze_[a-z0-9_]+", text))
    unknown = sorted(referenced - families)
    assert not unknown, f"alerts/phaze-alerts.yml references metric(s) the catalogue does not define: {unknown}"


def test_the_rules_parse_and_are_all_alerts() -> None:
    rules = _rules()
    assert rules, "no alert rules are defined"
    for rule in rules:
        assert "alert" in rule, f"non-alert rule in a file of alert rules: {rule}"
        assert rule.get("expr", "").strip()
        assert rule.get("for"), f"{rule['alert']} has no `for`; a single scrape blip would page"


def test_no_rule_fires_on_backlog_depth() -> None:
    """OPERATOR DECISION 2026-08-26, bead phaze-m1drf.5: asked how the 8,079-row awaiting
    backlog should be handled, the operator chose the option labelled "Accept the drain rate"
    (durable record: repowise decision e1e3374e; the question as put is quoted in
    docs/telemetry/alerting.md). Backlog DEPTH is therefore not a fault condition.

    An alert that fires on a settled decision trains the operator to ignore alerts, which is
    worse than having none. `phaze_pipeline_backlog` is also POLL-DRIVEN -- it is sampled by
    the admin UI's own /pipeline/stats read and goes stale when no tab is open -- so it is
    doubly unfit as an alert source, and this test forbids it on both counts.
    """
    for rule in _rules():
        assert "phaze_pipeline_backlog" not in rule["expr"], (
            f"{rule['alert']} alerts on backlog depth. The drain rate is a settled operator decision "
            "(repowise decision e1e3374e), and the backlog gauge is poll-driven and goes stale."
        )


def test_no_rule_bounds_an_analysis_by_wall_clock() -> None:
    """phaze-1b39 is the incident where a wall-clock bound SIGTERM'd legitimate 2-6 hour
    analyses and stalled the whole burst lane.

    A multi-hour concert set is EXPECTED to take hours; liveness is progress-based
    (`analysis_stall_timeout_sec`), never elapsed-based. A rule that says "an analysis has
    been running too long" would re-introduce that judgement at the monitoring layer, where
    it would page instead of kill -- still wrong, and still trained on the same false
    premise.
    """
    for rule in _rules():
        expr = rule["expr"]
        assert "phaze_analysis_run_duration_seconds_sum" not in expr or "audio_duration" in expr, (
            f"{rule['alert']} appears to threshold on analysis DURATION. phaze-1b39: a multi-hour set is "
            "expected to take hours. A duration used as a RATIO against audio seconds is fine; a bound is not."
        )


def test_every_rule_carries_a_runbook_pointer() -> None:
    """An alert whose reasoning is not written down is an alert that gets silenced."""
    for rule in _rules():
        annotations = rule.get("annotations", {})
        assert annotations.get("summary"), f"{rule['alert']} has no summary"
        assert annotations.get("description"), f"{rule['alert']} has no description"
        runbook = annotations.get("runbook", "")
        assert runbook.startswith("docs/telemetry/alerting.md#"), f"{rule['alert']} has no runbook pointer"


def test_every_runbook_anchor_resolves() -> None:
    """A dangling runbook link is discovered at 3am, by the person it was written for."""
    doc = DOC_PATH.read_text(encoding="utf-8").lower()
    for rule in _rules():
        anchor = rule["annotations"]["runbook"].split("#", 1)[1]
        assert f"## {anchor}" in doc.replace(" ", "") or anchor in doc.replace(" ", "").replace("`", ""), (
            f"{rule['alert']}'s runbook anchor #{anchor} is not in docs/telemetry/alerting.md"
        )


@pytest.mark.parametrize(
    ("alert", "citation"),
    [
        ("PhazeAnalysisProgressStalled", "1800"),
        ("PhazeAnalysisFailureRateElevated", "4,383"),
        ("PhazeAnalysisChunkPeakRssApproachingLimit", "4Gi"),
    ],
)
def test_every_threshold_cites_its_measured_baseline(alert: str, citation: str) -> None:
    """phaze-m1drf.5 acceptance 3: *every threshold traces to a measured baseline, not to a
    round number.* The citation has to be IN the file, next to the number it justifies --
    a rationale that lives only in a bead comment is one nobody reading the rule will find.
    """
    text = RULES_PATH.read_text(encoding="utf-8")
    block_start = text.index(f"alert: {alert}")
    block_end = text.find("- alert:", block_start + 1)
    block = text[block_start : block_end if block_end != -1 else len(text)]
    assert citation in block, f"{alert}'s threshold does not cite its measured baseline ({citation!r})"


def test_the_promtool_unit_tests_cover_the_accepted_drain_rate() -> None:
    """Acceptance 2 is a claim about what the rules do NOT do, so it needs a test that
    builds the condition and watches nothing fire. This asserts that test EXISTS; promtool
    is what runs it."""
    document = yaml.safe_load(TESTS_PATH.read_text(encoding="utf-8"))
    names = {test["name"] for test in document["tests"]}
    assert "the accepted drain rate is not a fault" in names
    drain_test = next(test for test in document["tests"] if test["name"] == "the accepted drain rate is not a fault")
    covered = {case["alertname"] for case in drain_test["alert_rule_test"]}
    defined = {rule["alert"] for rule in _rules()}
    assert covered == defined, f"the drain-rate test does not cover every rule: missing {sorted(defined - covered)}"
