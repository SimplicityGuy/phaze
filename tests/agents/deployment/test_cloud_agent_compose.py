"""Phase 51 D-05..D-08, CLOUDDEPLOY-01: docker-compose.cloud-agent.yml structural assertions.

Pure YAML-parse tests for the OCI A1 cloud compute-agent compose file (no docker
daemon), mirroring ``tests/test_deployment/test_agent_compose.py``.

Covers the cloud-agent invariants for ``docker-compose.cloud-agent.yml``:

1. Top-level ``services`` is exactly ``{worker}`` — worker-only, no
   ``watcher``/``audfprint``/``panako`` (a compute agent owns no media; D-06).
2. No service declares ``DATABASE_URL`` / ``POSTGRES_*`` or a ``depends_on``
   reference to postgres (DIST-04 — the compute agent reaches Postgres ONLY via
   ``PHAZE_QUEUE_URL`` for saq_jobs + the HTTP API, never the app ORM). T-51-04.
3. ``worker`` env carries BOTH ``PHAZE_ROLE=agent`` AND ``PHAZE_AGENT_KIND=compute``
   (the kind=compute env relaxes the empty-scan-roots gate, config.py:470).
4. NEW (vs the agent test) — the ``worker`` image is a GHCR image pinned via
   ``${PHAZE_IMAGE_TAG...}`` that ENDS with ``-arm64`` (D-08, Pitfall 3, T-51-05:
   no multi-arch manifest exists, an x86 image would not run on the Ampere A1).
5. NEW — the scratch mount's source is a NAMED volume (``cloud_scratch``; its
   left side has no leading ``/`` or ``.``) declared under top-level ``volumes:``
   (D-07 — ephemeral scratch is a docker-managed volume, not a host bind).
6. NEW — NO volume string contains ``SCAN_PATH`` or ``/data/music`` (a compute
   agent has no media bind — the inverse of the agent compose's fail-fast check).
7. The MODELS mount is ``:rw`` (D-07 model auto-download) and the CA mount is
   ``:ro`` (operator-distributed cert).
8. ``network_mode: host`` is present on the worker (D-05 — host tailscaled +
   MagicDNS reach lux Postgres/Redis/API over the tailnet).

These tests deliberately use ``yaml.safe_load`` so the assertions are robust
against YAML reformatting. ``yaml.safe_load`` does NOT perform docker-compose
env-var interpolation, so the raw ``${VAR:?...}`` / ``${PHAZE_IMAGE_TAG...}``
tokens are visible to the tests — that is intentional, because the test asserts
the source-file invariant, not the post-interpolation runtime value.
"""

from pathlib import Path
from typing import Any

import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.cloud-agent.yml"


def _load_cloud_agent_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _env_to_strs(env: Any) -> list[str]:
    """Normalize a compose ``environment`` to a list of ``"KEY=VALUE"`` strings.

    Compose accepts both list-of-string and dict forms.
    """
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return []


def test_cloud_agent_compose_service_list() -> None:
    """D-06: cloud-agent compose declares exactly one service — worker.

    No ``watcher``/``audfprint``/``panako`` media sidecars: a compute agent owns
    no scan roots and receives each long file pushed to its scratch volume.
    """
    data = _load_cloud_agent_compose()
    assert set(data["services"].keys()) == {"worker"}, (
        f"cloud-agent compose services must be exactly {{worker}}; got {sorted(data['services'].keys())!r}"
    )


def test_cloud_agent_compose_has_no_postgres_env() -> None:
    """DIST-04 / T-51-04: no service may have DATABASE_URL, POSTGRES_*, or depends_on: postgres.

    The compute agent reaches Postgres ONLY via PHAZE_QUEUE_URL (saq_jobs) and the
    application server's HTTP API. A DATABASE_URL on any service would punch
    through the trust boundary to the app ORM.
    """
    data = _load_cloud_agent_compose()
    for svc_name, svc in data["services"].items():
        env_strs = _env_to_strs(svc.get("environment", []))
        for entry in env_strs:
            assert "DATABASE_URL" not in entry, f"cloud-agent service {svc_name} has DATABASE_URL in environment: {entry!r}"
            assert "POSTGRES_" not in entry, f"cloud-agent service {svc_name} has POSTGRES_* env var: {entry!r}"
        depends = svc.get("depends_on", {})
        # depends_on accepts list (["postgres"]) and dict ({"postgres": {...}}) forms.
        if isinstance(depends, (list, dict)):
            assert "postgres" not in depends, f"cloud-agent service {svc_name} has depends_on: postgres"
    # There must be no inline postgres/redis service either (the agent connects out over Tailscale).
    assert "postgres" not in data["services"], "cloud-agent compose must not declare a postgres service"
    assert "redis" not in data["services"], "cloud-agent compose must not declare a redis service"


def test_worker_service_has_role_agent_and_kind_compute() -> None:
    """D-06: the worker runs under PHAZE_ROLE=agent AND PHAZE_AGENT_KIND=compute.

    kind=compute relaxes the empty-scan-roots gate (config.py:470) so the
    media-less cloud agent boots without scan roots.
    """
    data = _load_cloud_agent_compose()
    worker_env = _env_to_strs(data["services"]["worker"].get("environment", []))
    assert any("PHAZE_ROLE=agent" in e for e in worker_env), f"worker must have PHAZE_ROLE=agent in environment; got {worker_env!r}"
    assert any("PHAZE_AGENT_KIND=compute" in e for e in worker_env), f"worker must have PHAZE_AGENT_KIND=compute in environment; got {worker_env!r}"


def test_worker_consumes_single_analyze_lane() -> None:
    """quick-260707-dh1: the compute agent adopts PHAZE_AGENT_LANE=analyze (single lane, not a 4-split).

    Media-less + analysis-only (its ONLY task is process_file), so it consumes the analyze lane
    queue. The other 3 lanes would be permanently empty on a compute host. Single lane = single
    heartbeat, so PHAZE_AGENT_HEARTBEAT is left unset (defaults True) -- there must be no
    heartbeat=false override that would silence the agent's only liveness signal.
    """
    data = _load_cloud_agent_compose()
    worker_env = _env_to_strs(data["services"]["worker"].get("environment", []))
    assert any(e == "PHAZE_AGENT_LANE=analyze" for e in worker_env), f"compute worker must set PHAZE_AGENT_LANE=analyze; got {worker_env!r}"
    # No other lane, and no 4-service split (only the one worker service -- guarded elsewhere).
    assert not any(e.startswith("PHAZE_AGENT_LANE=") and e != "PHAZE_AGENT_LANE=analyze" for e in worker_env)
    # Single lane -> single heartbeat: never explicitly disabled on the sole worker.
    assert "PHAZE_AGENT_HEARTBEAT=false" not in worker_env, "the compute agent's only worker must keep its heartbeat (do not set =false)"


def test_worker_caps_analyze_lane_concurrency_to_one() -> None:
    """quick-260707-g84: the compute worker pins the analyze lane to 1 concurrent job.

    In lane mode the per-lane knob (PHAZE_LANE_ANALYZE_CONCURRENCY) is what ACTUALLY governs a
    lane worker's concurrency -- WORKER_MAX_JOBS is only a ceiling (concurrency = min(lane knob,
    worker_max_jobs)). On the OCI Ampere A1 (12 GB) compute agent a single process_file peaks
    ~8 GB, so the analyze lane must run one job at a time; relying on WORKER_MAX_JOBS=1 alone is
    inert in lane mode. Assert the explicit PHAZE_LANE_ANALYZE_CONCURRENCY=1 (<= WORKER_MAX_JOBS)
    is present on the worker.
    """
    data = _load_cloud_agent_compose()
    worker_env = _env_to_strs(data["services"]["worker"].get("environment", []))
    assert any(e == "PHAZE_LANE_ANALYZE_CONCURRENCY=1" for e in worker_env), (
        f"compute worker must cap the analyze lane via PHAZE_LANE_ANALYZE_CONCURRENCY=1 "
        f"(WORKER_MAX_JOBS is only a ceiling in lane mode); got {worker_env!r}"
    )


def test_worker_image_is_arm64_ghcr_pinned() -> None:
    """D-08 / T-51-05 / MCOMP-07: the worker image DEFAULT renders the arm64 GHCR tag pinned via PHAZE_IMAGE_TAG.

    No multi-arch manifest exists for the arm64 agent image (Phase 47 publishes a
    dedicated ``-arm64``-suffixed tag); an x86-tagged image would not run on the
    Ampere A1. The single compose file now also serves an x86 spill compute agent
    via the ``${PHAZE_CLOUD_AGENT_IMAGE:-…}`` override (D-05), so the raw image is
    wrapped as ``${PHAZE_CLOUD_AGENT_IMAGE:-ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64}``.
    yaml.safe_load does not interpolate, so the raw token is visible: assert the
    arm64 DEFAULT is preserved (prefix + PHAZE_IMAGE_TAG pin + arm64 suffix), while
    tolerating the override wrapper.
    """
    data = _load_cloud_agent_compose()
    image = data["services"]["worker"].get("image")
    assert image, f"worker must declare an image: pulling from GHCR; got {data['services']['worker']!r}"
    # Relaxed from startswith → substring: the raw image now begins with the
    # ${PHAZE_CLOUD_AGENT_IMAGE:-…} override wrapper, but the arm64 DEFAULT still
    # embeds the GHCR prefix (consistent with the PHAZE_IMAGE_TAG substring check below).
    assert "ghcr.io/simplicityguy/phaze:" in image, f"worker image DEFAULT must embed ghcr.io/simplicityguy/phaze:<tag>; got {image!r}"
    assert "PHAZE_IMAGE_TAG" in image, f"worker image must pin the tag via ${{PHAZE_IMAGE_TAG...}}; got {image!r}"
    # Relaxed from endswith("-arm64"): the raw string is now ${VAR:-…-arm64}-terminated
    # by the wrapper's closing brace. Assert the DEFAULT still renders the arm64 tag.
    assert "-arm64}" in image, f"worker image DEFAULT MUST render -arm64 (D-08; no multi-arch manifest exists); got {image!r}"


def test_worker_command_invokes_system_python_not_uv() -> None:
    """D-47-PY: the arm64 worker command must invoke python3 directly, never ``uv run``.

    The ``-arm64`` image is built on Python 3.13 with ``--system`` installs (no
    ``.venv``). A docker-compose ``command:`` override unconditionally replaces the
    Dockerfile ``CMD``; if it uses the ``uv`` launcher, uv re-validates
    ``requires-python >=3.14`` against the 3.13 interpreter and the container exits
    before SAQ starts. The x86 ``docker-compose.agent.yml`` legitimately uses
    ``uv run saq …`` (3.14 + .venv image) — this test guards against that pattern
    being copied here. Either omit ``command:`` (inherit the Dockerfile CMD) or set
    it to the ``python3 -m saq …`` form; both are accepted, ``uv`` is not.

    MCOMP-07: the command is now parametrized as
    ``${PHAZE_CLOUD_AGENT_CMD:-python3 -m saq phaze.tasks.agent_worker.settings}`` so the single
    compose file also serves an x86 spill agent (which sets PHAZE_CLOUD_AGENT_CMD=``uv run saq …``).
    yaml.safe_load does not interpolate, so the raw first token is
    ``${PHAZE_CLOUD_AGENT_CMD:-python3``. This test asserts the arm64 DEFAULT is preserved
    (``python3 -m saq phaze.tasks.agent_worker.settings``) and that ``uv`` is NOT the default launcher,
    while tolerating the ``${VAR:-default}`` override wrapper.
    """
    data = _load_cloud_agent_compose()
    worker = data["services"]["worker"]
    command = worker.get("command")
    if command is None:
        # No override → the Dockerfile.agent-arm64 CMD (python3 -m saq …) applies. Fine.
        return
    command_str = command if isinstance(command, str) else " ".join(str(t) for t in command)
    # Strip the ${PHAZE_CLOUD_AGENT_CMD:-…} override wrapper to inspect the arm64 DEFAULT.
    prefix = "${PHAZE_CLOUD_AGENT_CMD:-"
    default_str = command_str[len(prefix) :].rstrip("}") if command_str.startswith(prefix) else command_str
    default_tokens = default_str.split()
    assert "uv" not in default_tokens, (
        "cloud-agent worker command DEFAULT must NOT use the uv launcher on the arm64 (py3.13/--system) "
        f"image — uv re-validates requires-python >=3.14 and the container fails to boot; got {command!r}"
    )
    assert default_tokens[:3] == ["python3", "-m", "saq"], (
        "cloud-agent worker command DEFAULT must invoke the system interpreter as "
        f"'python3 -m saq …' to match Dockerfile.agent-arm64 CMD; got {command!r}"
    )
    assert "phaze.tasks.agent_worker.settings" in default_tokens, (
        f"cloud-agent worker command DEFAULT must run the agent_worker settings module; got {command!r}"
    )


def test_worker_uses_named_scratch_volume() -> None:
    """D-07: the scratch mount source is a named docker volume declared under top-level volumes:.

    The ephemeral push-pipeline scratch is a docker-managed named volume
    (``cloud_scratch``), NOT a host bind — its source segment has no leading
    ``/`` or ``.`` and it must be declared in the top-level ``volumes:`` block.
    """
    data = _load_cloud_agent_compose()
    volumes = data["services"]["worker"].get("volumes", []) or []
    scratch_sources: list[str] = []
    for vol in volumes:
        assert isinstance(vol, str), f"expected string volume entries; got {vol!r}"
        source = vol.split(":", 1)[0]
        # Named-volume sources are bare identifiers; binds start with / or . (or ${VAR...} host paths).
        if not source.startswith(("/", ".", "$")):
            scratch_sources.append(source)
    assert "cloud_scratch" in scratch_sources, f"worker must mount a named 'cloud_scratch' volume (no leading / or .); got volumes={volumes!r}"
    top_level_volumes = data.get("volumes", {}) or {}
    assert "cloud_scratch" in top_level_volumes, (
        f"'cloud_scratch' must be declared under the top-level volumes: block; got {sorted(top_level_volumes)!r}"
    )


def test_no_media_mount() -> None:
    """D-06: no volume mounts media — NO SCAN_PATH and NO /data/music anywhere.

    The inverse of the agent compose's fail-fast SCAN_PATH check: a compute agent
    owns no media library, so the media bind must be absent entirely.
    """
    data = _load_cloud_agent_compose()
    offenders: list[str] = []
    for svc_name, svc in data["services"].items():
        for vol in svc.get("volumes", []) or []:
            if not isinstance(vol, str):
                continue
            if "SCAN_PATH" in vol or "/data/music" in vol:
                offenders.append(f"{svc_name}: {vol}")
    assert not offenders, "cloud-agent compose must have NO media mount (no SCAN_PATH / /data/music):\n" + "\n".join(offenders)


def test_models_mount_rw_and_ca_mount_ro() -> None:
    """D-07: the MODELS mount is rw (model auto-download) and the CA mount is ro."""
    data = _load_cloud_agent_compose()
    volumes = [v for v in (data["services"]["worker"].get("volumes", []) or []) if isinstance(v, str)]
    models_mounts = [v for v in volumes if "MODELS_PATH" in v or ":/models" in v]
    ca_mounts = [v for v in volumes if "CA_PATH" in v or ":/certs" in v]
    assert models_mounts, f"worker must mount a MODELS volume at /models; got volumes={volumes!r}"
    assert all(v.endswith(":rw") for v in models_mounts), f"the MODELS mount must be :rw (D-07 model auto-download); got {models_mounts!r}"
    assert ca_mounts, f"worker must mount a CA volume at /certs; got volumes={volumes!r}"
    assert all(v.endswith(":ro") for v in ca_mounts), f"the CA mount must be :ro (operator-distributed cert); got {ca_mounts!r}"


def test_worker_uses_host_networking() -> None:
    """D-05: the worker uses network_mode: host to reach lux via host tailscaled + MagicDNS."""
    data = _load_cloud_agent_compose()
    network_mode = data["services"]["worker"].get("network_mode")
    assert network_mode == "host", f"worker must set network_mode: host (D-05 host tailscaled); got {network_mode!r}"
