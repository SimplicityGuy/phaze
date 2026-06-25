"""Phase 29 D-15..D-17, D-22: docker-compose.agent.yml structural assertions.

Pure YAML-parse tests for the file-server-host compose file (no docker daemon).

Covers five invariants for ``docker-compose.agent.yml``:

1. Top-level ``services`` is exactly ``{worker, watcher, audfprint, panako}``.
2. No agent service declares ``DATABASE_URL`` or a ``depends_on`` reference to
   postgres (DIST-04 invariant — agents reach Postgres ONLY via the HTTP API).
3. ``worker`` service has ``PHAZE_ROLE=agent`` in its environment.
4. WARNING-3: Every ``SCAN_PATH`` volume mount across all 4 services uses the
   fail-fast ``${VAR:?MESSAGE}`` operator (catches a future YAML drift to
   ``${SCAN_PATH:-/data/music}`` loose-default form which would silently let
   ``docker compose up`` succeed on a misconfigured host).
5. All four agent services pull a ``ghcr.io/simplicityguy/phaze`` image pinned
   via ``${PHAZE_IMAGE_TAG...}`` — ``worker``/``watcher`` from the bare repo and
   ``audfprint``/``panako`` from the ``/audfprint`` + ``/panako`` sub-paths. This
   guards against a regression back to a local ``build:`` block on the sidecars
   (a service with only ``build:`` and no ``image:`` fails the guard).

A sixth test (WARNING-4) parses ``.github/workflows/docker-publish.yml`` and
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
CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
CLEANUP_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "cleanup-images.yml"


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


def test_all_agent_services_pull_from_ghcr() -> None:
    """All four agent services pull a GHCR image pinned via PHAZE_IMAGE_TAG.

    Guards against a regression where a sidecar reverts to a local ``build:``
    block (and drops ``image:``), which would force every file-server host to
    carry the full phaze source context just to build the fingerprint sidecars.

    ``worker``/``watcher`` pull the bare repo (``ghcr.io/simplicityguy/phaze``);
    ``audfprint``/``panako`` pull the ``/audfprint`` + ``/panako`` sub-paths
    (matching the docker-publish.yml matrix ``image_suffix`` values). ``yaml.safe_load``
    does not interpolate, so the raw ``${PHAZE_IMAGE_TAG...}`` token is visible.
    """
    data = _load_agent_compose()
    expected_image_paths = {
        "worker": "ghcr.io/simplicityguy/phaze",
        "watcher": "ghcr.io/simplicityguy/phaze",
        "audfprint": "ghcr.io/simplicityguy/phaze/audfprint",
        "panako": "ghcr.io/simplicityguy/phaze/panako",
    }
    for svc_name, expected_path in expected_image_paths.items():
        svc = data["services"][svc_name]
        image = svc.get("image")
        assert image, f"agent service {svc_name} must declare an image: pulling from GHCR (no bare build:); got {svc!r}"
        assert image.startswith(f"{expected_path}:"), f"agent service {svc_name} image must be {expected_path}:<tag>; got {image!r}"
        assert "PHAZE_IMAGE_TAG" in image, f"agent service {svc_name} image must pin the tag via ${{PHAZE_IMAGE_TAG...}}; got {image!r}"


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
    jobs = workflow_data.get("jobs") or {}
    # Target the api publisher explicitly: phase 47 added build-arm64 and
    # parity-golden-x86 jobs that also use docker/metadata-action, so a
    # first-match scan would silently check the wrong tag strategy if jobs
    # are reordered. Fall back to a first-match scan only if the api job is
    # named differently (single shared-step workflow).
    api_job = jobs.get("build-and-push")
    candidate_jobs = [api_job] if api_job else list(jobs.values())
    for job in candidate_jobs:
        for step in (job or {}).get("steps", []) or []:
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


def _extract_build_arm64_metadata_step(workflow_data: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the ``docker/metadata-action`` step inside the ``build-arm64`` job.

    Phase 47 CLOUDIMG-02 adds a dedicated native-arm64 build job to
    docker-publish.yml. Its metadata-action step resolves the ``-arm64``-suffixed
    tags (and OCI labels) that the 47-04 parity-gated push later publishes. This
    helper returns ``None`` if either the job or its metadata-action step is
    absent, so the caller can emit a precise failure.
    """
    job = (workflow_data.get("jobs") or {}).get("build-arm64")
    if not isinstance(job, dict):
        return None
    for step in job.get("steps", []) or []:
        uses = (step.get("uses") or "").lower()
        if "docker/metadata-action" in uses:
            return step  # type: ignore[no-any-return]
    return None


def _flavor_lines(step: dict[str, Any]) -> list[str]:
    """Return the docker/metadata-action ``with.flavor:`` block split on newlines."""
    flavor_raw = (step.get("with") or {}).get("flavor", "")
    return [line.strip() for line in str(flavor_raw).splitlines() if line.strip()]


def test_docker_publish_arm64_job_tags_latest_and_version() -> None:
    """CLOUDIMG-02: the build-arm64 job emits BOTH a latest-arm64 AND a version-arm64 tag.

    The Phase 51 cloud-agent compose pins a ``<version>-arm64`` image tag, and a
    rolling ``latest-arm64`` must track the default branch. Both come from the
    ``build-arm64`` job's ``docker/metadata-action`` step. This test fails if the
    job is missing, or if a refactor drops the ``-arm64`` suffix (e.g. removes the
    ``flavor: suffix=-arm64`` line) or the latest/version tag patterns — catching
    the regression in CI rather than after the cloud compose can no longer resolve
    the image.

    Two equivalent tag mechanisms are accepted:
      1. ``flavor: suffix=-arm64`` applied over base ``type=raw,value=latest`` +
         ``type=semver,pattern={{version}}`` (or ``type=ref,event=tag``) patterns.
      2. explicit ``type=raw,value=latest-arm64`` + ``type=semver,pattern={{version}}-arm64``
         (or ``type=ref,event=tag`` with a ``-arm64`` suffix) patterns.
    """
    assert PUBLISH_WORKFLOW_PATH.exists(), f"docker-publish.yml missing at {PUBLISH_WORKFLOW_PATH}"
    workflow = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text())

    assert "build-arm64" in (workflow.get("jobs") or {}), (
        "docker-publish.yml is missing the `build-arm64` job. CLOUDIMG-02 requires a native-arm64 "
        "build job that resolves the matching -arm64 tags for the 47-04 parity-gated push."
    )

    step = _extract_build_arm64_metadata_step(workflow)
    assert step is not None, (
        "The `build-arm64` job has no docker/metadata-action step. It must resolve the -arm64 tags "
        "(latest-arm64 + <version>-arm64) and expose them as job outputs for the gated push."
    )

    tags = _metadata_action_tag_lines(step)
    flavor = _flavor_lines(step)
    assert tags, f"build-arm64 metadata-action step has no `with.tags:` block; got step={step!r}"

    suffix_via_flavor = any("suffix=-arm64" in f for f in flavor)
    # Base (suffix-applied) patterns, present in EITHER mechanism.
    has_latest = any("value=latest" in t for t in tags)
    has_version = any(("type=semver" in t) or ("type=ref,event=tag" in t) for t in tags)
    # Explicit-suffix fallback patterns.
    has_explicit_latest_arm64 = any("value=latest-arm64" in t for t in tags)
    has_explicit_version_arm64 = any("-arm64" in t and (("type=semver" in t) or ("type=ref,event=tag" in t)) for t in tags)

    latest_arm64_ok = (suffix_via_flavor and has_latest) or has_explicit_latest_arm64
    version_arm64_ok = (suffix_via_flavor and has_version) or has_explicit_version_arm64

    missing: list[str] = []
    if not latest_arm64_ok:
        missing.append("a latest-arm64 tag (flavor: suffix=-arm64 over type=raw,value=latest, or explicit type=raw,value=latest-arm64)")
    if not version_arm64_ok:
        missing.append("a <version>-arm64 tag (flavor: suffix=-arm64 over type=semver/type=ref,event=tag, or an explicit -arm64 semver/ref pattern)")
    assert not missing, (
        f"build-arm64 job tag strategy is missing: {missing}\nflavor={flavor}\ntags={tags}\n"
        "Fix: restore the -arm64 suffix (flavor: suffix=-arm64,onlatest=true) and the latest+version tag patterns "
        "under jobs.build-arm64.steps[uses=docker/metadata-action].with."
    )


def _load_ci_workflow_triggers(data: dict[Any, Any]) -> dict[str, Any]:
    """Return the ``on:`` trigger mapping from a parsed CI workflow.

    PyYAML parses the bare ``on:`` key as the boolean ``True`` in some
    documents, so the string key ``"on"`` may be absent. Fall back to the
    boolean ``True`` key before giving up.
    """
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), f"ci.yml `on:` block did not parse as a mapping; got {triggers!r}"
    return triggers


def _ci_detect_changes_filter_step() -> dict[str, Any]:
    """Locate the ``detect-changes`` job's ``id: filter`` step in ci.yml."""
    assert CI_WORKFLOW_PATH.exists(), f"ci.yml missing at {CI_WORKFLOW_PATH}"
    data = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    detect = (data.get("jobs") or {}).get("detect-changes")
    assert isinstance(detect, dict), "ci.yml is missing the `detect-changes` job"
    for step in detect.get("steps", []) or []:
        if step.get("id") == "filter":
            return step  # type: ignore[no-any-return]
    raise AssertionError("ci.yml `detect-changes` job has no step with `id: filter`")


def test_ci_workflow_triggers_on_version_tags() -> None:
    """Release fix: ci.yml fires on a 3-part semver tag push (and still on branches).

    Without ``on.push.tags``, pushing a release tag runs NO workflow, so
    docker-publish never builds the version-tagged GHCR image and the
    documented ``PHAZE_IMAGE_TAG=vX.Y.Z`` pin is unusable. This test fails if
    the tag trigger is dropped, and also guards that branch CI is not lost in
    the process.
    """
    assert CI_WORKFLOW_PATH.exists(), f"ci.yml missing at {CI_WORKFLOW_PATH}"
    data = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    triggers = _load_ci_workflow_triggers(data)
    push = triggers.get("push")
    assert isinstance(push, dict), f"ci.yml `on.push` must be a mapping; got {push!r}"

    tags = push.get("tags")
    assert isinstance(tags, list) and any("v*.*.*" in str(t) for t in tags), (
        f'ci.yml must trigger on 3-part semver tags. Add `on.push.tags: ["v*.*.*"]` so release-tag pushes run the publish pipeline; got tags={tags!r}'
    )

    branches = push.get("branches")
    assert isinstance(branches, list) and branches, (
        f"ci.yml lost its `on.push.branches` trigger — the tag edit must not drop branch CI; got branches={branches!r}"
    )


def test_ci_detect_changes_forces_code_changed_on_tags() -> None:
    """Release fix: detect-changes forces ``code-changed=true`` for tag refs.

    The ``docker-publish`` job gates on ``code-changed == 'true'``. A tag push
    carries no file diff against a base, so without an explicit tag-ref
    early-exit the diff logic would compute ``code-changed=false`` and SKIP
    docker-publish — the image would never ship. This test fails if the
    tag-ref forcing is removed.
    """
    step = _ci_detect_changes_filter_step()

    env = step.get("env") or {}
    assert isinstance(env, dict), f"detect-changes filter step has no env mapping; got {env!r}"
    ref_var_wired = any(("ref_type" in str(v).lower()) or (key in {"REF", "REF_NAME"} and "github.ref" in str(v).lower()) for key, v in env.items())
    assert ref_var_wired, (
        "detect-changes filter step must wire a ref-type/ref variable "
        "(e.g. `REF_TYPE: ${{ github.ref_type }}`) so the run script can detect tag pushes; "
        f"got env={env!r}"
    )

    run = str(step.get("run") or "")
    run_lower = run.lower()
    has_tag_check = ("ref_type" in run_lower) or ('"tag"' in run_lower) or ("refs/tags" in run_lower)
    assert has_tag_check and "code-changed=true" in run, (
        "detect-changes filter step must force `code-changed=true` for tag refs; otherwise a release-tag "
        "push computes code-changed=false and the docker-publish job is skipped, so the version-tagged "
        f"image never publishes. run script was:\n{run}"
    )


def test_ci_detect_changes_survives_force_push() -> None:
    """Force-push fix: detect-changes falls back to ``origin/main...HEAD`` when before-SHA is gone.

    On a force-pushed branch the push event carries ``github.event.before`` set to
    the pre-force-push tip, which is unreachable in the fresh ``fetch-depth: 0``
    clone. Running ``git diff "${BEFORE_SHA}" "${HEAD_SHA}"`` against it dies with
    ``fatal: bad object <old-tip>`` (exit 128), failing the whole job. The filter
    step must probe reachability (``git cat-file -e``) and fall back to the
    default-branch diff (``origin/main...HEAD``). This test fails if that
    reachability fallback is removed.
    """
    step = _ci_detect_changes_filter_step()

    run = str(step.get("run") or "")
    assert "git cat-file -e" in run, (
        "detect-changes filter step must probe whether `github.event.before` is reachable "
        "via `git cat-file -e`; otherwise a force-pushed branch hits `fatal: bad object` and "
        f"detect-changes fails with exit 128. run script was:\n{run}"
    )
    assert "origin/main..." in run, (
        "detect-changes filter step must fall back to `git diff origin/main...HEAD` when the "
        "before-SHA is unreachable (force-push); without this fallback a force-pushed branch "
        f"cannot compute changed files and CI errors out. run script was:\n{run}"
    )


def test_cleanup_package_list_matches_published_images() -> None:
    """GHCR-RECONCILE: cleanup-images.yml prunes exactly the packages docker-publish.yml ships.

    Derives the published GHCR package set from docker-publish.yml's build matrix
    (``("phaze" + image_suffix).rstrip("/")`` over each ``matrix.include`` entry,
    where the empty api suffix collapses to the bare-repo ``phaze`` package) and
    asserts it equals cleanup-images.yml's ``matrix.package`` set. If either
    workflow drifts — a new published image without a cleanup entry, or a cleanup
    entry for an unpublished/orphan path (e.g. the historical ``phaze/api``) — the
    symmetric difference pinpoints exactly which side diverged.
    """
    assert PUBLISH_WORKFLOW_PATH.exists(), f"docker-publish.yml missing at {PUBLISH_WORKFLOW_PATH}"
    assert CLEANUP_WORKFLOW_PATH.exists(), f"cleanup-images.yml missing at {CLEANUP_WORKFLOW_PATH}"

    publish = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text())
    matrix_include = publish["jobs"]["build-and-push"]["strategy"]["matrix"]["include"]
    published_packages = {("phaze" + entry["image_suffix"]).rstrip("/") for entry in matrix_include}

    cleanup = yaml.safe_load(CLEANUP_WORKFLOW_PATH.read_text())
    cleanup_packages = set(cleanup["jobs"]["cleanup"]["strategy"]["matrix"]["package"])

    only_published = published_packages - cleanup_packages
    only_cleanup = cleanup_packages - published_packages
    assert published_packages == cleanup_packages, (
        "Publish/cleanup GHCR package sets diverged.\n"
        f"  published (docker-publish.yml): {sorted(published_packages)}\n"
        f"  cleanup   (cleanup-images.yml): {sorted(cleanup_packages)}\n"
        f"  published but never pruned: {sorted(only_published)}\n"
        f"  pruned but never published: {sorted(only_cleanup)}\n"
        "Fix: keep cleanup-images.yml's matrix.package in sync with docker-publish.yml's image_suffix set."
    )
