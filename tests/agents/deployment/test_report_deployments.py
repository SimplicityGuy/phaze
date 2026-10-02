"""The host collector sends actual running image IDs and only this Compose project."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "report-deployments.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("report_deployments", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_selects_only_phaze_containers_and_running_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    working_dir = Path("/srv/phaze")
    entries = [
        {
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "Config": {
                "Image": "ghcr.io/simplicityguy/phaze:latest",
                "Labels": {"com.docker.compose.service": "worker-analyze", "com.docker.compose.project.working_dir": str(working_dir)},
                "Env": ["PHAZE_ROLE=agent", "PHAZE_AGENT_LANE=analyze", "PHAZE_AGENT_TOKEN=never-send-this"],
            },
        },
        {
            "Id": "1" * 64,
            "Image": "sha256:" + "2" * 64,
            "Config": {
                "Image": "ghcr.io/simplicityguy/phaze:latest-arm64",
                "Labels": {"com.docker.compose.service": "worker", "com.docker.compose.project.working_dir": str(working_dir)},
                "Env": ["PHAZE_ROLE=agent", "PHAZE_AGENT_LANE=analyze"],
            },
        },
        {
            "Id": "3" * 64,
            "Image": "sha256:" + "4" * 64,
            "Config": {
                "Image": "phaze-worker:latest",
                "Labels": {"com.docker.compose.service": "worker", "com.docker.compose.project.working_dir": str(working_dir)},
                "Env": ["PHAZE_ROLE=control"],
            },
        },
        {
            "Id": "c" * 64,
            "Image": "sha256:" + "d" * 64,
            "Config": {
                "Image": "postgres:18-alpine",
                "Labels": {"com.docker.compose.service": "postgres", "com.docker.compose.project.working_dir": str(working_dir)},
            },
        },
        {
            "Id": "e" * 64,
            "Image": "sha256:" + "f" * 64,
            "Config": {
                "Image": "ghcr.io/simplicityguy/phaze:latest",
                "Labels": {"com.docker.compose.service": "api", "com.docker.compose.project.working_dir": "/another/project"},
            },
        },
    ]

    def fake_docker(*args: str, timeout: int = 10) -> str:
        if args[:2] == ("ps", "-q"):
            return "\n".join(entry["Id"] for entry in entries)
        assert args[0] == "inspect"
        return json.dumps(entries)

    monkeypatch.setattr(module, "_docker", fake_docker)
    monkeypatch.setattr(module, "_app_version", lambda _container_id: "2026.9.7")
    assert module.collect(working_dir) == [
        {
            "container_id": "a" * 64,
            "service": "worker-analyze",
            "role": "agent",
            "lane": "analyze",
            "app_version": "2026.9.7",
            "image_ref": "ghcr.io/simplicityguy/phaze:latest",
            "image_digest": "sha256:" + "b" * 64,
        },
        {
            "container_id": "1" * 64,
            "service": "worker",
            "role": "agent",
            "lane": "analyze",
            "app_version": "2026.9.7",
            "image_ref": "ghcr.io/simplicityguy/phaze:latest-arm64",
            "image_digest": "sha256:" + "2" * 64,
        },
        {
            "container_id": "3" * 64,
            "service": "worker",
            "role": "control",
            "lane": None,
            "app_version": "2026.9.7",
            "image_ref": "phaze-worker:latest",
            "image_digest": "sha256:" + "4" * 64,
        },
    ]
