"""Explicit in-repo composition and invocation policy. Listing performs no I/O."""

import asyncio
from collections.abc import Iterable
import hashlib
import json

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
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.port import ProviderEffects, TracklistProvider


SUPPORTED_VERSION = (1, 0)


def context_key(context: RecordingContext) -> str:
    return hashlib.sha256(json.dumps(context.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class ProviderContractError(ValueError):
    """Configuration errors are synchronous and occur before any effect is activated."""


class _BoundedEffects:
    def __init__(self, effects: ProviderEffects, budget: DiscoveryBudget, allowed: tuple[SourceReference, ...]) -> None:
        self.effects = effects
        self.budget = budget
        self.allowed = allowed
        self.reads = 0
        self.bytes_read = 0
        self.characters_read = 0
        self.lines_read = 0
        self.violation: str | None = None

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        if source not in self.allowed:
            self.violation = "unauthorized_read"
            raise ProviderContractError(self.violation)
        if budget != self.budget:
            self.violation = "budget_changed"
            raise ProviderContractError(self.violation)
        self.reads += 1
        if self.reads > budget.max_reads:
            self.violation = "read_cap"
            raise _BudgetExceeded(self.violation)
        remaining = {
            "max_bytes": budget.max_bytes - self.bytes_read,
            "max_characters": budget.max_characters - self.characters_read,
            "max_lines": budget.max_lines - self.lines_read,
            "max_reads": budget.max_reads - self.reads + 1,
        }
        if any(value <= 0 for value in remaining.values()):
            self.violation = "input_cap"
            raise _BudgetExceeded(self.violation)
        try:
            result = SourceRead.model_validate(await self.effects.read(source, budget.model_copy(update=remaining)))
        except Exception:
            self.violation = "invalid_read"
            raise
        self.bytes_read += result.bytes_read
        self.characters_read += len(result.text or "")
        self.lines_read += len((result.text or "").splitlines())
        if self.bytes_read > budget.max_bytes or self.characters_read > budget.max_characters or self.lines_read > budget.max_lines:
            self.violation = "input_cap"
            raise _BudgetExceeded(self.violation)
        return result


class _BudgetExceeded(ValueError):
    pass


class ProviderRegistry:
    def __init__(self, providers: Iterable[TracklistProvider], *, disabled: frozenset[str] = frozenset()) -> None:
        self._providers: dict[str, TracklistProvider] = {}
        self._disabled = disabled
        self._descriptors: dict[str, ProviderDescriptor] = {}
        for provider in providers:
            descriptor = ProviderDescriptor.model_validate(provider.descriptor)
            if descriptor.id in self._providers:
                raise ProviderContractError(f"Duplicate provider: {descriptor.id}")
            if descriptor.contract_version != SUPPORTED_VERSION:
                raise ProviderContractError(f"Unsupported contract version: {descriptor.contract_version}")
            self._providers[descriptor.id] = provider
            self._descriptors[descriptor.id] = descriptor

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._providers))

    def _rejection(self, provider_id: str) -> tuple[OutcomeStatus, str] | None:
        if provider_id not in self._providers:
            return OutcomeStatus.CONTRACT_ERROR, "unknown_provider"
        if provider_id in self._disabled:
            return OutcomeStatus.UNAVAILABLE, "disabled_provider"
        return None

    async def discover(
        self, provider_id: str, context: RecordingContext, budget: DiscoveryBudget, effects: ProviderEffects, cursor: OpaqueCursor | None = None
    ) -> DiscoveryOutcome:
        rejection = self._rejection(provider_id)
        if rejection:
            return DiscoveryOutcome(status=rejection[0], code=rejection[1], scope=context.recording_id)
        provider = self._providers[provider_id]
        if cursor and (
            cursor.provider_id != provider_id or cursor.context_key != context_key(context) or cursor.contract_version != SUPPORTED_VERSION
        ):
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="cursor_mismatch", scope=context.recording_id)
        if Capability.DISCOVERY not in self._descriptors[provider_id].capabilities:
            return DiscoveryOutcome(status=OutcomeStatus.UNSUPPORTED, code="discovery_unsupported", scope=context.recording_id)
        guarded = _BoundedEffects(effects, budget, tuple(source for source in context.sources if source.identity.provider_id == provider_id))
        try:
            async with asyncio.timeout(budget.deadline_seconds):
                result = DiscoveryOutcome.model_validate(await provider.discover(context, budget, cursor, guarded))
            if guarded.violation:
                return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code=guarded.violation, scope=context.recording_id)
            if any(candidate.source is not None and candidate.source not in guarded.allowed for candidate in result.candidates):
                raise ProviderContractError("Candidate reference was not explicitly authorized")
            if any(candidate.identity.provider_id != provider_id for candidate in result.candidates):
                raise ProviderContractError("Foreign candidate identity")
            if result.cursor and (
                result.cursor.provider_id != provider_id
                or result.cursor.context_key != context_key(context)
                or result.cursor.contract_version != SUPPORTED_VERSION
            ):
                raise ProviderContractError("Foreign cursor")
            if len(result.candidates) > budget.max_candidates:
                return DiscoveryOutcome(
                    status=OutcomeStatus.INCOMPLETE,
                    code="candidate_cap",
                    scope=context.recording_id,
                    candidates=result.candidates[: budget.max_candidates],
                    completeness=Completeness(state="incomplete", reason="candidate_cap"),
                )
            return result
        except (TimeoutError, _BudgetExceeded) as exc:
            return DiscoveryOutcome(
                status=OutcomeStatus.INCOMPLETE,
                code=str(exc) or "deadline",
                scope=context.recording_id,
                completeness=Completeness(state="incomplete", reason=str(exc) or "deadline"),
            )
        except OSError:
            return DiscoveryOutcome(status=OutcomeStatus.UNAVAILABLE, code="read_error", scope=context.recording_id)
        except Exception as exc:
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_output", scope=context.recording_id, detail=str(exc)[:4096])

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: ProviderEffects) -> LoadOutcome:
        provider_id = candidate.identity.provider_id
        rejection = self._rejection(provider_id)
        if rejection:
            return LoadOutcome(status=rejection[0], code=rejection[1], scope=candidate.identity.native_id)
        allowed = (candidate.source,) if candidate.source else ()
        guarded = _BoundedEffects(effects, budget, allowed)
        try:
            async with asyncio.timeout(budget.deadline_seconds):
                result = LoadOutcome.model_validate(await self._providers[provider_id].load(candidate, budget, guarded))
            if guarded.violation:
                return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code=guarded.violation, scope=candidate.identity.native_id)
            if result.snapshot and result.snapshot.identity != candidate.identity:
                raise ProviderContractError("Snapshot identity mismatch")
            if result.snapshot and (
                len(result.snapshot.text or "") > budget.max_characters
                or len((result.snapshot.text or "").splitlines()) > budget.max_lines
                or len((result.snapshot.text or "").encode("utf-8")) > budget.max_bytes
            ):
                return LoadOutcome(status=OutcomeStatus.INCOMPLETE, code="output_cap", scope=candidate.identity.native_id)
            if result.snapshot and len(result.snapshot.tracks) > budget.max_tracks:
                partial = result.snapshot.model_copy(
                    update={
                        "tracks": result.snapshot.tracks[: budget.max_tracks],
                        "completeness": Completeness(state="incomplete", reason="track_cap"),
                    }
                )
                return LoadOutcome(status=OutcomeStatus.INCOMPLETE, code="track_cap", scope=candidate.identity.native_id, snapshot=partial)
            if result.snapshot and candidate.observed_revision is not None and result.snapshot.revision != candidate.observed_revision:
                return LoadOutcome(
                    status=OutcomeStatus.RETRY,
                    code="revision_changed",
                    scope=candidate.identity.native_id,
                    evidence=("Discovered revision differs from loaded revision",),
                )
            return result
        except (TimeoutError, _BudgetExceeded) as exc:
            return LoadOutcome(status=OutcomeStatus.INCOMPLETE, code=str(exc) or "deadline", scope=candidate.identity.native_id)
        except OSError:
            return LoadOutcome(status=OutcomeStatus.UNAVAILABLE, code="read_error", scope=candidate.identity.native_id)
        except Exception as exc:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_output", scope=candidate.identity.native_id, detail=str(exc)[:4096])
