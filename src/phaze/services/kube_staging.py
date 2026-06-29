"""Control-plane Kubernetes (Kueue) Job-staging service (Phase 54, Plan 03 -- KSUBMIT-01/05/06).

The single home of every kr8s call in the system. The control plane builds the suspended
``batch/v1`` Job manifest, submits it (idempotently), lists in-flight Jobs, resolves the paired
Kueue ``Workload`` to read admission state, and deletes a finished Job -- but it carries NO
analysis payload and reads NO result here. Kube credentials live on the control plane only
(DIST-01); the file-server agent and the one-shot pod are kube-credential-free.

Structure mirrors ``s3_staging.py`` verbatim: ``__future__`` annotations, a ``TYPE_CHECKING``
guard, a fail-loud custom error, a ``_kube_config()`` validation gate, an async client factory,
and the idempotent-delete idiom (swallow already-absent). There are NO ORM imports here -- the
service is pure kr8s keyed by ``file_id`` (reconcile-by-file_id; the deterministic Job name
``phaze-analyze-<file_id>`` is the single object identity, no per-attempt suffixes).

The kr8s client is built from the operator-provided ``ControlSettings`` kube surface, so it
reaches ANY reachable kube API endpoint (over Tailscale/WireGuard) via an explicit ``url`` and
namespace; credentials come from the ``_FILE``-resolved ``SecretStr`` fields and are never logged
(T-54-07).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import kr8s
import kr8s.asyncio
from kr8s.asyncio.objects import Job, new_class

from phaze.config import get_settings


if TYPE_CHECKING:
    import uuid

    from phaze.config import ControlSettings


# 15 min = 3x the */5 reconcile tick. D-04 makes the explicit delete-after-record primary, so the
# TTL only ever fires in the "phaze never reconciled at all" orphan case (Pitfall 1 -- never a
# config knob; consistent with the fixed */5 cron, D-03).
JOB_TTL_SECONDS = 900

_QUEUE_NAME_LABEL = "kueue.x-k8s.io/queue-name"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_MANAGED_BY_VALUE = "phaze"
_FILE_ID_LABEL = "phaze.dev/file-id"
# A2 (de-risked): the precise Workload->Job linkage label is a Phase-56 live-cluster verification
# item; get_workload_for falls back to an owner-reference match when this label lookup misses.
_JOB_UID_LABEL = "kueue.x-k8s.io/job-uid"


class KubeStagingError(RuntimeError):
    """Raised when the kube staging substrate is unconfigured or a control-side kube call fails.

    Fail-loud (cf. ``S3StagingError``): an unset ``kube_api_url`` / ``kube_namespace`` /
    ``kube_local_queue`` is an operator misconfiguration that must surface immediately, never a
    silent no-op that would leave a file un-submitted.
    """


def job_name(file_id: uuid.UUID) -> str:
    """Return the deterministic, ``file_id``-scoped Job name (KSUBMIT-01, T-54-06).

    ``phaze-analyze-<file_id>`` where ``file_id`` is a server-generated UUID -- DNS-1123 safe
    (14 + 36 = 50 chars, well under 63) and injection-free (no operator free-text enters the kube
    object name). The same ``file_id`` always maps to the same name, so a duplicate submit hits a
    409 (idempotency for free).
    """
    return f"phaze-analyze-{file_id}"


def _kube_config() -> ControlSettings:
    """Return ControlSettings with the kube staging surface validated as present.

    Raises ``KubeStagingError`` if ``kube_api_url`` / ``kube_namespace`` / ``kube_local_queue`` is
    unset so a submit/reconcile never proceeds against a half-configured cluster.
    """
    cfg = cast("ControlSettings", get_settings())
    if not cfg.kube_api_url or not cfg.kube_namespace or not cfg.kube_local_queue:
        raise KubeStagingError(
            "Kube staging requires kube_api_url, kube_namespace, and kube_local_queue to be configured "
            "(set PHAZE_KUBE_API_URL / PHAZE_KUBE_NAMESPACE / PHAZE_KUBE_LOCAL_QUEUE)"
        )
    return cfg


async def _api(cfg: ControlSettings) -> Any:
    """Build the async kr8s API client from the ControlSettings kube surface.

    The control plane runs OUTSIDE the cluster (home server, reaching the API over
    Tailscale/WireGuard), so it authenticates via the operator-provided ``kube_api_url`` plus an
    optional ServiceAccount bearer token from the ``_FILE``-resolved ``SecretStr`` field. The token
    is set on the auth object and never logged (T-54-07). The exact auth/constructor form is a
    Phase-56 live-cluster verification item (RESEARCH Q3, deferred).
    """
    api = await kr8s.asyncio.api(url=cfg.kube_api_url, namespace=cfg.kube_namespace)
    token = cfg.kube_sa_token.get_secret_value() if cfg.kube_sa_token else None
    if token:
        # kr8s bakes ``Authorization: Bearer <token>`` into its httpx session at session-CREATION time
        # (kr8s 0.20.15 ``_api._create_session``), and that session was already built during the
        # ``api()`` version-check -- so a post-construction ``auth.token`` assignment alone never reaches
        # the wire. Set the token, then rebuild the session so the Bearer header actually applies. The
        # token is never logged (T-54-07); exact auth form stays a Phase-56 live-cluster item (RESEARCH Q3).
        api.auth.token = token
        await api._create_session()
    return api


def build_job_manifest(file_id: uuid.UUID, cfg: ControlSettings) -> dict[str, Any]:
    """Build the suspended ``batch/v1`` Job manifest phaze submits (KSUBMIT-01/05).

    Exactly one object phaze writes: ``suspend: true`` (never starts a pod before Kueue gates it),
    ``parallelism/completions: 1``, ``backoffLimit: 0`` (KSUBMIT-05 -- the first pod failure is
    immediately terminal; pod-level retry neutralized, control plane owns retry),
    ``ttlSecondsAfterFinished`` = ``JOB_TTL_SECONDS`` (D-04 orphan backstop only),
    ``restartPolicy: Never``, the ``kueue.x-k8s.io/queue-name`` label ON THE JOB (Kueue reads it
    off the Job, not the pod template), and ``resources.requests`` ONLY -- NO ``limits`` (Kueue's
    quota accounting reads requests; Q1 RESOLVED-adopted: requests-only is locked).

    The internal CA is MOUNTED at runtime, not baked into the image (Phase 56, KJOB-05 reversed ->
    KDEPLOY-06): the pod spec carries a ``phaze-ca`` volume sourced from the operator-created Secret
    named by ``kube_ca_secret_name`` (key ``phaze-ca.crt``), mounted read-only at ``/certs``, and
    the container sets ``PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`` so the one-shot callback verifies
    the control-plane TLS chain (never ``verify=False``). CA rotation = Secret update + re-submit.

    Fail-loud on an unset ``kube_job_image`` / ``kube_job_cpu_request`` / ``kube_job_memory_request``
    (all ``Optional`` in Phase 54): a half-configured manifest would otherwise carry ``None`` values
    and surface as an opaque non-409 ``KubeStagingError`` from the kube API, instead of naming the
    missing operator variable. Mirrors the connection-field discipline in :func:`_kube_config`.
    """
    missing = [
        name
        for name, value in (
            ("kube_job_image", cfg.kube_job_image),
            ("kube_job_cpu_request", cfg.kube_job_cpu_request),
            ("kube_job_memory_request", cfg.kube_job_memory_request),
        )
        if not value
    ]
    if missing:
        raise KubeStagingError(
            f"Kube Job submission requires {', '.join(missing)} to be configured (set {' / '.join(f'PHAZE_{name.upper()}' for name in missing)})"
        )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(file_id),
            "namespace": cfg.kube_namespace,
            "labels": {
                _QUEUE_NAME_LABEL: cfg.kube_local_queue,
                _MANAGED_BY_LABEL: _MANAGED_BY_VALUE,
                _FILE_ID_LABEL: str(file_id),
            },
        },
        "spec": {
            "suspend": True,
            "parallelism": 1,
            "completions": 1,
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    # The internal CA is MOUNTED from the operator-created Secret at runtime, NOT
                    # baked into the image (KJOB-05 reversed -> KDEPLOY-06). The Secret named by
                    # kube_ca_secret_name carries key `phaze-ca.crt`; mounting it read-only at
                    # /certs surfaces /certs/phaze-ca.crt, which PHAZE_AGENT_CA_FILE (below) points
                    # construct_agent_client at to verify the control-plane TLS chain (never
                    # verify=False). Rotation is a Secret update + re-submit -- no image rebuild.
                    "volumes": [
                        {
                            "name": "phaze-ca",
                            "secret": {"secretName": cfg.kube_ca_secret_name},
                        }
                    ],
                    "containers": [
                        {
                            "name": "analyze",
                            "image": cfg.kube_job_image,
                            # Two env sources with distinct lifecycles (JOB-ENV-CONTRACT):
                            #   - `env`: the PER-JOB PHAZE_JOB_FILE_ID, code-injected here because it
                            #     varies per submit and CANNOT come from a static ConfigMap/Secret.
                            #     job_runner reads it and sys.exit(EXIT_CONFIG)=20 if it is absent.
                            #     (PHAZE_AGENT_CA_FILE stays here too -- it points at the mounted CA.)
                            #   - `envFrom`: the STATIC-per-deployment agent env (PHAZE_ROLE=agent,
                            #     PHAZE_AGENT_API_URL, PHAZE_MODELS_DIR from the ConfigMap;
                            #     PHAZE_AGENT_TOKEN from the Secret) the pod entrypoint requires to
                            #     build AgentSettings + call back. Both objects are operator-created;
                            #     phaze references them by name only (kube_env_*_name).
                            "env": [
                                {"name": "PHAZE_AGENT_CA_FILE", "value": "/certs/phaze-ca.crt"},
                                {"name": "PHAZE_JOB_FILE_ID", "value": str(file_id)},
                            ],
                            "envFrom": [
                                {"configMapRef": {"name": cfg.kube_env_configmap_name}},
                                {"secretRef": {"name": cfg.kube_env_secret_name}},
                            ],
                            "volumeMounts": [
                                {"name": "phaze-ca", "mountPath": "/certs", "readOnly": True},
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": cfg.kube_job_cpu_request,
                                    "memory": cfg.kube_job_memory_request,
                                },
                            },
                        }
                    ],
                },
            },
        },
    }


async def submit_job(file_id: uuid.UUID) -> tuple[str, str]:
    """Submit the suspended Job for ``file_id`` idempotently; return ``(name, uid)`` (KSUBMIT-01).

    One fast kube POST. The deterministic name means a duplicate submit hits a 409 AlreadyExists --
    swallowed by refreshing the existing object (no error, no duplicate) so a re-drive after a
    partial run is safe. Any non-409 server error surfaces as ``KubeStagingError``.
    """
    cfg = _kube_config()
    api = await _api(cfg)
    job = Job(build_job_manifest(file_id, cfg), api=api)
    try:
        await job.create()
    except kr8s.ServerError as exc:
        if getattr(exc.response, "status_code", None) == 409:
            await job.refresh()  # load the existing object's uid/status -- idempotent
        else:
            raise KubeStagingError(f"failed to submit job for {file_id}") from exc
    return job.name, str(job.metadata.get("uid", ""))


async def get_job(name: str) -> Any:
    """Fetch the Job by name (its ``status`` carries succeeded/failed -- the terminal signals)."""
    cfg = _kube_config()
    api = await _api(cfg)
    job = Job({"metadata": {"name": name, "namespace": cfg.kube_namespace}}, api=api)
    await job.refresh()
    return job


async def get_local_queue() -> Any:
    """GET the configured Kueue LocalQueue by name (Phase 56, KDEPLOY-04 reachability probe).

    Mirrors :func:`get_job`: construct-by-name + ``refresh()``. The LocalQueue lives in the same
    ``kueue.x-k8s.io`` group as the Workload, so it reuses ``kube_workload_api_version`` via
    ``new_class`` (no new import). This service RAISES -- it never swallows: ``refresh()`` raises
    ``kr8s.NotFoundError`` on a 404 (the queue is mis-named / absent -> operator misconfig) and a
    generic ``kr8s.ServerError`` on a transient kube-API/mesh failure. The non-fatal catch belongs to
    the controller.startup caller (D-05/D-06), which treats BOTH 404 and transient errors as
    "unreachable" and flags it without aborting boot.
    """
    cfg = _kube_config()
    api = await _api(cfg)
    local_queue_cls = new_class(kind="LocalQueue", version=cfg.kube_workload_api_version, namespaced=True)
    local_queue = local_queue_cls({"metadata": {"name": cfg.kube_local_queue, "namespace": cfg.kube_namespace}}, api=api)
    await local_queue.refresh()
    return local_queue


async def list_inflight_jobs() -> list[Any]:
    """Reserved orphan-Job sweep -- built + tested here, intentionally NOT invoked in Phase 54.

    Reconcile iterates the ``cloud_job`` sidecar per D-02, NOT this label-list; this verb is the
    cross-check / orphan-Job sweep capability reserved for a future tick. Do NOT treat the unused
    export as dead code -- it is exercised by the seam tests and wired by a later phase.
    """
    cfg = _kube_config()
    api = await _api(cfg)
    return [job async for job in Job.list(namespace=cfg.kube_namespace, label_selector={_MANAGED_BY_LABEL: _MANAGED_BY_VALUE}, api=api)]


async def get_workload_for(job_uid: str) -> Any | None:
    """Resolve the Kueue Workload paired with ``job_uid`` (KSUBMIT-04, A2 de-risk).

    Tries the ``kueue.x-k8s.io/job-uid`` label selector first; on an EMPTY result, falls back to
    scanning the namespace Workloads and returning the one whose ``metadata.ownerReferences[*].uid``
    equals ``job_uid``. Returns ``None`` only when BOTH the label lookup and the owner-ref scan
    miss -- so a wrong/changed live label key degrades to the fallback instead of silently leaving
    admission state unreadable (the exact live label key is verified in Phase 56).
    """
    cfg = _kube_config()
    api = await _api(cfg)
    workload_cls = new_class(kind="Workload", version=cfg.kube_workload_api_version, namespaced=True)

    by_label = [wl async for wl in workload_cls.list(namespace=cfg.kube_namespace, label_selector={_JOB_UID_LABEL: job_uid}, api=api)]
    if by_label:
        return by_label[0]

    async for wl in workload_cls.list(namespace=cfg.kube_namespace, api=api):
        workload = cast("Any", wl)
        for ref in workload.metadata.get("ownerReferences", []) or []:
            if ref.get("uid") == job_uid:
                return workload
    return None


async def delete_job(name: str) -> None:
    """Delete the Job (Kueue GCs the owned Workload) -- idempotent on 404 (KSUBMIT-06, T-54-09).

    ``Background`` propagation removes the Job and lets Kueue garbage-collect the paired Workload.
    A missing Job is the desired end state, so a ``NotFoundError`` (404) is swallowed -- safe to
    re-run after a partial reconcile tick. Any other error surfaces as ``KubeStagingError``.
    """
    cfg = _kube_config()
    api = await _api(cfg)
    job = Job({"metadata": {"name": name, "namespace": cfg.kube_namespace}}, api=api)
    try:
        await job.delete(propagation_policy="Background")
    except kr8s.NotFoundError:
        return
    except kr8s.ServerError as exc:
        raise KubeStagingError(f"failed to delete job {name}") from exc
