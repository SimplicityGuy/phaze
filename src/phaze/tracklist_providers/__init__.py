"""Source-neutral provider boundary; adapters depend inward, consumers use the port.

No registration, filesystem reads, storage or scheduling occurs during import.
"""

from phaze.tracklist_providers.local import LocalProvider
from phaze.tracklist_providers.port import ProviderEffects, SourceInterpreter, TracklistProvider
from phaze.tracklist_providers.registry import ProviderContractError, ProviderRegistry


def local_registry(interpreter: SourceInterpreter | None = None) -> ProviderRegistry:
    """Explicit composition root for the shipped local adapter."""
    return ProviderRegistry((LocalProvider(interpreter),))
