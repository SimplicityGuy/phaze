"""Seam tests for the pure kr8s kube-staging service (Phase 54, Plan 03 -- KSUBMIT-01/05/06).

Two test layers meet here:

- The **manifest spec** test (HTTP-free) pins every KSUBMIT-01/05 field of the suspended Job
  ``build_job_manifest`` emits -- suspend/parallelism/backoffLimit/TTL/queue-name label/
  requests-only -- so a spec regression fails loudly.
- The **respx seam** tests (Layer 2) stub the kr8s REST surface (kr8s talks httpx) via the
  shared ``kube_respx`` discovery fixture and exercise create/201, create/409-idempotent,
  get, list-by-label, delete/200, delete/404-idempotent, plus ``get_workload_for`` across the
  label-hit / owner-ref-fallback / both-miss paths (the A2 de-risk).

Phase 70 (MKUE-01/D-04): every verb now takes an explicit ``kube: KubeConfig`` (the module-global
``active_kube`` read + the ``api.auth.token = token; await api._create_session()`` hack are RETIRED).
The client is built from a synthesized in-memory kubeconfig dict via constructor-time auth, so both
auth forms (``kubeconfig``+``context`` and ``api_url``+``sa_token``) unify onto one mechanism and two
distinct clusters yield two distinct cached clients.

A final import-boundary test asserts the module is a pure kr8s seam with NO ORM imports
(mirrors the ``s3_staging`` purity discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

from httpx import Response
import kr8s
from pydantic import SecretStr, ValidationError
import pytest
import structlog
import yaml

from phaze.config_backends import KubeConfig
from phaze.services import kube_staging
from tests.conftest import KUBE_TEST_API_URL
from tests.kube_fakes import fake_job, fake_pod


if TYPE_CHECKING:
    from respx import MockRouter


_NS = "phaze"
_LQ = "phaze-lq"
_IMAGE = "phaze/job-runner:test"
_JOBS_PATH = f"/apis/batch/v1/namespaces/{_NS}/jobs"
_WL_PATH = f"/apis/kueue.x-k8s.io/v1beta1/namespaces/{_NS}/workloads"
_PODS_PATH = f"/api/v1/namespaces/{_NS}/pods"
_LQ_PATH = f"/apis/kueue.x-k8s.io/v1beta1/namespaces/{_NS}/localqueues/{_LQ}"

# A full kubeconfig YAML for the kubeconfig+context auth form: raw content the operator supplies as a
# SecretStr, parsed to an in-memory dict (no secret touches disk). Its cluster server is the seam host
# so the respx discovery stubs resolve it end-to-end.
_KUBECONFIG_YAML = f"""\
apiVersion: v1
kind: Config
clusters:
- name: c1
  cluster:
    server: {KUBE_TEST_API_URL}
users:
- name: u1
  user:
    token: KUBECONFIG-BEARER
contexts:
- name: ctx-primary
  context:
    cluster: c1
    user: u1
    namespace: {_NS}
current-context: ctx-primary
"""


def _kube(**overrides: object) -> KubeConfig:
    """Build a fully-configured ``KubeConfig`` (the per-backend cluster config threaded to every verb).

    Phase 70: the seam no longer reads a module-global ``active_kube``; the caller passes THIS backend's
    ``KubeConfig`` directly. ``overrides`` name the KubeConfig fields (``api_url``, ``namespace``,
    ``sa_token``, ``kubeconfig``, ``context``, ...).
    """
    fields: dict[str, object] = {
        "api_url": KUBE_TEST_API_URL,
        "namespace": _NS,
        "local_queue": _LQ,
        "job_image": _IMAGE,
        "cpu_request": "2",
        "memory_request": "4Gi",
        "workload_api_version": "kueue.x-k8s.io/v1beta1",
        "ca_secret_name": "phaze-internal-ca",
        "env_configmap_name": "phaze-agent-env",
        "env_secret_name": "phaze-agent-token",
        "sa_token": None,
    }
    fields.update(overrides)
    return KubeConfig(**fields)


def _job_json(name: str, uid: str = "job-uid", *, succeeded: int = 0, failed: int = 0) -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": _NS, "uid": uid},
        "spec": {"suspend": True},
        "status": {"succeeded": succeeded, "failed": failed},
    }


def _workload_list(*items: dict[str, object]) -> dict[str, object]:
    return {"apiVersion": "kueue.x-k8s.io/v1beta1", "kind": "WorkloadList", "metadata": {}, "items": list(items)}


def _workload_item(name: str, *, owner_uid: str | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {"name": name, "namespace": _NS, "uid": f"{name}-uid"}
    if owner_uid is not None:
        metadata["ownerReferences"] = [{"uid": owner_uid}]
    return {"apiVersion": "kueue.x-k8s.io/v1beta1", "kind": "Workload", "metadata": metadata, "status": {"conditions": []}}


# --------------------------------------------------------------------------- #
# build_job_manifest -- KSUBMIT-01/05 spec (HTTP-free)
# --------------------------------------------------------------------------- #


def test_build_job_manifest_spec() -> None:
    """Every KSUBMIT-01/05 field is present: suspend, parallelism, backoffLimit 0, TTL=900,
    queue-name label ON the Job, restartPolicy Never, requests-only (NO limits), deterministic name."""
    fid = uuid.uuid4()
    manifest = kube_staging.build_job_manifest(fid, _kube())

    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == f"phaze-analyze-{fid}"
    assert manifest["metadata"]["namespace"] == _NS

    labels = manifest["metadata"]["labels"]
    assert labels["kueue.x-k8s.io/queue-name"] == _LQ  # ON THE JOB (KSUBMIT-01)
    assert labels["app.kubernetes.io/managed-by"] == "phaze"
    assert labels["phaze.dev/file-id"] == str(fid)

    spec = manifest["spec"]
    assert spec["suspend"] is True
    assert spec["parallelism"] == 1
    assert spec["completions"] == 1
    assert spec["backoffLimit"] == 0  # KSUBMIT-05: pod-level retry neutralized
    # phaze-1q4g: backoffLimit alone does NOT make "one Job => one pod" true. The default
    # ``TerminatingOrFailed`` replacement policy mints a replacement the moment a pod starts
    # TERMINATING -- before the failure is counted -- so a pod stuck Terminating on a dead node yields
    # an unbounded stream of replacement pods that phaze never asked for and cannot count against
    # ``cloud_job.attempts``. ``Failed`` makes the Job wait for the Failed phase, at which point
    # backoffLimit=0 terminalizes it instead of replacing it.
    assert spec["podReplacementPolicy"] == "Failed"
    assert spec["ttlSecondsAfterFinished"] == 900
    assert spec["ttlSecondsAfterFinished"] == kube_staging.JOB_TTL_SECONDS

    pod_spec = spec["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    container = pod_spec["containers"][0]
    assert container["image"] == _IMAGE
    resources = container["resources"]
    assert resources["requests"] == {"cpu": "2", "memory": "4Gi"}  # KSUBMIT-01: requests only
    assert "limits" not in resources  # Q1 RESOLVED (adopted): requests-only is LOCKED


def test_build_job_manifest_omits_active_deadline_seconds_by_default() -> None:
    """phaze-202e ACCEPTANCE: with no ``active_deadline_seconds`` the manifest carries NO deadline key.

    THE REGRESSION TEST for the 2026-07-28 incident. phaze-1b39 made ``activeDeadlineSeconds`` a
    required 3h bound; k8s then SIGTERM'd every 2-6 h concert-set analyze at exactly 3h, burning the
    file's whole cloud attempt budget. The key must be ABSENT -- not 0, not a sentinel -- because an
    absent ``activeDeadlineSeconds`` is how k8s spells "no wall-clock bound".
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())

    assert KubeConfig().active_deadline_seconds is None  # the field default is OFF, not 3h
    assert "activeDeadlineSeconds" not in manifest["spec"]
    # The finished-Job TTL is a DIFFERENT thing and stays -- it only fires after a Job finishes, so it
    # can never cut a run short.
    assert manifest["spec"]["ttlSecondsAfterFinished"] == kube_staging.JOB_TTL_SECONDS


def test_build_job_manifest_emits_active_deadline_seconds_when_explicitly_set() -> None:
    """An operator who deliberately opts one backend back into a hard bound still gets it emitted."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(active_deadline_seconds=1800))

    assert manifest["spec"]["activeDeadlineSeconds"] == 1800


def test_kube_config_still_validates_a_deployed_active_deadline_seconds() -> None:
    """phaze-202e ACCEPTANCE: an already-deployed backends.toml carrying the key keeps validating.

    The field went ``int`` -> ``int | None``; making it optional must not reject the configs that are
    live today (including the incident's 604800s ops stopgap). A non-positive value is still rejected --
    ``gt=0`` survives on the int branch, so 0 remains a config-load error rather than a Job k8s refuses.
    """
    assert _kube(active_deadline_seconds=10800).active_deadline_seconds == 10800
    assert _kube(active_deadline_seconds=604800).active_deadline_seconds == 604800
    with pytest.raises(ValidationError):
        _kube(active_deadline_seconds=0)


def test_build_job_manifest_mounts_ca_secret() -> None:
    """KDEPLOY-06: the internal CA is MOUNTED from the operator-created Secret at runtime, never
    baked into the image (KJOB-05 reversed). The pod spec carries a `phaze-ca` volume sourced from
    the Secret named by kube_ca_secret_name; the analyze container mounts it read-only at /certs and
    points PHAZE_AGENT_CA_FILE at /certs/phaze-ca.crt so construct_agent_client verifies the
    control-plane TLS chain (never verify=False)."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())
    pod_spec = manifest["spec"]["template"]["spec"]

    volumes = pod_spec["volumes"]
    ca_volume = next(v for v in volumes if v["name"] == "phaze-ca")
    assert ca_volume["secret"]["secretName"] == "phaze-internal-ca"  # kube.ca_secret_name

    container = pod_spec["containers"][0]
    ca_mount = next(m for m in container["volumeMounts"] if m["name"] == "phaze-ca")
    assert ca_mount["mountPath"] == "/certs"
    assert ca_mount["readOnly"] is True

    assert {"name": "PHAZE_AGENT_CA_FILE", "value": "/certs/phaze-ca.crt"} in container["env"]


def test_build_job_manifest_mounts_models_pvc_when_set() -> None:
    """When ``models_pvc_name`` is set, the pod gains a SECOND, separate ``models`` volume: an
    operator-provisioned PVC (claimName + readOnly) mounted read-only at /models (== PHAZE_MODELS_DIR),
    so the analyze container reads essentia weights from provisioned storage (no fat image, no download).
    The existing /certs CA Secret mount is untouched (the PVC carries ONLY weights, never certs)."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(models_pvc_name="phaze-essentia-models"))
    pod_spec = manifest["spec"]["template"]["spec"]

    models_volume = next(v for v in pod_spec["volumes"] if v["name"] == "models")
    assert models_volume["persistentVolumeClaim"] == {"claimName": "phaze-essentia-models", "readOnly": True}
    assert "secret" not in models_volume  # a PVC, never a Secret -- weights only, never certs

    container = pod_spec["containers"][0]
    models_mount = next(m for m in container["volumeMounts"] if m["name"] == "models")
    assert models_mount["mountPath"] == "/models"  # INVARIANT: == the ConfigMap's PHAZE_MODELS_DIR
    assert models_mount["readOnly"] is True

    # The CA mount is entirely separate and unchanged (KDEPLOY-06 preserved).
    ca_volume = next(v for v in pod_spec["volumes"] if v["name"] == "phaze-ca")
    assert ca_volume["secret"]["secretName"] == "phaze-internal-ca"


def test_build_job_manifest_omits_models_volume_when_unset() -> None:
    """Regression guard: with ``models_pvc_name`` unset (default None), the pod has ONLY the ``phaze-ca``
    volume + /certs mount -- NO ``models`` volume/mount is emitted, so existing deploys are byte-identical."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())
    pod_spec = manifest["spec"]["template"]["spec"]

    assert [v["name"] for v in pod_spec["volumes"]] == ["phaze-ca"]
    assert [m["name"] for m in pod_spec["containers"][0]["volumeMounts"]] == ["phaze-ca"]


def test_build_job_manifest_emits_memory_limit_when_set() -> None:
    """ADR-0005 (phaze-k6d5) ACCEPTANCE: with ``memory_limit`` set, the analyze container carries
    ``resources.limits.memory`` == the configured value, and ``resources.requests`` is UNCHANGED
    (Kueue's quota accounting reads requests only; ADR-0005 keeps requests authoritative -- the
    limit is a kernel bound, invisible to scheduling, and must not distort the request)."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(memory_limit="16Gi"))
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["limits"] == {"memory": "16Gi"}
    assert resources["requests"] == {"cpu": "2", "memory": "4Gi"}  # untouched by ADR-0005
    assert "cpu" not in resources["limits"]  # deliberately memory-only (QoS stays Burstable)


def test_build_job_manifest_omits_memory_limit_by_default() -> None:
    """ADR-0005 (phaze-k6d5) ACCEPTANCE / regression guard: with ``memory_limit`` unset (the
    default), the manifest is BYTE-IDENTICAL to the pre-ADR-0005, requests-only form -- no
    ``limits`` key, not an empty ``limits: {}``. Any consumer that has not opted in sees zero
    change. Asserted structurally (not by eye) via a full manifest equality against the
    known-good pre-ADR-0005 shape."""
    fid = uuid.uuid4()
    kube = _kube()
    assert kube.memory_limit is None  # the field default is OFF

    manifest = kube_staging.build_job_manifest(fid, kube)
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources == {"requests": {"cpu": "2", "memory": "4Gi"}}  # no "limits" key at all

    # Full-manifest byte-identical assertion: rebuilding with memory_limit explicitly set to None
    # yields the exact same dict as the default -- no sentinel, no drift.
    assert manifest == kube_staging.build_job_manifest(fid, _kube(memory_limit=None))


def test_build_job_manifest_memory_limit_keeps_qos_burstable() -> None:
    """ADR-0005 (phaze-k6d5) ACCEPTANCE: a memory limit WITHOUT a matching CPU limit must not
    promote the pod's Kubernetes QoS class to Guaranteed -- Guaranteed requires EVERY container to
    set limits == requests on BOTH cpu and memory (K8s QoS spec). Verified here rather than
    assumed: a QoS change would silently alter eviction ordering, which is exactly what ADR-0005
    promises NOT to do (the pods are already Burstable per the OOM records; this must stay true)."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(memory_limit="16Gi"))
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    def _qos_class(res: dict[str, dict[str, str]]) -> str:
        """Minimal K8s QoS classifier (BestEffort/Burstable/Guaranteed) mirroring kubelet's rule."""
        requests, limits = res.get("requests", {}), res.get("limits", {})
        if not requests and not limits:
            return "BestEffort"
        if requests.get("cpu") == limits.get("cpu") and requests.get("memory") == limits.get("memory") and requests and limits:
            return "Guaranteed"
        return "Burstable"

    assert "cpu" not in resources["limits"]  # no CPU limit -> Guaranteed is structurally impossible
    assert _qos_class(resources) == "Burstable"


def test_build_job_manifest_injects_env_contract() -> None:
    """JOB-ENV-CONTRACT: the analyze container carries the per-Job PHAZE_JOB_FILE_ID (== str(file_id))
    PLUS an envFrom that sources the static agent env from the operator-created ConfigMap + Secret.

    Without these, every admitted pod hits job_runner with no file id / no agent role+url+token and
    exits EXIT_CONFIG=20 before any analysis. The pre-existing PHAZE_AGENT_CA_FILE entry must remain
    (the injection is additive, not a replacement)."""
    fid = uuid.uuid4()
    kube = _kube()
    manifest = kube_staging.build_job_manifest(fid, kube)
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    # (a) the per-Job file id is code-injected (cannot come from a static ConfigMap/Secret).
    assert {"name": "PHAZE_JOB_FILE_ID", "value": str(fid)} in container["env"]

    # (b) the static agent env is sourced via envFrom from the configured ConfigMap + Secret.
    env_from = container["envFrom"]
    assert {"configMapRef": {"name": kube.env_configmap_name}} in env_from
    assert {"secretRef": {"name": kube.env_secret_name}} in env_from

    # (c) regression guard: the additive change keeps the existing CA env entry.
    assert {"name": "PHAZE_AGENT_CA_FILE", "value": "/certs/phaze-ca.crt"} in container["env"]


def test_build_job_manifest_injects_agent_kind_compute() -> None:
    """phaze-4xks ACCEPTANCE: the analyze container ALWAYS carries PHAZE_AGENT_KIND=compute,
    code-injected the same way as PHAZE_JOB_FILE_ID/PHAZE_AGENT_CA_FILE -- regardless of what the
    operator's documented ``phaze-agent-env`` ConfigMap (docs/k8s-burst.md §6) does or does not carry.

    ``AgentSettings.kind`` defaults to ``"fileserver"`` (config.py); every one-shot analyze pod is a
    ``"compute"`` agent (it owns no scan roots), so without this env var
    ``_enforce_required_agent_fields`` raises and the pod dies at settings validation before it can
    call back at all -- the bug this test pins.
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    assert {"name": "PHAZE_AGENT_KIND", "value": "compute"} in container["env"]
    # Not sourced from the ConfigMap/Secret envFrom -- a fixed, code-injected literal.
    assert kube_staging._ANALYZE_AGENT_KIND == "compute"


def test_job_name_is_deterministic_and_file_id_scoped() -> None:
    """The Job name is the deterministic ``phaze-analyze-<file_id>`` (T-54-06: server UUID, DNS-1123)."""
    fid = uuid.uuid4()
    assert kube_staging.job_name(fid) == f"phaze-analyze-{fid}"
    assert kube_staging.job_name(fid) == kube_staging.job_name(fid)
    assert kube_staging.job_name(uuid.uuid4()) != kube_staging.job_name(fid)


@pytest.mark.parametrize("missing", ["api_url", "namespace", "local_queue"])
def test_require_kube_raises_when_unset(missing: str) -> None:
    """``_require_kube`` fail-louds when any of the connection surface (api_url/namespace/local_queue)
    is unset with no kubeconfig fallback (operator misconfig) -- so a submit/reconcile never proceeds
    against a half-configured cluster (the guard moved off the retired ``_kube_config``)."""
    with pytest.raises(kube_staging.KubeStagingError):
        kube_staging._require_kube(_kube(**{missing: None}))


@pytest.mark.parametrize("missing", ["job_image", "cpu_request", "memory_request"])
def test_build_job_manifest_raises_when_manifest_field_unset(missing: str) -> None:
    """WR-02: an unset image/cpu/memory fail-louds with a message NAMING the missing field,
    instead of building a ``None``-valued manifest the kube API rejects with an opaque error."""
    with pytest.raises(kube_staging.KubeStagingError, match=missing):
        kube_staging.build_job_manifest(uuid.uuid4(), _kube(**{missing: None}))


# --------------------------------------------------------------------------- #
# D-04 auth: synthesized kubeconfig dict, both forms, distinct clients, no hack
# --------------------------------------------------------------------------- #


def test_kubeconfig_dict_from_synthesizes_from_api_url_and_token() -> None:
    """The api_url+sa_token form synthesizes a minimal in-memory kubeconfig carrying server+token+namespace."""
    kc = kube_staging._kubeconfig_dict_from(_kube(sa_token=SecretStr("SA-BEARER")))

    assert kc["clusters"][0]["cluster"]["server"] == KUBE_TEST_API_URL
    assert kc["users"][0]["user"]["token"] == "SA-BEARER"
    assert kc["contexts"][0]["context"]["namespace"] == _NS
    assert kc["current-context"] == kc["contexts"][0]["name"]


def test_kubeconfig_dict_from_parses_inline_kubeconfig_yaml() -> None:
    """The kubeconfig+context form parses the raw YAML content to a dict (no synthesized cluster/user)."""
    kc = kube_staging._kubeconfig_dict_from(_kube(kubeconfig=SecretStr(_KUBECONFIG_YAML), api_url=None))

    assert kc == yaml.safe_load(_KUBECONFIG_YAML)
    assert kc["current-context"] == "ctx-primary"
    assert kc["clusters"][0]["cluster"]["server"] == KUBE_TEST_API_URL


def test_kubeconfig_dict_from_malformed_yaml_never_leaks_token(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed kubeconfig (phaze-7hzo): PyYAML's MarkedYAMLError embeds a verbatim snippet of the
    offending line -- including a credential sitting on it -- in ``str(exc)``. ``_kubeconfig_dict_from``
    must catch that, raise a sanitized ``KubeStagingError`` (location only), and suppress the chained
    cause (``from None``) so the snippet-bearing exception can never reach a logger (T-54-07)."""
    secret_token = "SUPER-SECRET-BEARER-TOKEN-abc123"
    # A stray tab in the indentation before the token line is a genuine PyYAML structural fault
    # (ScannerError) -- unlike base64 corruption of the value, which stays parseable as a scalar.
    malformed_yaml = f"""\
apiVersion: v1
kind: Config
users:
- name: u1
  user:
\t token: {secret_token}
"""
    with pytest.raises(kube_staging.KubeStagingError) as exc_info:
        kube_staging._kubeconfig_dict_from(_kube(kubeconfig=SecretStr(malformed_yaml), api_url=None))

    message = str(exc_info.value)
    assert secret_token not in message
    assert exc_info.value.__cause__ is None  # `from None`: no chained MarkedYAMLError to leak later

    # Mirror the real leak site (backends.py:780): a structlog warning with exc_info=True over the
    # escaped exception. Even with the traceback rendered, the redacted message means the token
    # never lands in the log stream (T-54-07).
    logger = structlog.get_logger("test_kube_staging")
    with caplog.at_level("WARNING"):
        try:
            raise exc_info.value
        except kube_staging.KubeStagingError:
            logger.warning("simulated_reconcile_guard", exc_info=True)
    assert secret_token not in caplog.text


def test_kubeconfig_dict_from_unmarked_yaml_error_reports_no_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``YAMLError`` carrying NO ``problem_mark`` still sanitizes -- it just has no location to add.

    The handler above builds its message from ``exc.problem_mark``, which only the ``MarkedYAMLError``
    subclasses set. A plain ``yaml.YAMLError`` -- what a custom loader, a reader-level failure, or a
    future PyYAML raises -- has none, and the ``mark is None`` arm was the one path through that
    handler no test exercised (phaze-frq98, closing a branch the per-bead branch gate flagged).

    It is reached by monkeypatching ``safe_load`` rather than by crafting input, deliberately: every
    malformed document PyYAML can produce from a string sets a mark, so the arm is unreachable
    through the front door. That is exactly why it needs pinning -- an untested arm in the ONE
    handler whose job is to keep a bearer token out of the message is worth a test even when the
    input that triggers it must be forced.
    """
    secret_token = "SUPER-SECRET-BEARER-TOKEN-def456"

    def _raise_unmarked(_content: str) -> object:
        raise yaml.YAMLError(f"catastrophe near {secret_token}")

    monkeypatch.setattr(yaml, "safe_load", _raise_unmarked)

    with pytest.raises(kube_staging.KubeStagingError) as exc_info:
        kube_staging._kubeconfig_dict_from(_kube(kubeconfig=SecretStr("apiVersion: v1\n"), api_url=None))

    message = str(exc_info.value)
    assert secret_token not in message  # the sanitization holds with or without a mark
    assert "at line" not in message  # no mark => no location clause, rather than a bogus one
    assert "YAMLError" in message  # the exception TYPE is still reported, which is the useful half
    assert exc_info.value.__cause__ is None


def test_kubeconfig_dict_from_rejects_non_mapping_yaml() -> None:
    """A syntactically valid YAML document that is NOT a mapping (e.g. a bare scalar/list) must still
    fail loud with a sanitized ``KubeStagingError`` rather than returning a non-dict to callers that
    index it like a kubeconfig."""
    with pytest.raises(kube_staging.KubeStagingError, match="mapping"):
        kube_staging._kubeconfig_dict_from(_kube(kubeconfig=SecretStr("- just\n- a\n- list\n"), api_url=None))


async def test_api_passes_dict_kubeconfig_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_api`` builds the client via constructor-time auth: it passes the synthesized dict kubeconfig
    AND the selected context to ``kr8s.asyncio.api`` (never a no-arg call -> arbitrary cached client)."""
    captured: dict[str, object] = {}

    async def fake_api(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(kube_staging.kr8s.asyncio, "api", fake_api)

    await kube_staging._api(_kube(kubeconfig=SecretStr(_KUBECONFIG_YAML), api_url=None, context="ctx-primary"))

    assert isinstance(captured["kubeconfig"], dict)
    assert captured["kubeconfig"]["current-context"] == "ctx-primary"  # type: ignore[index]
    assert captured["context"] == "ctx-primary"
    assert captured["namespace"] == _NS


async def test_distinct_kubeconfigs_yield_distinct_clients(kube_respx: MockRouter) -> None:
    """Two distinct KubeConfigs (two clusters) build two DISTINCT cached kr8s clients -- never a shared
    or post-construction-mutated one (verified: kr8s hash_kwargs json-keys the dict, so distinct dicts
    -> distinct cached Api instances)."""
    api_a = await kube_staging._api(_kube(namespace="ns-a"))
    api_b = await kube_staging._api(_kube(namespace="ns-b"))

    assert api_a is not api_b


async def test_kubeconfig_form_applies_bearer(kube_respx: MockRouter) -> None:
    """The kubeconfig+context auth form authenticates from the parsed dict: outgoing requests carry the
    kubeconfig user's ``Authorization: Bearer <token>`` (constructor-time auth, no token hack)."""
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    route = kube_respx.post(_JOBS_PATH).mock(return_value=Response(201, json=_job_json(name, "uid-kc")))

    await kube_staging.submit_job(fid, _kube(kubeconfig=SecretStr(_KUBECONFIG_YAML), context="ctx-primary"))

    assert route.called
    assert route.calls.last.request.headers.get("Authorization") == "Bearer KUBECONFIG-BEARER"


def test_source_has_no_token_hack() -> None:
    """D-04: the retired ``api.auth.token = token; await api._create_session()`` hack is GONE -- the
    module source contains neither ``_create_session`` nor an ``api.auth.token`` mutation."""
    source = Path(kube_staging.__file__).read_text(encoding="utf-8")
    assert "_create_session" not in source
    assert "api.auth.token" not in source


# --------------------------------------------------------------------------- #
# submit_job -- create 201 + 409-idempotent (KSUBMIT-01)
# --------------------------------------------------------------------------- #


async def test_submit_job_creates_suspended_job(kube_respx: MockRouter) -> None:
    """A clean create POSTs the suspended Job and returns its (name, uid)."""
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    route = kube_respx.post(_JOBS_PATH).mock(return_value=Response(201, json=_job_json(name, "job-uid-1")))

    result_name, result_uid = await kube_staging.submit_job(fid, _kube())

    assert route.called
    assert result_name == name
    assert result_uid == "job-uid-1"


async def test_resubmit_409_is_idempotent(kube_respx: MockRouter) -> None:
    """A duplicate submit hits 409 AlreadyExists -> submit_job refreshes instead of raising (KSUBMIT-01)."""
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    status_409 = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "reason": "AlreadyExists",
        "code": 409,
        "message": f'jobs.batch "{name}" already exists',
    }
    kube_respx.post(_JOBS_PATH).mock(return_value=Response(409, json=status_409))
    refresh = kube_respx.get(f"{_JOBS_PATH}/{name}").mock(return_value=Response(200, json=_job_json(name, "job-uid-existing")))

    result_name, result_uid = await kube_staging.submit_job(fid, _kube())

    assert refresh.called  # the idempotent refresh path ran
    assert result_name == name
    assert result_uid == "job-uid-existing"


async def test_submit_job_reraises_non_409(kube_respx: MockRouter) -> None:
    """A non-409 server error surfaces as KubeStagingError (not silently swallowed)."""
    fid = uuid.uuid4()
    status_500 = {"kind": "Status", "status": "Failure", "reason": "InternalError", "code": 500, "message": "boom"}
    kube_respx.post(_JOBS_PATH).mock(return_value=Response(500, json=status_500))
    with pytest.raises(kube_staging.KubeStagingError):
        await kube_staging.submit_job(fid, _kube())


async def test_sa_token_applied_as_bearer(kube_respx: MockRouter) -> None:
    """WR-03: when ``sa_token`` is set, outgoing kube requests carry ``Authorization: Bearer <token>``.

    The control plane runs OUTSIDE the cluster and authenticates with an operator-provided SA token;
    this covers the single credential-application line (``_api``) so a wrong auth form is caught here
    rather than as live-cluster 401s.
    """
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    route = kube_respx.post(_JOBS_PATH).mock(return_value=Response(201, json=_job_json(name, "uid-tok")))

    await kube_staging.submit_job(fid, _kube(sa_token=SecretStr("sa-secret-token")))

    assert route.called
    assert route.calls.last.request.headers.get("Authorization") == "Bearer sa-secret-token"


# --------------------------------------------------------------------------- #
# get_job / list_inflight_jobs
# --------------------------------------------------------------------------- #


async def test_get_job_returns_status(kube_respx: MockRouter) -> None:
    """``get_job`` GETs the Job by name and exposes its status."""
    name = "phaze-analyze-getme"
    kube_respx.get(f"{_JOBS_PATH}/{name}").mock(return_value=Response(200, json=_job_json(name, "u1", succeeded=1)))

    job = await kube_staging.get_job(name, _kube())

    assert job.name == name
    assert int(job.status.get("succeeded")) == 1


async def test_list_inflight_jobs_by_label(kube_respx: MockRouter) -> None:
    """``list_inflight_jobs`` lists Jobs by the managed-by label (the deferred orphan-sweep cross-check)."""
    name = "phaze-analyze-listed"
    body = {
        "apiVersion": "batch/v1",
        "kind": "JobList",
        "metadata": {},
        "items": [_job_json(name, "u2")],
    }
    kube_respx.get(_JOBS_PATH).mock(return_value=Response(200, json=body))

    jobs = await kube_staging.list_inflight_jobs(_kube())

    assert [j.name for j in jobs] == [name]


def test_list_inflight_jobs_marked_deferred() -> None:
    """The unused export carries a docstring marking it a deferred/uninvoked orphan-sweep capability."""
    doc = kube_staging.list_inflight_jobs.__doc__ or ""
    assert "Reserved orphan-Job sweep" in doc
    assert "intentionally NOT invoked" in doc


# --------------------------------------------------------------------------- #
# get_workload_for -- label-hit / owner-ref-fallback / both-miss (A2 de-risk)
# --------------------------------------------------------------------------- #


async def test_get_workload_for_label_hit(kube_respx: MockRouter) -> None:
    """The job-uid label selector resolves the Workload directly."""
    job_uid = "job-uid-1"
    selector = f"kueue.x-k8s.io/job-uid={job_uid}"
    kube_respx.get(_WL_PATH, params__contains={"labelSelector": selector}).mock(
        return_value=Response(200, json=_workload_list(_workload_item("wl-labelhit")))
    )

    workload = await kube_staging.get_workload_for(job_uid, _kube())

    assert workload is not None
    assert workload.name == "wl-labelhit"


async def test_get_workload_for_owner_ref_fallback(kube_respx: MockRouter) -> None:
    """When the label lookup misses, fall back to the Workload whose ownerReference.uid == job_uid (A2)."""
    job_uid = "job-uid-2"
    selector = f"kueue.x-k8s.io/job-uid={job_uid}"
    # Registered first: the label selector lookup MISSES (empty list).
    kube_respx.get(_WL_PATH, params__contains={"labelSelector": selector}).mock(return_value=Response(200, json=_workload_list()))
    # Registered second (no labelSelector): the namespace scan returns the owner-ref match.
    kube_respx.get(_WL_PATH).mock(return_value=Response(200, json=_workload_list(_workload_item("wl-ownerref", owner_uid=job_uid))))

    workload = await kube_staging.get_workload_for(job_uid, _kube())

    assert workload is not None
    assert workload.name == "wl-ownerref"


async def test_get_workload_for_both_miss_returns_none(kube_respx: MockRouter) -> None:
    """Both the label lookup and the owner-ref scan miss -> None (admission state genuinely absent)."""
    job_uid = "job-uid-3"
    kube_respx.get(_WL_PATH).mock(return_value=Response(200, json=_workload_list()))

    assert await kube_staging.get_workload_for(job_uid, _kube()) is None


async def test_get_workload_for_skips_a_workload_owned_by_a_different_job(kube_respx: MockRouter) -> None:
    """A namespace scan that finds OTHER jobs' Workloads must keep looking, not claim the first one.

    The existing both-miss test returns an EMPTY list, so the owner-ref scan never enters its loop
    body at all. This is the populated miss -- the shape a real shared namespace always has, where
    several analyze Jobs are in flight and every one of them has a Workload. Two branches that
    nothing exercised (phaze-frq98, flagged by the per-bead branch gate):

    * the ``ref.get("uid") == job_uid`` comparison going FALSE -- i.e. actually rejecting a
      non-matching owner rather than only ever confirming a matching one, which is the entire
      correctness claim of the fallback;
    * the inner ``for ref`` loop RUNNING OUT without returning, so the scan advances to the next
      Workload instead of stopping.

    Both matter: a wrong turn here binds one file's admission state to another file's Workload.
    The matching Workload is placed LAST so the scan is forced through both.
    """
    job_uid = "job-uid-4"
    selector = f"kueue.x-k8s.io/job-uid={job_uid}"
    kube_respx.get(_WL_PATH, params__contains={"labelSelector": selector}).mock(return_value=Response(200, json=_workload_list()))
    kube_respx.get(_WL_PATH).mock(
        return_value=Response(
            200,
            json=_workload_list(
                _workload_item("wl-someone-else", owner_uid="a-different-jobs-uid"),
                _workload_item("wl-no-owner-refs-at-all"),
                _workload_item("wl-ours", owner_uid=job_uid),
            ),
        )
    )

    workload = await kube_staging.get_workload_for(job_uid, _kube())

    assert workload is not None
    assert workload.name == "wl-ours"


# --------------------------------------------------------------------------- #
# delete_job -- 200 + 404-idempotent (KSUBMIT-06)
# --------------------------------------------------------------------------- #


async def test_delete_job_success(kube_respx: MockRouter) -> None:
    """A present Job is deleted with Background propagation."""
    name = "phaze-analyze-del"
    route = kube_respx.delete(f"{_JOBS_PATH}/{name}").mock(return_value=Response(200, json={"kind": "Status", "status": "Success"}))

    await kube_staging.delete_job(name, _kube())

    assert route.called


async def test_delete_idempotent_404(kube_respx: MockRouter) -> None:
    """A 404/NotFound on delete is swallowed -- a missing Job is the desired end state (KSUBMIT-06)."""
    name = "phaze-analyze-gone"
    status_404 = {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404, "message": "not found"}
    kube_respx.delete(f"{_JOBS_PATH}/{name}").mock(return_value=Response(404, json=status_404))

    # Must NOT raise.
    await kube_staging.delete_job(name, _kube())


# --------------------------------------------------------------------------- #
# phaze-202e -- pod-state wedge classifier (PURE, HTTP-free)
#
# The state-based replacement for the phaze-1b39 wall clock. The invariant these pin, in order of
# importance: a Running pod is NEVER a wedge, at any age. Everything else is proof-of-death.
# --------------------------------------------------------------------------- #


_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


def _ago(seconds: int) -> str:
    """RFC3339 stamp ``seconds`` before ``_NOW`` -- k8s renders condition timestamps in this form."""
    return (_NOW - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("age_seconds", [0, 3600, 10800, 86400, 30 * 86400])
def test_classify_job_pods_never_terminalizes_a_running_pod(age_seconds: int) -> None:
    """phaze-202e ACCEPTANCE / THE INVARIANT: a Running pod is ALIVE regardless of age.

    This is the whole point of the bead. phaze-1b39 bounded a run by ``activeDeadlineSeconds`` and
    killed every 2-6 h concert set at exactly 3h. Age is not an input to this verdict at all -- the
    parametrization spans a month to make that structural, not incidental. A pod that has been running
    for 30 days is still ALIVE; only positive proof of death terminalizes.
    """
    # An age-shaped signal is present (the pod has ALSO been unschedulable-stamped long ago) purely to
    # prove that a live pod short-circuits BEFORE any clock is consulted.
    pod = fake_pod("Running", unschedulable_since=_ago(age_seconds))

    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.ALIVE


def test_classify_job_pods_alive_dominates_a_dead_sibling() -> None:
    """One Running pod outvotes a wedged sibling -- never kill a Job that has work in flight."""
    pods = [fake_pod("Pending", waiting_reason="ImagePullBackOff"), fake_pod("Running")]

    assert kube_staging.classify_job_pods(pods, now=_NOW) is kube_staging.PodLiveness.ALIVE


def test_classify_job_pods_succeeded_is_alive() -> None:
    """A Succeeded pod whose callback is still in flight must not be mistaken for a wedge."""
    assert kube_staging.classify_job_pods([fake_pod("Succeeded")], now=_NOW) is kube_staging.PodLiveness.ALIVE


@pytest.mark.parametrize("reason", sorted(kube_staging.DEAD_BEFORE_START_WAITING_REASONS))
def test_classify_job_pods_flags_every_fatal_waiting_reason(reason: str) -> None:
    """phaze-202e ACCEPTANCE: ImagePullBackOff / CreateContainerConfigError et al are dead-before-start.

    These are the phaze-1b39 wedge shapes -- a bad image, or the missing operator ConfigMap/Secret the
    pod's ``envFrom`` needs. k8s retries them forever on its own, so age adds no information and the
    verdict is immediate.
    """
    assert kube_staging.classify_job_pods([fake_pod("Pending", waiting_reason=reason)], now=_NOW) is kube_staging.PodLiveness.DEAD_BEFORE_START


def test_classify_job_pods_flags_a_wedged_init_container() -> None:
    """A wedged INIT container is just as permanently un-started as a wedged app container."""
    pod = fake_pod("Pending", init_waiting_reason="CreateContainerConfigError")

    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.DEAD_BEFORE_START


def test_classify_job_pods_holds_a_transient_waiting_reason() -> None:
    """``ContainerCreating`` is normal startup, not a wedge -- a false positive here burns a cloud attempt."""
    assert kube_staging.classify_job_pods([fake_pod("Pending", waiting_reason="ContainerCreating")], now=_NOW) is kube_staging.PodLiveness.STARTING


def test_classify_job_pods_holds_a_briefly_unschedulable_pod() -> None:
    """Unschedulable INSIDE the scheduling probe is held -- that is the normal cluster-autoscaler shape."""
    pod = fake_pod("Pending", unschedulable_since=_ago(kube_staging.UNSCHEDULABLE_PROBE_SECONDS - 60))

    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.STARTING


def test_classify_job_pods_flags_a_persistently_unschedulable_pod() -> None:
    """Unschedulable PAST the probe is a wedge -- the clock is the k8s condition's own transition time.

    Note what is being measured: how long SCHEDULING has been failing, read off the pod's
    ``PodScheduled`` condition. It is never how long an analysis has run.
    """
    pod = fake_pod("Pending", unschedulable_since=_ago(kube_staging.UNSCHEDULABLE_PROBE_SECONDS + 60))

    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.UNSCHEDULABLE


def test_classify_job_pods_holds_an_unschedulable_pod_with_no_readable_timestamp() -> None:
    """An unschedulable condition with an unparseable ``lastTransitionTime`` cannot be probed -> hold."""
    pod = SimpleNamespace(
        status={"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]},
        metadata=SimpleNamespace(name="p"),
    )

    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.STARTING


def test_classify_job_pods_treats_an_empty_pod_list_as_starting_not_dead() -> None:
    """An empty pod list is UNKNOWN, never dead.

    ``list_pods_for_job`` returns ``[]`` both when the Job genuinely has no pod AND when the cluster
    uses a pod label this code does not know. Terminalizing on that would mass-kill every in-flight run
    on a single label drift, so the classifier refuses -- the zero-pod case is escalated only by
    reconcile, and only with independent corroboration from the Job's own ``status.active``.
    """
    assert kube_staging.classify_job_pods([], now=_NOW) is kube_staging.PodLiveness.STARTING


def test_classify_job_pods_defaults_now_to_wall_clock() -> None:
    """``now`` is injectable for tests but defaults to the real clock in production."""
    assert kube_staging.classify_job_pods([fake_pod("Running")]) is kube_staging.PodLiveness.ALIVE


def test_job_started_at_and_suspended_read_the_job_status() -> None:
    """The zero-pod probe's two Job-side reads: ``status.startTime`` and ``spec.suspend``."""
    assert kube_staging.job_started_at(fake_job(start_time=_ago(60))) == _NOW - timedelta(seconds=60)
    assert kube_staging.job_started_at(fake_job()) is None  # absent -> unprobeable, hold
    assert kube_staging.job_started_at(fake_job(start_time="not-a-timestamp")) is None
    assert kube_staging.job_is_suspended(fake_job(suspend=True)) is True
    assert kube_staging.job_is_suspended(fake_job(suspend=False)) is False


def test_describe_job_pods_summarises_for_the_operator_log() -> None:
    """The warning log must name the phase + fatal reason so an operator can act without kubectl."""
    assert kube_staging.describe_job_pods([]) == "no-pods"
    assert kube_staging.describe_job_pods([fake_pod("Pending", waiting_reason="ImagePullBackOff")]) == "Pending/ImagePullBackOff"
    assert kube_staging.describe_job_pods([fake_pod("Running")]) == "Running"
    # phaze-1q4g: a node-loss reason is what the operator needs first -- it is rendered ahead of any
    # container state, which on a dead node is a consequence rather than an independent finding.
    assert kube_staging.describe_job_pods([fake_pod("Failed", status_reason="NodeShutdown")]) == "Failed/NodeShutdown"
    assert (
        kube_staging.describe_job_pods([fake_pod("Failed", status_reason="NodeShutdown", waiting_reason="ImagePullBackOff")]) == "Failed/NodeShutdown"
    )
    assert (
        kube_staging.describe_job_pods([fake_pod("Failed", disruption_target_reason="DeletionByTaintManager")])
        == "Failed/DisruptionTarget/DeletionByTaintManager"
    )


# --------------------------------------------------------------------------- #
# phaze-1q4g -- NODE_LOST: the pod died WITH ITS NODE, not because of the file
# --------------------------------------------------------------------------- #
#
# A node-loss re-drive deliberately does NOT charge ``cloud_job.attempts`` (an infrastructure fault
# is not the file's fault). That made it invisible to the retry ceiling, which is how one file
# produced eight pods over five days against a cap of three, crashing the burst node each time
# (spike phaze-wcrb §5). The classifier is what makes the case NAMEABLE, so reconcile can give it its
# own bounded budget instead of either an unbounded free pass or the wrong meter.


@pytest.mark.parametrize("reason", sorted(kube_staging.NODE_LOSS_POD_STATUS_REASONS))
def test_classify_job_pods_flags_every_node_loss_status_reason(reason: str) -> None:
    """Each node-scoped ``status.reason`` the node controller / kubelet stamps reads as NODE_LOST."""
    assert kube_staging.classify_job_pods([fake_pod("Failed", status_reason=reason)], now=_NOW) is kube_staging.PodLiveness.NODE_LOST


@pytest.mark.parametrize("reason", ["DeletionByTaintManager", "TerminationByKubelet", "DeletionByPodGC", "PreemptionByScheduler"])
def test_classify_job_pods_flags_a_disruption_target_condition(reason: str) -> None:
    """The k8s>=1.26 ``DisruptionTarget=True`` condition is node loss for EVERY reason value.

    Its reason vocabulary is open and version-dependent, and every member of it means the same thing:
    the control plane, not the analysis, ended this pod. Filtering on the reason would silently miss
    whichever spelling a future cluster uses -- so only the condition itself is matched.
    """
    assert kube_staging.classify_job_pods([fake_pod("Failed", disruption_target_reason=reason)], now=_NOW) is kube_staging.PodLiveness.NODE_LOST


def test_classify_job_pods_ignores_a_false_disruption_target_condition() -> None:
    """``DisruptionTarget`` with ``status != "True"`` proves nothing and must not read as node loss."""
    pod = fake_pod("Failed")
    pod.status["conditions"] = [{"type": "DisruptionTarget", "status": "False", "reason": "DeletionByTaintManager"}]
    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.STARTING


def test_classify_job_pods_alive_still_dominates_node_loss() -> None:
    """A Running pod is NEVER terminalized, not even alongside a node-lost sibling (the 1b39 invariant).

    NODE_LOST is ranked strictly below ALIVE on purpose: a pod still reporting Running is doing work,
    and no node-scoped marker on a *sibling* may kill it. This is the same alive-dominates rule that
    protects a 2-6 h concert-set analyze, and phaze-1q4g does not weaken it.
    """
    pods = [fake_pod("Failed", status_reason="NodeShutdown"), fake_pod("Running")]
    assert kube_staging.classify_job_pods(pods, now=_NOW) is kube_staging.PodLiveness.ALIVE


def test_classify_job_pods_node_loss_outranks_a_dead_before_start_container() -> None:
    """A node-lost pod whose container also shows a fatal waiting reason is NODE_LOST, not DEAD_BEFORE_START.

    Both facts are true; only one is the cause. The node took the pod, and the container state it left
    behind is a consequence -- charging it to the operator-misconfig budget would meter the wrong thing.
    """
    pod = fake_pod("Failed", status_reason="NodeLost", waiting_reason="CreateContainerConfigError")
    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.NODE_LOST


def test_classify_job_pods_an_in_container_oomkill_is_not_node_loss() -> None:
    """A container OOMKilled under its OWN ``limits.memory`` is the file overrunning, NOT node loss.

    ADR-0005's memory limit converts a node-scoped OOM into a pod-scoped one; that pod is paying for
    its own excess and MUST keep charging the ordinary ``attempts`` budget. Only node-scoped fields
    (``status.reason`` / ``DisruptionTarget``) select the node-loss budget -- a container's terminated
    reason never does.
    """
    pod = fake_pod("Failed")
    pod.status["containerStatuses"] = [{"name": "analyze", "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]
    assert kube_staging.classify_job_pods([pod], now=_NOW) is kube_staging.PodLiveness.STARTING


# --------------------------------------------------------------------------- #
# phaze-202e -- list_pods_for_job (respx seam, modern + legacy job-name label)
# --------------------------------------------------------------------------- #


def _pod_list(*names: str) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "PodList",
        "metadata": {},
        "items": [{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": n, "namespace": _NS}, "status": {"phase": "Running"}} for n in names],
    }


async def test_list_pods_for_job_uses_the_modern_job_name_label(kube_respx: MockRouter) -> None:
    """k8s >=1.27 stamps ``batch.kubernetes.io/job-name`` -- the first selector tried."""
    route = kube_respx.get(_PODS_PATH, params__contains={"labelSelector": "batch.kubernetes.io/job-name=phaze-analyze-x"}).mock(
        return_value=Response(200, json=_pod_list("phaze-analyze-x-abcde"))
    )

    pods = await kube_staging.list_pods_for_job("phaze-analyze-x", _kube())

    assert route.called
    assert [p.name for p in pods] == ["phaze-analyze-x-abcde"]


async def test_list_pods_for_job_falls_back_to_the_legacy_label(kube_respx: MockRouter) -> None:
    """An older cluster labels pods ``job-name``; an EMPTY modern result must degrade to it, not to []."""
    modern = kube_respx.get(_PODS_PATH, params__contains={"labelSelector": "batch.kubernetes.io/job-name=phaze-analyze-x"}).mock(
        return_value=Response(200, json=_pod_list())
    )
    legacy = kube_respx.get(_PODS_PATH, params__contains={"labelSelector": "job-name=phaze-analyze-x"}).mock(
        return_value=Response(200, json=_pod_list("legacy-pod"))
    )

    pods = await kube_staging.list_pods_for_job("phaze-analyze-x", _kube())

    assert modern.called
    assert legacy.called
    assert [p.name for p in pods] == ["legacy-pod"]


async def test_list_pods_for_job_returns_empty_when_both_labels_miss(kube_respx: MockRouter) -> None:
    """Both selectors empty -> ``[]``, which callers MUST read as "unknown", never as "dead"."""
    kube_respx.get(_PODS_PATH).mock(return_value=Response(200, json=_pod_list()))

    assert await kube_staging.list_pods_for_job("phaze-analyze-x", _kube()) == []


# --------------------------------------------------------------------------- #
# Import-boundary purity (mirror s3_staging)
# --------------------------------------------------------------------------- #


def test_kube_staging_has_no_orm_imports() -> None:
    """The seam is pure kr8s -- NO sqlalchemy / phaze.models imports (mirror s3_staging purity)."""
    source = Path(kube_staging.__file__).read_text(encoding="utf-8")
    assert "import sqlalchemy" not in source
    assert "from sqlalchemy" not in source
    assert "phaze.models" not in source


# --------------------------------------------------------------------------- #
# get_local_queue -- success / NotFoundError / transient (Phase 56, KDEPLOY-04 probe)
#
# The startup reachability probe GETs the configured Kueue LocalQueue by name: refresh() raises
# ``kr8s.NotFoundError`` on a 404 (the queue is missing / mis-named -> operator misconfig) and a
# generic ``kr8s.ServerError`` on a transient kube-API/mesh failure. The caller (controller.startup)
# treats BOTH as "unreachable" and flags it non-fatally. Phase 70 (MKUE-03): the probe is per-cluster,
# taking the backend's own ``KubeConfig``.
# --------------------------------------------------------------------------- #


def _local_queue_json() -> dict[str, object]:
    return {
        "apiVersion": "kueue.x-k8s.io/v1beta1",
        "kind": "LocalQueue",
        "metadata": {"name": _LQ, "namespace": _NS, "uid": "lq-uid"},
        "spec": {"clusterQueue": "phaze-cq"},
        "status": {},
    }


async def test_get_local_queue_success(kube_respx: MockRouter) -> None:
    """A 200 on the configured LocalQueue GET returns the refreshed object (reachable)."""
    route = kube_respx.get(_LQ_PATH).mock(return_value=Response(200, json=_local_queue_json()))

    lq = await kube_staging.get_local_queue(_kube())

    assert route.called
    assert lq.name == _LQ


async def test_get_local_queue_not_found(kube_respx: MockRouter) -> None:
    """A 404/NotFound on the LocalQueue GET surfaces as ``kr8s.NotFoundError`` (queue mis-named/absent)."""
    status_404 = {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404, "message": "not found"}
    kube_respx.get(_LQ_PATH).mock(return_value=Response(404, json=status_404))

    with pytest.raises(kr8s.NotFoundError):
        await kube_staging.get_local_queue(_kube())


async def test_get_local_queue_transient(kube_respx: MockRouter) -> None:
    """A 500 on the LocalQueue GET raises (transient kube-API/mesh failure -> caller treats as unreachable)."""
    status_500 = {"kind": "Status", "status": "Failure", "reason": "InternalError", "code": 500, "message": "boom"}
    kube_respx.get(_LQ_PATH).mock(return_value=Response(500, json=status_500))

    with pytest.raises(kr8s.ServerError):
        await kube_staging.get_local_queue(_kube())
