"""Control-plane Kubernetes (Kueue) Job-staging service (Phase 54, Plan 03 -- KSUBMIT-01/05/06).

The single home of every kr8s call in the system. The control plane builds the suspended
``batch/v1`` Job manifest, submits it (idempotently), lists in-flight Jobs, resolves the paired
Kueue ``Workload`` to read admission state, and deletes a finished Job -- but it carries NO
analysis payload and reads NO result here. Kube credentials live on the control plane only
(DIST-01); the file-server agent and the one-shot pod are kube-credential-free.

Structure mirrors ``s3_staging.py`` verbatim: ``__future__`` annotations, a ``TYPE_CHECKING``
guard, a fail-loud custom error, a ``_require_kube()`` validation gate, an async client factory,
and the idempotent-delete idiom (swallow already-absent). There are NO ORM imports here -- the
service is pure kr8s keyed by ``file_id`` (reconcile-by-file_id; the deterministic Job name
``phaze-analyze-<file_id>`` is the single object identity, no per-attempt suffixes).

Phase 70 (MKUE-01/D-04): every verb takes an explicit ``kube: KubeConfig`` (the module-global
``active_kube`` read is RETIRED), so ONE control plane reaches N distinct clusters -- each verb
authenticates against THIS file's backend cluster. The kr8s client is built via constructor-time auth
from a synthesized in-memory kubeconfig dict (``kubeconfig``+``context`` parses the operator YAML;
``api_url``+``sa_token`` synthesizes a minimal dict) -- the fragile post-construction bearer-token
session-rebuild hack (kr8s private-API) is gone. Distinct kubeconfig dicts key distinct cached kr8s
clients (verified). Credentials come from the ``_FILE``-resolved ``SecretStr`` fields and are never
logged (T-54-07); the synthesized dict is in-memory only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import kr8s
import kr8s.asyncio
from kr8s.asyncio.objects import Job, new_class
import yaml


if TYPE_CHECKING:
    import uuid

    from phaze.config_backends import KubeConfig


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

    Fail-loud (cf. ``S3StagingError``): a missing active kueue backend or an unset
    ``api_url`` / ``namespace`` / ``local_queue`` on its ``[kube]`` config is an operator
    misconfiguration that must surface immediately, never a silent no-op that would leave a file
    un-submitted.
    """


def job_name(file_id: uuid.UUID) -> str:
    """Return the deterministic, ``file_id``-scoped Job name (KSUBMIT-01, T-54-06).

    ``phaze-analyze-<file_id>`` where ``file_id`` is a server-generated UUID -- DNS-1123 safe
    (14 + 36 = 50 chars, well under 63) and injection-free (no operator free-text enters the kube
    object name). The same ``file_id`` always maps to the same name, so a duplicate submit hits a
    409 (idempotency for free).
    """
    return f"phaze-analyze-{file_id}"


def _require_kube(kube: KubeConfig) -> None:
    """Fail loud if THIS backend's ``[kube]`` connection surface is half-configured (D-04 guard).

    Replaces the retired module-global active-kube reader: the caller now threads the
    per-backend ``KubeConfig`` directly, so the fail-loud validation moves here. Auth comes from EITHER
    an inline ``kubeconfig`` (the YAML carries the server) OR an explicit ``api_url`` (the synthesized
    form); ``namespace`` + ``local_queue`` are phaze-level config used in every manifest/probe
    regardless of auth form. A missing piece raises ``KubeStagingError`` so a submit/reconcile never
    proceeds against a half-configured cluster.
    """
    has_auth = kube.kubeconfig is not None or bool(kube.api_url)
    if not has_auth or not kube.namespace or not kube.local_queue:
        raise KubeStagingError(
            "Kube staging requires a configured [kube] backend with (kubeconfig OR api_url), namespace, "
            "and local_queue set in its [kube] table (backends.toml)"
        )


def _kubeconfig_dict_from(kube: KubeConfig) -> dict[str, Any]:
    """Build an in-memory kubeconfig dict from THIS backend's KubeConfig (D-04, verified live kr8s 0.20.15).

    Two auth forms unify onto one constructor-time mechanism:

    * ``kubeconfig``+``context``: the ``kubeconfig`` field holds raw YAML *content* (a ``SecretStr``,
      not a path -- config_backends resolves ``kubeconfig_file`` verbatim), so parse it to a dict; NO
      secret touches disk.
    * ``api_url``+``sa_token``: synthesize a minimal single-context kubeconfig carrying the server +
      (optional) bearer token + namespace.

    The dict is in-memory only and NEVER logged (T-54-07). kr8s ``hash_kwargs`` json-serializes the
    dict for its client cache key, so distinct dicts (distinct clusters) key distinct cached clients.
    """
    if kube.kubeconfig is not None:
        try:
            parsed = yaml.safe_load(kube.kubeconfig.get_secret_value())
        except yaml.YAMLError as exc:
            # PyYAML's MarkedYAMLError subclasses embed a verbatim snippet of the offending
            # document line (bearer tokens, client-key-data) in str(exc) via problem_mark's
            # get_snippet(). Re-raise sanitized -- location only, never the document text -- and
            # suppress the chained cause (`from None`) so the snippet-bearing exception never
            # propagates to a logger (T-54-07).
            location = ""
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                location = f" at line {mark.line + 1}, column {mark.column + 1}"
            raise KubeStagingError(f"Kueue backend kubeconfig is not valid YAML ({type(exc).__name__}{location})") from None
        if not isinstance(parsed, dict):
            raise KubeStagingError("Kueue backend kubeconfig did not parse to a YAML mapping")
        return cast("dict[str, Any]", parsed)
    token = kube.sa_token.get_secret_value() if kube.sa_token else None
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "phaze", "cluster": {"server": kube.api_url}}],
        "users": [{"name": "phaze", "user": ({"token": token} if token else {})}],
        "contexts": [{"name": "phaze", "context": {"cluster": "phaze", "user": "phaze", "namespace": kube.namespace}}],
        "current-context": "phaze",
    }


async def _api(kube: KubeConfig) -> Any:
    """Build the async kr8s client for THIS backend via constructor-time auth (D-04, MKUE-01).

    The control plane runs OUTSIDE the cluster (home server, reaching the API over
    Tailscale/WireGuard). It authenticates from a synthesized in-memory kubeconfig dict
    (:func:`_kubeconfig_dict_from`) -- kr8s ``KubeAuth`` loads the server, bearer token, and namespace
    from the dict with NO network call and NO post-construction session rebuild (the retired hack).
    NEVER call ``kr8s.asyncio.api()`` with no args -- that returns an arbitrary cached client (wrong
    cluster in N-cluster mode). The token/dict are never logged (T-54-07).
    """
    _require_kube(kube)
    kc = _kubeconfig_dict_from(kube)
    context = kube.context if kube.context else None
    # kr8s.asyncio.api types ``kubeconfig`` as ``str | None`` (a path), but ``KubeConfigSet`` accepts a
    # dict at runtime (``Union[PathType, dict]``); pass the in-memory dict (cast past the narrow stub).
    return await kr8s.asyncio.api(kubeconfig=cast("Any", kc), namespace=kube.namespace, context=context)


def build_job_manifest(file_id: uuid.UUID, kube: KubeConfig) -> dict[str, Any]:
    """Build the suspended ``batch/v1`` Job manifest phaze submits (KSUBMIT-01/05).

    Exactly one object phaze writes: ``suspend: true`` (never starts a pod before Kueue gates it),
    ``parallelism/completions: 1``, ``backoffLimit: 0`` (KSUBMIT-05 -- the first pod failure is
    immediately terminal; pod-level retry neutralized, control plane owns retry),
    ``ttlSecondsAfterFinished`` = ``JOB_TTL_SECONDS`` (D-04 orphan backstop only),
    ``activeDeadlineSeconds`` = ``kube.active_deadline_seconds`` (phaze-1b39 -- the pipeline's ONLY
    wall-clock bound; job_runner delegates all of it here and ttlSecondsAfterFinished cannot help a
    Job that never finishes),
    ``restartPolicy: Never``, the ``kueue.x-k8s.io/queue-name`` label ON THE JOB (Kueue reads it
    off the Job, not the pod template), and ``resources.requests`` ONLY -- NO ``limits`` (Kueue's
    quota accounting reads requests; Q1 RESOLVED-adopted: requests-only is locked).

    The internal CA is MOUNTED at runtime, not baked into the image (Phase 56, KJOB-05 reversed ->
    KDEPLOY-06): the pod spec carries a ``phaze-ca`` volume sourced from the operator-created Secret
    named by ``kube_ca_secret_name`` (key ``phaze-ca.crt``), mounted read-only at ``/certs``, and
    the container sets ``PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`` so the one-shot callback verifies
    the control-plane TLS chain (never ``verify=False``). CA rotation = Secret update + re-submit.

    Optional models PVC (backward-compatible): when ``kube.models_pvc_name`` is set, the pod gains a
    SECOND, entirely separate volume -- a ``models`` ``persistentVolumeClaim`` (``readOnly``) mounted
    read-only at ``/models`` -- so the analyze container reads its essentia weights from an
    operator-provisioned, ReadOnlyMany PVC instead of a fat image or a runtime download (the image
    ships weights-free; ``job_runner`` never downloads them). **INVARIANT:** the ``/models`` mountPath
    MUST equal the agent-env ConfigMap's ``PHAZE_MODELS_DIR`` (default ``/models``) -- the container
    reads weights from ``PHAZE_MODELS_DIR``, so a drift would mount the PVC where nothing looks for it.
    phaze creates no PV/PVC and references the claim by name only (same posture as the LocalQueue /
    Secret / ConfigMap it references by name). When ``models_pvc_name`` is None, NO models volume/mount
    is emitted -- the manifest is byte-identical to the CA-only form (regression-guarded). The PVC
    carries ONLY model weights, never secrets/certs (the CA stays on its own ``/certs`` Secret mount).

    Fail-loud on an unset ``job_image`` / ``cpu_request`` / ``memory_request`` (all ``Optional`` on
    ``KubeConfig``): a half-configured manifest would otherwise carry ``None`` values and surface as
    an opaque non-409 ``KubeStagingError`` from the kube API, instead of naming the missing operator
    field. Mirrors the connection-field discipline in :func:`_require_kube`.
    """
    missing = [
        name
        for name, value in (
            ("job_image", kube.job_image),
            ("cpu_request", kube.cpu_request),
            ("memory_request", kube.memory_request),
        )
        if not value
    ]
    if missing:
        raise KubeStagingError(
            f"Kube Job submission requires {', '.join(missing)} to be configured in the active backend's [kube] config (backends.toml)"
        )
    manifest: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(file_id),
            "namespace": kube.namespace,
            "labels": {
                _QUEUE_NAME_LABEL: kube.local_queue,
                _MANAGED_BY_LABEL: _MANAGED_BY_VALUE,
                _FILE_ID_LABEL: str(file_id),
            },
        },
        "spec": {
            "suspend": True,
            "parallelism": 1,
            "completions": 1,
            "backoffLimit": 0,
            # phaze-1b39: the ONLY wall-clock bound in the whole pipeline. job_runner's exit-code
            # contract delegates all wall-clock bounding here (the analyze stage runs timeout=None), and
            # ttlSecondsAfterFinished below only fires AFTER a Job finishes -- so it can never rescue a
            # Job that never finishes. Without activeDeadlineSeconds an admitted-but-stalled pod stays
            # non-terminal forever and reconcile re-affirms RUNNING every tick, permanently consuming a
            # burst-lane cap slot. With it, k8s SIGTERMs the pod (-> exit 143) and marks the Job Failed,
            # which the reconcile loop already routes to _handle_no_callback_terminal (re-drive/spill).
            "activeDeadlineSeconds": kube.active_deadline_seconds,
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
                            "secret": {"secretName": kube.ca_secret_name},
                        }
                    ],
                    "containers": [
                        {
                            "name": "analyze",
                            "image": kube.job_image,
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
                                {"configMapRef": {"name": kube.env_configmap_name}},
                                {"secretRef": {"name": kube.env_secret_name}},
                            ],
                            "volumeMounts": [
                                {"name": "phaze-ca", "mountPath": "/certs", "readOnly": True},
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": kube.cpu_request,
                                    "memory": kube.memory_request,
                                },
                            },
                        }
                    ],
                },
            },
        },
    }
    # Optional models PVC (additive, entirely separate from the phaze-ca Secret mount above). When set,
    # mount the operator-provisioned claim read-only at /models (== PHAZE_MODELS_DIR) so the analyze
    # container reads essentia weights from provisioned storage. Unset -> no models volume/mount is
    # emitted, so the manifest stays byte-identical to the CA-only form (regression-guarded).
    if kube.models_pvc_name:
        pod_spec = manifest["spec"]["template"]["spec"]
        pod_spec["volumes"].append(
            {
                "name": "models",
                "persistentVolumeClaim": {"claimName": kube.models_pvc_name, "readOnly": True},
            }
        )
        pod_spec["containers"][0]["volumeMounts"].append({"name": "models", "mountPath": "/models", "readOnly": True})
    return manifest


async def submit_job(file_id: uuid.UUID, kube: KubeConfig) -> tuple[str, str]:
    """Submit the suspended Job for ``file_id`` to ``kube``'s cluster idempotently; return ``(name, uid)`` (KSUBMIT-01).

    One fast kube POST against THIS file's backend cluster (``kube``). The deterministic name means a
    duplicate submit hits a 409 AlreadyExists -- swallowed by refreshing the existing object (no error,
    no duplicate) so a re-drive after a partial run is safe. Any non-409 server error surfaces as
    ``KubeStagingError``.
    """
    api = await _api(kube)
    job = Job(build_job_manifest(file_id, kube), api=api)
    try:
        await job.create()
    except kr8s.ServerError as exc:
        if getattr(exc.response, "status_code", None) == 409:
            await job.refresh()  # load the existing object's uid/status -- idempotent
        else:
            raise KubeStagingError(f"failed to submit job for {file_id}") from exc
    return job.name, str(job.metadata.get("uid", ""))


async def get_job(name: str, kube: KubeConfig) -> Any:
    """Fetch the Job by name from ``kube``'s cluster (its ``status`` carries succeeded/failed -- the terminal signals)."""
    api = await _api(kube)
    job = Job({"metadata": {"name": name, "namespace": kube.namespace}}, api=api)
    await job.refresh()
    return job


async def get_local_queue(kube: KubeConfig) -> Any:
    """GET ``kube``'s configured Kueue LocalQueue by name (Phase 56, KDEPLOY-04; MKUE-03 per-cluster probe).

    Mirrors :func:`get_job`: construct-by-name + ``refresh()``. The LocalQueue lives in the same
    ``kueue.x-k8s.io`` group as the Workload, so it reuses ``kube_workload_api_version`` via
    ``new_class`` (no new import). This service RAISES -- it never swallows: ``refresh()`` raises
    ``kr8s.NotFoundError`` on a 404 (the queue is mis-named / absent -> operator misconfig) and a
    generic ``kr8s.ServerError`` on a transient kube-API/mesh failure. The non-fatal catch belongs to
    the per-cluster caller (``KueueBackend.is_available`` / controller.startup, D-05/D-06), which treats
    BOTH 404 and transient errors as "unreachable" and flags it without aborting boot.
    """
    api = await _api(kube)
    local_queue_cls = new_class(kind="LocalQueue", version=kube.workload_api_version, namespaced=True)
    local_queue = local_queue_cls({"metadata": {"name": kube.local_queue, "namespace": kube.namespace}}, api=api)
    await local_queue.refresh()
    return local_queue


async def list_inflight_jobs(kube: KubeConfig) -> list[Any]:
    """Reserved orphan-Job sweep on ``kube``'s cluster -- built + tested here, intentionally NOT invoked in Phase 54.

    Reconcile iterates the ``cloud_job`` sidecar per D-02, NOT this label-list; this verb is the
    cross-check / orphan-Job sweep capability reserved for a future tick. Do NOT treat the unused
    export as dead code -- it is exercised by the seam tests and wired by a later phase.
    """
    api = await _api(kube)
    return [job async for job in Job.list(namespace=kube.namespace, label_selector={_MANAGED_BY_LABEL: _MANAGED_BY_VALUE}, api=api)]


async def get_workload_for(job_uid: str, kube: KubeConfig) -> Any | None:
    """Resolve the Kueue Workload paired with ``job_uid`` on ``kube``'s cluster (KSUBMIT-04, A2 de-risk).

    Tries the ``kueue.x-k8s.io/job-uid`` label selector first; on an EMPTY result, falls back to
    scanning the namespace Workloads and returning the one whose ``metadata.ownerReferences[*].uid``
    equals ``job_uid``. Returns ``None`` only when BOTH the label lookup and the owner-ref scan
    miss -- so a wrong/changed live label key degrades to the fallback instead of silently leaving
    admission state unreadable (the exact live label key is verified in Phase 56).
    """
    api = await _api(kube)
    workload_cls = new_class(kind="Workload", version=kube.workload_api_version, namespaced=True)

    by_label = [wl async for wl in workload_cls.list(namespace=kube.namespace, label_selector={_JOB_UID_LABEL: job_uid}, api=api)]
    if by_label:
        return by_label[0]

    async for wl in workload_cls.list(namespace=kube.namespace, api=api):
        workload = cast("Any", wl)
        for ref in workload.metadata.get("ownerReferences", []) or []:
            if ref.get("uid") == job_uid:
                return workload
    return None


async def delete_job(name: str, kube: KubeConfig) -> None:
    """Delete the Job on ``kube``'s cluster (Kueue GCs the owned Workload) -- idempotent on 404 (KSUBMIT-06, T-54-09).

    ``Background`` propagation removes the Job and lets Kueue garbage-collect the paired Workload.
    A missing Job is the desired end state, so a ``NotFoundError`` (404) is swallowed -- safe to
    re-run after a partial reconcile tick. Any other error surfaces as ``KubeStagingError``.
    """
    api = await _api(kube)
    job = Job({"metadata": {"name": name, "namespace": kube.namespace}}, api=api)
    try:
        await job.delete(propagation_policy="Background")
    except kr8s.NotFoundError:
        return
    except kr8s.ServerError as exc:
        raise KubeStagingError(f"failed to delete job {name}") from exc
