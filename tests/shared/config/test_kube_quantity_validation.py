"""Seam F1, Quantity half: ``cpu_request`` / ``memory_request`` / ``memory_limit`` are parsed at config load.

**The gap this closes.** These three fields reach ``build_job_manifest`` and are copied verbatim
into the Job's ``resources.requests`` / ``resources.limits``. Before ``services/k8s_quantity.py``
they were checked only for truthiness, so the typo seam F1 names -- ``"4GB"`` where ``"4Gi"`` was
meant -- was accepted by phaze, asserted on by phaze's own tests, and rejected only by a live
cluster, an S3 stage and a submit round-trip after the operator typed it.

**Why this is not covered by the manifest schema check**, and therefore why it is a second
mechanism rather than a duplicate: Kubernetes types ``resource.Quantity`` as a plain ``string`` in
its OpenAPI schema, so ``"4GB"`` is schema-valid. ``kubeconform`` reads that same schema and would
also pass it. Measured, and pinned, in
``tests/analyze/services/backends/test_kube_manifest_schema.py::
test_the_schema_does_not_catch_a_bad_quantity_which_is_why_this_module_exists``.

**FORMAT ONLY.** Nothing here judges whether a valid quantity is a *good* quantity, and in
particular nothing raises, lowers, defaults or normalises ``memory_limit`` -- ``backends.toml`` and
ADR-0005 are explicit that it is not a knob to change casually, and CLAUDE.md records that
duration-linear memory growth is a bug rather than a sizing input. The valid cases below assert the
string comes back **byte-identical**, which is what keeps a "validator" from quietly becoming a
rewriter.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from phaze.config_backends import KubeConfig
from phaze.services.k8s_quantity import QUANTITY_FIELDS, validate_quantity


# Every rejection carries the reason it is a rejection, because the grammar's edges are exactly
# where operator typos land and a bare list would not survive its first "surely that's valid?".
_INVALID = [
    ("4GB", "seam F1's named typo: there is no 'B' suffix in any of the three families"),
    ("4Gb", "same, lowercased"),
    ("4K", "decimalSI has a LOWERCASE k and no uppercase K -- not a synonym"),
    ("4gi", "binarySI is 'Gi'; suffixes are case-sensitive"),
    ("4Gib", "'Gi' is the whole suffix; the trailing 'b' is not optional decoration"),
    ("1500 m", "no internal whitespace"),
    (" 1500m", "no leading whitespace"),
    ("1500m ", "no trailing whitespace"),
    ("", "empty is not a quantity -- previously indistinguishable from 'unset' under a truthiness check"),
    ("Gi", "a suffix with no number"),
    ("4Gi4", "trailing junk after the suffix"),
    ("1e", "decimalExponent requires digits; bare 'e' is not decimalSI (only uppercase 'E' is)"),
    ("1.2.3", "one decimal point"),
    ("--1", "one sign"),
    ("4Mi Gi", "two suffixes"),
    ("four", "not a number at all"),
    ("3Gi\n", "a stray newline from a heredoc-built TOML value"),
]

# The corresponding acceptances, INCLUDING the values this repo's own docs and fixtures use, so a
# grammar tightened past reality fails here rather than in a deployment.
_VALID = [
    "1500m",  # docs/k8s-burst.md: the burst node's cpu_request
    "3Gi",  # docs/k8s-burst.md: the burst node's memory_request
    "4Gi",  # docs/k8s-burst.md + ADR-0005: the burst node's memory_limit
    "2",  # the existing test fixtures' cpu_request -- a bare number is 2 whole CPUs
    "0",  # zero is a legal quantity; only NEGATIVE is refused
    "4G",  # decimalSI: 4 * 10^9, not the same number as 4Gi but a legal string
    "4k",  # lowercase k, the one decimalSI prefix that is not uppercase
    "1.5Gi",
    ".5Gi",
    "5.",
    "1e3",
    "1E3",
    "1E",  # bare 'E' is decimalSI (exa), distinct from the 'E<digits>' exponent above it
    "4Ei",
    "100n",
    "2u",
    "+4Gi",
]


@pytest.mark.parametrize("value, why", _INVALID, ids=[repr(v) for v, _ in _INVALID])
@pytest.mark.parametrize("field", QUANTITY_FIELDS)
def test_a_malformed_quantity_is_rejected_at_config_load(field: str, value: str, why: str) -> None:
    """A ``backends.toml`` carrying a malformed quantity refuses to LOAD, naming the field.

    This is the acceptance criterion of seam F1's Quantity half: ``"4GB"`` is rejected where today
    it is accepted. Asserted through ``KubeConfig`` rather than through ``validate_quantity``
    directly, because the claim is about **config load** -- the operator-visible boundary -- not
    about a helper function.
    """
    with pytest.raises(ValidationError, match="not a valid Kubernetes quantity") as exc:
        KubeConfig(**{field: value})
    assert field in str(exc.value), why


@pytest.mark.parametrize("value", _VALID)
@pytest.mark.parametrize("field", QUANTITY_FIELDS)
def test_a_valid_quantity_loads_and_is_returned_byte_identical(field: str, value: str) -> None:
    """A well-formed quantity loads, and comes back exactly as written.

    The byte-identity half is the guard against a validator drifting into a normaliser: ``"4Gi"``,
    ``"4096Mi"`` and ``"4.0Gi"`` are one quantity to the apiserver and three different strings, and
    rewriting between them would make the submitted manifest stop matching the config the operator
    wrote.
    """
    assert getattr(KubeConfig(**{field: value}), field) == value


@pytest.mark.parametrize("field", QUANTITY_FIELDS)
def test_a_negative_quantity_is_rejected_with_its_own_message(field: str) -> None:
    """``-1Gi`` parses as a Quantity but is refused: a request or limit must be >= 0.

    ``resource.ParseQuantity`` accepts a leading sign (a Quantity is signed in general), so the
    grammar alone would let this through; the apiserver's resource validation then refuses it with
    ``must be greater than or equal to 0``. Rejecting it here, with a message that says *negative*
    rather than *malformed*, is the difference between an operator finding the minus sign and an
    operator re-reading the suffix table.
    """
    with pytest.raises(ValidationError, match="negative"):
        KubeConfig(**{field: "-1Gi"})


@pytest.mark.parametrize("field", QUANTITY_FIELDS)
def test_none_still_means_unset_and_is_not_promoted_to_required(field: str) -> None:
    """Validating the FORMAT of these fields must not make them mandatory.

    All three are ``Optional`` and their unset-ness is load-bearing: ``memory_limit = None`` means
    "emit no ``limits`` key at all" (ADR-0005, byte-identical to the pre-ADR manifest), and an unset
    ``cpu_request`` / ``memory_request`` is caught later, BY NAME, in ``build_job_manifest``'s
    fail-loud check -- which produces a better message than a validator here could, because it names
    the backend entry. A field validator that rejected ``None`` would silently move that error and
    break every deployment that leaves ``memory_limit`` unset, which is the documented default.
    """
    assert getattr(KubeConfig(**{field: None}), field) is None


def test_the_validator_covers_exactly_the_fields_that_reach_the_manifest() -> None:
    """``QUANTITY_FIELDS`` is the full set of ``KubeConfig`` values that land in ``resources``.

    A fourth quantity field added to ``KubeConfig`` and not added here would be unvalidated with
    nothing to say so. This pins the list against the model rather than against itself.
    """
    assert set(QUANTITY_FIELDS) == {"cpu_request", "memory_request", "memory_limit"}
    assert set(QUANTITY_FIELDS) <= set(KubeConfig.model_fields)


def test_a_syntactically_valid_but_wrong_cpu_value_is_NOT_caught_and_that_is_stated() -> None:
    """``cpu_request = "500"`` -- seam F1's second named typo -- passes, and this pins WHY.

    ``"500"`` meant as ``"500m"`` is a **well-formed** Quantity: 500 whole CPUs. No format check can
    reject it, and the only thing that could is a plausibility bound -- a value judgement this
    validator deliberately does not make, on the same reasoning that keeps it away from
    ``memory_limit``.

    Recorded as an executable statement rather than a caveat in prose so that nobody later reads the
    bead's "an operator typo -- '4GB' instead of '4Gi', '500' instead of '500m'" and concludes both
    halves shipped. One did. The other is a Kueue quota rejection at admission (500 CPUs against a
    ``nominalQuota`` of 6), which is loud, immediate, and out of this validator's reach.
    """
    assert validate_quantity("500", field="cpu_request") == "500"
    assert KubeConfig(cpu_request="500").cpu_request == "500"
