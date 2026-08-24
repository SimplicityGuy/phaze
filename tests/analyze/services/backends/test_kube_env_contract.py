"""Seam F2: the ``envFrom`` env contract, asserted instead of hoped for.

**The gap this closes** (``docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md``, row
F2). ``build_job_manifest`` references the agent-env ConfigMap and the token Secret **by name
only**; the pod's ``job_runner.run`` then reads those keys back out of ``os.environ``. phaze never
enumerated the keys at either end, so **the seam was not crossed by anything** -- no proxy, no mock,
nothing. A ConfigMap missing ``PHAZE_AGENT_API_URL`` was invisible to every test in the repo and
surfaced only as a pod exiting 20, one S3 stage, one submit and one Kueue admission later.

**Which artifact is the consumer-side twin, and why it is the docs and not a fixture.** The
ConfigMap and the Secret are operator-created -- phaze creates neither, and that is deliberate
(``docs/k8s-burst.md`` §5/§6). So the artifact an operator actually applies is the YAML **in the
runbook**, and that is what this module parses and asserts against. A fixture restating the keys
here would prove only that this file agrees with itself, which is precisely the shape ADR-0012
rule 3 names: *verify with the artifact's real consumer, not the tool that produced it.* Parsing
the runbook means a runbook edit that drops a key fails the build.

**The other half is the real consumer, not a re-listing of it.** ``test_the_documented_objects_are
_exactly_what_agentsettings_needs`` loads the documented env into the process environment and calls
``phaze.config.get_settings()`` -- the same call ``job_runner._load_config_step`` makes, reaching
the same ``_enforce_required_agent_fields`` that produces the exit 20. The assertion is therefore
about the pod's startup, not about a set literal.

**The ``/models`` invariant binds three participants, not two.** ``build_job_manifest``'s
docstring predicted a drift between the ``/models`` mountPath and the ConfigMap's
``PHAZE_MODELS_DIR`` and shipped no test for it. Enumerating the contract turned up a third participant the comment did not know about:
``job_runner.run`` reads ``PHAZE_MODELS_DIR`` **or falls back to ``cfg.models_path``**, whose default
is also ``/models``. The ConfigMap key is therefore *soft*-required -- omit it and the fallback
silently agrees -- which means a change to ``models_path`` alone would mount the PVC where nothing
looks for it on any deployment that left the key out, with a green suite and a correct-looking
manifest. All three are compared below.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import uuid

import pytest
import yaml

from phaze.config import AgentSettings, get_settings
from phaze.config_backends import KubeConfig
from phaze.services import kube_staging


_RUNBOOK = Path(__file__).parents[3].parent / "docs" / "k8s-burst.md"

# Fenced ```yaml blocks in the runbook. The runbook also carries ```bash and ```toml fences, so the
# language tag is part of the match; `re.DOTALL` on a non-greedy body stops at the first closing
# fence rather than swallowing the rest of the document.
_YAML_FENCE = re.compile(r"^```yaml\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _runbook_object(kind: str, name: str) -> dict[str, Any]:
    """Return the one documented ``kind``/``name`` object from ``docs/k8s-burst.md``.

    Fails loudly on zero matches or on more than one: either means the runbook moved and the
    contract below would otherwise be asserted against the wrong object -- or, worse, against
    nothing at all while still reporting green.
    """
    found = [
        doc
        for block in _YAML_FENCE.findall(_RUNBOOK.read_text(encoding="utf-8"))
        # A fence may hold several documents, and several fences hold commented-out alternatives
        # that parse to None -- filter to real mappings before reading their keys.
        for doc in yaml.safe_load_all(block)
        if isinstance(doc, dict) and doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(found) == 1, f"expected exactly one documented {kind}/{name} in {_RUNBOOK.name}, found {len(found)}"
    return found[0]


def _documented_configmap_keys() -> frozenset[str]:
    return frozenset(_runbook_object("ConfigMap", "phaze-agent-env")["data"])


def _documented_secret_keys() -> frozenset[str]:
    # `stringData` is the plaintext-at-apply form the runbook uses; `data` would be base64.
    return frozenset(_runbook_object("Secret", "phaze-agent-token")["stringData"])


def _kube(**overrides: object) -> KubeConfig:
    fields: dict[str, object] = {
        "api_url": "https://kueue-a.mesh:6443",
        "namespace": "phaze",
        "local_queue": "phaze-lq",
        "job_image": "phaze/job-runner:test",
        "cpu_request": "1500m",
        "memory_request": "3Gi",
    }
    fields.update(overrides)
    return KubeConfig(**fields)


# --------------------------------------------------------------------------- #
# The contract, against the objects the operator actually applies
# --------------------------------------------------------------------------- #


def test_the_documented_configmap_supplies_every_key_the_contract_requires() -> None:
    """The runbook's ConfigMap carries exactly ``JOB_ENV_FROM_CONFIGMAP`` -- no more, no less.

    Equality rather than a superset check, in both directions and on purpose:

    - a **missing** key is the F2 failure itself (pod exits 20 on
      ``_enforce_required_agent_fields``, or on the ``isinstance`` guard for ``PHAZE_ROLE``);
    - an **extra** key is not harmless either. ``docs/k8s-burst.md`` §6 records two traps that are
      extra keys: ``PHAZE_AGENT_KIND`` here would be silent dead weight (``env`` overrides
      ``envFrom`` of the same name), and ``TF_NUM_INTRAOP_THREADS`` and friends would *take effect*,
      silently replacing the host-derived thread sizing with one node's numbers on every burst node
      the ConfigMap is copied to.
    """
    assert _documented_configmap_keys() == kube_staging.JOB_ENV_FROM_CONFIGMAP


def test_the_documented_secret_supplies_every_key_the_contract_requires() -> None:
    """The runbook's §5 Secret carries exactly ``JOB_ENV_FROM_SECRET``.

    The key name is the whole assertion: ``envFrom.secretRef`` injects each Secret key **under its
    own name**, so the Secret key must be ``PHAZE_AGENT_TOKEN`` -- the name
    ``AgentSettings.agent_token`` reads via its ``AliasChoices``.
    """
    assert _documented_secret_keys() == kube_staging.JOB_ENV_FROM_SECRET


def test_the_manifest_names_the_objects_the_runbook_documents() -> None:
    """``KubeConfig``'s defaults and the runbook's ``metadata.name``s are the same two strings.

    The contract above is worthless if the manifest points ``envFrom`` at objects that do not exist:
    an unresolvable ConfigMap/Secret ref is a ``CreateContainerConfigError``, already in
    ``DEAD_BEFORE_START_WAITING_REASONS``, but nothing checked that the DEFAULTS agree with the
    runbook that tells the operator what to create.
    """
    kube = _kube()
    assert kube.env_configmap_name == _runbook_object("ConfigMap", "phaze-agent-env")["metadata"]["name"]
    assert kube.env_secret_name == _runbook_object("Secret", "phaze-agent-token")["metadata"]["name"]

    env_from = kube_staging.build_job_manifest(uuid.uuid4(), kube)["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert env_from == [
        {"configMapRef": {"name": kube.env_configmap_name}},
        {"secretRef": {"name": kube.env_secret_name}},
    ]


def test_the_code_injected_env_is_exactly_the_contract_says_it_is() -> None:
    """``build_job_manifest``'s literal ``env`` names ``JOB_ENV_CODE_INJECTED``, and no operator key overlaps it."""
    env = kube_staging.build_job_manifest(uuid.uuid4(), _kube())["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {entry["name"] for entry in env} == kube_staging.JOB_ENV_CODE_INJECTED

    # `env` wins over `envFrom` of the same name, so an overlap would be an operator key that
    # silently does nothing -- the confusing-dead-weight case docs/k8s-burst.md §6 warns about.
    assert not kube_staging.JOB_ENV_CODE_INJECTED & (_documented_configmap_keys() | _documented_secret_keys())


# --------------------------------------------------------------------------- #
# The real consumer: what the pod's own startup does with that env
# --------------------------------------------------------------------------- #


def _apply_documented_pod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the pod's full environment -- ConfigMap + Secret + code-injected -- into ``os.environ``.

    Values come from the runbook for the operator-supplied halves (so the documented example is what
    is exercised, placeholders and all -- every required field is checked for truthiness, not shape)
    and from the manifest itself for the code-injected half.
    """
    configmap = _runbook_object("ConfigMap", "phaze-agent-env")["data"]
    secret = _runbook_object("Secret", "phaze-agent-token")["stringData"]
    for key, value in {**configmap, **secret}.items():
        monkeypatch.setenv(key, str(value))
    for entry in kube_staging.build_job_manifest(uuid.uuid4(), _kube())["spec"]["template"]["spec"]["containers"][0]["env"]:
        monkeypatch.setenv(entry["name"], entry["value"])
    get_settings.cache_clear()


def test_the_documented_objects_are_exactly_what_agentsettings_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented env boots the pod's settings -- asserted through ``get_settings``, the real consumer.

    This is the call ``job_runner._load_config_step`` makes. It must return an ``AgentSettings``
    (a ``ControlSettings`` is the ``isinstance`` guard's exit 20) and it must survive
    ``_enforce_required_agent_fields``, which is where a missing token or API URL raises.

    Note what ``PHAZE_AGENT_KIND=compute`` is doing here, since it is the reason this passes with no
    scan roots and no queue URL: it relaxes both the ``scan_roots`` gate and the ``queue_url``
    fail-fast, neither of which a one-shot analyze pod can satisfy. It is code-injected precisely so
    that no ConfigMap has to get it right.
    """
    _apply_documented_pod_env(monkeypatch)
    cfg = get_settings()
    assert isinstance(cfg, AgentSettings)
    assert cfg.kind == "compute"
    assert cfg.agent_api_url
    assert cfg.agent_token.get_secret_value()
    get_settings.cache_clear()


@pytest.mark.parametrize("dropped", sorted(kube_staging.JOB_ENV_FROM_CONFIGMAP | kube_staging.JOB_ENV_FROM_SECRET))
def test_dropping_any_operator_supplied_key_fails_a_test_rather_than_a_pod(dropped: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove one key the operator supplies and the pod's own startup call stops working.

    This is the assertion seam F2 had none of, and it is a **characterisation**, not a uniform
    claim -- the three keys fail in three different ways, and the differences are the finding:

    - ``PHAZE_ROLE`` -- ``get_settings()`` returns ``ControlSettings`` instead of raising. Not an
      error at all until ``job_runner``'s ``isinstance`` guard turns it into exit 20, so it is
      checked as a type here rather than as an exception.
    - ``PHAZE_AGENT_API_URL`` / ``PHAZE_AGENT_TOKEN`` -- ``ValidationError`` from
      ``_enforce_required_agent_fields``, which ``_load_config_step`` catches and turns into exit 20.
    - ``PHAZE_MODELS_DIR`` -- **does not fail**, and is asserted so explicitly below rather than
      quietly excluded from this parametrisation. ``job_runner`` falls back to ``cfg.models_path``.
      That fallback is only safe while it equals the mountPath, which is the next test.
    """
    _apply_documented_pod_env(monkeypatch)
    monkeypatch.delenv(dropped, raising=False)
    get_settings.cache_clear()

    if dropped == "PHAZE_MODELS_DIR":
        cfg = get_settings()
        assert isinstance(cfg, AgentSettings)
        assert cfg.models_path == kube_staging.MODELS_MOUNT_PATH
    elif dropped == "PHAZE_ROLE":
        assert not isinstance(get_settings(), AgentSettings)
    else:
        with pytest.raises(Exception, match=dropped):
            get_settings()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The /models invariant the code's own comment predicted
# --------------------------------------------------------------------------- #


def test_the_models_mountpath_matches_the_configmap_and_the_settings_fallback() -> None:
    """Close the drift ``build_job_manifest``'s docstring predicted in writing -- all three participants.

    ``kube_staging.py``'s docstring has said since the models-PVC change that the ``/models``
    mountPath **MUST** equal the ConfigMap's ``PHAZE_MODELS_DIR``, because the container reads its
    essentia weights from ``PHAZE_MODELS_DIR`` and a drift mounts the PVC where nothing looks for
    it. Nothing compared the two until now.

    The third participant is ``AgentSettings.models_path``: ``job_runner.run`` reads
    ``os.environ["PHAZE_MODELS_DIR"] or cfg.models_path``, so on a deployment that leaves the
    (soft-required) ConfigMap key out, the *settings default* is what has to match the mountPath.
    A change to it alone would break those deployments with nothing in the manifest to show for it.
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(models_pvc_name="phaze-models"))
    mounts = manifest["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    mount_path = next(m["mountPath"] for m in mounts if m["name"] == "models")

    documented = _runbook_object("ConfigMap", "phaze-agent-env")["data"]["PHAZE_MODELS_DIR"]
    fallback = AgentSettings(agent_api_url="http://app.test", agent_token="t", kind="compute").models_path  # noqa: S106

    assert mount_path == kube_staging.MODELS_MOUNT_PATH
    assert documented == kube_staging.MODELS_MOUNT_PATH
    assert fallback == kube_staging.MODELS_MOUNT_PATH


def test_the_models_volume_and_its_mount_refer_to_the_same_volume() -> None:
    """A mount naming a volume the pod spec does not declare is a pod that never starts.

    The PVC path adds a volume and a volumeMount in two separate statements; only the shared
    ``name`` ties them together, and the JSON schema cannot see that relationship (it validates each
    list independently). This is the one structural invariant of the models path that
    ``test_kube_manifest_schema.py`` genuinely cannot cover.
    """
    pod_spec = kube_staging.build_job_manifest(uuid.uuid4(), _kube(models_pvc_name="phaze-models"))["spec"]["template"]["spec"]
    declared = {volume["name"] for volume in pod_spec["volumes"]}
    mounted = {mount["name"] for mount in pod_spec["containers"][0]["volumeMounts"]}
    assert mounted <= declared
    assert {"phaze-ca", "models"} == mounted
