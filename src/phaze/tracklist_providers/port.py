"""The inward provider and authorized read/interpretation seams."""

from typing import Protocol

from phaze.tracklist_providers.domain import (
    DiscoveryBudget,
    DiscoveryOutcome,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    ProviderCandidate,
    ProviderDescriptor,
    RecordingContext,
    SourceRead,
    SourceReference,
)


class ProviderEffects(Protocol):
    """The core effect injected per invocation."""

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead: ...


class AuthorizedEffects(ProviderEffects, Protocol):
    """The registry's bounded view of the core effect, as a provider receives it."""

    @property
    def minted(self) -> tuple[SourceReference, ...]:
        """Remote references the registry minted for this invocation; read the next page by copying one."""
        ...

    @property
    def skipped_terms(self) -> tuple[str, ...]:
        """Field names of usable facts left out of the minted search for exceeding the term bound; never values."""
        ...


class SourceInterpreter(Protocol):
    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome: ...


class TracklistProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome: ...

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome: ...
