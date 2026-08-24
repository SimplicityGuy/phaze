"""Seam F1: validate ``build_job_manifest``'s output against the REAL Kubernetes Job schema.

**The gap this closes** (``docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md``, row
F1). ``build_job_manifest`` produces an artifact whose real consumer is the Kubernetes apiserver.
Nothing in this repo ever handed it to one, or to anything that knows what a Job is:

- the respx submit tests return a canned ``201`` and **never inspect the request body**;
- every other manifest test asserts on the producer's own dict -- the manifest compared to the
  thing that built it, which can only ever prove that ``build_job_manifest`` is deterministic;
- there was **no** ``kubeconform`` / ``kubeval`` / ``kubectl --dry-run`` anywhere in the repo.

This module is the missing consumer-side check, and it is the general form of the lesson ADR-0012
rule 3 states: *verify with the artifact's real consumer, not the tool that produced it.*

**The validator, and why it is this one.** The manifest is validated against
``tests/vendor/kubernetes-json-schema/v1.32.0-standalone-strict/job-batch-v1.json`` -- a verbatim
copy of the upstream ``yannh/kubernetes-json-schema`` artifact, which is generated from the
Kubernetes OpenAPI specification. Three properties earn it the job: it is **not phaze** (nothing in
this repo authored it, so it cannot agree with a producer bug), it is **offline** (no cluster, no
network, deterministic in CI), and ``standalone-strict`` means every ``$ref`` is inlined and every
object carries ``additionalProperties: false`` -- so a **typo'd field name is a failure**, not a
silently-ignored key. Alternatives, and why not:

- **kubeconform** -- the obvious pick, and it was rejected on the measurement below rather than on
  taste. kubeconform *is* a JSON-Schema validator over exactly these files (``-strict`` is exactly
  the ``standalone-strict`` variant used here), so a Go binary in CI **and** on every dev machine
  buys nothing the schema does not already carry, and it would still need the schemas vendored for
  offline determinism. The in-process form costs a pure-Python library that this repo **already
  resolves transitively** (``litellm`` depends on ``jsonschema``), so the environment gains zero
  packages and the dev-side setup step is the ``uv sync`` that already runs.
- **kubectl --dry-run=server** -- strictly stronger (it reaches admission and it is the only thing
  that parses Quantities), and unavailable: CI has no apiserver.
- **kubectl --dry-run=client** -- weaker than the schema for the same dependency cost.

**What this does NOT cover, stated rather than left to be discovered.** A JSON Schema cannot catch
a bad Quantity: Kubernetes types ``resource.Quantity`` as a plain ``string``, so ``"4GB"`` is
schema-valid here and would be schema-valid under kubeconform too. That is measured below, in
``test_the_schema_does_not_catch_a_bad_quantity_which_is_why_this_module_exists``, and it is why
``services/k8s_quantity.py`` validates those three fields at config load instead. Nor does a schema
reach Kueue admission or the kubelet -- ``suspend: true`` plus a queue-name label is structurally
valid regardless of whether the named LocalQueue exists.

**Why v1.32.0.** It is the version at which every field the manifest emits is GA --
``podReplacementPolicy`` was beta-on-by-default in 1.29 and went GA in 1.32 -- so validating there
proves the manifest is accepted by a cluster new enough to honour every field it sets. Re-verify
the vendored copy at any time with::

    curl -sSL https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/v1.32.0-standalone-strict/job-batch-v1.json | shasum -a 256
    # 8460596a5425451a6d2763419d819a1ace94b9a89d2745cc4c0fe3bbbdc9cb59
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

from jsonschema import Draft7Validator
import pytest

from phaze.config_backends import KubeConfig
from phaze.services import kube_staging


if TYPE_CHECKING:
    from collections.abc import Iterator


# tests/analyze/services/backends/<this file> -> parents[3] is tests/.
_SCHEMA_PATH = Path(__file__).parents[3] / "vendor" / "kubernetes-json-schema" / "v1.32.0-standalone-strict" / "job-batch-v1.json"

# The upstream artifact declares the version-less `http://json-schema.org/schema#`, which
# `validator_for` resolves to whatever jsonschema currently considers newest. Pin draft-07
# explicitly: these files are generated in draft-07 shape, and letting the resolution float would
# make a jsonschema upgrade silently change what this suite validates.
_VALIDATOR = Draft7Validator(json.loads(_SCHEMA_PATH.read_text()))


def _kube(**overrides: object) -> KubeConfig:
    """A fully-configured KubeConfig; ``overrides`` name KubeConfig fields."""
    fields: dict[str, object] = {
        "api_url": "https://kueue-a.mesh:6443",
        "namespace": "phaze",
        "local_queue": "phaze-lq",
        "job_image": "phaze/job-runner:test",
        # The values docs/k8s-burst.md documents for the 4-physical-core burst node, so the schema
        # check runs against the shape a real deployment submits rather than a rounder fixture.
        "cpu_request": "1500m",
        "memory_request": "3Gi",
    }
    fields.update(overrides)
    return KubeConfig(**fields)


def _errors(manifest: dict[str, Any]) -> list[str]:
    """Every schema violation in ``manifest``, as ``path: message`` strings for a readable failure."""
    return [f"{'.'.join(str(p) for p in err.absolute_path)}: {err.message}" for err in _VALIDATOR.iter_errors(manifest)]


# Every combination of the three optional, backward-compatibility-guarded knobs. Each one adds a
# DIFFERENT structural region to the manifest -- `models_pvc_name` a volume plus a volumeMount,
# `memory_limit` a `resources.limits` map, `active_deadline_seconds` a top-level spec field -- so
# validating only the default form would leave the three additive paths unvalidated, which is
# precisely where a hand-built dict tends to go wrong.
_OPTIONAL_KNOBS: list[dict[str, object]] = [
    {},
    {"models_pvc_name": "phaze-models"},
    {"memory_limit": "4Gi"},
    {"active_deadline_seconds": 10800},
    {"models_pvc_name": "phaze-models", "memory_limit": "4Gi", "active_deadline_seconds": 10800},
]


@pytest.fixture
def manifest() -> dict[str, Any]:
    """The default-form manifest, for the negative controls to mutate."""
    return kube_staging.build_job_manifest(uuid.uuid4(), _kube())


@pytest.mark.parametrize("knobs", _OPTIONAL_KNOBS, ids=lambda k: "+".join(sorted(k)) or "defaults")
def test_the_manifest_validates_against_the_real_kubernetes_job_schema(knobs: dict[str, object]) -> None:
    """Every form phaze can emit is a structurally valid ``batch/v1`` Job.

    This is the assertion the F1 seam had none of: the manifest checked against an artifact phaze
    did not write, rather than against the producer that wrote it.
    """
    assert _errors(kube_staging.build_job_manifest(uuid.uuid4(), _kube(**knobs))) == []


def test_the_schema_is_strict_enough_to_catch_a_typod_field_name(manifest: dict[str, Any]) -> None:
    """A misspelled field is a REJECTION, not a silently-ignored key.

    This is the property that makes ``standalone-strict`` worth vendoring over the permissive
    variant, and the property a hand-rolled "does it have the keys I expect" assertion can never
    have: it fails on keys nobody thought to look for. ``suspendd: true`` is a Job that would be
    admitted and would then start a pod immediately -- exactly the KSUBMIT-01 invariant
    ``suspend: true`` exists to hold.
    """
    manifest["spec"]["suspendd"] = manifest["spec"].pop("suspend")
    assert _errors(manifest) != []


def test_the_schema_catches_a_wrong_field_type(manifest: dict[str, Any]) -> None:
    """A stringly-typed ``backoffLimit`` fails, where phaze's own dict comparisons would not notice."""
    manifest["spec"]["backoffLimit"] = "0"
    assert any("backoffLimit" in err for err in _errors(manifest))


def test_the_schema_catches_a_missing_required_container_field(manifest: dict[str, Any]) -> None:
    """A container without ``name`` fails -- the schema knows the required set; phaze never asserted it."""
    del manifest["spec"]["template"]["spec"]["containers"][0]["name"]
    assert _errors(manifest) != []


def test_the_schema_does_not_catch_a_bad_quantity_which_is_why_this_module_exists() -> None:
    """**The measurement that chose the validator**, pinned so the reasoning cannot rot.

    Kubernetes types ``resource.Quantity`` as a plain ``string``, so a manifest carrying
    ``requests.memory = "4GB"`` is schema-valid -- here, and equally under ``kubeconform``, which
    reads this same file. Only ``resource.ParseQuantity`` in the apiserver rejects it, and CI has no
    apiserver.

    Two conclusions follow, and both are load-bearing:

    1. Adding a Go binary would NOT have closed the Quantity half of seam F1. That is what made the
       in-process validator the cheaper choice at equal power, rather than a shortcut.
    2. ``services/k8s_quantity.py`` is not redundant with this module and must not be deleted as
       such. It is the only thing in the repo that rejects ``"4GB"``, and it does so at config load
       where the operator can still see it --
       ``tests/shared/config/test_kube_quantity_validation.py`` is the paired proof.

    The manifest is built through ``build_job_manifest`` and then mutated, deliberately: a
    ``KubeConfig(memory_request="4GB")`` no longer *constructs*, which is the fix working. This
    reaches past it to characterise the schema itself.
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] = "4GB"
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "1500 m"

    assert _errors(manifest) == []

    from phaze.services.k8s_quantity import validate_quantity

    with pytest.raises(ValueError, match="not a valid Kubernetes quantity"):
        validate_quantity("4GB", field="memory_request")


def test_the_vendored_schema_is_the_batch_v1_job_schema() -> None:
    """Guard the vendored artifact's identity, so a wrong-file swap fails here and not obscurely.

    A schema that validates *nothing* (an empty object, a 404 page saved as JSON) would make every
    assertion above pass vacuously -- the ``exit 0 having measured nothing`` failure mode CLAUDE.md
    names for ``phaze-jnj90`` / ``phaze-nqawu``. Checking the group-version-kind stamp costs one
    assertion and removes that whole class.
    """
    schema = json.loads(_SCHEMA_PATH.read_text())
    assert schema["x-kubernetes-group-version-kind"] == [{"group": "batch", "kind": "Job", "version": "v1"}]
    assert schema["additionalProperties"] is False


def _walk_container_specs(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from manifest["spec"]["template"]["spec"]["containers"]


def test_memory_limit_reaches_the_schema_as_limits_not_requests() -> None:
    """ADR-0005's shape survives schema validation: a memory-only limit, requests untouched.

    Pins the ADR-0005 invariant at the schema layer rather than only against the producer dict --
    ``requests`` is Kueue's quota input and must not acquire the limit's value, and no CPU limit may
    appear (a memory-only limit keeps the pod QoS class Burstable, not Guaranteed).
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(memory_limit="4Gi"))
    assert _errors(manifest) == []
    resources = next(_walk_container_specs(manifest))["resources"]
    assert resources["limits"] == {"memory": "4Gi"}
    assert resources["requests"] == {"cpu": "1500m", "memory": "3Gi"}


def test_a_manifest_that_lost_its_apiversion_fails_the_schema(manifest: dict[str, Any]) -> None:
    """The schema pins ``apiVersion``/``kind`` themselves, which no producer-side assertion re-derives."""
    broken = copy.deepcopy(manifest)
    broken["apiVersion"] = "batch/v2"
    assert _errors(broken) != []
