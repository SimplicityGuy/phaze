"""Explicit in-repo composition and invocation policy. Listing performs no I/O.

Authorization: a local read is allowed for the references the caller supplied (context sources on
discover, the candidate's own source on load). A remote read is allowed only for a reference this
registry minted for the invocation from the provider's core-held host profile, see
docs/design/0025-remote-provider-sources.md. A caller-supplied remote reference is refused.
"""

import asyncio
from collections.abc import Iterable
import hashlib
import json

from phaze.tracklist_providers.domain import (
    MAX_SEARCH_TERM_CHARACTERS,
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
    RemoteRequest,
    SearchTerm,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.host_profile import RemoteHostProfile
from phaze.tracklist_providers.port import ProviderEffects, TracklistProvider


SUPPORTED_VERSION = (1, 0)


def context_key(context: RecordingContext) -> str:
    return hashlib.sha256(json.dumps(context.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class ProviderContractError(ValueError):
    """Configuration errors are synchronous and occur before any effect is activated."""


class _BoundedEffects:
    def __init__(
        self,
        effects: ProviderEffects,
        budget: DiscoveryBudget,
        allowed: tuple[SourceReference, ...],
        minted: tuple[SourceReference, ...] = (),
        max_search_page: int = 1,
        skipped_terms: tuple[str, ...] = (),
    ) -> None:
        self.effects = effects
        self.budget = budget
        self.allowed = allowed
        self._minted = minted
        self._max_search_page = max_search_page
        self._skipped_terms = skipped_terms
        self.reads = 0
        self.bytes_read = 0
        self.characters_read = 0
        self.lines_read = 0
        self.violation: str | None = None

    @property
    def minted(self) -> tuple[SourceReference, ...]:
        return self._minted

    @property
    def skipped_terms(self) -> tuple[str, ...]:
        return self._skipped_terms

    def _authorized(self, source: SourceReference) -> bool:
        if getattr(source, "channel", None) != "remote":
            return source in self.allowed
        try:
            # A model_copy skips validation, so the page and kind invariants are re-checked here.
            reference = SourceReference.model_validate(source)
        except ValueError:
            return False
        request = reference.request
        if request is None:  # pragma: no cover - the channel validator forbids it
            return False
        first_page = reference.model_copy(update={"request": request.model_copy(update={"page": 1})})
        bound = self._max_search_page if request.kind == "search" else 1
        return first_page in self._minted and request.page <= bound

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        if not self._authorized(source):
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
    def __init__(
        self,
        providers: Iterable[TracklistProvider],
        *,
        disabled: frozenset[str] = frozenset(),
        remote_profiles: Iterable[RemoteHostProfile] = (),
    ) -> None:
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
        self._profiles: dict[str, RemoteHostProfile] = {}
        for value in remote_profiles:
            profile = RemoteHostProfile.model_validate(value)
            if profile.provider_id in self._profiles:
                raise ProviderContractError(f"Duplicate remote host profile: {profile.provider_id}")
            if profile.provider_id not in self._descriptors:
                raise ProviderContractError(f"Remote host profile for unknown provider: {profile.provider_id}")
            if Capability.REMOTE_TRANSPORT not in self._descriptors[profile.provider_id].capabilities:
                raise ProviderContractError(f"Remote host profile for a provider without remote transport: {profile.provider_id}")
            self._profiles[profile.provider_id] = profile
        for descriptor in self._descriptors.values():
            if Capability.REMOTE_TRANSPORT in descriptor.capabilities and descriptor.id not in self._profiles:
                raise ProviderContractError(f"Remote transport without a core host profile: {descriptor.id}")

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._providers))

    def _rejection(self, provider_id: str) -> tuple[OutcomeStatus, str] | None:
        if provider_id not in self._providers:
            return OutcomeStatus.CONTRACT_ERROR, "unknown_provider"
        if provider_id in self._disabled:
            return OutcomeStatus.UNAVAILABLE, "disabled_provider"
        return None

    def _mint_search(self, provider_id: str, context: RecordingContext) -> tuple[tuple[SourceReference, ...], tuple[str, ...]]:
        """Mint the one search a context allows, and name the fields skipped for exceeding the term bound."""
        profile = self._profiles.get(provider_id)
        if profile is None or profile.search is None:
            return (), ()
        facts = (("artist", context.artist), ("event", context.event), ("date", context.date))
        usable = [
            (field, fact.value)
            for field, fact in facts
            if field in profile.search.fields and fact.certainty in {"known", "inferred"} and fact.value and fact.value.strip()
        ]
        # Too long is skipped, never truncated: a truncated value is a query core never saw as a fact.
        skipped = tuple(field for field, value in usable if len(value) > MAX_SEARCH_TERM_CHARACTERS)
        terms = tuple(SearchTerm(field=field, value=value) for field, value in usable if field not in skipped)
        if not terms:
            return (), skipped
        identity = SourceIdentity(provider_id=provider_id, native_id=f"search:{context_key(context)}")
        request = RemoteRequest(kind="search", terms=terms)
        return (SourceReference(identity=identity, channel="remote", format=profile.search.format, request=request),), skipped

    async def discover(
        self, provider_id: str, context: RecordingContext, budget: DiscoveryBudget, effects: ProviderEffects, cursor: OpaqueCursor | None = None
    ) -> DiscoveryOutcome:
        rejection = self._rejection(provider_id)
        if rejection:
            return DiscoveryOutcome(status=rejection[0], code=rejection[1], scope=context.recording_id)
        if any(source.channel == "remote" for source in context.sources):
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="caller_remote_reference", scope=context.recording_id)
        provider = self._providers[provider_id]
        if cursor and (
            cursor.provider_id != provider_id or cursor.context_key != context_key(context) or cursor.contract_version != SUPPORTED_VERSION
        ):
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="cursor_mismatch", scope=context.recording_id)
        if Capability.DISCOVERY not in self._descriptors[provider_id].capabilities:
            return DiscoveryOutcome(status=OutcomeStatus.UNSUPPORTED, code="discovery_unsupported", scope=context.recording_id)
        profile = self._profiles.get(provider_id)
        minted, skipped = self._mint_search(provider_id, context)
        guarded = _BoundedEffects(
            effects,
            budget,
            tuple(source for source in context.sources if source.identity.provider_id == provider_id),
            minted,
            profile.search.max_page if profile and profile.search else 1,
            skipped,
        )
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
        if candidate.source is not None and candidate.source.channel == "remote":
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="caller_remote_reference", scope=candidate.identity.native_id)
        minted: tuple[SourceReference, ...] = ()
        profile = self._profiles.get(provider_id)
        if profile is not None and profile.fetch is not None:
            if not profile.fetch.accepts(candidate.identity.native_id):
                return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="unauthorized_identity", scope=candidate.identity.native_id)
            minted = (
                SourceReference(identity=candidate.identity, channel="remote", format=profile.fetch.format, request=RemoteRequest(kind="fetch")),
            )
        allowed = (candidate.source,) if candidate.source else ()
        guarded = _BoundedEffects(effects, budget, allowed, minted)
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
