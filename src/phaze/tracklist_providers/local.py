"""Local source adapter. Authorized reads and parsing are explicitly injected."""

from phaze.tracklist_providers.domain import (
    Capability,
    Completeness,
    DiscoveryBudget,
    DiscoveryOutcome,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    OutcomeStatus,
    ProviderCandidate,
    ProviderDescriptor,
    RecordingContext,
    Snapshot,
)
from phaze.tracklist_providers.port import ProviderEffects, SourceInterpreter
from phaze.tracklist_providers.registry import context_key


class LocalProvider:
    descriptor = ProviderDescriptor(
        id="local",
        display_name="Local companions and embedded metadata",
        capabilities=frozenset(
            {Capability.LOAD, Capability.DISCOVERY, Capability.LOCAL_SIDECAR, Capability.EMBEDDED_METADATA, Capability.REVISION_EVIDENCE}
        ),
    )

    def __init__(self, interpreter: SourceInterpreter | None = None) -> None:
        self.interpreter = interpreter

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: ProviderEffects
    ) -> DiscoveryOutcome:
        del effects
        sources = tuple(source for source in context.sources if source.identity.provider_id == self.descriptor.id)
        try:
            start = int(cursor.token) if cursor else 0
        except ValueError:
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_cursor", scope=context.recording_id)
        if start < 0 or start > len(sources):
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_cursor", scope=context.recording_id)
        selected = sources[start : start + budget.max_candidates]
        end = start + len(selected)
        complete = end == len(sources)
        return DiscoveryOutcome(
            status=OutcomeStatus.FOUND if complete else OutcomeStatus.INCOMPLETE,
            code="linked_sources_exhausted" if complete else "candidate_cap",
            scope=context.recording_id,
            candidates=tuple(
                ProviderCandidate(identity=s.identity, source=s, observed_revision=s.revision, display_name=s.display_name) for s in selected
            ),
            completeness=Completeness(state="complete" if complete else "incomplete", reason="Explicitly supplied linked-source scope"),
            cursor=None if complete else OpaqueCursor(provider_id="local", context_key=context_key(context), token=str(end)),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: ProviderEffects) -> LoadOutcome:
        if candidate.source is None:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_source_reference", scope=candidate.identity.native_id)
        content = await effects.read(candidate.source, budget)
        if content.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE} or content.text is None:
            return LoadOutcome(status=content.status, code=content.code, scope=content.scope, evidence=content.evidence)
        if content.truncated or content.status == OutcomeStatus.INCOMPLETE:
            return LoadOutcome(status=OutcomeStatus.INCOMPLETE, code="read_truncated", scope=content.scope, evidence=content.evidence)
        if candidate.observed_revision is not None and candidate.observed_revision != content.revision:
            return LoadOutcome(status=OutcomeStatus.RETRY, code="revision_changed", scope=content.scope, evidence=content.evidence)
        if self.interpreter is not None:
            return self.interpreter.interpret(candidate, content, budget)
        return LoadOutcome(
            status=OutcomeStatus.INCOMPLETE,
            code="recognition_only",
            scope=content.scope,
            snapshot=Snapshot(
                identity=candidate.identity,
                revision=content.revision,
                revision_scope=content.revision_scope,
                retrieved_at=content.retrieved_at,
                text=content.text,
                encoding=content.encoding,
                source_format=candidate.source.format,
                parser_version="recognition-only-v1",
                completeness=Completeness(state="incomplete", reason="No interpreter configured"),
                provenance=content.evidence,
            ),
        )
