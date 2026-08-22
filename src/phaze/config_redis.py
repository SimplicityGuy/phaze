"""The Redis AUTH password half of the settings surface (phaze-bk9el.18).

Extracted VERBATIM from `phaze.config.BaseSettings`, which measured LCOM4=3. This module is
the second of those three groups: the `redis_url` DSN, the raw `redis_password` that
docker-compose hands over un-encoded, and the `mode="after"` validator that percent-encodes
one into the other.

`RedisPasswordSettingsMixin` is a base of `phaze.config.BaseSettings`, so `settings.redis_url`
and `settings.redis_password` resolve exactly as before at all 153 dependent call sites, and
`_apply_redis_password` still runs ahead of the subclass guards
(`ControlSettings._enforce_redis_password_in_production`, its `AgentSettings` twin) that read
the DSN it produces -- a base class stays earlier in the MRO, which is what fixes that order.
"""

from urllib.parse import quote, urlparse, urlunparse

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings


class RedisPasswordSettingsMixin(PydanticBaseSettings):
    """`redis_url` + `redis_password`, and the encode-one-into-the-other validator.

    The pair is inseparable: `redis_password` exists ONLY to be injected into `redis_url`,
    and the injection is the reason the raw password is a field at all rather than being
    interpolated into the DSN by compose (phaze-1g89i).
    """

    # Redis
    # Phase 29 CR-02: bind PHAZE_REDIS_URL via validation_alias so the agent-side
    # `_enforce_redis_password_in_production` validator actually sees operator-supplied
    # credentials. Without the alias the env var is silently ignored and the
    # production agent fails to start with the misleading "requires a password" error.
    redis_url: str = Field(
        default="redis://redis:6379/0",
        validation_alias=AliasChoices("PHAZE_REDIS_URL", "REDIS_URL", "redis_url"),
    )

    # phaze-1g89i: the RAW (un-encoded) Redis AUTH password. Mirrors the SAME env var that
    # docker-compose.yml's `redis-server --requirepass "${REDIS_PASSWORD}"` and its
    # `redis-cli -a "${REDIS_PASSWORD}"` healthcheck consume verbatim -- both accept ANY byte
    # sequence, no URL parsing involved. `redis.asyncio.Redis.from_url(redis_url)`, by
    # contrast, RFC-3986-parses the DSN and percent-DECODES the userinfo: a password
    # containing `/`, `#`, or `?` used to break compose's raw string interpolation into
    # `redis://default:${REDIS_PASSWORD}@redis:6379/0` (truncated netloc -> `ValueError: Port
    # could not be cast to integer`), and one containing a `%XX` sequence parsed cleanly but
    # decoded to the WRONG bytes, so every AUTH silently sent the wrong password. Setting
    # REDIS_PASSWORD here (instead of pre-assembling the URL in compose) lets
    # `_apply_redis_password` below `urllib.parse.quote` it into `redis_url`'s userinfo AFTER
    # Python -- not a shell -- has the full, unmangled byte string.
    redis_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("REDIS_PASSWORD", "redis_password"),
        description="Raw Redis AUTH password; percent-encoded into redis_url by _apply_redis_password.",
    )

    @model_validator(mode="after")
    def _apply_redis_password(self) -> "RedisPasswordSettingsMixin":
        """phaze-1g89i: safely inject `redis_password` into `redis_url`'s userinfo.

        Runs whenever `redis_password` is set, regardless of what `redis_url` already
        contains -- mirroring the precedence compose's own interpolation used to have
        (`REDIS_URL=redis://default:${REDIS_PASSWORD}@redis:6379/0` always overrode the
        passwordless `.env` default). The password is percent-encoded with
        `safe=""` so every RFC-3986 reserved byte (`/`, `#`, `?`, `%`, `@`, `:`, ...) round-trips
        through `Redis.from_url`'s parser exactly, instead of corrupting the URL shape or
        silently decoding to different bytes.

        A `redis_url` that fails to parse (e.g. still carries a raw-embedded special-character
        password from an old-style `${REDIS_PASSWORD}` interpolation) is left untouched --
        this validator repairs the DSN going forward, it does not attempt to recover an
        already-mangled one.
        """
        if self.redis_password is None:
            return self
        parsed = urlparse(self.redis_url)
        if not parsed.hostname:
            return self
        user = parsed.username or "default"
        encoded_password = quote(self.redis_password.get_secret_value(), safe="")
        netloc = f"{quote(user, safe='')}:{encoded_password}@{parsed.hostname}"
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        self.redis_url = urlunparse(parsed._replace(netloc=netloc))
        return self
