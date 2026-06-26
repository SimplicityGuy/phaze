"""Unit tests for the Phase 50 push-pipeline config knobs.

ControlSettings gains the bounded ≤N cloud window (`cloud_max_in_flight`, D-03),
the push attempt cap (`push_max_attempts`, D-12), and the control-side scratch
mirror (`compute_scratch_dir`). AgentSettings gains the rsync-over-SSH target
fields plus two file-mounted secrets (`push_ssh_key`, `push_known_hosts`) wired
into SECRET_FILE_FIELDS so the shared `_resolve_secret_files` validator resolves
their `<VAR>_FILE` siblings. Pure pydantic-settings tests — no DB, no Redis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr
import pytest

from phaze.config import AgentSettings, ControlSettings


if TYPE_CHECKING:
    from pathlib import Path


_VALID_URL = "http://app.test:8000"
_VALID_TOKEN = "phaze_agent_test-token-abc123"
_VALID_ROOTS = "/data/music,/data/concerts"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from phaze.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)


# --------------------------------------------------------------------------- #
# ControlSettings: cloud_max_in_flight (D-03)
# --------------------------------------------------------------------------- #
def test_cloud_max_in_flight_default() -> None:
    assert ControlSettings().cloud_max_in_flight == 2


def test_cloud_max_in_flight_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_CLOUD_MAX_IN_FLIGHT", "5")
    assert ControlSettings().cloud_max_in_flight == 5


def test_cloud_max_in_flight_rejects_zero() -> None:
    with pytest.raises(ValueError, match="cloud_max_in_flight"):
        ControlSettings(cloud_max_in_flight=0)


def test_cloud_max_in_flight_rejects_too_large() -> None:
    with pytest.raises(ValueError, match="cloud_max_in_flight"):
        ControlSettings(cloud_max_in_flight=100)


# --------------------------------------------------------------------------- #
# ControlSettings: push_max_attempts (D-12)
# --------------------------------------------------------------------------- #
def test_push_max_attempts_default() -> None:
    assert ControlSettings().push_max_attempts == 3


def test_push_max_attempts_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_PUSH_MAX_ATTEMPTS", "7")
    assert ControlSettings().push_max_attempts == 7


def test_push_max_attempts_rejects_zero() -> None:
    with pytest.raises(ValueError, match="push_max_attempts"):
        ControlSettings(push_max_attempts=0)


def test_push_max_attempts_rejects_too_large() -> None:
    with pytest.raises(ValueError, match="push_max_attempts"):
        ControlSettings(push_max_attempts=20)


# --------------------------------------------------------------------------- #
# ControlSettings: compute_scratch_dir
# --------------------------------------------------------------------------- #
def test_compute_scratch_dir_default_none() -> None:
    c = ControlSettings()
    assert hasattr(c, "compute_scratch_dir")
    assert c.compute_scratch_dir is None


def test_compute_scratch_dir_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch/cloud")
    assert ControlSettings().compute_scratch_dir == "/scratch/cloud"


# --------------------------------------------------------------------------- #
# AgentSettings: push/SSH/scratch knobs
# --------------------------------------------------------------------------- #
def test_agent_push_knob_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _agent_env(monkeypatch)
    s = AgentSettings()
    assert s.push_ssh_host is None
    assert s.push_ssh_user is None
    assert s.cloud_scratch_dir is None
    assert s.push_timeout_sec == 600
    assert s.push_connect_timeout_sec == 30
    assert s.push_ssh_key is None
    assert s.push_known_hosts is None


def test_agent_push_knob_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    _agent_env(monkeypatch)
    monkeypatch.setenv("PHAZE_PUSH_SSH_HOST", "compute.internal")
    monkeypatch.setenv("PHAZE_PUSH_SSH_USER", "phaze")
    monkeypatch.setenv("PHAZE_CLOUD_SCRATCH_DIR", "/scratch/cloud")
    monkeypatch.setenv("PHAZE_PUSH_TIMEOUT_SEC", "900")
    monkeypatch.setenv("PHAZE_PUSH_CONNECT_TIMEOUT_SEC", "45")
    s = AgentSettings()
    assert s.push_ssh_host == "compute.internal"
    assert s.push_ssh_user == "phaze"
    assert s.cloud_scratch_dir == "/scratch/cloud"
    assert s.push_timeout_sec == 900
    assert s.push_connect_timeout_sec == 45


def test_agent_push_timeout_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _agent_env(monkeypatch)
    with pytest.raises(ValueError, match="push_timeout_sec"):
        AgentSettings(push_timeout_sec=0)


def test_agent_push_connect_timeout_rejects_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    _agent_env(monkeypatch)
    with pytest.raises(ValueError, match="push_connect_timeout_sec"):
        AgentSettings(push_connect_timeout_sec=3600)


# --------------------------------------------------------------------------- #
# AgentSettings: _FILE secret resolution for push_ssh_key / push_known_hosts
# --------------------------------------------------------------------------- #
def test_push_ssh_key_and_known_hosts_in_secret_file_fields() -> None:
    assert "push_ssh_key" in AgentSettings.SECRET_FILE_FIELDS
    assert "push_known_hosts" in AgentSettings.SECRET_FILE_FIELDS


def test_push_ssh_key_read_from_file_preserves_trailing_newline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WR-01: PHAZE_PUSH_SSH_KEY_FILE keeps the key VERBATIM, including the trailing newline.

    OpenSSH's private-key parser rejects a key without a final newline ("invalid format" /
    "error in libcrypto"), so the shared ``.strip()`` applied to other ``_FILE`` secrets must NOT
    touch the SSH key -- otherwise every PHAZE_PUSH_SSH_KEY_FILE-provisioned push fails at the ssh
    layer (the documented Docker/SOPS path).
    """
    _agent_env(monkeypatch)
    secret = tmp_path / "id_ed25519"
    secret.write_text("-----BEGIN KEY-----\nabc\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_PUSH_SSH_KEY", raising=False)
    monkeypatch.setenv("PHAZE_PUSH_SSH_KEY_FILE", str(secret))
    s = AgentSettings()
    assert isinstance(s.push_ssh_key, SecretStr)
    # Verbatim: the trailing newline is preserved (NOT stripped).
    assert s.push_ssh_key.get_secret_value() == "-----BEGIN KEY-----\nabc\n"


def test_push_known_hosts_read_from_file_preserves_trailing_newline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WR-01: PHAZE_PUSH_KNOWN_HOSTS_FILE keeps the pinned host keys verbatim (trailing newline kept)."""
    _agent_env(monkeypatch)
    secret = tmp_path / "known_hosts"
    secret.write_text("compute.internal ssh-ed25519 AAAA\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_PUSH_KNOWN_HOSTS", raising=False)
    monkeypatch.setenv("PHAZE_PUSH_KNOWN_HOSTS_FILE", str(secret))
    s = AgentSettings()
    assert isinstance(s.push_known_hosts, SecretStr)
    assert s.push_known_hosts.get_secret_value() == "compute.internal ssh-ed25519 AAAA\n"


def test_agent_token_file_still_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WR-01 guard: the verbatim exemption is key-material-only -- agent_token (hashed) stays stripped."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)
    token_file = tmp_path / "token"
    token_file.write_text(f"{_VALID_TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(token_file))
    s = AgentSettings()
    # The trailing newline is stripped so the hashed wire string matches an operator-typed env var.
    assert s.agent_token.get_secret_value() == _VALID_TOKEN
