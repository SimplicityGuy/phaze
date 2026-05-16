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
