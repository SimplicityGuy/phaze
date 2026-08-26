"""The cardinality guard (phaze-m1drf.3 acceptance 4) and the catalogue's own invariants.

**Why this file is a build gate rather than a review checklist.** phaze does not own the
Prometheus that scrapes these metrics -- homelab does. A high-cardinality label added here
damages a SHARED system, and it does so silently: nothing fails, nothing logs, the series
count simply grows until someone else's storage is in trouble. The archive holds 11,428
files and analysis touches up to 34 models per window, so the difference between a safe
label and a catastrophic one is one identifier.

The guard has two halves and needs both. This file is the STATIC half: it reads the
catalogue and the source tree. ``instruments._checked_attributes`` is the RUNTIME half,
because an attribute assembled from a variable at a call site is invisible to any static
check -- and a value read out of a payload is exactly how a file id becomes a label.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from phaze.services.analysis_models import GENRE_MODEL, MODEL_SETS
from phaze.telemetry import instruments
from phaze.telemetry.catalogue import BY_NAME, CATALOGUE, FORBIDDEN_LABEL_SUBSTRINGS, MODEL_COMBINATIONS, total_series


SRC = Path(__file__).resolve().parents[3] / "src" / "phaze"

#: Pinned so a change to the budget is a deliberate edit to this number and shows up in a
#: diff, rather than drifting a hundred series at a time. Raising it is allowed; doing so
#: without noticing is what this pin prevents.
SERIES_CEILING = 9783


def test_every_label_states_a_finite_bound() -> None:
    """A label with no stated bound is a label nobody costed."""
    for spec in CATALOGUE:
        for label in spec.labels:
            assert label.cardinality > 0, f"{spec.name}.{label.name} has no bound"
            assert label.description.strip(), f"{spec.name}.{label.name} has no description"


def test_enumerated_labels_match_their_stated_cardinality() -> None:
    """When a label lists its values, the count and the bound must agree.

    They drift in the direction that matters: someone adds a value and leaves the number.
    """
    for spec in CATALOGUE:
        for label in spec.labels:
            if label.values is not None:
                assert len(set(label.values)) == label.cardinality, f"{spec.name}.{label.name}: {len(label.values)} values, bound {label.cardinality}"


def test_no_label_name_looks_like_an_identifier() -> None:
    """The whole point. A file id, a path or a window index must never be a label."""
    for spec in CATALOGUE:
        for label in spec.labels:
            lowered = label.name.lower()
            offending = [bad for bad in FORBIDDEN_LABEL_SUBSTRINGS if bad in lowered]
            assert not offending, f"{spec.name} declares label {label.name!r}, which matches forbidden {offending}"


def test_model_label_bound_matches_the_registry() -> None:
    """34 is not a magic number: it is ``len(MODEL_SETS) * 3 + 1``, read from the registry.

    If a twelfth characteristic set is added, this fails and the budget is recomputed --
    which is the only way the documented arithmetic in the metric catalogue stays true.
    """
    registry_models = [model for model_set in MODEL_SETS for model in model_set.models] + [GENRE_MODEL]
    assert len(registry_models) == MODEL_COMBINATIONS

    spec = BY_NAME["phaze.analysis.model.inference.duration"]
    declared = {label.name: set(label.values or ()) for label in spec.labels}
    assert {model.name for model in registry_models} <= declared["model_name"]
    assert {model.variant for model in registry_models} <= declared["model_variant"]
    assert {model.classifier_type for model in registry_models} <= declared["classifier_type"]


def test_every_instrument_is_catalogued_and_every_catalogue_entry_is_built() -> None:
    assert instruments.instrument_names() == {spec.name for spec in CATALOGUE}


def test_no_instrument_is_created_outside_the_instruments_module() -> None:
    """The chokepoint. An instrument built elsewhere would carry no catalogue entry, and
    therefore no bound -- so this is what makes the other assertions in this file total."""
    pattern = re.compile(r"\.create_(counter|histogram|gauge|up_down_counter|observable_\w+)\s*\(")
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "instruments.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"instruments must be created only in telemetry/instruments.py; found in {offenders}"


def test_every_histogram_carries_measured_buckets() -> None:
    """The SDK default ladder is 5 ms .. 10 s. This workload runs from a sub-millisecond
    graph release to a twelve-hour coarse tier, so a histogram on the default is useless at
    both ends -- every observation lands in the first or the last bucket."""
    for spec in CATALOGUE:
        if spec.kind == "histogram":
            assert spec.buckets, f"{spec.name} has no explicit bucket ladder"
            assert list(spec.buckets) == sorted(spec.buckets), f"{spec.name} bucket ladder is not ascending"


def test_the_series_ceiling_is_pinned() -> None:
    """Costed with histogram buckets counted, which is the arithmetic that gets skipped:
    a histogram is ``len(buckets) + 3`` series per label combination, not one."""
    assert total_series() == SERIES_CEILING, (
        f"the catalogue now mints up to {total_series()} series (was {SERIES_CEILING}). "
        "Update SERIES_CEILING and docs/telemetry/metric-catalogue.md together, deliberately."
    )


def test_recording_an_undeclared_attribute_raises_under_strict(strict_telemetry: None) -> None:
    """The runtime half of the guard: what a static check structurally cannot see.

    A file id reaching a label does not arrive as the literal string ``file_id`` in the
    catalogue -- it arrives as a variable at a call site. This is what catches it.
    """
    with pytest.raises(ValueError, match="undeclared attribute"):
        instruments.record("phaze.analysis.tier.duration", 1.0, tier="fine", file_id="a-real-uuid")


def test_recording_an_undeclared_attribute_drops_it_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """...and never raises when strict is off, because instrumentation may not fail the work.

    The label is dropped, the observation is kept. A wrong label is worse than a missing
    one; a raised exception in the analysis path is worse than both.
    """
    monkeypatch.delenv("PHAZE_TELEMETRY_STRICT", raising=False)
    instruments.record("phaze.analysis.tier.duration", 1.0, tier="fine", file_id="a-real-uuid")
