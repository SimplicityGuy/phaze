"""The remote tracklist providers this process composes, and the one shape an adapter contributes.

Each remote adapter bead appends exactly one :class:`RemoteProviderBinding` to :data:`REMOTE_PROVIDERS`. The
composition root (``phaze.services.remote_lookup``) builds one registry and one transport from the tuple, once per
process. A binding names no host of its own: the host profile is core's, declared FOR the provider
(docs/design/0025-remote-provider-sources.md, section 2), and every provider starts disabled until its settings
enable it (``phaze.config_remote_providers``).
"""

from collections.abc import Callable
from dataclasses import dataclass

from phaze.config_remote_providers import RemoteProviderSpec
from phaze.tracklist_providers.domain import ProviderDescriptor
from phaze.tracklist_providers.host_profile import RemoteHostProfile
from phaze.tracklist_providers.mixesdb import PROFILE as MIXESDB_PROFILE, MixesDbProvider
from phaze.tracklist_providers.port import TracklistProvider
from phaze.tracklist_providers.trackid import CREDENTIAL_NAME, TRACKID_PROFILE, TrackIdProvider, check_trackid_credential


@dataclass(frozen=True)
class RemoteProviderBinding:
    """One remote adapter at the composition root.

    ``provider`` is the adapter; ``profile`` is core's host profile for it; ``credential_name`` names the settings
    credential (``PHAZE_PROVIDER_<ID>_<NAME>``) that fills the profile's credential slot, or ``None`` when the
    profile has no slot. ``credential_check`` optionally validates its value's shape at startup. ``spec`` is the
    settings declaration derived from them.
    """

    provider: TracklistProvider
    profile: RemoteHostProfile
    credential_name: str | None = None
    # Raises ValueError, naming the setting and never the value, when the credential is malformed. Run at startup.
    credential_check: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        descriptor = ProviderDescriptor.model_validate(self.provider.descriptor)
        if descriptor.id != self.profile.provider_id:
            raise ValueError(f"Host profile {self.profile.provider_id} is not for provider {descriptor.id}")
        if (self.credential_name is None) != (self.profile.credential is None):
            raise ValueError(f"A credential name is given exactly when the host profile has a credential slot: {descriptor.id}")

    @property
    def spec(self) -> RemoteProviderSpec:
        return RemoteProviderSpec(provider_id=self.profile.provider_id, credential_names=(self.credential_name,) if self.credential_name else ())


# The shipped remote adapters. Each adapter bead appends its binding here. Every one ships disabled.
REMOTE_PROVIDERS: tuple[RemoteProviderBinding, ...] = (
    RemoteProviderBinding(TrackIdProvider(), TRACKID_PROFILE, CREDENTIAL_NAME, credential_check=check_trackid_credential),
    RemoteProviderBinding(MixesDbProvider(), MIXESDB_PROFILE, None),
)
