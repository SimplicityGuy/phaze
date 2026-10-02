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
            "app_version": "2026.9.7",
            "image_ref": "ghcr.io/simplicityguy/phaze:latest",
            "image_digest": "sha256:" + "b" * 64,
        }
    ]
