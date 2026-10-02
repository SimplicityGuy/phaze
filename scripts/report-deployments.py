"""Report this host's running Phaze Compose containers to the control plane.

Run on the Docker HOST, never inside an application container. Docker's container ``Image``
field is the immutable image ID actually running; a mutable tag is sent separately.
"""

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx


SERVICES = {"api", "worker", "worker-analyze", "worker-meta", "worker-io", "worker-drain", "watcher"}
VERSION_CODE = "import importlib.metadata; print(importlib.metadata.version('phaze'))"


def _docker(*args: str, timeout: int = 10) -> str:
    """Run one bounded Docker CLI read and return its stdout."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=timeout)  # noqa: S603, S607 — fixed CLI
    return result.stdout.strip()


def _app_version(container_id: str) -> str | None:
    """Read the installed package version from the running container, if possible."""
    for command in (("uv", "run", "--no-sync", "python"), ("python3",)):
        try:
            version = _docker("exec", container_id, *command, "-c", VERSION_CODE, timeout=8)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if version and len(version) <= 64:
            return version
    return None


def _role_and_lane(entry: dict[str, Any], service: str) -> tuple[str | None, str | None]:
    """Read only role/lane names from Docker env; never forward env values or secrets."""
    selected: dict[str, str] = {}
    for assignment in entry.get("Config", {}).get("Env") or []:
        key, _separator, value = assignment.partition("=")
        if key in {"PHAZE_ROLE", "PHAZE_AGENT_LANE"}:
            selected[key] = value
    if service == "api":
        return "api", None
    if service == "worker":
        role = selected.get("PHAZE_ROLE")
        lane = selected.get("PHAZE_AGENT_LANE")
        return (role if role in {"agent", "control"} else None), (lane if role == "agent" and lane in {"analyze", "meta", "io", "drain"} else None)
    if service.startswith("worker-"):
        return "agent", service.removeprefix("worker-")
    return "agent", None


def collect(working_dir: Path) -> list[dict[str, str | None]]:
    """Collect only allowlisted services in the Compose project at ``working_dir``."""
    ids = _docker("ps", "-q", "--no-trunc").splitlines()
    if not ids:
        return []
    inspected = json.loads(_docker("inspect", *ids, timeout=20))
    containers: list[dict[str, str | None]] = []
    for entry in inspected:
        labels = entry.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        project_dir = labels.get("com.docker.compose.project.working_dir")
        if service not in SERVICES or project_dir != str(working_dir):
            continue
        container_id = entry["Id"]
        image_id = entry.get("Image")
        role, lane = _role_and_lane(entry, service)
        containers.append(
            {
                "container_id": container_id,
                "service": service,
                "role": role,
                "lane": lane,
                "app_version": _app_version(container_id),
                "image_ref": entry.get("Config", {}).get("Image"),
                "image_digest": image_id if isinstance(image_id, str) and image_id.startswith("sha256:") else None,
            }
        )
    return containers


def main() -> None:
    """Send one snapshot; schedule this script on each host for continuous observation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Control-plane base URL, without credentials")
    parser.add_argument("--ca-file", type=Path, help="CA certificate for the control-plane HTTPS endpoint")
    args = parser.parse_args()
    url = urlsplit(args.api_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        parser.error("--api-url must be an HTTP(S) base URL without credentials")
    body = {"host": socket.gethostname(), "containers": collect(Path.cwd().resolve())}
    with httpx.Client(timeout=15, verify=str(args.ca_file) if args.ca_file else True) as client:
        response = client.post(f"{args.api_url.rstrip('/')}/api/internal/deployments", json=body)
        response.raise_for_status()
        if response.status_code != 204:
            raise RuntimeError(f"deployment report returned HTTP {response.status_code}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, httpx.HTTPError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"deployment report failed: {exc}\n")
        raise SystemExit(1) from exc
