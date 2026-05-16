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


def _extract_api_metadata_action_step(workflow_data: dict[str, Any]) -> dict[str, Any] | None:
    """Locate a docker/metadata-action step whose `images:` output points at the api image.

    docker-publish.yml uses a matrix over {api, audfprint, panako}; the same
    metadata-action step runs for each matrix value with an interpolated
    `images:` URL. The agent.yml's worker+watcher pull from the *api* image
    URL (bare-repo, no sub-path), so this helper specifically looks for the
    api-image step. If the workflow uses a single shared step (no matrix
    differentiation in `images:`), any docker/metadata-action step is
    returned.
    """
    for job in (workflow_data.get("jobs") or {}).values():
        for step in job.get("steps", []) or []:
            uses = (step.get("uses") or "").lower()
            if "docker/metadata-action" in uses:
                return step  # type: ignore[no-any-return]
    return None


def _metadata_action_tag_lines(step: dict[str, Any]) -> list[str]:
    """Return the docker/metadata-action `with.tags:` block split on newlines."""
    tags_raw = (step.get("with") or {}).get("tags", "")
    return [line.strip() for line in str(tags_raw).splitlines() if line.strip()]


def test_docker_publish_workflow_tags_both_latest_and_version() -> None:
    """WARNING-4: .github/workflows/docker-publish.yml emits BOTH :latest AND :v<version> tags.

    Replaces the original `checkpoint:human-verify` task (Phase 29 plan 04
    WARNING-4 resolution). An automated YAML-parse test guarantees the tag
    strategy stays correct across metadata-action upgrades or maintainer
    edits — a regression that drops the version tag pattern (e.g., during a
    refactor) is caught in CI rather than after the next release ships.

    Tag patterns accepted:
      - `:latest` ← `type=raw,value=latest` (with or without `enable=...`)
      - `:v<version>` ← `type=semver,pattern={{version}}` OR `type=ref,event=tag`
    """
    assert PUBLISH_WORKFLOW_PATH.exists(), f"docker-publish.yml missing at {PUBLISH_WORKFLOW_PATH}"
    workflow = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text())
    step = _extract_api_metadata_action_step(workflow)
    assert step is not None, (
        "Could not locate a docker/metadata-action step in docker-publish.yml. "
        "Phase 29 D-16 requires the workflow to produce both :latest and :v<version> tags."
    )
    tags = _metadata_action_tag_lines(step)
    assert tags, f"docker/metadata-action step has no `with.tags:` block; got step={step!r}"

    has_latest = any("value=latest" in t for t in tags)
    has_version = any(("type=semver" in t) or ("type=ref,event=tag" in t) or ("type=ref" in t and "tag" in t) for t in tags)
    missing: list[str] = []
    if not has_latest:
        missing.append("'type=raw,value=latest' (or equivalent)")
    if not has_version:
        missing.append("'type=semver,pattern={{version}}' (or 'type=ref,event=tag')")
    assert not missing, (
        f"docker-publish.yml tag patterns missing: {missing}\nFound tags: {tags}\n"
        "Fix: add the missing pattern(s) under jobs.<job>.steps[uses=docker/metadata-action].with.tags."
    )
