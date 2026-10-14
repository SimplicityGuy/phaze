"""Label matchers and grouping labels in alerts/ and dashboards/ against the catalogue (phaze-5rvda).

The family-name guards (``test_alert_rules.py::test_every_referenced_metric_family_is_catalogued``,
``test_dashboards.py::test_every_metric_referenced_is_catalogued``) stop a metric RENAME. They say
nothing about a renamed label or label VALUE, and that fails the same silent way: ``phaze_x{stage="analyze"}``
against a series now labelled ``stage="analysis"`` is an EMPTY vector, not an error -- the alert goes
quiet and the panel reads as an idle system. This reuses the SAME OTLP -> Prometheus translation
(``_prometheus_translation``) to map a selector's family back to its catalogue spec.

``job`` / ``instance`` / ``le`` are not catalogue labels: ``job`` and ``instance`` are attached by the
scraper from the resource, ``le`` by the histogram translation. They are exempt by name.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest
import yaml

from phaze.telemetry.catalogue import CATALOGUE, MetricSpec
from tests.shared.telemetry._prometheus_translation import prometheus_families


REPO = Path(__file__).resolve().parents[3]
RULES_PATH = REPO / "alerts" / "phaze-alerts.yml"
DASHBOARDS = sorted((REPO / "dashboards").glob("*.json"))

#: Labels the scrape/translation attaches; never declared on a catalogue metric.
EXTERNAL_LABELS: frozenset[str] = frozenset({"job", "instance", "le"})

_SELECTOR = re.compile(r"\b(phaze_[a-z0-9_]+)\s*\{([^}]*)\}")
_MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)")


def _spec_for_family() -> dict[str, MetricSpec]:
    """Every Prometheus series name the catalogue mints -> the spec that mints it."""
    mapping: dict[str, MetricSpec] = {}
    for spec in CATALOGUE:
        for family in prometheus_families((spec,)):
            mapping[family] = spec
    return mapping


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _strings(value)]
    return []


def label_problems(expr: str, specs: dict[str, MetricSpec] | None = None) -> list[str]:
    """Every way ``expr``'s label matchers and grouping labels disagree with the catalogue."""
    specs = specs if specs is not None else _spec_for_family()
    declared_anywhere = {name for spec in CATALOGUE for name in spec.label_names}
    problems: list[str] = []
    for family, body in _SELECTOR.findall(expr):
        spec = specs.get(family)
        if spec is None:
            continue  # an unknown family is the family-name guards' finding, not this one's
        labels = {label.name: label for label in spec.labels}
        for name, op, value in _MATCHER.findall(body):
            if name in EXTERNAL_LABELS:
                continue
            if name not in labels:
                problems.append(f"{family}: label {name!r} is not declared on {spec.name} (declared: {sorted(labels)})")
                continue
            domain = labels[name].values
            if domain is None or "$" in value:
                continue
            if op in ("=", "!="):
                if value not in domain:
                    problems.append(f"{family}: {name}{op}{value!r} is not in the declared domain {sorted(domain)}")
            elif not any(re.fullmatch(value, candidate) for candidate in domain):
                problems.append(f"{family}: {name}{op}{value!r} matches no value in the declared domain {sorted(domain)}")
    for clause in _GROUPING.findall(expr):
        for name in (part.strip() for part in clause.split(",")):
            if name and name not in EXTERNAL_LABELS and name not in declared_anywhere:
                problems.append(f"grouping label {name!r} is declared on no catalogue metric")
    return problems


def _rule_exprs() -> list[str]:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return [rule["expr"] for group in document["groups"] for rule in group["rules"]]


def _dashboard_exprs(path: Path) -> list[str]:
    return [s for s in _strings(json.loads(path.read_text(encoding="utf-8"))) if "phaze_" in s]


def test_every_alert_label_matcher_is_declared_by_the_catalogue() -> None:
    problems = [p for expr in _rule_exprs() for p in label_problems(expr)]
    assert not problems, f"alerts/phaze-alerts.yml: {problems}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_dashboard_label_matcher_is_declared_by_the_catalogue(path: Path) -> None:
    problems = [p for expr in _dashboard_exprs(path) for p in label_problems(expr)]
    assert not problems, f"{path.name}: {problems}"


def test_the_guard_actually_sees_the_real_selectors() -> None:
    """A parser that finds nothing passes every file; pin that it parses the real ones."""
    count = sum(len(_SELECTOR.findall(e)) for e in _rule_exprs())
    count += sum(len(_SELECTOR.findall(e)) for path in DASHBOARDS for e in _dashboard_exprs(path))
    assert count >= 40, f"only {count} label-bearing selectors parsed; measured 50 on 2026-10-14"


@pytest.mark.parametrize(
    "expr",
    [
        'phaze_analysis_windows_total{outcome=~"analyz.*"}',
        'phaze_analysis_windows_total{outcome!="skipped"}',
        'phaze_analysis_windows_total{outcome!~"skip.*", job=~"$job"}',
        "sum by (le, outcome) (rate(phaze_analysis_run_duration_seconds_bucket[5m]))",
    ],
)
def test_regex_and_negative_matchers_on_valid_values_are_not_flagged(expr: str) -> None:
    assert label_problems(expr) == []


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ('phaze_analysis_windows_total{outcome="analysed"}', "not in the declared domain"),
        ('phaze_analysis_windows_total{outcome!="analysed"}', "not in the declared domain"),
        ('phaze_analysis_windows_total{outcome=~"analys[ei]d"}', "matches no value"),
        ('phaze_analysis_windows_total{result="analyzed"}', "not declared on"),
        ("sum by (result) (phaze_analysis_windows_total)", "declared on no catalogue metric"),
    ],
)
def test_the_guard_rejects_a_renamed_label_or_value(expr: str, fragment: str) -> None:
    problems = label_problems(expr)
    assert problems and fragment in problems[0], problems
