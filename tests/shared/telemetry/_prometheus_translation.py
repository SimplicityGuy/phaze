"""The OTLP -> Prometheus name translation, kept in exactly ONE place.

Extracted from ``test_dashboards.py`` and ``test_metric_catalogue.py``, which each carried
their own copy of this arithmetic (phaze-aa07i item 3). The translation is fragile enough
that it is measured against a live collector rather than derived from the naming rules on
paper -- see ``docs/telemetry/metric-catalogue.md`` section 5, and the two defects that
measurement caught that reading the spec would have missed. Fragile, measured logic
duplicated across files is exactly the shape that silently drifts, so it lives here once.
``test_alert_rules.py`` (phaze-jjjy8) reuses :func:`prometheus_families` for the same reason,
so a third copy never gets written.
"""

from __future__ import annotations

from phaze.telemetry.catalogue import CATALOGUE, MetricSpec


def prometheus_base_name(spec: MetricSpec) -> str:
    """The family's base name -- before a histogram's ``_bucket``/``_sum``/``_count`` or a
    counter's ``_total`` suffix is applied."""
    base = "phaze_" + spec.name.removeprefix("phaze.").replace(".", "_")
    if spec.unit == "s":
        base += "_seconds"
    elif spec.unit == "By":
        base += "_bytes"
    return base


def prometheus_family(spec: MetricSpec) -> str:
    """The ONE family name a non-histogram metric produces.

    For a histogram this is the *base* name only -- the doc-sync check this feeds
    (``test_the_documented_catalogue_lists_every_metric``) looks for the base name in prose,
    not for the sub-metric suffixes. Use :func:`prometheus_families` for every series name a
    histogram actually mints.
    """
    base = prometheus_base_name(spec)
    return f"{base}_total" if spec.kind == "counter" else base


def prometheus_families(catalogue: tuple[MetricSpec, ...] = CATALOGUE) -> set[str]:
    """Every Prometheus family name ``catalogue`` can produce, histogram sub-metrics included."""
    families: set[str] = set()
    for spec in catalogue:
        base = prometheus_base_name(spec)
        if spec.kind == "histogram":
            families.update({base, f"{base}_bucket", f"{base}_sum", f"{base}_count"})
        elif spec.kind == "counter":
            families.add(f"{base}_total")
        else:
            families.add(base)
    return families
