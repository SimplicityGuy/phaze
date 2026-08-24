"""Kubernetes ``resource.Quantity`` FORMAT validation at config load (phaze-frq98, seam F1).

**Why this module exists, and why a JSON-Schema validator is not a substitute.** Seam F1 of
``docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md`` records that
``kube_staging.build_job_manifest`` emits ``cpu_request`` / ``memory_request`` / ``memory_limit``
verbatim as free-form operator strings, checked only for truthiness, and that **only the apiserver
ever parses them as Quantities**. The obvious reflex -- validate the manifest against the real
Kubernetes schema -- does **not** close this: the Kubernetes OpenAPI schema types
``resource.Quantity`` as a plain ``string``, so ``"4GB"`` is schema-valid. Measured on the vendored
artifact this repo now validates against (``tests/vendor/kubernetes-json-schema/``, the same files
``kubeconform`` consumes): a manifest carrying ``requests.memory = "4GB"`` produces **zero** schema
errors. ``tests/analyze/services/backends/test_kube_manifest_schema.py::
test_the_schema_does_not_catch_a_bad_quantity_which_is_why_this_module_exists`` pins that fact so
nobody deletes this module believing the schema covers it.

So the check has to happen somewhere phaze controls, and **config load is the right somewhere**:
the operator who typed the value is present, the error names the field and the file, and the
failure arrives before a single Job is submitted rather than as an apiserver rejection or a
``cloud_job`` stuck in a re-drive loop.

**Scope is deliberately FORMAT ONLY -- no value is judged.** This module rejects strings the
apiserver's ``resource.ParseQuantity`` would reject. It expresses no opinion on whether a
syntactically valid quantity is a *good* quantity: ``memory_limit = "4Gi"`` and
``memory_limit = "400Gi"`` are equally acceptable here. That boundary is load-bearing --
``backends.toml`` and ADR-0005 are explicit that ``memory_limit`` is not a knob to raise casually,
and CLAUDE.md records that duration-linear memory growth is a bug rather than a sizing input. A
format validator that quietly normalised or clamped a value would be doing sizing policy under
cover of validation.

**What this therefore does NOT catch, stated plainly rather than left for someone to discover.**
``cpu_request = "500"`` -- the second typo named in seam F1, meant as ``"500m"`` -- is a
**syntactically valid Quantity** (500 whole CPUs) and passes this validator. No format check can
catch it, because the string is well-formed; only a plausibility bound could, and a plausibility
bound is a value judgement this module deliberately does not make. Likewise unmodelled: int64
overflow, and the precision loss ``ParseQuantity`` applies to very long fractions.

**The grammar**, transcribed from ``k8s.io/apimachinery/pkg/api/resource/quantity.go``::

    <quantity>        ::= <signedNumber><suffix>
    <number>          ::= <digits> | <digits>.<digits> | <digits>. | .<digits>
    <signedNumber>    ::= <number> | <sign><number>
    <suffix>          ::= <binarySI> | <decimalExponent> | <decimalSI>
    <binarySI>        ::= Ki | Mi | Gi | Ti | Pi | Ei
    <decimalSI>       ::= n | u | m | "" | k | M | G | T | P | E
    <decimalExponent> ::= ("e" | "E") <signedNumber>

Two details of that grammar are exactly where operator typos land, so note them:

- **``decimalSI`` has a lowercase ``k`` and no uppercase ``K``** (every other decimal prefix is
  uppercase). ``"4K"`` is a rejection, not a synonym for ``"4k"``.
- **There is no ``B``.** ``"4GB"`` -- the typo seam F1 names -- is rejected because ``GB`` is not a
  suffix in any of the three families; the binary form is ``"4Gi"`` and the decimal form is
  ``"4G"``.
"""

from __future__ import annotations

import re
from typing import Final


# The grammar above as one anchored pattern. Two details are load-bearing.
#
# ALTERNATION ORDER, twice, and not cosmetic: the two-character binary suffixes must precede the
# single-character decimal ones (otherwise "Ei" matches "E" and strands the "i"), and the
# decimal-exponent form must precede them too (otherwise "E5" matches "E" and strands the "5"). The
# end anchor would force a backtrack to the correct branch in both cases, but ordering makes the
# intent readable instead of accidental.
#
# `\Z`, NOT `$`. Python's `$` also matches immediately BEFORE a trailing newline, so `"3Gi\n"` --
# exactly what a heredoc-built or editor-mangled TOML value looks like -- passed the first draft of
# this pattern and would have reached the apiserver as a rejection. `\Z` matches only at the true
# end of the string. Caught by `test_kube_quantity_validation.py`'s `'3Gi\n'` case, which is there
# for this reason and should not be dropped as exotic.
_QUANTITY_RE: Final = re.compile(
    r"""
    ^
    [+-]?                                       # <sign>, optional
    (?: \d+ (?: \. \d* )? | \. \d+ )            # <number>: 1 | 1.5 | 1. | .5
    (?:                                         # <suffix>, optional (the "" of decimalSI)
        Ki | Mi | Gi | Ti | Pi | Ei             #   <binarySI>
      | [eE] [+-]? \d+                          #   <decimalExponent>
      | [numkMGTPE]                             #   <decimalSI>
    )?
    \Z
    """,
    re.VERBOSE,
)

#: The field names this repo validates as Quantities, in the order ``KubeConfig`` declares them.
#: Exported so the tests enumerate the real set rather than restating a list that can drift.
QUANTITY_FIELDS: Final[tuple[str, ...]] = ("cpu_request", "memory_request", "memory_limit")


def validate_quantity(value: str, *, field: str) -> str:
    """Return ``value`` unchanged if it is a well-formed non-negative Kubernetes Quantity; else raise.

    ``ValueError`` is the contract pydantic wants -- a ``field_validator`` raising it surfaces as a
    ``ValidationError`` naming the offending field and the ``backends.toml`` entry, which is the
    whole point of validating here rather than at admission.

    The value is returned **verbatim**, never normalised. ``"4Gi"``, ``"4096Mi"`` and ``"4.0Gi"``
    are the same quantity to the apiserver but three different strings, and rewriting one into
    another would make the deployed manifest stop matching the config the operator wrote.

    Negative quantities are rejected separately from the grammar. ``ParseQuantity`` accepts a
    leading ``-`` (a Quantity is a signed value in general), but the apiserver's own resource
    validation refuses a negative request or limit -- ``must be greater than or equal to 0`` -- so
    a negative here is a certain admission failure and gets its own message rather than a confusing
    "not a valid quantity".
    """
    if not _QUANTITY_RE.match(value):
        raise ValueError(
            f"{field}={value!r} is not a valid Kubernetes quantity. Expected a number with an optional suffix: "
            f"Ki/Mi/Gi/Ti/Pi/Ei (binary), n/u/m/k/M/G/T/P/E (decimal, note the LOWERCASE k), or an e/E exponent. "
            f"There is no 'B' suffix -- write '4Gi' (binary) or '4G' (decimal), not '4GB'; and CPU millicores are "
            f"'500m', not '500' (which means 500 whole CPUs)."
        )
    if value.startswith("-"):
        raise ValueError(f"{field}={value!r} is negative; a Kubernetes resource request or limit must be greater than or equal to 0")
    return value
