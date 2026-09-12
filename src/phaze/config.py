"""Public facade for role-specific Pydantic settings selected at process startup."""

from enum import StrEnum
from functools import lru_cache
import os

from pydantic import SecretStr

from phaze.config_agent import AgentSettings
from phaze.config_base import BaseSettings
from phaze.config_control import ControlSettings, logger as _logger


logger = _logger


class Role(StrEnum):
    """v4.0 role selector. Controller = application server (fileless tasks); Agent = file server (file-bound tasks)."""

    CONTROL = "control"
    AGENT = "agent"


@lru_cache(maxsize=1)
def get_settings() -> BaseSettings:
    """Return the role-specific settings instance for this process.

    Reads `PHAZE_ROLE` from the env once (default: "control") and dispatches to the
    matching subclass. The instance is cached via `lru_cache` so the singleton is
    constructed exactly once per process.
    """
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == Role.AGENT.value:
        return AgentSettings()
    return ControlSettings()


def export_llm_api_keys(*, anthropic_api_key: SecretStr | None, openai_api_key: SecretStr | None) -> None:
    """Bridge file-loaded LLM secrets into the provider env vars litellm reads.

    litellm resolves provider credentials from the process environment
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``), never from phaze's settings. Phaze
    loads these keys via the ``<VAR>_FILE`` secret convention into ``SecretStr`` fields
    on :class:`ControlSettings`, so without this bridge every ``litellm.acompletion``
    call lacks provider authentication.

    Called once from the control worker's startup hook. Each present key is exported
    ONLY when the bare provider env var is unset, so an operator-supplied
    ``ANTHROPIC_API_KEY`` always wins. The secret value is never logged.
    """
    for env_name, secret in (
        ("ANTHROPIC_API_KEY", anthropic_api_key),
        ("OPENAI_API_KEY", openai_api_key),
    ):
        if secret is not None and not os.environ.get(env_name):
            os.environ[env_name] = secret.get_secret_value()


def _build_default_settings() -> ControlSettings:
    """Construct the module-level singleton. Splits out from `get_settings()` so the
    module-level type checks as `ControlSettings` — every existing call site reads
    `settings.llm_*` / `settings.discogs_match_concurrency`, which live on
    `ControlSettings`. When `PHAZE_ROLE=agent`, the agent worker should call
    `get_settings()` explicitly (or import `AgentSettings`) rather than reading the
    module-level singleton.
    """
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == Role.AGENT.value:
        # Agent entry points call get_settings() directly; this legacy singleton stays Control-typed.
        return ControlSettings()
    return ControlSettings()


# Module-level singleton preserves back-compat with `from phaze.config import settings`.
# Typed as ControlSettings because the legacy `Settings` class was effectively the
# Control superset; the agent worker uses `get_settings()` / `AgentSettings()` directly.
settings: ControlSettings = _build_default_settings()

# Back-compat alias for callers that still import `Settings`. Some test files
# import the class directly (e.g., `from phaze.config import Settings`). Until those
# call sites migrate to `ControlSettings` / `AgentSettings` / `get_settings()`, the
# alias keeps them working — `Settings` resolves to `ControlSettings` (the superset
# that contains every field the old monolithic class had).
Settings = ControlSettings
