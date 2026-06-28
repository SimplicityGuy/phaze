"""Seam tests for the pure kr8s kube-staging service (Phase 54, Plan 03 -- KSUBMIT-01/05/06).

Two test layers meet here:

- The **manifest spec** test (HTTP-free) pins every KSUBMIT-01/05 field of the suspended Job
  ``build_job_manifest`` emits -- suspend/parallelism/backoffLimit/TTL/queue-name label/
  requests-only -- so a spec regression fails loudly.
- The **respx seam** tests (Layer 2) stub the kr8s REST surface (kr8s talks httpx) via the
  shared ``kube_respx`` discovery fixture and exercise create/201, create/409-idempotent,
  get, list-by-label, delete/200, delete/404-idempotent, plus ``get_workload_for`` across the
  label-hit / owner-ref-fallback / both-miss paths (the A2 de-risk).

A final import-boundary test asserts the module is a pure kr8s seam with NO ORM imports
(mirrors the ``s3_staging`` purity discipline).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

from httpx import Response
import pytest

from phaze.services import kube_staging
from tests.conftest import KUBE_TEST_API_URL


if TYPE_CHECKING:
    from respx import MockRouter


_NS = "phaze"
_LQ = "phaze-lq"
_IMAGE = "phaze/job-runner:test"
_JOBS_PATH = f"/apis/batch/v1/namespaces/{_NS}/jobs"
_WL_PATH = f"/apis/kueue.x-k8s.io/v1beta1/namespaces/{_NS}/workloads"


class _StubCfg(SimpleNamespace):
    """A duck-typed ControlSettings stand-in carrying only the kube_* fields the seam reads."""

    def __init__(self, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "kube_api_url": KUBE_TEST_API_URL,
            "kube_namespace": _NS,
            "kube_local_queue": _LQ,
            "kube_job_image": _IMAGE,
            "kube_job_cpu_request": "2",
            "kube_job_memory_request": "4Gi",
            "kube_workload_api_version": "kueue.x-k8s.io/v1beta1",
            "kube_sa_token": None,
        }
        defaults.update(overrides)
        super().__init__(**defaults)


@pytest.fixture
def stub_cfg(monkeypatch: pytest.MonkeyPatch) -> _StubCfg:
    """Point the seam's ``get_settings`` at a fully-configured kube stub."""
    cfg = _StubCfg()
    monkeypatch.setattr(kube_staging, "get_settings", lambda: cfg)
    return cfg


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


def test_build_job_manifest_spec(stub_cfg: _StubCfg) -> None:
    """Every KSUBMIT-01/05 field is present: suspend, parallelism, backoffLimit 0, TTL=900,
    queue-name label ON the Job, restartPolicy Never, requests-only (NO limits), deterministic name."""
    fid = uuid.uuid4()
    manifest = kube_staging.build_job_manifest(fid, stub_cfg)

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


def test_job_name_is_deterministic_and_file_id_scoped() -> None:
    """The Job name is the deterministic ``phaze-analyze-<file_id>`` (T-54-06: server UUID, DNS-1123)."""
    fid = uuid.uuid4()
    assert kube_staging.job_name(fid) == f"phaze-analyze-{fid}"
    assert kube_staging.job_name(fid) == kube_staging.job_name(fid)
    assert kube_staging.job_name(uuid.uuid4()) != kube_staging.job_name(fid)


@pytest.mark.parametrize("missing", ["kube_api_url", "kube_namespace", "kube_local_queue"])
def test_kube_config_raises_when_unset(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """``_kube_config`` fail-louds when any of api_url/namespace/local_queue is unset (operator misconfig)."""
    cfg = _StubCfg(**{missing: None})
    monkeypatch.setattr(kube_staging, "get_settings", lambda: cfg)
    with pytest.raises(kube_staging.KubeStagingError):
        kube_staging._kube_config()


@pytest.mark.parametrize("missing", ["kube_job_image", "kube_job_cpu_request", "kube_job_memory_request"])
def test_build_job_manifest_raises_when_manifest_field_unset(missing: str) -> None:
    """WR-02: an unset image/cpu/memory fail-louds with a message NAMING the missing variable,
    instead of building a ``None``-valued manifest the kube API rejects with an opaque error."""
    cfg = _StubCfg(**{missing: None})
    with pytest.raises(kube_staging.KubeStagingError, match=missing):
        kube_staging.build_job_manifest(uuid.uuid4(), cfg)


# --------------------------------------------------------------------------- #
# submit_job -- create 201 + 409-idempotent (KSUBMIT-01)
# --------------------------------------------------------------------------- #


async def test_submit_job_creates_suspended_job(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """A clean create POSTs the suspended Job and returns its (name, uid)."""
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    route = kube_respx.post(_JOBS_PATH).mock(return_value=Response(201, json=_job_json(name, "job-uid-1")))

    result_name, result_uid = await kube_staging.submit_job(fid)

    assert route.called
    assert result_name == name
    assert result_uid == "job-uid-1"


async def test_resubmit_409_is_idempotent(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
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

    result_name, result_uid = await kube_staging.submit_job(fid)

    assert refresh.called  # the idempotent refresh path ran
    assert result_name == name
    assert result_uid == "job-uid-existing"


async def test_submit_job_reraises_non_409(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """A non-409 server error surfaces as KubeStagingError (not silently swallowed)."""
    fid = uuid.uuid4()
    status_500 = {"kind": "Status", "status": "Failure", "reason": "InternalError", "code": 500, "message": "boom"}
    kube_respx.post(_JOBS_PATH).mock(return_value=Response(500, json=status_500))
    with pytest.raises(kube_staging.KubeStagingError):
        await kube_staging.submit_job(fid)


async def test_sa_token_applied_as_bearer(monkeypatch: pytest.MonkeyPatch, kube_respx: MockRouter) -> None:
    """WR-03: when ``kube_sa_token`` is set, outgoing kube requests carry ``Authorization: Bearer <token>``.

    The control plane runs OUTSIDE the cluster and authenticates with an operator-provided SA token;
    this covers the single credential-application line (``_api``) so a wrong auth form is caught here
    rather than as live-cluster 401s.
    """
    from pydantic import SecretStr

    cfg = _StubCfg(kube_sa_token=SecretStr("sa-secret-token"))
    monkeypatch.setattr(kube_staging, "get_settings", lambda: cfg)
    fid = uuid.uuid4()
    name = f"phaze-analyze-{fid}"
    route = kube_respx.post(_JOBS_PATH).mock(return_value=Response(201, json=_job_json(name, "uid-tok")))

    await kube_staging.submit_job(fid)

    assert route.called
    assert route.calls.last.request.headers.get("Authorization") == "Bearer sa-secret-token"


# --------------------------------------------------------------------------- #
# get_job / list_inflight_jobs
# --------------------------------------------------------------------------- #


async def test_get_job_returns_status(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """``get_job`` GETs the Job by name and exposes its status."""
    name = "phaze-analyze-getme"
    kube_respx.get(f"{_JOBS_PATH}/{name}").mock(return_value=Response(200, json=_job_json(name, "u1", succeeded=1)))

    job = await kube_staging.get_job(name)

    assert job.name == name
    assert int(job.status.get("succeeded")) == 1


async def test_list_inflight_jobs_by_label(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """``list_inflight_jobs`` lists Jobs by the managed-by label (the deferred orphan-sweep cross-check)."""
    name = "phaze-analyze-listed"
    body = {
        "apiVersion": "batch/v1",
        "kind": "JobList",
        "metadata": {},
        "items": [_job_json(name, "u2")],
    }
    kube_respx.get(_JOBS_PATH).mock(return_value=Response(200, json=body))

    jobs = await kube_staging.list_inflight_jobs()

    assert [j.name for j in jobs] == [name]


def test_list_inflight_jobs_marked_deferred() -> None:
    """The unused export carries a docstring marking it a deferred/uninvoked orphan-sweep capability."""
    doc = kube_staging.list_inflight_jobs.__doc__ or ""
    assert "Reserved orphan-Job sweep" in doc
    assert "intentionally NOT invoked" in doc


# --------------------------------------------------------------------------- #
# get_workload_for -- label-hit / owner-ref-fallback / both-miss (A2 de-risk)
# --------------------------------------------------------------------------- #


async def test_get_workload_for_label_hit(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """The job-uid label selector resolves the Workload directly."""
    job_uid = "job-uid-1"
    selector = f"kueue.x-k8s.io/job-uid={job_uid}"
    kube_respx.get(_WL_PATH, params__contains={"labelSelector": selector}).mock(
        return_value=Response(200, json=_workload_list(_workload_item("wl-labelhit")))
    )

    workload = await kube_staging.get_workload_for(job_uid)

    assert workload is not None
    assert workload.name == "wl-labelhit"


async def test_get_workload_for_owner_ref_fallback(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """When the label lookup misses, fall back to the Workload whose ownerReference.uid == job_uid (A2)."""
    job_uid = "job-uid-2"
    selector = f"kueue.x-k8s.io/job-uid={job_uid}"
    # Registered first: the label selector lookup MISSES (empty list).
    kube_respx.get(_WL_PATH, params__contains={"labelSelector": selector}).mock(return_value=Response(200, json=_workload_list()))
    # Registered second (no labelSelector): the namespace scan returns the owner-ref match.
    kube_respx.get(_WL_PATH).mock(return_value=Response(200, json=_workload_list(_workload_item("wl-ownerref", owner_uid=job_uid))))

    workload = await kube_staging.get_workload_for(job_uid)

    assert workload is not None
    assert workload.name == "wl-ownerref"


async def test_get_workload_for_both_miss_returns_none(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """Both the label lookup and the owner-ref scan miss -> None (admission state genuinely absent)."""
    job_uid = "job-uid-3"
    kube_respx.get(_WL_PATH).mock(return_value=Response(200, json=_workload_list()))

    assert await kube_staging.get_workload_for(job_uid) is None


# --------------------------------------------------------------------------- #
# delete_job -- 200 + 404-idempotent (KSUBMIT-06)
# --------------------------------------------------------------------------- #


async def test_delete_job_success(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """A present Job is deleted with Background propagation."""
    name = "phaze-analyze-del"
    route = kube_respx.delete(f"{_JOBS_PATH}/{name}").mock(return_value=Response(200, json={"kind": "Status", "status": "Success"}))

    await kube_staging.delete_job(name)

    assert route.called


async def test_delete_idempotent_404(stub_cfg: _StubCfg, kube_respx: MockRouter) -> None:
    """A 404/NotFound on delete is swallowed -- a missing Job is the desired end state (KSUBMIT-06)."""
    name = "phaze-analyze-gone"
    status_404 = {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404, "message": "not found"}
    kube_respx.delete(f"{_JOBS_PATH}/{name}").mock(return_value=Response(404, json=status_404))

    # Must NOT raise.
    await kube_staging.delete_job(name)


# --------------------------------------------------------------------------- #
# Import-boundary purity (mirror s3_staging)
# --------------------------------------------------------------------------- #


def test_kube_staging_has_no_orm_imports() -> None:
    """The seam is pure kr8s -- NO sqlalchemy / phaze.models imports (mirror s3_staging purity)."""
    source = Path(kube_staging.__file__).read_text(encoding="utf-8")
    assert "import sqlalchemy" not in source
    assert "from sqlalchemy" not in source
    assert "phaze.models" not in source
