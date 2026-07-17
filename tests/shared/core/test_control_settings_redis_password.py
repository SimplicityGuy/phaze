"""Unit tests for ControlSettings production-mode Redis-password enforcement (phaze-hti8).

docker-compose.yml starts Redis with ``--requirepass ${REDIS_PASSWORD:?...}``, so every
Redis client MUST authenticate. AgentSettings already refuses a passwordless ``redis_url``
in production (Phase 29 D-06), but ControlSettings — the app-server role whose clients
(``app.state.redis``, the controller queue's ``cache_redis``, per-agent queue counters /
rate-limits) all consume ``redis_url`` verbatim — had NO parallel guard, so a misconfigured
control plane connected NOAUTH and only failed on first Redis use.

Covers 4 behaviors of ``phaze.config.ControlSettings``:
1. ``control_env="production"`` + passwordless ``redis_url`` → ``ValidationError`` containing
   ``"requires a password in redis_url"``.
2. ``control_env="production"`` + passworded ``redis_url`` constructs successfully.
3. ``control_env="dev"`` + passwordless ``redis_url`` constructs successfully (fresh-clone
   convenience — mirrors AgentSettings dev behavior).
4. Default ``control_env`` is ``"dev"`` when not set (preserves existing dev/test workflow).

No DB, no Redis required.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest


_PASSWORDLESS_URL = "redis://redis:6379/0"
_PASSWORDED_URL = "redis://default:secret@redis:6379/0"


def test_production_refuses_passwordless_redis_url() -> None:
    """phaze-hti8: control_env=production + passwordless redis_url raises ValidationError."""
    from phaze.config import ControlSettings

    with pytest.raises(ValidationError) as exc_info:
        ControlSettings(control_env="production", redis_url=_PASSWORDLESS_URL)
    assert "requires a password in redis_url" in str(exc_info.value), f"Expected password hint in error; got: {exc_info.value}"


def test_production_accepts_passworded_redis_url() -> None:
    """phaze-hti8: control_env=production + `redis://default:<pw>@host:6379` constructs OK."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="production", redis_url=_PASSWORDED_URL)
    assert cfg.control_env == "production"
    assert cfg.redis_url == _PASSWORDED_URL


def test_dev_accepts_passwordless_redis_url() -> None:
    """phaze-hti8: control_env=dev (the default) permits passwordless redis_url for local dev."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="dev", redis_url=_PASSWORDLESS_URL)
    assert cfg.control_env == "dev"
    assert cfg.redis_url == _PASSWORDLESS_URL


def test_default_control_env_is_dev() -> None:
    """phaze-hti8: omitting control_env defaults to 'dev' so existing call sites are unaffected."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(redis_url=_PASSWORDLESS_URL)
    assert cfg.control_env == "dev", f"Default control_env must be 'dev'; got {cfg.control_env!r}"
