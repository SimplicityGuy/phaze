"""Phase 29 D-19: app-server compose declares NO music/model/output mounts on api or worker.

Pure YAML-parse structural assertions. No Docker required; runs in ~50ms.

Covers four invariants for the root ``docker-compose.yml``:

1. ``api`` service has no banned filesystem mounts (DIST-01).
2. ``worker`` (controller) service has no banned filesystem mounts (DIST-01).
3. ``watcher``, ``agent-worker``, ``audfprint``, ``panako`` services are absent
   from the root compose (D-15, D-17 — those live in ``docker-compose.agent.yml``).
4. ``redis`` service is hardened: ``--requirepass``, IP-prefixed port binding,
   and an authenticated healthcheck with ``--no-auth-warning`` (D-05 / AUTH-03).

These tests deliberately use ``yaml.safe_load`` rather than regex so the
assertions are robust against YAML reformatting. ``yaml.safe_load`` does NOT
perform docker-compose env-var interpolation, so the raw ``${VAR:-default}``
tokens are visible to the tests — that is intentional, because the test asserts
the source-file invariant, not the post-interpolation runtime value.
"""

from pathlib import Path
from typing import Any

import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.yml"
BANNED_MOUNT_TARGETS = ("/data/music", "/models", "/data/output")


def _volume_target(entry: Any) -> str:
    """Return the container-side target path for a docker-compose volume entry.

    Compose accepts two volume forms:

    - Short string form: ``"<host>:<container>[:ro|rw]"``
    - Long dict form:    ``{"type": "bind", "source": ..., "target": ...}``

    For named-volume short form (``"name:/path"``) the second segment is still
    the container target, so the same ``split(":")[1]`` logic applies.
    """
    if isinstance(entry, str):
        return entry.split(":")[1] if ":" in entry else entry
    if isinstance(entry, dict):
        return str(entry.get("target", ""))
    return ""


def _load_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_api_service_has_no_file_mounts() -> None:
    """DIST-01: the application server's api container reads no music/model/output paths."""
    data = _load_compose()
    api_volumes = data["services"]["api"].get("volumes", []) or []
    for vol_entry in api_volumes:
        target = _volume_target(vol_entry)
        for banned in BANNED_MOUNT_TARGETS:
            assert banned not in target, f"api service has banned mount: {vol_entry}"


def test_controller_worker_has_no_file_mounts() -> None:
    """DIST-01: the controller worker is fileless — no music/model/output mounts."""
    data = _load_compose()
    worker_volumes = data["services"]["worker"].get("volumes", []) or []
    for vol_entry in worker_volumes:
        target = _volume_target(vol_entry)
        for banned in BANNED_MOUNT_TARGETS:
            assert banned not in target, f"worker has banned mount: {vol_entry}"


def test_no_watcher_or_agent_worker_in_root_compose() -> None:
    """D-15 / D-17: agent + sidecar services live ONLY in docker-compose.agent.yml.

    The root compose is the application-server compose; it must not declare
    watcher, agent-worker, audfprint, or panako.
    """
    data = _load_compose()
    services = data["services"]
    assert "watcher" not in services, "watcher belongs in docker-compose.agent.yml (D-17)"
    assert "agent-worker" not in services, "agent-worker belongs in docker-compose.agent.yml (D-17)"
    assert "audfprint" not in services, "audfprint sidecar is file-server-local (D-15)"
    assert "panako" not in services, "panako sidecar is file-server-local (D-15)"


def test_redis_hardened() -> None:
    """D-05 / AUTH-03: redis service uses requirepass + LAN binding + authenticated healthcheck."""
    data = _load_compose()
    redis = data["services"]["redis"]

    # --- command: requirepass + REDIS_PASSWORD interpolation token present ---
    command = redis.get("command")
    assert command is not None, "redis service must declare a command with --requirepass"
    # yaml.safe_load preserves list-form command verbatim; join for substring checks.
    command_str = " ".join(command) if isinstance(command, list) else str(command)
    assert "requirepass" in command_str, f"redis command missing --requirepass: {command!r}"
    assert "REDIS_PASSWORD" in command_str, f"redis command missing REDIS_PASSWORD interpolation token: {command!r}"

    # --- ports: IP-prefixed (not a bare 6379:6379 that defaults to 0.0.0.0) ---
    ports = redis.get("ports", [])
    assert ports, "redis service must declare a ports entry"
    # Look for a "<ip-or-token>:6379:6379" form. Reject a bare "6379:6379" or
    # ":6379:6379" (no host IP) since both would bind 0.0.0.0.
    assert any(isinstance(p, str) and ":6379:6379" in p and not p.startswith(":") and p != "6379:6379" for p in ports), (
        f"redis ports must be IP-prefixed (e.g. ${{REDIS_BIND_IP:-127.0.0.1}}:6379:6379); got {ports!r}"
    )

    # --- healthcheck: redis-cli --no-auth-warning -a <password> ping ---
    healthcheck = redis.get("healthcheck", {})
    test_cmd = healthcheck.get("test", [])
    assert isinstance(test_cmd, list), f"redis healthcheck.test must be a list; got {test_cmd!r}"
    assert "redis-cli" in test_cmd, f"redis healthcheck missing redis-cli: {test_cmd!r}"
    assert "--no-auth-warning" in test_cmd, f"redis healthcheck missing --no-auth-warning: {test_cmd!r}"
    assert "-a" in test_cmd, f"redis healthcheck missing -a flag: {test_cmd!r}"
    assert any("REDIS_PASSWORD" in entry for entry in test_cmd if isinstance(entry, str)), (
        f"redis healthcheck must reference ${{REDIS_PASSWORD}}: {test_cmd!r}"
    )


def _env_list(service: dict[str, Any]) -> list[str]:
    """Normalize a service ``environment:`` block to a list of ``KEY=VALUE`` strings.

    Compose accepts both the list form (``["KEY=VALUE"]``) and the mapping form
    (``{KEY: VALUE}``); this test asserts a source-file invariant regardless of shape.
    """
    env = service.get("environment", []) or []
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return [str(e) for e in env]


def test_app_services_assemble_authenticated_redis_url() -> None:
    """phaze-hti8: api + worker inject an authenticated REDIS_URL via compose interpolation.

    Redis runs with ``--requirepass``, so the app-server's own Redis clients must
    authenticate. Because ``env_file`` does not interpolate, the authenticated URL
    is assembled in each service's ``environment:`` block with a ``${REDIS_PASSWORD}``
    token — making the NOAUTH drift impossible.
    """
    data = _load_compose()
    for svc_name in ("api", "worker"):
        env = _env_list(data["services"][svc_name])
        redis_entries = [e for e in env if e.startswith("REDIS_URL=")]
        assert redis_entries, f"{svc_name} must set REDIS_URL in its environment block to authenticate against requirepass Redis"
        entry = redis_entries[0]
        assert "REDIS_PASSWORD" in entry, f"{svc_name} REDIS_URL must interpolate ${{REDIS_PASSWORD}}; got {entry!r}"
        assert "default:" in entry, f"{svc_name} REDIS_URL must use the `default:` ACL user for the password; got {entry!r}"
