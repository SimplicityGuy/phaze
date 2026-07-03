"""Unit tests for AgentSettings production-mode Redis-password enforcement (Phase 29 D-06).

Covers 4 behaviors of `phaze.config.AgentSettings`:
1. `agent_env="production"` + passwordless `redis_url` → `ValidationError` containing
   the substring ``"requires a password in redis_url"``.
2. `agent_env="production"` + passworded `redis_url` (`redis://default:secret@host:6379`)
   constructs successfully.
3. `agent_env="dev"` + passwordless `redis_url` constructs successfully (dev convenience
   per RESEARCH §Pitfall 7).
4. Default `agent_env` is `"dev"` when not set (preserves existing dev workflow).

Tests pass kwargs directly to `AgentSettings(...)` rather than using env-var indirection;
this is cleaner than the env-var monkeypatch pattern used elsewhere because the contract
under test is the model itself, not the env-var → field mapping.

No DB, no Redis required.
"""

from __future__ import annotations

from pydantic import SecretStr, ValidationError
import pytest


_VALID_API_URL = "https://api.test:8000"
_VALID_TOKEN = SecretStr("phaze_agent_test-token-abc123")
_VALID_ROOTS = ["/data/music"]
_PASSWORDLESS_URL = "redis://localhost:6379/0"
_PASSWORDED_URL = "redis://default:secret@localhost:6379/0"


def test_production_refuses_passwordless_redis_url() -> None:
    """D-06: agent_env=production + passwordless redis_url raises ValidationError.

    The error message must contain ``"requires a password in redis_url"`` so the
    operator sees an actionable hint pointing at Phase 29 D-06.
    """
    from phaze.config import AgentSettings

    with pytest.raises(ValidationError) as exc_info:
        AgentSettings(
            agent_env="production",
            redis_url=_PASSWORDLESS_URL,
            agent_api_url=_VALID_API_URL,
            agent_token=_VALID_TOKEN,
            scan_roots=_VALID_ROOTS,
        )
    assert "requires a password in redis_url" in str(exc_info.value), f"Expected D-06 password hint in error; got: {exc_info.value}"


def test_production_accepts_passworded_redis_url() -> None:
    """D-06: agent_env=production + `redis://default:<pw>@host:6379` constructs OK."""
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_env="production",
        redis_url=_PASSWORDED_URL,
        agent_api_url=_VALID_API_URL,
        agent_token=_VALID_TOKEN,
        scan_roots=_VALID_ROOTS,
    )
    assert cfg.agent_env == "production"
    assert cfg.redis_url == _PASSWORDED_URL


def test_dev_accepts_passwordless_redis_url() -> None:
    """D-06: agent_env=dev (the default) permits passwordless redis_url.

    Pitfall 7: fresh dev clones must `docker compose up` without supplying a
    Redis password; the `agent_env=dev` default lets this work.
    """
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_env="dev",
        redis_url=_PASSWORDLESS_URL,
        agent_api_url=_VALID_API_URL,
        agent_token=_VALID_TOKEN,
        scan_roots=_VALID_ROOTS,
    )
    assert cfg.agent_env == "dev"
    assert cfg.redis_url == _PASSWORDLESS_URL


def test_default_agent_env_is_dev() -> None:
    """D-06: omitting `agent_env` defaults to `"dev"` so existing call sites are unaffected."""
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        redis_url=_PASSWORDLESS_URL,
        agent_api_url=_VALID_API_URL,
        agent_token=_VALID_TOKEN,
        scan_roots=_VALID_ROOTS,
    )
    assert cfg.agent_env == "dev", f"Default agent_env must be 'dev'; got {cfg.agent_env!r}"


# ---------------------------------------------------------------------------
# Phase 29 CR-01: production refuses http:// for agent_api_url
# ---------------------------------------------------------------------------


def test_production_refuses_http_agent_api_url() -> None:
    """CR-01: agent_env=production + http:// agent_api_url raises ValidationError.

    The bearer token would otherwise transit in plaintext on the LAN, defeating
    the entire TLS bootstrap landed in this phase.
    """
    from phaze.config import AgentSettings

    with pytest.raises(ValidationError) as exc_info:
        AgentSettings(
            agent_env="production",
            agent_api_url="http://app.test:8000",
            agent_token=_VALID_TOKEN,
            redis_url=_PASSWORDED_URL,
            scan_roots=_VALID_ROOTS,
        )
    assert "requires https://" in str(exc_info.value), f"Expected CR-01 https:// hint in error; got: {exc_info.value}"


def test_production_accepts_https_agent_api_url() -> None:
    """CR-01: agent_env=production + https:// agent_api_url constructs OK."""
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_env="production",
        agent_api_url="https://app.test:8000",
        agent_token=_VALID_TOKEN,
        redis_url=_PASSWORDED_URL,
        scan_roots=_VALID_ROOTS,
    )
    assert cfg.agent_api_url == "https://app.test:8000"


def test_dev_accepts_http_agent_api_url() -> None:
    """CR-01: agent_env=dev permits http:// agent_api_url for local dev convenience."""
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_env="dev",
        agent_api_url="http://localhost:8000",
        agent_token=_VALID_TOKEN,
        redis_url=_PASSWORDLESS_URL,
        scan_roots=_VALID_ROOTS,
    )
    assert cfg.agent_api_url == "http://localhost:8000"


# ---------------------------------------------------------------------------
# Phase 29 CR-02: PHAZE_REDIS_URL env var must bind to BaseSettings.redis_url
# ---------------------------------------------------------------------------


def test_phaze_redis_url_env_var_binds(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """CR-02: setting PHAZE_REDIS_URL=... overrides the default redis_url.

    The original BaseSettings.redis_url had no validation_alias, so pydantic-
    settings silently ignored PHAZE_REDIS_URL. A production agent following
    `.env.example.agent` would fall back to the default and trigger the
    passwordless-Redis validator at startup with a misleading error.
    """
    from phaze.cert_bootstrap import ensure_certs_present
    from phaze.config import AgentSettings

    ensure_certs_present(tmp_path, cn="localhost", sans_csv="localhost,127.0.0.1")

    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://default:operator-supplied@redis.test:6379/0")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "https://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    monkeypatch.setenv("PHAZE_AGENT_ENV", "production")
    monkeypatch.setenv("PHAZE_AGENT_CA_FILE", str(tmp_path / "phaze-ca.crt"))

    cfg = AgentSettings()

    assert cfg.redis_url == "redis://default:operator-supplied@redis.test:6379/0", (
        f"PHAZE_REDIS_URL must override the default redis_url; got: {cfg.redis_url!r}"
    )
    assert cfg.agent_env == "production"
