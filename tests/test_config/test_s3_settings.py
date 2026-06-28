"""Unit tests for the Phase 53 S3 object-staging config surface (KSTAGE-02/05).

The control plane presigns S3 PUT/GET and deletes staged objects; it owns the S3
endpoint/bucket/region/addressing/credentials. These land on ``ControlSettings`` ONLY
(KSTAGE-02 -- the agent and pod get no bucket credentials) and honor the ``<VAR>_FILE``
secret convention (KSTAGE-05) via the inherited ``SECRET_FILE_FIELDS`` machinery.

Threat coverage:
  - T-53-01: creds are ``SecretStr`` on ControlSettings only, never on AgentSettings.
  - T-53-02: ``s3_endpoint_url`` must be a well-formed http(s) URL with a netloc
             (rejects SSRF-shaped ``file://`` / scheme-less values).
  - T-53-03: every int knob (presign TTLs, lifecycle TTL, multipart part size) is
             bounded so an out-of-range operator value fails fast at startup.

These are pure pydantic-settings tests -- no DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr, ValidationError
import pytest

from phaze.config import AgentSettings, ControlSettings


if TYPE_CHECKING:
    from pathlib import Path


_VALID_AGENT_URL = "http://app.test:8000"
_VALID_ROOTS = "/data/music,/data/concerts"


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the non-secret required agent fields so AgentSettings can construct."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_AGENT_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)


# --------------------------------------------------------------------------- #
# Endpoint / bucket / region binding
# --------------------------------------------------------------------------- #
def test_s3_endpoint_bucket_region_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHAZE_S3_ENDPOINT_URL / _BUCKET / _REGION bind to the ControlSettings fields."""
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.us-west-1.example.com")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_REGION", "us-west-1")

    cfg = ControlSettings()

    assert cfg.s3_endpoint_url == "https://s3.us-west-1.example.com"
    assert cfg.s3_bucket == "phaze-staging"
    assert cfg.s3_region == "us-west-1"


def test_s3_fields_default_none_when_unset() -> None:
    """An all-local deploy (cloud off) leaves the S3 fields optional/None."""
    cfg = ControlSettings()
    assert cfg.s3_endpoint_url is None
    assert cfg.s3_bucket is None
    assert cfg.s3_region is None
    assert cfg.s3_access_key_id is None
    assert cfg.s3_secret_access_key is None


# --------------------------------------------------------------------------- #
# _FILE secret resolution (KSTAGE-05, T-53-01)
# --------------------------------------------------------------------------- #
def test_s3_credentials_resolve_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PHAZE_S3_ACCESS_KEY_ID_FILE / _SECRET_ACCESS_KEY_FILE resolve to SecretStr."""
    access = tmp_path / "s3_access"
    access.write_text("AKIAEXAMPLE\n", encoding="utf-8")
    secret = tmp_path / "s3_secret"
    secret.write_text("super/secret/value\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("PHAZE_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("PHAZE_S3_ACCESS_KEY_ID_FILE", str(access))
    monkeypatch.setenv("PHAZE_S3_SECRET_ACCESS_KEY_FILE", str(secret))

    cfg = ControlSettings()

    assert isinstance(cfg.s3_access_key_id, SecretStr)
    assert cfg.s3_access_key_id.get_secret_value() == "AKIAEXAMPLE"
    assert isinstance(cfg.s3_secret_access_key, SecretStr)
    assert cfg.s3_secret_access_key.get_secret_value() == "super/secret/value"


def test_s3_secret_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved S3 secret stays SecretStr and is masked in repr (T-53-01)."""
    monkeypatch.setenv("PHAZE_S3_SECRET_ACCESS_KEY", "leak-me-not")
    cfg = ControlSettings()
    assert isinstance(cfg.s3_secret_access_key, SecretStr)
    assert "leak-me-not" not in repr(cfg)


# --------------------------------------------------------------------------- #
# Addressing style (Literal)
# --------------------------------------------------------------------------- #
def test_s3_addressing_style_default_path() -> None:
    """s3_addressing_style defaults to 'path' (broadest S3-compatible support)."""
    assert ControlSettings().s3_addressing_style == "path"


def test_s3_addressing_style_accepts_virtual(monkeypatch: pytest.MonkeyPatch) -> None:
    """'virtual' is the only other accepted addressing style."""
    monkeypatch.setenv("PHAZE_S3_ADDRESSING_STYLE", "virtual")
    assert ControlSettings().s3_addressing_style == "virtual"


def test_s3_addressing_style_rejects_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any value outside {path, virtual} fails validation."""
    monkeypatch.setenv("PHAZE_S3_ADDRESSING_STYLE", "dns")
    with pytest.raises(ValidationError):
        ControlSettings()


# --------------------------------------------------------------------------- #
# Endpoint URL validation (T-53-02 SSRF surface)
# --------------------------------------------------------------------------- #
def test_s3_endpoint_url_rejects_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file:// endpoint (SSRF-shaped) is rejected."""
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "file:///etc/passwd")
    with pytest.raises(ValidationError, match="s3_endpoint_url"):
        ControlSettings()


def test_s3_endpoint_url_rejects_scheme_less(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare hostname with no http(s) scheme is rejected."""
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "s3.example.com")
    with pytest.raises(ValidationError, match="s3_endpoint_url"):
        ControlSettings()


def test_s3_endpoint_url_accepts_http_and_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed http or https URL with a netloc is accepted."""
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "http://minio.internal:9000")
    assert ControlSettings().s3_endpoint_url == "http://minio.internal:9000"


# --------------------------------------------------------------------------- #
# Cloud-enabled fail-fast (mirrors compute_scratch_dir guard)
# --------------------------------------------------------------------------- #
def test_cloud_enabled_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """cloud_burst_enabled=True with no s3_bucket fails fast at construction."""
    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.delenv("PHAZE_S3_BUCKET", raising=False)
    with pytest.raises(ValueError, match="PHAZE_S3_BUCKET"):
        ControlSettings()


def test_cloud_enabled_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """cloud_burst_enabled=True with no s3_endpoint_url fails fast at construction."""
    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.delenv("PHAZE_S3_ENDPOINT_URL", raising=False)
    with pytest.raises(ValueError, match="PHAZE_S3_ENDPOINT_URL"):
        ControlSettings()


def test_cloud_enabled_with_full_s3_config_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """cloud_burst_enabled=True with bucket + endpoint + scratch dir constructs cleanly."""
    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")

    cfg = ControlSettings()

    assert cfg.cloud_burst_enabled is True
    assert cfg.s3_bucket == "phaze-staging"
    assert cfg.s3_endpoint_url == "https://s3.example.com"


def test_cloud_disabled_leaves_s3_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF (the default) needs no S3 config -- all-local deploys stay config-free."""
    monkeypatch.delenv("PHAZE_CLOUD_BURST_ENABLED", raising=False)
    monkeypatch.delenv("PHAZE_S3_BUCKET", raising=False)
    monkeypatch.delenv("PHAZE_S3_ENDPOINT_URL", raising=False)
    cfg = ControlSettings()
    assert cfg.cloud_burst_enabled is False
    assert cfg.s3_bucket is None
    assert cfg.s3_endpoint_url is None


# --------------------------------------------------------------------------- #
# KSTAGE-02: S3 config lives ONLY on the control plane
# --------------------------------------------------------------------------- #
def test_agent_settings_has_no_s3_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentSettings must expose NO s3_* field -- the agent never sees bucket creds."""
    _agent_env(monkeypatch)
    agent = AgentSettings()
    s3_fields = [name for name in AgentSettings.model_fields if name.startswith("s3_")]
    assert s3_fields == [], f"AgentSettings must not carry S3 fields (KSTAGE-02): {s3_fields}"
    assert not hasattr(agent, "s3_bucket")


# --------------------------------------------------------------------------- #
# Bounded int knobs (T-53-03)
# --------------------------------------------------------------------------- #
def test_presign_ttl_defaults() -> None:
    """PUT TTL defaults to 3600s; GET TTL defaults to 900s (short, just-in-time)."""
    cfg = ControlSettings()
    assert cfg.s3_presign_put_ttl_sec == 3600
    assert cfg.s3_presign_get_ttl_sec == 900


def test_presign_put_ttl_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-positive PUT TTL fails validation (gt=0)."""
    monkeypatch.setenv("PHAZE_S3_PRESIGN_PUT_TTL_SEC", "0")
    with pytest.raises(ValidationError, match="PHAZE_S3_PRESIGN_PUT_TTL_SEC"):
        ControlSettings()


def test_presign_get_ttl_rejects_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET TTL at/above the one-day cap fails validation (lt=86400)."""
    monkeypatch.setenv("PHAZE_S3_PRESIGN_GET_TTL_SEC", "86400")
    with pytest.raises(ValidationError, match="PHAZE_S3_PRESIGN_GET_TTL_SEC"):
        ControlSettings()


def test_lifecycle_ttl_default_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifecycle TTL defaults to 2 days and rejects an out-of-range value."""
    assert ControlSettings().s3_lifecycle_ttl_days == 2
    monkeypatch.setenv("PHAZE_S3_LIFECYCLE_TTL_DAYS", "0")
    with pytest.raises(ValidationError, match="PHAZE_S3_LIFECYCLE_TTL_DAYS"):
        ControlSettings()


def test_multipart_part_size_default_and_min(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multipart part size defaults to 64 MiB and rejects below the S3 5 MiB minimum."""
    assert ControlSettings().s3_multipart_part_size_bytes == 67108864
    monkeypatch.setenv("PHAZE_S3_MULTIPART_PART_SIZE_BYTES", "1048576")  # 1 MiB < 5 MiB min
    with pytest.raises(ValidationError, match="PHAZE_S3_MULTIPART_PART_SIZE_BYTES"):
        ControlSettings()
