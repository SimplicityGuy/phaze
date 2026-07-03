"""Static deployment guards for the Phase 52 x86 Job-runner image.

These tests prove the Dockerfile.job + docker-publish.yml contract WITHOUT a live
docker build. They parse the workflow with ``yaml.safe_load`` (robust against YAML
reformatting) and grep ``Dockerfile.job`` as text. Four guards:

a. ``build-job-runner`` is a ``needs: build-and-push``-gated job (NOT a sibling
   matrix row) — Dockerfile.job ``FROM`` the api tag cannot race the api push
   (Pitfall 1 / T-52-06).
b. The job builds ``Dockerfile.job`` and passes ``BASE_IMAGE`` as a build-arg.
c. ``Dockerfile.job`` adds zero new deps (no pip/uv add — KJOB-01) and its CMD
   targets ``phaze.job_runner``.
d. ``Dockerfile.job`` does not pin a moving ``:latest`` base tag (T-52-06).
"""

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
DOCKERFILE_JOB_PATH = REPO_ROOT / "Dockerfile.job"


def _load_workflow() -> dict[str, Any]:
    assert PUBLISH_WORKFLOW_PATH.exists(), f"docker-publish.yml missing at {PUBLISH_WORKFLOW_PATH}"
    return yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text())


def _job_runner_job() -> dict[str, Any]:
    workflow = _load_workflow()
    jobs = workflow.get("jobs", {})
    assert "build-job-runner" in jobs, (
        "docker-publish.yml is missing the `build-job-runner` job. Phase 52 (D-04) requires the x86 "
        "Job-runner image to be built in a needs-gated job off the same release tag as the api image."
    )
    return jobs["build-job-runner"]


def test_build_job_runner_needs_build_and_push() -> None:
    """Guard (a): the Job image job is gated on the api image being pushed first.

    Dockerfile.job ``FROM ${BASE_IMAGE}`` resolves the freshly-pushed api tag. A
    sibling matrix row has no ordering and could build before the api image
    exists (Pitfall 1 / T-52-06). ``needs: build-and-push`` enforces the order.
    """
    job = _job_runner_job()
    needs = job.get("needs")
    # `needs` may be a bare string or a list.
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert "build-and-push" in needs_list, (
        f"build-job-runner must declare `needs: build-and-push` (got needs={needs!r}). "
        "Without the gate the Job image can build FROM a not-yet-pushed api tag."
    )


def test_build_job_runner_builds_dockerfile_job_with_base_image() -> None:
    """Guard (b): the job builds Dockerfile.job and passes BASE_IMAGE as a build-arg."""
    job = _job_runner_job()
    steps = job.get("steps", [])

    build_steps = [s for s in steps if isinstance(s, dict) and isinstance(s.get("uses"), str) and "build-push-action" in s["uses"]]
    assert build_steps, "build-job-runner has no docker/build-push-action step."

    file_targets = {(s.get("with") or {}).get("file") for s in build_steps}
    assert "Dockerfile.job" in file_targets, f"build-job-runner build-push step must set `file: Dockerfile.job` (found files: {file_targets})."

    # At least one build step must pass BASE_IMAGE so Dockerfile.job's `FROM ${BASE_IMAGE}` resolves.
    passes_base_image = any("BASE_IMAGE" in ((s.get("with") or {}).get("build-args") or "") for s in build_steps)
    assert passes_base_image, (
        "build-job-runner must pass `BASE_IMAGE=...` in build-args so Dockerfile.job builds FROM the resolved freshly-pushed api tag (T-52-06)."
    )


def test_dockerfile_job_zero_new_deps_and_targets_job_runner() -> None:
    """Guard (c): Dockerfile.job adds zero deps (KJOB-01) and CMDs phaze.job_runner."""
    assert DOCKERFILE_JOB_PATH.exists(), f"Dockerfile.job missing at {DOCKERFILE_JOB_PATH}"
    text = DOCKERFILE_JOB_PATH.read_text()

    for forbidden in ("pip install", "uv add", "uv pip install"):
        assert forbidden not in text, (
            f"Dockerfile.job must add zero new dependencies (KJOB-01) — found `{forbidden}`. The api base already carries every dependency."
        )

    cmd_lines = [line for line in text.splitlines() if line.lstrip().startswith("CMD")]
    assert cmd_lines, "Dockerfile.job has no CMD instruction."
    assert any("phaze.job_runner" in line for line in cmd_lines), (
        f"Dockerfile.job CMD must target the one-shot entrypoint `phaze.job_runner` (got: {cmd_lines})."
    )


def test_dockerfile_job_does_not_pin_latest_base() -> None:
    """Guard (d): Dockerfile.job builds FROM ARG BASE_IMAGE, never a moving :latest tag (T-52-06)."""
    assert DOCKERFILE_JOB_PATH.exists(), f"Dockerfile.job missing at {DOCKERFILE_JOB_PATH}"
    text = DOCKERFILE_JOB_PATH.read_text()

    assert ":latest" not in text, (
        "Dockerfile.job must not pin a `:latest` base tag — a stale shared tag could ship old code "
        "(T-52-06). The base is the resolved release tag passed via ARG BASE_IMAGE."
    )
    from_lines = [line for line in text.splitlines() if line.lstrip().upper().startswith("FROM")]
    assert from_lines, "Dockerfile.job has no FROM instruction."
    assert any("BASE_IMAGE" in line for line in from_lines), f"Dockerfile.job FROM must reference the ARG BASE_IMAGE (got: {from_lines})."
