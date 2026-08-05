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

phaze-202e adds the POD surface (:func:`list_pods_for_job`) and the pure wedge classifier
(:func:`classify_job_pods`). The Job manifest no longer carries ``activeDeadlineSeconds`` by default,
so nothing kills a long analyze; the question "is this Job wedged?" is answered from pod state
instead, and a Running pod is never a wedge at any age. The classifier is deliberately pure and
HTTP-free so the whole decision table is testable without a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import kr8s
import kr8s.asyncio
from kr8s.asyncio.objects import Job, Pod, new_class
import yaml


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from phaze.config_backends import KubeConfig


# 15 min = 3x the */5 reconcile tick. D-04 makes the explicit delete-after-record primary, so the
# TTL only ever fires in the "phaze never reconciled at all" orphan case (Pitfall 1 -- never a
# config knob; consistent with the fixed */5 cron, D-03).
JOB_TTL_SECONDS = 900

# phaze-4xks: the one-shot analyze pod is ALWAYS a "compute" agent (it owns no scan roots -- it
# analyzes exactly the one file named by PHAZE_JOB_FILE_ID and calls back, it never walks a
# filesystem). AgentSettings.kind defaults to "fileserver" (config.py), and
# _enforce_required_agent_fields raises unless kind == "compute" or scan_roots is set; the
# one-shot pod has neither a compensating ENV in Dockerfile.job nor an .env file, so nothing else
# supplies this. Code-injecting the literal here (never operator-configurable, never per-job)
# guarantees every analyze pod passes settings validation regardless of what the operator's
# documented phaze-agent-env ConfigMap (docs/k8s-burst.md §6) does or does not carry.
_ANALYZE_AGENT_KIND = "compute"

_QUEUE_NAME_LABEL = "kueue.x-k8s.io/queue-name"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_MANAGED_BY_VALUE = "phaze"
_FILE_ID_LABEL = "phaze.dev/file-id"
# A2 (de-risked): the precise Workload->Job linkage label is a Phase-56 live-cluster verification
# item; get_workload_for falls back to an owner-reference match when this label lookup misses.
_JOB_UID_LABEL = "kueue.x-k8s.io/job-uid"
# phaze-202e pod discovery. k8s >=1.27 stamps the prefixed key on Job-owned pods; older clusters use
# the un-prefixed legacy one. list_pods_for_job tries them in this order (modern first).
_JOB_NAME_LABEL = "batch.kubernetes.io/job-name"
_LEGACY_JOB_NAME_LABEL = "job-name"
_REASON_UNSCHEDULABLE = "Unschedulable"

# phaze-202e: pod phases that mean "this pod is, or was, genuinely doing the work". Succeeded is
# included so a success whose callback is still in flight is never mistaken for a wedge.
_ALIVE_POD_PHASES = frozenset({"Running", "Succeeded"})

# phaze-202e: container ``state.waiting.reason`` values that mean the container can NEVER start
# without operator action -- the pod is dead before it began, so no amount of waiting helps. Every one
# of these is a phaze-1b39 wedge shape: a bad/absent image, or an unresolvable ConfigMap/Secret
# reference (exactly what a missing ``phaze-agent-env`` / ``phaze-agent-token`` produces). Deliberately
# NARROW: transient-looking reasons (``ContainerCreating``, ``PodInitializing``) are excluded, because a
# false positive here burns a cloud attempt on healthy work.
DEAD_BEFORE_START_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
    }
)

# phaze-202e: how long ``PodScheduled=False/Unschedulable`` must PERSIST before it counts as a wedge
# ("unschedulable past a scheduling probe"). A brief unschedulable window is the normal shape of a
# cluster-autoscaler scale-up, so an instantaneous verdict would fight the autoscaler. 15 min = 3x the
# */5 reconcile tick, measured on the k8s condition's own ``lastTransitionTime`` -- this is a clock on
# SCHEDULING FAILURE, never on run time.
UNSCHEDULABLE_PROBE_SECONDS = 900


class PodLiveness(StrEnum):
    """phaze-202e verdict for a Job's pods -- the state-based replacement for the 1b39 wall clock."""

    ALIVE = "alive"
    """At least one pod is Running or Succeeded. NEVER terminalize, at any age."""

    DEAD_BEFORE_START = "dead_before_start"
    """A container is wedged in a fatal waiting reason (bad image / unresolvable ConfigMap-Secret)."""

    UNSCHEDULABLE = "unschedulable"
    """No pod could be scheduled, and it has stayed that way past the scheduling probe."""

    STARTING = "starting"
    """Healthy-pending, not-yet-populated, or unreadable. Hold -- proof of death is absent."""


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
    ``restartPolicy: Never``, the ``kueue.x-k8s.io/queue-name`` label ON THE JOB (Kueue reads it
    off the Job, not the pod template), and ``resources.requests`` ONLY -- NO ``limits`` (Kueue's
    quota accounting reads requests; Q1 RESOLVED-adopted: requests-only is locked).

    **SUPERSEDED (2026-08-04) -- ADR-0005** (``docs/design/0005-analyze-job-memory-limits.md``)
    supersedes the requests-only lock above on measured evidence from spike ``phaze-esut``. The
    lock rested on the premise that requests approximate actual usage; the spike measured a
    duration-INDEPENDENT 8.5-10.5 GiB floor that EVERY file exceeds (a 3.3-minute file peaks at
    9.73 GiB against an 8Gi request), and the absence of a limit is what made the resulting kills
    ``constraint=CONSTRAINT_NONE`` global OOMs that took out coredns/metrics-server/
    local-path-provisioner instead of cgroup-OOMKilling the offending pod. Note the stated
    rationale above is about ``requests`` (true, and ADR-0005 keeps requests authoritative) -- it
    never supported omitting ``limits``, which Kueue's quota accounting does not read.

    **phaze-k6d5 (this function, implementing ADR-0005):** when the optional
    ``kube.memory_limit`` is set, the analyze container gains ``resources.limits.memory`` --
    ``requests`` is untouched (Kueue's quota input stays authoritative), and NO CPU limit is
    emitted (a memory-only limit does not promote the pod's QoS class off Burstable -- see
    ``tests/analyze/services/test_kube_staging.py::test_build_job_manifest_memory_limit_keeps_qos_burstable``).
    When ``memory_limit`` is unset (the default), NO ``limits`` key is emitted at all -- the
    manifest is byte-identical to the pre-ADR-0005 form (regression-guarded), the same
    backward-compatibility posture already used for ``models_pvc_name`` /
    ``active_deadline_seconds``.

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
                            #   - `env`: code-injected literals CANNOT come from a static
                            #     ConfigMap/Secret -- PHAZE_JOB_FILE_ID varies per submit;
                            #     PHAZE_AGENT_CA_FILE points at the mounted CA; PHAZE_AGENT_KIND is
                            #     a fixed "compute" (phaze-4xks) because every one-shot analyze pod
                            #     IS a compute agent and AgentSettings.kind defaults to "fileserver"
                            #     -- without it _enforce_required_agent_fields raises before the pod
                            #     can call back. job_runner reads PHAZE_JOB_FILE_ID and
                            #     sys.exit(EXIT_CONFIG)=20 if it is absent; a missing/wrong
                            #     PHAZE_AGENT_KIND fails the same way, one layer up, in settings
                            #     validation.
                            #   - `envFrom`: the STATIC-per-deployment agent env (PHAZE_ROLE=agent,
                            #     PHAZE_AGENT_API_URL, PHAZE_MODELS_DIR from the ConfigMap;
                            #     PHAZE_AGENT_TOKEN from the Secret) the pod entrypoint requires to
                            #     build AgentSettings + call back. Both objects are operator-created;
                            #     phaze references them by name only (kube_env_*_name).
                            "env": [
                                {"name": "PHAZE_AGENT_CA_FILE", "value": "/certs/phaze-ca.crt"},
                                {"name": "PHAZE_JOB_FILE_ID", "value": str(file_id)},
                                {"name": "PHAZE_AGENT_KIND", "value": _ANALYZE_AGENT_KIND},
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
    # phaze-202e: the wall-clock Job deadline is OPT-IN and OFF by default. When
    # ``kube.active_deadline_seconds`` is None the key is ABSENT from the manifest entirely (not 0, not
    # a sentinel -- k8s treats an absent activeDeadlineSeconds as "no bound"), so a 2-6 h concert-set
    # analyze runs to completion instead of being SIGTERM'd mid-run. phaze-1b39 had this as a required
    # 3h bound; that killed every long recording at exactly 3h and burned the whole cloud attempt budget
    # per file. The wedged-pod protection 1b39 was reaching for now lives in reconcile as POD-STATE
    # detection (:func:`classify_job_pods`), which cannot mistake a slow analyze for a hang.
    if kube.active_deadline_seconds is not None:
        manifest["spec"]["activeDeadlineSeconds"] = kube.active_deadline_seconds
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
    # ADR-0005 (phaze-k6d5): OPT-IN memory limit, OFF by default. When set, the analyze container
    # gains `resources.limits.memory` so the kernel cgroup-OOMKills the offending pod instead of a
    # global, node-scoped OOM choosing a victim by oom_score_adj (the failure mode that killed
    # coredns/metrics-server/local-path-provisioner in production). `requests` is NOT touched --
    # Kueue's quota accounting reads requests only and is unaffected. Deliberately NO cpu limit
    # (memory-only keeps the pod QoS class Burstable, not Guaranteed -- see
    # test_build_job_manifest_memory_limit_keeps_qos_burstable). Unset (None, the default) -> NO
    # `limits` key at all, so the manifest stays byte-identical to the pre-ADR-0005 form
    # (regression-guarded by test_build_job_manifest_omits_memory_limit_by_default).
    if kube.memory_limit:
        manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {"memory": kube.memory_limit}
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


async def list_pods_for_job(name: str, kube: KubeConfig) -> list[Any]:
    """List the pods the Job ``name`` owns on ``kube``'s cluster -- the input to :func:`classify_job_pods`.

    phaze-202e: the pod is the ONLY surface that can tell a genuinely-running 4h analyze apart from a
    pod that can never start. The Job's own counters cannot (a Pending ImagePullBackOff pod and a
    Running pod both read ``active=1``), and a wall clock cannot either -- which is exactly why the
    phaze-1b39 ``activeDeadlineSeconds`` bound killed real work.

    Mirrors :func:`get_workload_for`'s label-uncertainty discipline: k8s 1.27+ labels Job pods
    ``batch.kubernetes.io/job-name``, older clusters use the un-prefixed ``job-name``. Try the modern
    key, fall back to the legacy one on an EMPTY result, so a cluster on either side of the rename
    still yields pods. Returns ``[]`` when both miss -- and callers MUST treat ``[]`` as "unknown",
    never as "dead": an empty list is indistinguishable from a label the cluster does not use, and
    terminalizing on it would mass-kill every in-flight run on a label drift.
    """
    api = await _api(kube)
    for label in (_JOB_NAME_LABEL, _LEGACY_JOB_NAME_LABEL):
        pods = [pod async for pod in Pod.list(namespace=kube.namespace, label_selector={label: name}, api=api)]
        if pods:
            return pods
    return []


def _pod_phase(pod: Any) -> str:
    """Return the pod's ``status.phase`` (``Pending`` / ``Running`` / ``Succeeded`` / ``Failed`` / ``Unknown``)."""
    status = getattr(pod, "status", None) or {}
    return str(status.get("phase") or "")


def _parse_k8s_time(value: Any) -> datetime | None:
    """Parse an RFC3339 kube timestamp (``2026-07-28T10:00:00Z``) to an aware UTC datetime, or None.

    Every clock this module reads is kube-side and OPTIONAL (a condition may carry no
    ``lastTransitionTime``, a fake may omit it). An unparseable/absent value returns None, which the
    probes below read as "cannot prove anything" -> hold. Never raises.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dead_before_start_reason(pod: Any) -> str | None:
    """Return the fatal ``state.waiting.reason`` on any container of ``pod``, or None.

    Scans BOTH ``initContainerStatuses`` and ``containerStatuses``: a wedged init container is just as
    permanently un-started as a wedged app container, and phaze's pod has no init container today only
    by accident of the current manifest.
    """
    status = getattr(pod, "status", None) or {}
    for key in ("initContainerStatuses", "containerStatuses"):
        for container in status.get(key, []) or []:
            reason = ((container.get("state") or {}).get("waiting") or {}).get("reason")
            if reason in DEAD_BEFORE_START_WAITING_REASONS:
                return str(reason)
    return None


def _unschedulable_since(pod: Any) -> datetime | None:
    """Return when ``pod`` went ``PodScheduled=False/Unschedulable``, or None if it is schedulable.

    A pod with the condition but NO readable ``lastTransitionTime`` returns the epoch-free ``None``
    only for the not-unschedulable case; when the condition IS present but its timestamp is
    unreadable the caller cannot run the probe, so this returns None there too (hold, do not kill).
    """
    status = getattr(pod, "status", None) or {}
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == "PodScheduled" and cond.get("status") == "False" and cond.get("reason") == _REASON_UNSCHEDULABLE:
            return _parse_k8s_time(cond.get("lastTransitionTime"))
    return None


def classify_job_pods(
    pods: Sequence[Any], *, now: datetime | None = None, unschedulable_probe_seconds: int = UNSCHEDULABLE_PROBE_SECONDS
) -> PodLiveness:
    """Classify a Job's pods into a liveness verdict -- PURE, the phaze-202e wedge detector.

    Replaces the phaze-1b39 wall clock. The ordering is the whole point and is deliberately
    alive-dominates: if ANY pod is Running or Succeeded the verdict is :attr:`PodLiveness.ALIVE`, no
    matter how long it has been running and no matter what its siblings look like. **A running analyze
    is never terminal**, at any age -- that invariant is what this function exists to encode.

    Only when nothing is alive does it look for proof of death:

    * :attr:`PodLiveness.DEAD_BEFORE_START` -- a container is waiting in a reason from
      :data:`DEAD_BEFORE_START_WAITING_REASONS` (bad image, unresolvable ConfigMap/Secret). These need
      operator action; k8s will retry the image pull forever on its own, so age adds no information
      and the verdict is immediate.
    * :attr:`PodLiveness.UNSCHEDULABLE` -- ``PodScheduled=False/Unschedulable`` that has PERSISTED past
      ``unschedulable_probe_seconds`` (the "scheduling probe"). A brief unschedulable window is normal
      while the cluster autoscales, so this one IS time-qualified -- but the clock is the k8s
      condition's own ``lastTransitionTime``, i.e. how long scheduling has been failing, never how
      long the analysis has been running.
    * :attr:`PodLiveness.STARTING` -- anything else, including an empty ``pods``: a healthy Pending
      pod, a pod whose status is not yet populated, or a pod list that came back empty. Held, never
      terminalized (see :func:`list_pods_for_job` on why an empty list must not mean "dead").
    """
    reference = now or datetime.now(UTC)
    if any(_pod_phase(pod) in _ALIVE_POD_PHASES for pod in pods):
        return PodLiveness.ALIVE
    if any(_dead_before_start_reason(pod) is not None for pod in pods):
        return PodLiveness.DEAD_BEFORE_START
    for pod in pods:
        since = _unschedulable_since(pod)
        if since is not None and (reference - since).total_seconds() > unschedulable_probe_seconds:
            return PodLiveness.UNSCHEDULABLE
    return PodLiveness.STARTING


def job_started_at(job: Any) -> datetime | None:
    """Return the Job's ``status.startTime`` (when Kueue un-suspended it), or None if unreadable.

    This is a POD-SIDE clock, not a run clock: it answers "has this Job been un-gated long enough that
    a pod should exist by now", which is the only question the zero-pod wedge probe asks. It is never
    compared against how long an analysis has been running.
    """
    status = getattr(job, "status", None) or {}
    return _parse_k8s_time(status.get("startTime"))


def job_is_suspended(job: Any) -> bool:
    """Return whether the Job is still Kueue-gated (``spec.suspend``) -- a suspended Job has no pod BY DESIGN."""
    spec = getattr(job, "spec", None) or {}
    return bool(spec.get("suspend"))


def describe_job_pods(pods: Sequence[Any]) -> str:
    """Render a compact ``phase/reason`` summary of ``pods`` for the reconcile warning log (never raises)."""
    parts: list[str] = []
    for pod in pods:
        reason = _dead_before_start_reason(pod)
        parts.append(f"{_pod_phase(pod) or 'unknown'}{'/' + reason if reason else ''}")
    return ",".join(parts) if parts else "no-pods"


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
