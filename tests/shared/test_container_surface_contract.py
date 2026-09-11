"""Contracts for the explicit, shared container/operator surface."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
VALIDATOR = (REPO_ROOT / "scripts" / "validate-containers.sh").read_text(encoding="utf-8")
IMAGE_HELPER = REPO_ROOT / "scripts" / "container-images.sh"


def _recipe(name: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(name)}(?: [^:]*)?:[^\n]*\n(?P<body>(?:^(?:    |\t).*$\n?)*)",
        JUSTFILE,
    )
    assert match is not None, f"missing recipe: {name}"
    return match.group("body")


def test_topology_sensitive_recipes_name_compose_files_explicitly() -> None:
    expected_files = {
        "up": ["docker-compose.yml"],
        "down": ["docker-compose.yml"],
        "rebuild": ["docker-compose.yml"],
        "logs": ["docker-compose.yml"],
        "up-dev": ["docker-compose.yml", "docker-compose.dev.yml"],
        "down-dev": ["docker-compose.yml", "docker-compose.dev.yml"],
        "up-agent": ["docker-compose.agent.yml"],
        "agent-down": ["docker-compose.agent.yml"],
        "cloud-agent-up": ["docker-compose.cloud-agent.yml"],
        "cloud-agent-down": ["docker-compose.cloud-agent.yml"],
        "up-all": ["docker-compose.yml", "docker-compose.agent.yml"],
        "down-all": ["docker-compose.yml", "docker-compose.agent.yml"],
    }
    for recipe, compose_files in expected_files.items():
        body = _recipe(recipe)
        assert "docker compose -f" in body, recipe
        for compose_file in compose_files:
            assert f"-f {compose_file}" in body, recipe


def test_shared_validator_covers_every_shipped_compose_shape() -> None:
    commands = {
        "docker-compose.yml": 'compose -f docker-compose.yml "${common[@]}"',
        "docker-compose.yml+docker-compose.dev.yml": ('compose -f docker-compose.yml -f docker-compose.dev.yml "${common[@]}"'),
        "docker-compose.agent.yml": 'compose -f docker-compose.agent.yml "${common[@]}"',
        "docker-compose.cloud-agent.yml": 'compose -f docker-compose.cloud-agent.yml "${common[@]}"',
        "docker-compose.telemetry.example.yml": 'compose -f docker-compose.telemetry.example.yml "${common[@]}"',
    }
    for shape, command in commands.items():
        assert command in VALIDATOR, shape


def test_docker_validation_workflow_delegates_to_shared_recipes() -> None:
    path = REPO_ROOT / ".github" / "workflows" / "docker-validate.yml"
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    commands = [step.get("run") for job in data["jobs"].values() for step in job["steps"] if "run" in step]
    assert "just docker-validate" in commands
    assert "just docker-compose-validate" in commands
    assert "docker compose" not in text


def test_non_ui_workflows_do_not_install_tailwind() -> None:
    workflows = [REPO_ROOT / ".github" / "workflows" / name for name in ("code-quality.yml", "security.yml", "docker-publish.yml")]
    for workflow in workflows:
        assert "just install" not in workflow.read_text(encoding="utf-8"), workflow.name

    tests_workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert tests_workflow.count("run: just install") == 1
    assert "test-browser" in tests_workflow
    assert "up: tailwind" not in JUSTFILE
    assert "rebuild: tailwind" not in JUSTFILE
    assert "up-dev: tailwind" in JUSTFILE
    assert "test-browser: tailwind" in JUSTFILE


def test_local_image_recipes_delegate_to_one_helper() -> None:
    for recipe in ("image-push", "image-build-arm64", "image-push-arm64"):
        body = _recipe(recipe)
        assert "scripts/container-images.sh" in body, recipe
        assert "ghcr.io" not in body, recipe
        assert "docker build" not in body, recipe
    for recipe in ("parity-golden-regen", "parity-check"):
        assert "scripts/container-images.sh ref" in _recipe(recipe)
        assert "scripts/container-images.sh models-dir" in _recipe(recipe)


def test_image_helper_derives_bare_and_arm64_ghcr_references() -> None:
    remote = subprocess.run(["/usr/bin/git", "remote", "get-url", "origin"], cwd=REPO_ROOT, check=True, text=True, capture_output=True).stdout.strip()
    repository = re.sub(r"^.*github\.com[:/]", "", remote).removesuffix(".git").lower()
    api = subprocess.run(  # noqa: S603 - fixed in-repository script and literal arguments
        ["/bin/bash", IMAGE_HELPER, "ref", "candidate", "api"], cwd=REPO_ROOT, check=True, text=True, capture_output=True
    ).stdout.strip()
    arm64 = subprocess.run(  # noqa: S603 - fixed in-repository script and literal arguments
        ["/bin/bash", IMAGE_HELPER, "ref", "candidate", "arm64"], cwd=REPO_ROOT, check=True, text=True, capture_output=True
    ).stdout.strip()
    assert api == f"ghcr.io/{repository}:candidate"
    assert arm64 == f"ghcr.io/{repository}:candidate-arm64"


def test_image_helper_preserves_models_path_with_spaces(tmp_path: Path) -> None:
    models = tmp_path / "model cache"
    models.mkdir()
    result = subprocess.run(  # noqa: S603 - fixed in-repository script and literal arguments
        ["/bin/bash", IMAGE_HELPER, "models-dir"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={"MODELS_PATH": str(models), "PATH": "/usr/bin:/bin"},
    )
    assert result.stdout.strip() == str(models)


def test_image_helper_authenticates_then_builds_and_pushes(tmp_path: Path) -> None:
    call_log = tmp_path / "docker calls"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\nif [[ \"${1:-}\" == api ]]; then printf 'test-operator\\n'; else printf 'test-token\\n'; fi\n",
        encoding="utf-8",
    )
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nif [[ "${1:-}" == login ]]; then read -r _token; fi\nprintf \'%s\\n\' "$*" >>"$CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    fake_docker.chmod(0o755)
    env = os.environ | {
        "CALL_LOG": str(call_log),
        "DOCKER_BIN": str(fake_docker),
        "GH_BIN": str(fake_gh),
    }

    subprocess.run(  # noqa: S603 - fixed in-repository script, fake executables, and literal arguments
        ["/bin/bash", IMAGE_HELPER, "push", "api", "candidate"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "login ghcr.io --username test-operator --password-stdin"
    assert calls[1].startswith("build -f Dockerfile -t ghcr.io/")
    assert calls[1].endswith(":candidate .")
    assert calls[2].startswith("push ghcr.io/")
    assert calls[2].endswith(":candidate")
