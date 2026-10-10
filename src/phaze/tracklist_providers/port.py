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
    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead: ...


class SourceInterpreter(Protocol):
    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome: ...


class TracklistProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: ProviderEffects
    ) -> DiscoveryOutcome: ...

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: ProviderEffects) -> LoadOutcome: ...
