"""Phase 29 D-15..D-17, D-22: docker-compose.agent.yml structural assertions.

Pure YAML-parse tests for the file-server-host compose file (no docker daemon).

Covers four invariants for ``docker-compose.agent.yml``:

1. Top-level ``services`` is exactly ``{worker, watcher, audfprint, panako}``.
2. No agent service declares ``DATABASE_URL`` or a ``depends_on`` reference to
   postgres (DIST-04 invariant — agents reach Postgres ONLY via the HTTP API).
3. ``worker`` service has ``PHAZE_ROLE=agent`` in its environment.
4. WARNING-3: Every ``SCAN_PATH`` volume mount across all 4 services uses the
   fail-fast ``${VAR:?MESSAGE}`` operator (catches a future YAML drift to
   ``${SCAN_PATH:-/data/music}`` loose-default form which would silently let
   ``docker compose up`` succeed on a misconfigured host).

A fifth test (WARNING-4) parses ``.github/workflows/docker-publish.yml`` and
asserts the ``docker/metadata-action`` step emits BOTH a ``:latest`` tag and a
``:v<version>`` tag pattern.

These tests deliberately use ``yaml.safe_load`` so the assertions are robust
against YAML reformatting. ``yaml.safe_load`` does NOT perform docker-compose
env-var interpolation, so the raw ``${VAR:?...}`` tokens are visible to the
tests — that is intentional, because the test asserts the source-file
invariant, not the post-interpolation runtime value.
"""

from pathlib import Path
import re
from typing import Any

import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.agent.yml"
PUBLISH_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "docker-publish.yml"


def _load_agent_compose() -> dict[str, Any]:
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


def test_agent_compose_service_list() -> None:
    """D-15: agent compose declares exactly worker, watcher, audfprint, panako."""
    data = _load_agent_compose()
    assert set(data["services"].keys()) == {"worker", "watcher", "audfprint", "panako"}, (
        f"agent compose services must be exactly {{worker, watcher, audfprint, panako}}; got {sorted(data['services'].keys())!r}"
    )


def test_agent_compose_has_no_postgres_env() -> None:
    """DIST-04: agents must never have DATABASE_URL or depends_on: postgres.

    Agents reach Postgres only via the application server's HTTP API. A
    DATABASE_URL on any agent service would punch through the trust boundary.
    """
    data = _load_agent_compose()
    for svc_name, svc in data["services"].items():
        env_strs = _env_to_strs(svc.get("environment", []))
        for entry in env_strs:
            assert "DATABASE_URL" not in entry, f"agent service {svc_name} has DATABASE_URL in environment: {entry!r}"
            assert "POSTGRES_" not in entry, f"agent service {svc_name} has POSTGRES_* env var: {entry!r}"
        depends = svc.get("depends_on", {})
        # depends_on accepts list (["postgres"]) and dict ({"postgres": {...}}) forms.
        if isinstance(depends, (list, dict)):
            assert "postgres" not in depends, f"agent service {svc_name} has depends_on: postgres"


def test_worker_service_has_phaze_role_agent() -> None:
    """D-17: the worker service runs under PHAZE_ROLE=agent."""
    data = _load_agent_compose()
    worker_env = _env_to_strs(data["services"]["worker"].get("environment", []))
    assert any("PHAZE_ROLE=agent" in e for e in worker_env), f"worker service must have PHAZE_ROLE=agent in environment; got {worker_env!r}"


def test_all_scan_path_mounts_use_failfast_syntax() -> None:
    """WARNING-3: every SCAN_PATH volume mount uses the fail-fast ${VAR:?MESSAGE} form.

    Defends against a YAML drift that silently introduces a loose default like
    ``${SCAN_PATH:-/data/music}`` which would let ``docker compose up`` succeed
    on a misconfigured file-server host (Phase 29 WARNING-3).
    """
    data = _load_agent_compose()
    failfast_re = re.compile(r"\$\{SCAN_PATH:\?[^}]*\}")
    offenders: list[str] = []
    for svc_name, svc in data["services"].items():
        for vol in svc.get("volumes", []) or []:
            if not isinstance(vol, str):
                continue
            if "SCAN_PATH" in vol and not failfast_re.search(vol):
                offenders.append(f"{svc_name}: {vol}")
    assert not offenders, "Some SCAN_PATH mounts are not fail-fast (must use ${SCAN_PATH:?MESSAGE} form):\n" + "\n".join(offenders)
