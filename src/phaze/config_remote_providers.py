"""Generic per-provider settings for REMOTE tracklist providers (phaze-c2zke).

One pattern for every remote provider, not one settings block per provider: a provider declares a
:class:`RemoteProviderSpec` (its id and the names of the credentials it needs) and this module reads
``PHAZE_PROVIDER_<ID>_*`` from the environment for it. Nothing here names a concrete provider.

* ``..._ENABLED`` defaults to **false**. A remote provider the operator did not turn on is fail-closed:
  :func:`disabled_provider_ids` puts it in the registry's ``disabled`` set, so it stays listed by
  ``ProviderRegistry.descriptors()`` while discover/load return ``unavailable``/``disabled_provider``
  (docs/design/tracklist-provider-contract.md). Enabling one provider never enables another.
* Credentials are :class:`~pydantic.SecretStr` (masked in ``str``/``repr``) and honour the repo's
  ``<VAR>_FILE`` convention; a direct variable wins over its ``_FILE`` sibling.
* ``..._MIN_INTERVAL_SECONDS`` and ``..._MAX_REQUESTS_PER_RUN`` override the pacing and budget the
  transport applies. Unset means the transport's own conservative default, never "unlimited".

Credentials are restart-only. Enablement and pacing/budget defaults may be overridden live through
global provider Settings; durable provider policy rows are validated by ``services.remote_policy``
(docs/design/0019-runtime-config-hot-reload.md and docs/design/0025-remote-provider-sources.md).
"""

from collections.abc import Iterable, Mapping
import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from phaze.config_backends import _read_secret_file
from phaze.config_base import BaseSettings
from phaze.config_secrets import _resolution_env
from phaze.tracklist_providers.domain import Capability, ProviderDescriptor, ProviderId


ENV_PREFIX: Final = "PHAZE_PROVIDER_"
_RESERVED_NAMES: Final = frozenset({"ENABLED", "MIN_INTERVAL_SECONDS", "MAX_REQUESTS_PER_RUN"})
_CREDENTIAL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_BOOL: Final = TypeAdapter(bool)


class RemoteProviderSpec(BaseModel):
    """What a provider bead declares: its id and the names of its credentials (never their values)."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    credential_names: tuple[str, ...] = ()

    def model_post_init(self, __context: object) -> None:
        for name in self.credential_names:
            if not _CREDENTIAL_NAME.match(name) or name.upper() in _RESERVED_NAMES or name.upper().endswith("_FILE"):
                raise ValueError(f"Invalid credential name for provider {self.provider_id}: {name!r}")


class RemoteProviderSettings(BaseModel):
    """One provider's resolved settings. Off unless the operator says otherwise."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    enabled: bool = False
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    min_interval_seconds: float | None = Field(default=None, gt=0)
    max_requests_per_run: int | None = Field(default=None, gt=0)

    def credential(self, name: str) -> str | None:
        """The one place a secret value is unwrapped; callers must not log or repr it."""
        secret = self.credentials.get(name)
        return secret.get_secret_value() if secret is not None else None


def env_name(provider_id: str, suffix: str) -> str:
    return f"{ENV_PREFIX}{provider_id.upper()}_{suffix.upper()}"


def _lookup(env: Mapping[str, str], provider_id: str, suffix: str) -> str | None:
    value = env.get(env_name(provider_id, suffix))
    return value if value not in (None, "") else None


def _credential(env: Mapping[str, str], provider_id: str, name: str) -> SecretStr | None:
    direct = _lookup(env, provider_id, name)
    if direct is not None:
        return SecretStr(direct)
    path = _lookup(env, provider_id, f"{name}_FILE")
    return SecretStr(_read_secret_file(path, preserve_whitespace=False)) if path is not None else None


def load_remote_provider_settings(specs: Iterable[RemoteProviderSpec], environ: Mapping[str, str] | None = None) -> dict[str, RemoteProviderSettings]:
    """Resolve every declared provider's settings from ``environ`` (default: process env layered over ``.env``)."""
    env = {key.upper(): value for key, value in environ.items()} if environ is not None else _resolution_env(BaseSettings.model_config)
    resolved: dict[str, RemoteProviderSettings] = {}
    for spec in specs:
        if spec.provider_id in resolved:
            raise ValueError(f"Duplicate remote provider spec: {spec.provider_id}")
        enabled = _lookup(env, spec.provider_id, "ENABLED")
        interval = _lookup(env, spec.provider_id, "MIN_INTERVAL_SECONDS")
        budget = _lookup(env, spec.provider_id, "MAX_REQUESTS_PER_RUN")
        credentials = {name: secret for name in spec.credential_names if (secret := _credential(env, spec.provider_id, name)) is not None}
        resolved[spec.provider_id] = RemoteProviderSettings(
            provider_id=spec.provider_id,
            enabled=_BOOL.validate_python(enabled) if enabled is not None else False,
            credentials=credentials,
            min_interval_seconds=float(interval) if interval is not None else None,
            max_requests_per_run=int(budget) if budget is not None else None,
        )
    return resolved


def disabled_provider_ids(descriptors: Iterable[ProviderDescriptor], settings: Mapping[str, RemoteProviderSettings]) -> frozenset[str]:
    """The registry's ``disabled`` set: every remote provider not explicitly enabled. Fail-closed.

    A remote provider with no settings entry at all is disabled. A local provider is never disabled here.
    """
    return frozenset(
        descriptor.id
        for descriptor in descriptors
        if Capability.REMOTE_TRANSPORT in descriptor.capabilities and not (descriptor.id in settings and settings[descriptor.id].enabled)
    )


def provider_policy_keys() -> frozenset[str]:
    """Exact registered policy row names; credential names never enter this set."""
    from phaze.services.remote_providers import REMOTE_PROVIDERS  # noqa: PLC0415 -- composition import avoids cycle

    return frozenset(f"provider_{binding.profile.provider_id}_policy" for binding in REMOTE_PROVIDERS)
