"""Unit tests for `BaseSettings._apply_redis_password` (phaze-1g89i).

docker-compose.yml used to assemble the FULL authenticated Redis DSN via raw string
interpolation: ``REDIS_URL=redis://default:${REDIS_PASSWORD}@redis:6379/0``. Compose
substitutes the byte sequence verbatim, but ``redis.asyncio.Redis.from_url`` then
RFC-3986-parses the result and percent-DECODES the userinfo:

- a password containing ``/``, ``#``, or ``?`` truncates the netloc, raising
  ``ValueError: Port could not be cast to integer value as '...'`` at ``from_url`` --
  crash-looping the api/worker under ``restart: unless-stopped``;
- a password containing a ``%XX`` sequence parses cleanly but is silently decoded to
  DIFFERENT bytes (e.g. ``p%41ss`` -> ``pAss``), so every AUTH sends the wrong password.

Meanwhile ``redis-server --requirepass "${REDIS_PASSWORD}"`` and its
``redis-cli -a "${REDIS_PASSWORD}" ping`` healthcheck both consume the SAME raw value with
no URL parsing at all, so redis itself stays green while only the app-server's own clients
break -- the worst possible failure shape.

Fix: stop assembling the DSN in compose. Pass ``REDIS_PASSWORD`` through as its own env var
(no URL involved, so no truncation/decoding risk) and let ``config.py``'s
``_apply_redis_password`` `urllib.parse.quote` it into ``redis_url``'s userinfo in Python,
where the byte string is still intact.

No DB, no Redis required.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from pydantic import ValidationError
import pytest


# Deliberately exercises every character class the bug report calls out:
# '/', '#', '?' (netloc-truncating) and '%' (silently mis-decoding).
_SLASH_PASSWORD = "str0ng/pa#ss?word"
_SIMPLE_PASSWORD = "hunter2"
_PERCENT_PASSWORD = "p%41ss"


def test_redis_password_is_percent_encoded_into_redis_url() -> None:
    """A password with RFC-3986 reserved characters round-trips through redis_url."""
    from phaze.config import ControlSettings

    # redis_url passed explicitly: init kwargs outrank env in pydantic-settings, so an ambient
    # REDIS_URL/PHAZE_REDIS_URL (a dev shell's seat export, CI's localhost service) cannot swap
    # the host out from under the assertions below -- that non-hermeticity is exactly how this
    # test passed locally and failed in CI.
    cfg = ControlSettings(control_env="production", redis_url="redis://redis:6379/0", redis_password=_SLASH_PASSWORD)
    parsed = urlparse(cfg.redis_url)
    assert unquote(parsed.password) == _SLASH_PASSWORD, (
        f"expected password to round-trip via urlparse+unquote; got {parsed.password!r} from {cfg.redis_url!r}"
    )
    assert parsed.hostname == "redis"
    assert parsed.port == 6379


def test_redis_password_with_percent_sequence_round_trips_exactly() -> None:
    """A password containing a literal `%XX`-shaped substring is not mis-decoded (phaze-1g89i)."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="production", redis_password=_PERCENT_PASSWORD)
    parsed = urlparse(cfg.redis_url)
    assert unquote(parsed.password) == _PERCENT_PASSWORD, f"expected exact round-trip (no %-decoding drift); got {unquote(parsed.password)!r}"


def test_redis_url_unchanged_when_no_redis_password_set() -> None:
    """Omitting redis_password leaves redis_url exactly as configured (back-compat)."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="dev", redis_url="redis://redis:6379/0")
    assert cfg.redis_url == "redis://redis:6379/0"


def test_redis_password_defaults_username_to_default() -> None:
    """A host-only redis_url (no explicit username) gets the `default` ACL user."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="production", redis_url="redis://redis:6379/0", redis_password=_SIMPLE_PASSWORD)
    parsed = urlparse(cfg.redis_url)
    assert parsed.username == "default"
    assert parsed.password == _SIMPLE_PASSWORD


def test_redis_password_preserves_existing_username() -> None:
    """An operator-set explicit username in redis_url is preserved, only the password swaps."""
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="production", redis_url="redis://custom-user@redis:6379/0", redis_password=_SIMPLE_PASSWORD)
    parsed = urlparse(cfg.redis_url)
    assert parsed.username == "custom-user"
    assert parsed.password == _SIMPLE_PASSWORD


def test_redis_password_satisfies_production_enforcement_guard() -> None:
    """phaze-hti8's production guard sees the injected password -- validator ordering holds.

    `_apply_redis_password` (BaseSettings) must run before
    `_enforce_redis_password_in_production` (ControlSettings) so a passwordless `redis_url`
    plus a `redis_password` still satisfies the production guard, exactly as compose's own
    `REDIS_PASSWORD` pass-through now relies on.
    """
    from phaze.config import ControlSettings

    cfg = ControlSettings(control_env="production", redis_url="redis://redis:6379/0", redis_password=_SLASH_PASSWORD)
    assert cfg.control_env == "production"
    parsed = urlparse(cfg.redis_url)
    assert unquote(parsed.password) == _SLASH_PASSWORD


def test_production_still_refuses_when_neither_password_source_is_set() -> None:
    """No redis_password AND no embedded password still raises (phaze-hti8 regression guard)."""
    from phaze.config import ControlSettings

    with pytest.raises(ValidationError) as exc_info:
        ControlSettings(control_env="production", redis_url="redis://redis:6379/0")
    assert "requires a password in redis_url" in str(exc_info.value)


def test_agent_settings_also_applies_redis_password() -> None:
    """redis_password lives on BaseSettings, so AgentSettings gets the same encoding."""
    from pydantic import SecretStr

    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_env="dev",
        agent_api_url="https://api.test:8000",
        agent_token=SecretStr("phaze_agent_test-token-abc123"),
        scan_roots=["/data/music"],
        redis_url="redis://cache-host:6379/0",
        redis_password=_SLASH_PASSWORD,
        # phaze-27myl fails fast when a non-compute agent's queue_url still points at the
        # compose 'postgres' service default; this test is about redis_password encoding, so
        # satisfy the validator explicitly instead of leaning on ambient PHAZE_QUEUE_URL.
        queue_url="postgresql://phaze:phaze@queue-host:5432/phaze",
    )
    parsed = urlparse(cfg.redis_url)
    assert unquote(parsed.password) == _SLASH_PASSWORD
    assert parsed.hostname == "cache-host"
