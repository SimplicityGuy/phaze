"""Explicit composition root for the shipped REMOTE providers. Nothing here runs at import.

Calling :func:`remote_composition` is the one place a remote provider is registered: no import-time
registration, no entry points, no plugin discovery. The caller supplies the settings it already resolved with
``load_remote_provider_settings``; this module reads no environment and opens no connection.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import SecretStr
import structlog

from phaze.config_remote_providers import RemoteProviderSettings, disabled_provider_ids
from phaze.tracklist_providers.host_profile import RemoteHostProfile
from phaze.tracklist_providers.local import LocalProvider
from phaze.tracklist_providers.port import SourceInterpreter
from phaze.tracklist_providers.registry import ProviderRegistry
from phaze.tracklist_providers.trackid import PROVIDER_ID as TRACKID, TRACKID_SPEC, TrackIdProvider, trackid_credential, trackid_profile


logger = structlog.get_logger(__name__)

REMOTE_SPECS = (TRACKID_SPEC,)


@dataclass(frozen=True)
class RemoteComposition:
    """The registry plus what the transport needs. The credentials never appear in a repr."""

    registry: ProviderRegistry
    profiles: tuple[RemoteHostProfile, ...]
    credentials: Mapping[str, SecretStr] = field(repr=False)


def remote_composition(settings: Mapping[str, RemoteProviderSettings], *, local_interpreter: SourceInterpreter | None = None) -> RemoteComposition:
    """Register the local and TrackId providers; a remote provider is disabled unless its settings enable it."""
    trackid_settings = settings.get(TRACKID)
    profile = trackid_profile(trackid_settings)
    providers = (LocalProvider(local_interpreter), TrackIdProvider())
    disabled = disabled_provider_ids((provider.descriptor for provider in providers), settings)
    credentials: dict[str, SecretStr] = {}
    if trackid_settings is not None and trackid_settings.enabled:
        credential = trackid_credential(trackid_settings)
        if credential is None:
            # Enabled without a usable credential would send unauthenticated requests: fail closed instead.
            disabled |= {TRACKID}
            logger.warning("remote_registry.credential_missing", provider_id=TRACKID)
        else:
            credentials[TRACKID] = credential
    registry = ProviderRegistry(providers, disabled=disabled, remote_profiles=(profile,))
    return RemoteComposition(registry=registry, profiles=(profile,), credentials=credentials)
