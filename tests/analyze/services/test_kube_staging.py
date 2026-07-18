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

from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from httpx import Response
import kr8s
from pydantic import SecretStr, ValidationError
import pytest
import yaml

from phaze.config_backends import KubeConfig
from phaze.services import kube_staging
from tests.conftest import KUBE_TEST_API_URL


if TYPE_CHECKING:
    from respx import MockRouter


_NS = "phaze"
_LQ = "phaze-lq"
_IMAGE = "phaze/job-runner:test"
_JOBS_PATH = f"/apis/batch/v1/namespaces/{_NS}/jobs"
_WL_PATH = f"/apis/kueue.x-k8s.io/v1beta1/namespaces/{_NS}/workloads"
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
    assert spec["ttlSecondsAfterFinished"] == 900
    assert spec["ttlSecondsAfterFinished"] == kube_staging.JOB_TTL_SECONDS

    pod_spec = spec["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    container = pod_spec["containers"][0]
    assert container["image"] == _IMAGE
    resources = container["resources"]
    assert resources["requests"] == {"cpu": "2", "memory": "4Gi"}  # KSUBMIT-01: requests only
    assert "limits" not in resources  # Q1 RESOLVED (adopted): requests-only is LOCKED


def test_build_job_manifest_sets_active_deadline_seconds() -> None:
    """phaze-1b39: the Job MUST carry ``activeDeadlineSeconds`` -- the pipeline's only wall-clock bound.

    ``job_runner``'s exit-code contract delegates ALL wall-clock bounding to this field (the analyze
    stage runs ``timeout=None``) and ``ttlSecondsAfterFinished`` only fires AFTER a Job finishes, so
    without it a hung/never-started pod is never terminal and holds its burst-lane cap slot forever.
    """
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube())
    spec = manifest["spec"]

    assert spec["activeDeadlineSeconds"] == 10800  # default: 3h, the KubeConfig default
    # It is a DISTINCT bound from the finished-Job TTL -- the TTL cannot rescue a Job that never finishes.
    assert spec["activeDeadlineSeconds"] != spec["ttlSecondsAfterFinished"]


def test_build_job_manifest_active_deadline_seconds_is_per_backend_configurable() -> None:
    """phaze-1b39: a slow cluster can raise the deadline via ``[kube].active_deadline_seconds``."""
    manifest = kube_staging.build_job_manifest(uuid.uuid4(), _kube(active_deadline_seconds=1800))

    assert manifest["spec"]["activeDeadlineSeconds"] == 1800


def test_kube_config_rejects_non_positive_active_deadline_seconds() -> None:
    """A zero/negative deadline would make k8s reject the Job (or bound nothing) -- fail at config load."""
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
