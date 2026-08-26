"""The CI-runnable half of the alert-rule guarantees (phaze-m1drf.5).

The other half is `promtool`, which is the real consumer of both the rules and their unit
tests (ADR-0012 rule 3) and needs a Prometheus binary CI does not have:

    docker run --rm -v "$PWD/alerts:/alerts:ro" --entrypoint /bin/promtool \
      prom/prometheus:v3.10.0 test rules /alerts/phaze-alerts.test.yml

What CI holds instead is the set of properties that are about what the rules must NOT do.
Those are the ones that decay: a rule added later, in a hurry, that fires on something the
operator has already settled (2026-08-26, bead phaze-m1drf.5 -- the drain rate) or that
bounds an analysis by wall clock (phaze-1b39) would pass every syntax check ever written and
would be exactly the regression this epic must not ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
RULES_PATH = REPO / "alerts" / "phaze-alerts.yml"
TESTS_PATH = REPO / "alerts" / "phaze-alerts.test.yml"
DOC_PATH = REPO / "docs" / "telemetry" / "alerting.md"


def _rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


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
