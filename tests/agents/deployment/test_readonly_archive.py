"""phaze-luy62: the read-only archive view -- the :ro mount, the write-refusal probe, and the runbook."""

import errno
import importlib.util
import os
from pathlib import Path
import stat
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "docker-compose.readonly-archive.example.yml"
SCRIPT = ROOT / "scripts" / "check_readonly_archive.py"


def _probe_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_readonly_archive", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_compose_mounts_the_archive_read_only() -> None:
    """Every volume of every service is a :ro bind -- the consumer view can never be writable."""
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert services
    for name, service in services.items():
        volumes = service["volumes"]
        assert volumes, name
        for volume in volumes:
            assert volume.endswith(":ro"), f"{name}: {volume} is not read-only"
        assert any(v.startswith("${ARCHIVE_MOUNT:?") for v in volumes), f"{name}: archive mount must fail fast when unset"


def test_probe_passes_when_the_filesystem_refuses_the_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    probe = _probe_module()

    def refuse(*_a: object, **_k: object) -> int:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(probe.os, "open", refuse)
    refused, detail = probe.probe_write(tmp_path)
    assert refused is True
    assert "EROFS" in detail
    assert probe.main([str(tmp_path)]) == 0


def test_probe_fails_on_an_unrecognised_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ENOSPC is not proof of read-only, so it must not read as a pass."""
    probe = _probe_module()

    def full(*_a: object, **_k: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(probe.os, "open", full)
    refused, _detail = probe.probe_write(tmp_path)
    assert refused is False
    assert probe.main([str(tmp_path)]) == 1


def test_probe_fails_and_cleans_up_on_a_writable_directory(tmp_path: Path) -> None:
    probe = _probe_module()
    refused, detail = probe.probe_write(tmp_path)
    assert refused is False
    assert "WRITE SUCCEEDED" in detail
    assert list(tmp_path.iterdir()) == []
    assert probe.main([str(tmp_path)]) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permission bits")
def test_probe_passes_on_a_real_unwritable_directory(tmp_path: Path) -> None:
    """Real kernel refusal, no mock: a 0555 directory refuses the create with EACCES."""
    probe = _probe_module()
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        refused, detail = probe.probe_write(locked)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert refused is True
    assert "EACCES" in detail


def test_probe_rejects_a_missing_directory(tmp_path: Path) -> None:
    assert _probe_module().main([str(tmp_path / "absent")]) == 1
