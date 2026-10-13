"""Operator-triggered remote lookup for ANY registered remote provider: discover, hold candidates, load, import.

The same shape as ``local_source_import`` with core-owned effects (docs/design/tracklist-provider-orchestration.md):

1. **Read phase.** One short session reads the bounded ``RecordingContext``, the rejected objects and the stop
   state, then closes. Nothing is held open across the next phase.
2. **Effect phase.** ``ProviderRegistry.discover`` / ``load`` run with the process's ``RemoteTransport`` and NO
   database session open, so no write transaction, row lock or storage advisory lock is ever held across the
   network. The transport's own pacing lock lives on its own connection (``services.remote_transport``).
3. **Write phase.** One short transaction validates and persists: discovered candidates with their assessment
   evidence (``tracklist_providers.matching``, always ``review_required``), a loaded snapshot as a PENDING
   version with its review candidate, and the failed reads of the fetched object.

Selection is a separate explicit decision (:func:`decide_remote_candidate`); importing never selects, and never
touches an approved or manually selected row. A candidate this recording's operator rejected is not proposed
again for the same provider object, whatever its revision.

Nothing here branches on a provider id: every provider is reached through the same registry, transport and
persistence calls, and a failed read always reaches the operator as its own typed outcome, never as "nothing
found" (:func:`operator_outcome`).

No queue: a lookup is bounded by the registry's deadline (``DiscoveryBudget.deadline_seconds``), and the
transport answers ``paced`` rather than waiting past it, so it runs inside the operator's request. That is an
implementer's decision (bead phaze-b6430); a queue would add a polling surface and a second transport holder.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import UTC, datetime
import hashlib
import math
from typing import TYPE_CHECKING, Any, TypeVar, cast
import uuid

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import structlog

from phaze.config_remote_providers import RemoteProviderSettings, disabled_provider_ids, load_remote_provider_settings
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.models.remote_transport import RemoteTransportLedger
from phaze.schemas.local_source_import import SelectedRecordingSource
from phaze.schemas.remote_lookup import (
    LoadCommand,
    LoadResult,
    LookupCandidate,
    LookupCommand,
    LookupResult,
    LookupStatus,
    OperatorOutcome,
    PreviouslyRejected,
    ProviderStatus,
    RemoteSourceDecision,
)
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.local_source_import import get_selected_recording_source, review_text_section, selection_token
from phaze.services.provider_persistence import (
    DISCOVERED,
    add_candidate,
    lock_source_bucket,
    source_availability,
    store_discovery,
    store_snapshot,
    store_source_read,
)
from phaze.services.remote_providers import REMOTE_PROVIDERS, RemoteProviderBinding
from phaze.services.remote_transport import Clock, HttpxSocket, RemoteSocket, RemoteTransport, SystemClock, clear_stop
from phaze.tracklist_providers.domain import (
    DiscoveryBudget,
    Fact,
    OutcomeStatus,
    ProviderCandidate,
    ProviderDescriptor,
    RecordingContext,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.host_profile import RemoteHostProfile
from phaze.tracklist_providers.matching import AssessedSubject, MatchAssessment, assess_candidates, assess_recording_match
from phaze.tracklist_providers.port import ProviderEffects
from phaze.tracklist_providers.registry import ProviderRegistry


if TYPE_CHECKING:
    from pydantic import SecretStr


logger = structlog.get_logger(__name__)
_T = TypeVar("_T")

READ_PARSER_VERSION = "source-read-v1"
_IMPORTABLE = frozenset({OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE, OutcomeStatus.AMBIGUOUS})


def effective_profile(profile: RemoteHostProfile, settings: RemoteProviderSettings | None) -> RemoteHostProfile:
    """Apply operator overrides that may only make pacing MORE conservative, never less.

    The gap is ``max(profile, override)`` and the per-run budget ``min(profile, override)``: an override below
    the profile's own floor is ignored rather than honoured (operator record 1, 2026-10-11, epic phaze-oprdu: "be responsible").
    """
    updates: dict[str, object] = {}
    if settings is not None and settings.min_interval_seconds is not None:
        updates["min_gap_seconds"] = max(profile.min_gap_seconds, settings.min_interval_seconds)
    if settings is not None and settings.max_requests_per_run is not None:
        updates["per_run_budget"] = min(profile.per_run_budget, settings.max_requests_per_run)
    return RemoteHostProfile.model_validate({**profile.model_dump(), **updates})


# ---------------------------------------------------------------------------------------------------------------
# Every code a lookup can surface, mapped to what the operator can do about it. None of them means "no tracklist".
# ---------------------------------------------------------------------------------------------------------------
_MESSAGES: dict[str, tuple[LookupStatus, str]] = {
    "disabled_provider": ("unavailable", "This provider is turned off in settings, so nothing was requested."),
    "no_usable_hint": (
        "unavailable",
        "This recording has no artist, event or date to search with, so nothing was sent. That is not evidence that no tracklist exists.",
    ),
    "no_search_hint": (
        "unavailable",
        "None of this recording's facts can be searched by this provider, so nothing was sent. That is not evidence that no tracklist exists.",
    ),
    "unknown_provider": ("contract_error", "No provider with this id is registered."),
    "transport_stopped": (
        "unavailable",
        "Requests to this provider are stopped ({stop}). Nothing is sent until an operator clears the stop.",
    ),
    "run_budget_exhausted": ("unavailable", "This lookup used its whole request allowance, so the result is partial. Run it again later."),
    "durable_budget_exhausted": ("retry", "The provider's rolling request budget is spent. Try again in {retry_after}."),
    "paced": ("retry", "The next request to this provider is not allowed yet (pacing). Try again in {retry_after}."),
    "clock_discontinuity": ("unavailable", "The system clock jumped, so requests are paused for one pacing gap. Try again shortly."),
    "ledger_corrupt": ("unavailable", "The provider's request ledger is inconsistent, so nothing is sent. This needs investigation."),
    "robots_disallow": (
        "unavailable",
        "The provider's robots.txt disallows this request, so requests are stopped until an operator clears the stop.",
    ),
    "redirect_not_followed": ("unavailable", "The provider answered with a redirect, which is never followed. Nothing was read."),
    "http_304_not_requested": ("unavailable", "The provider answered 'not modified' to a request that never asked for it. Nothing was read."),
    "response_too_large": ("unavailable", "The provider's response was larger than allowed, so it was rejected unread."),
    "undecodable_response": ("unsupported", "The provider's response could not be decoded as text."),
    "challenge": ("unavailable", "The provider answered with a bot challenge, so requests are stopped until an operator clears the stop."),
    "timeout": ("retry", "The provider did not answer in time. Try again later."),
    "transport_error": ("retry", "The connection to the provider failed. Try again later."),
    "http_400": ("contract_error", "The provider rejected the request as malformed."),
    "http_401": ("unavailable", "The provider refused the credentials. Check this provider's settings."),
    "http_403": ("unavailable", "The provider refused access (403), so requests are stopped until an operator clears the stop."),
    "http_404": ("absent", "The provider reports this object does not exist. That applies to this one object only."),
    "http_408": ("retry", "The provider timed out the request. Try again later."),
    "http_429": ("unavailable", "The provider asked us to slow down (429), so requests are stopped until an operator clears the stop."),
    "unauthorized_read": ("refused", "The provider tried to read something core did not authorize, so its answer was discarded."),
    "unauthorized_identity": ("refused", "This candidate's identity is not one this provider's profile allows to be fetched."),
    "caller_remote_reference": ("refused", "A remote reference arrived from outside core, so it was refused before any request."),
    "revision_changed": (
        "changed_source",
        "The source changed between discovery and load, so nothing was imported. Run the lookup again to review the new revision.",
    ),
    "discovery_unsupported": ("unsupported", "This provider does not support search; it can only load a known object."),
    "deadline": ("retry", "The lookup ran out of time before the provider finished. Try again later."),
    "read_cap": ("retry", "The provider needed more reads than one lookup allows, so the result is partial."),
    "input_cap": ("retry", "The provider read more than one lookup allows, so the result is partial."),
    "candidate_cap": ("retry", "More candidates were found than one lookup keeps, so the list is partial."),
    "output_cap": ("unsupported", "The provider's snapshot was larger than one load allows, so nothing was imported."),
    "track_cap": ("unsupported", "The source has more tracks than one load allows, so only a partial snapshot is held for review."),
    "read_error": ("unavailable", "Reading the source failed. Try again later."),
    "budget_changed": ("refused", "The provider changed its read allowance, so its answer was discarded."),
    "invalid_read": ("refused", "A read returned malformed data, so the provider's answer was discarded."),
    "invalid_output": ("contract_error", "The provider returned malformed output, so it was discarded."),
}
_STATUS_FALLBACK: dict[OutcomeStatus, LookupStatus] = {
    OutcomeStatus.ABSENT: "absent",
    OutcomeStatus.INCOMPLETE: "retry",
    OutcomeStatus.AMBIGUOUS: "unavailable",
    OutcomeStatus.UNSUPPORTED: "unsupported",
    OutcomeStatus.UNAVAILABLE: "unavailable",
    OutcomeStatus.RETRY: "retry",
    OutcomeStatus.CONTRACT_ERROR: "contract_error",
    OutcomeStatus.FOUND: "unsupported",
}


def _evidence_value(evidence: Iterable[str], key: str) -> str | None:
    prefix = f"{key}="
    return next((item.removeprefix(prefix) for item in evidence if item.startswith(prefix)), None)


def _retry_after(evidence: Iterable[str]) -> float | None:
    raw = _evidence_value(evidence, "retry_after_seconds")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value >= 0 else None


def operator_outcome(status: OutcomeStatus, code: str, *, evidence: tuple[str, ...] = (), detail: str = "") -> OperatorOutcome:
    """Map a provider, registry or transport outcome to the operator's typed result. Never an empty "found"."""
    # The registry reports a refusal the provider let propagate as invalid_output with detail unauthorized_read,
    # and one it swallowed as unauthorized_read itself: the same refusal either way.
    if code == "invalid_output" and detail.strip() == "unauthorized_read":
        code = "unauthorized_read"
    retry_after = _retry_after(evidence)
    stop = _evidence_value(evidence, "stop")
    if code in _MESSAGES:
        kind, message = _MESSAGES[code]
    elif code.startswith("robots_"):
        kind, message = "unavailable", f"Fetching the provider's robots.txt failed ({code.removeprefix('robots_')}), so nothing else was sent."
    elif code.startswith("http_5"):
        kind, message = "retry", "The provider had a server error. Try again later."
    else:
        kind, message = _STATUS_FALLBACK[status], f"The provider answered {status.value} ({code})."
    message = message.format(stop=stop or "stop code unrecorded", retry_after=f"{retry_after:g} s" if retry_after is not None else "a while")
    return OperatorOutcome(status=kind, code=code, message=message, retry_after_seconds=retry_after, stop_code=stop)


class _ObservedEffects:
    """The per-run transport effect, recording every read so a provider cannot hide a failed one."""

    def __init__(self, effect: ProviderEffects) -> None:
        self._effect = effect
        self.reads: list[tuple[SourceReference, SourceRead]] = []

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        result = SourceRead.model_validate(await self._effect.read(source, budget))
        self.reads.append((source, result))
        return result

    def failures(self) -> tuple[OperatorOutcome, ...]:
        return tuple(operator_outcome(read.status, read.code, evidence=read.evidence) for _, read in self.reads if read.status != OutcomeStatus.FOUND)


def context_from_metadata(media_id: uuid.UUID, metadata: FileMetadata | None) -> RecordingContext:
    """The bounded facts a remote search may use: tag artist, tag year, duration. Never file content or paths.

    Per docs/spikes/phaze-fi8rs-recording-facts-for-remote-search.md: the artist tag is ``known``, a year alone is a
    year-precision ``inferred`` date, and no URL fact exists. No event is derived here: the archive stores none, and an
    album tag is not an event (operator record 3, 2026-10-11, epic phaze-oprdu, allows only artist, event, date and duration).
    """
    artist, date, duration = Fact(), Fact(), None
    if metadata is not None and metadata.failed_at is None:
        if metadata.artist and metadata.artist.strip():
            artist = Fact(value=metadata.artist.strip()[:4096], certainty="known", evidence=("tag:artist",))
        if metadata.year is not None and 1 <= metadata.year <= 9999:
            date = Fact(value=f"{metadata.year:04d}", certainty="inferred", evidence=("tag:year",))
        if metadata.duration is not None and math.isfinite(metadata.duration) and metadata.duration >= 0:
            duration = metadata.duration
    return RecordingContext(recording_id=str(media_id), artist=artist, date=date, duration_seconds=duration)


def _searchable(context: RecordingContext) -> bool:
    """Whether any fact could become a search term; without one, nothing is sent (phaze-fi8rs: say so, never guess)."""
    return any(
        fact.certainty in {"known", "inferred"} and fact.value and fact.value.strip() for fact in (context.artist, context.event, context.date)
    )


async def _recording_context(session: AsyncSession, media_id: uuid.UUID) -> RecordingContext:
    media = await session.get(FileRecord, media_id)
    if media is None or media.missing_at is not None or media.file_type not in MEDIA_FILE_TYPES:
        raise ValueError("Recording is unavailable")
    metadata = await session.scalar(select(FileMetadata).where(FileMetadata.file_id == media_id))
    return context_from_metadata(media_id, metadata)


async def _rejections(session: AsyncSession, media_id: uuid.UUID, provider_id: str) -> dict[str, datetime | None]:
    """Provider objects this recording's operator rejected, under any revision or observation, with when.

    A rejection is a candidate row in status ``rejected``; an explicit ``reopen`` decision returns it to pending.
    """
    rows = await session.execute(
        select(ProviderSourceObject.native_id, func.max(ProviderSelectionEvent.selected_at))
        .select_from(ProviderRecordingCandidate)
        .join(ProviderSourceObservation, ProviderSourceObservation.id == ProviderRecordingCandidate.observation_id)
        .join(ProviderSourceObject, ProviderSourceObject.id == ProviderSourceObservation.object_id)
        .outerjoin(
            ProviderSelectionEvent,
            and_(
                ProviderSelectionEvent.media_id == media_id,
                ProviderSelectionEvent.observation_id == ProviderSourceObservation.id,
                ProviderSelectionEvent.action == "reject",
            ),
        )
        .where(
            ProviderRecordingCandidate.media_id == media_id,
            ProviderRecordingCandidate.status == "rejected",
            ProviderSourceObject.provider_id == provider_id,
        )
        .group_by(ProviderSourceObject.native_id)
    )
    return dict(rows.tuples().all())


def _assessment_evidence(assessment: MatchAssessment) -> tuple[str, ...]:
    """The assessment as bounded candidate evidence: relations and band, never a score, and never authority."""
    date = assessment.date
    return (
        f"assessment:eligibility:{assessment.eligibility}",
        f"assessment:band:{assessment.band}",
        f"assessment:artist:{assessment.artist.relation}",
        f"assessment:event:{assessment.event.relation}",
        f"assessment:date:{date.relation}:{date.precision or 'unknown'}",
        *(f"assessment:date_alternative:{value}" for value in date.recording_alternatives[:4]),
        f"assessment:scope:{assessment.scope.state}:{assessment.scope.reason}"[:4096],
        f"assessment:tied_with:{len(assessment.tied_with)}",
        *(f"assessment:reason:{reason}"[:4096] for reason in assessment.reasons[:8]),
    )


class RemoteLookup:
    """The composition of every remote provider, built ONCE per process: one registry, one transport.

    Close it with :meth:`aclose` at shutdown: it disposes the transport's dedicated lock engine, the socket it
    owns and, when it owns it, the ledger engine.
    """

    def __init__(
        self,
        bindings: Iterable[RemoteProviderBinding],
        *,
        settings: Mapping[str, RemoteProviderSettings],
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        socket: RemoteSocket,
        clock: Clock | None = None,
        owns_engine: bool = False,
    ) -> None:
        bindings = tuple(bindings)
        profiles = tuple(effective_profile(binding.profile, settings.get(binding.profile.provider_id)) for binding in bindings)
        providers = tuple(binding.provider for binding in bindings)
        descriptors = tuple(ProviderDescriptor.model_validate(provider.descriptor) for provider in providers)
        self._descriptors = {descriptor.id: descriptor for descriptor in descriptors}
        disabled = set(disabled_provider_ids(descriptors, settings))
        credentials: dict[str, SecretStr] = {}
        for binding in bindings:
            provider_id = binding.profile.provider_id
            provider_settings = settings.get(provider_id)
            if binding.credential_name and provider_settings is not None and provider_settings.enabled:
                secret = provider_settings.credentials.get(binding.credential_name)
                if secret is None:
                    # Enabled without its credential would send unauthenticated requests: fail closed.
                    disabled.add(provider_id)
                    logger.warning("remote_lookup.credential_missing", provider_id=provider_id)
                    continue
                if binding.credential_check is not None:
                    binding.credential_check(secret.get_secret_value())
                credentials[provider_id] = secret
        self._disabled = frozenset(disabled)
        self.registry = ProviderRegistry(providers, disabled=self._disabled, remote_profiles=profiles)
        self._clock = clock or SystemClock()
        self.transport = RemoteTransport(profiles=profiles, engine=engine, socket=socket, clock=self._clock, credentials=credentials)
        self._engine = engine
        self._owns_engine = owns_engine
        self._sessions = sessions
        self._socket = socket
        self._inflight: dict[tuple[object, ...], asyncio.Future[Any]] = {}

    def __repr__(self) -> str:
        return f"RemoteLookup(providers={sorted(self._descriptors)}, disabled={sorted(self._disabled)})"

    async def aclose(self) -> None:
        await self.transport.aclose()
        if isinstance(self._socket, HttpxSocket):
            await self._socket.aclose()
        if self._owns_engine:
            await self._engine.dispose()

    def _rejection(self, provider_id: str) -> OperatorOutcome | None:
        if provider_id not in self._descriptors:
            return operator_outcome(OutcomeStatus.CONTRACT_ERROR, "unknown_provider")
        if provider_id in self._disabled:
            return operator_outcome(OutcomeStatus.UNAVAILABLE, "disabled_provider")
        return None

    @staticmethod
    async def _stopped(session: AsyncSession, provider_id: str) -> OperatorOutcome | None:
        stop_code = await session.scalar(select(RemoteTransportLedger.stop_code).where(RemoteTransportLedger.provider_id == provider_id))
        if stop_code is None:
            return None
        return operator_outcome(OutcomeStatus.UNAVAILABLE, "transport_stopped", evidence=(f"stop={stop_code}",))

    async def provider_status(self) -> tuple[ProviderStatus, ...]:
        """Every registered remote provider, enabled or not, with its persistent stop state. Reads only."""
        if not self._descriptors:
            return ()
        async with self._sessions() as session:
            ledgers = {
                row.provider_id: row
                for row in await session.scalars(select(RemoteTransportLedger).where(RemoteTransportLedger.provider_id.in_(self._descriptors)))
            }
        statuses = []
        for provider_id, descriptor in sorted(self._descriptors.items()):
            ledger = ledgers.get(provider_id)
            statuses.append(
                ProviderStatus(
                    provider_id=provider_id,
                    display_name=descriptor.display_name,
                    enabled=provider_id not in self._disabled,
                    stopped=ledger is not None and ledger.stop_code is not None,
                    stop_code=ledger.stop_code if ledger else None,
                    stopped_at=ledger.stopped_at if ledger else None,
                    cleared_at=ledger.cleared_at if ledger else None,
                    cleared_by=ledger.cleared_by if ledger else None,
                    clear_reason=ledger.clear_reason if ledger else None,
                )
            )
        return tuple(statuses)

    async def clear_stop(self, provider_id: str, *, operator: str, reason: str) -> bool:
        """The explicit operator action that lifts a persistent stop, recording who and why."""
        if provider_id not in self._descriptors:
            raise ValueError("No remote provider with this id is registered")
        cleared = await clear_stop(self._engine, provider_id, operator=operator, reason=reason, clock=self._clock)
        logger.warning("remote_lookup.stop_cleared", provider_id=provider_id, operator=operator, cleared=cleared)
        return cleared

    async def _single_flight(self, key: tuple[object, ...], work: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """One in-flight run per key in this process: a concurrent duplicate awaits the first instead of spending
        budget again. The run is shielded, so a caller that disconnects does not abandon a charged request."""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(work())
            self._inflight[key] = task
            task.add_done_callback(lambda _done: self._inflight.pop(key, None))
        return cast("_T", await asyncio.shield(task))

    async def lookup(self, command: LookupCommand) -> LookupResult:
        """Discover candidates for one recording from one provider, and hold them for review.

        Runs in the caller's request, bounded by the registry deadline. A task wrapper (SAQ) can call this same
        method later without any change here.
        """
        command = LookupCommand.model_validate(command)
        return await self._single_flight(("lookup", command.media_id, command.provider_id), lambda: self._lookup(command))

    async def _lookup(self, command: LookupCommand) -> LookupResult:
        media_id, provider_id = command.media_id, command.provider_id
        if (refused := self._rejection(provider_id)) is not None:
            return LookupResult(media_id=media_id, provider_id=provider_id, outcome=refused)
        async with self._sessions() as session:
            context = await _recording_context(session, media_id)
            stopped = await self._stopped(session, provider_id)
        if stopped is not None:
            return LookupResult(media_id=media_id, provider_id=provider_id, outcome=stopped)
        if not _searchable(context):
            return LookupResult(media_id=media_id, provider_id=provider_id, outcome=operator_outcome(OutcomeStatus.UNAVAILABLE, "no_usable_hint"))

        effects = _ObservedEffects(self.transport.run())
        outcome = await self.registry.discover(provider_id, context, command.budget, effects)
        failures = effects.failures()
        retrieved_at = self._clock.wall()

        persisted: list[LookupCandidate] = []
        previously_rejected: list[PreviouslyRejected] = []
        if outcome.candidates:
            assessed = assess_candidates(
                context, tuple(AssessedSubject(source=candidate) for candidate in outcome.candidates), scope=outcome.completeness
            )
            assessments = {(item.identity.provider_id, item.identity.native_id): item for item in assessed.ranked}
            async with self._sessions() as session, session.begin():
                rejected = await _rejections(session, media_id, provider_id)
                for candidate in outcome.candidates:
                    if candidate.identity.native_id in rejected:
                        # Not proposed again, and never silently dropped: the operator is told which, and when.
                        previously_rejected.append(
                            PreviouslyRejected(identity=candidate.identity, rejected_at=rejected[candidate.identity.native_id])
                        )
                        continue
                    stored = await store_discovery(session, candidate, retrieved_at=retrieved_at)
                    evidence = (
                        f"discovery:{outcome.status.value}:{outcome.code}"[:4096],
                        *_assessment_evidence(assessments[(candidate.identity.provider_id, candidate.identity.native_id)]),
                        *candidate.evidence[:32],
                    )
                    row = await add_candidate(session, media_id, stored.observation.id, evidence=evidence[:64])
                    persisted.append(
                        LookupCandidate(
                            candidate_id=row.id,
                            observation_id=stored.observation.id,
                            identity=candidate.identity,
                            display_name=candidate.display_name,
                            url=candidate.url,
                            evidence=tuple(row.evidence),
                        )
                    )
        result_outcome, warnings = self._discovery_outcome(
            outcome.status, outcome.code, outcome.detail, outcome.evidence, persisted, failures, len(previously_rejected)
        )
        logger.info(
            "remote_lookup.discovered", provider_id=provider_id, media_id=str(media_id), status=result_outcome.status, code=result_outcome.code
        )
        return LookupResult(
            media_id=media_id,
            provider_id=provider_id,
            outcome=result_outcome,
            candidates=tuple(persisted),
            scope=outcome.completeness,
            warnings=warnings,
            previously_rejected=tuple(previously_rejected),
        )

    @staticmethod
    def _discovery_outcome(
        status: OutcomeStatus,
        code: str,
        detail: str,
        evidence: tuple[str, ...],
        persisted: list[LookupCandidate],
        failures: tuple[OperatorOutcome, ...],
        suppressed: int,
    ) -> tuple[OperatorOutcome, tuple[OperatorOutcome, ...]]:
        if persisted:
            message = f"{len(persisted)} candidate(s) held for review. Loading one is a separate step, and nothing is selected automatically."
            return OperatorOutcome(status="candidates", code=code, message=message), failures
        if failures:
            # A failed read is never reported as an empty search, whatever the provider made of it.
            return failures[0], failures[1:]
        if status in {OutcomeStatus.FOUND, OutcomeStatus.ABSENT, OutcomeStatus.INCOMPLETE}:
            message = "No candidate was found in the searched scope. That is not evidence that no tracklist exists."
            if status == OutcomeStatus.INCOMPLETE:
                message = "The search was incomplete and held no candidate. That is not evidence that no tracklist exists."
            if suppressed:
                message += f" {suppressed} candidate(s) you rejected for this recording were not proposed again; reopen one to review it."
            return OperatorOutcome(status="no_candidates_in_scope", code=code, message=message), ()
        return operator_outcome(status, code, evidence=evidence, detail=detail), ()

    async def load(self, command: LoadCommand) -> LoadResult:
        """Load the discovered candidate named in the command, and import it as a PENDING version. Never selects."""
        command = LoadCommand.model_validate(command)
        return await self._single_flight(("load", command.candidate_id), lambda: self._load(command))

    async def _load(self, command: LoadCommand) -> LoadResult:
        async with self._sessions() as session:
            row = await session.get(ProviderRecordingCandidate, command.candidate_id)
            observation = await session.get(ProviderSourceObservation, row.observation_id) if row else None
            source = await session.get(ProviderSourceObject, observation.object_id) if observation else None
            if row is None or observation is None or source is None or source.channel != "remote" or observation.status != DISCOVERED:
                raise ValueError("Only a discovered remote candidate can be loaded")
            media_id, provider_id = row.media_id, source.provider_id
            if source.native_id in await _rejections(session, media_id, provider_id):
                raise ValueError("This candidate was rejected for this recording, so it is not loaded again")
            candidate = ProviderCandidate.model_validate(observation.payload["discovered"])
            context = await _recording_context(session, media_id)
            stopped = None if self._rejection(provider_id) else await self._stopped(session, provider_id)
            discovery_id = observation.id
        if (refused := self._rejection(provider_id) or stopped) is not None:
            return LoadResult(media_id=media_id, candidate_id=command.candidate_id, outcome=refused)

        effects = _ObservedEffects(self.transport.run())
        outcome = await self.registry.load(candidate, command.budget, effects)
        failures = effects.failures()

        stored_id: uuid.UUID | None = None
        review_id: uuid.UUID | None = None
        evidence: tuple[str, ...] = ()
        async with self._sessions() as session, session.begin():
            if source.native_id in await _rejections(session, media_id, provider_id):
                raise ValueError("This candidate was rejected for this recording while it loaded, so nothing was imported")
            for reference, read in effects.reads:
                # Failed reads of THIS object are evidence about it (a 404 makes stored copies "absent").
                if read.status != OutcomeStatus.FOUND and reference.identity == candidate.identity:
                    await store_source_read(session, candidate.identity, read, parser_version=READ_PARSER_VERSION, channel="remote")
            snapshot = outcome.snapshot
            if snapshot is not None and outcome.status in _IMPORTABLE:
                stored = await store_snapshot(session, snapshot, status=outcome.status.value, channel="remote", parent_id=discovery_id)
                evidence = (
                    "kind:tracklist",
                    f"discovered:{discovery_id}",
                    *_assessment_evidence(assess_recording_match(context, AssessedSubject(source=snapshot), scope=snapshot.completeness)),
                )
                review = await add_candidate(session, media_id, stored.observation.id, evidence=evidence)
                stored_id, review_id = stored.observation.id, review.id
        result_outcome = self._load_outcome(outcome.status, outcome.code, outcome.detail, outcome.evidence, stored_id is not None, failures)
        logger.info("remote_lookup.loaded", provider_id=provider_id, media_id=str(media_id), status=result_outcome.status, code=result_outcome.code)
        return LoadResult(
            media_id=media_id,
            candidate_id=command.candidate_id,
            outcome=result_outcome,
            observation_id=stored_id,
            review_candidate_id=review_id,
            evidence=evidence,
        )

    @staticmethod
    def _load_outcome(
        status: OutcomeStatus, code: str, detail: str, evidence: tuple[str, ...], imported: bool, failures: tuple[OperatorOutcome, ...]
    ) -> OperatorOutcome:
        if imported and status == OutcomeStatus.FOUND:
            return OperatorOutcome(
                status="loaded", code=code, message="Imported as a pending version for review. Nothing is selected until you choose it."
            )
        if imported:
            message = "A partial snapshot is held for review as evidence. It cannot be selected as a complete tracklist."
            return OperatorOutcome(status="loaded", code=code, message=message)
        if code == "revision_changed":
            return operator_outcome(status, code, evidence=evidence, detail=detail)
        if failures:
            return failures[0]
        return operator_outcome(status, code, evidence=evidence, detail=detail)


def build_remote_lookup(
    database_url: str, sessions: async_sessionmaker[AsyncSession], bindings: Iterable[RemoteProviderBinding] = REMOTE_PROVIDERS
) -> RemoteLookup:
    """The composition root: settings from the environment, the production socket, and a ledger engine of its own
    (unpooled, so a transport connection never comes out of the request-serving pool). Builds nothing remote."""
    bindings = tuple(bindings)
    settings = load_remote_provider_settings(binding.spec for binding in bindings)
    engine = create_async_engine(database_url, poolclass=NullPool)
    return RemoteLookup(bindings, settings=settings, engine=engine, sessions=sessions, socket=HttpxSocket(), owns_engine=True)


async def decide_remote_candidate(session: AsyncSession, command: RemoteSourceDecision, *, actor: str) -> SelectedRecordingSource | None:
    """Select, reject or reopen a remote candidate: compare-and-set under locks, like ``decide_local_source``.

    Uses the same selection and event tables, so every consumer of a selected source reads it the same way.
    Selection requires a complete parsed snapshot that is current for this recording. A rejection holds for the
    provider object under any revision, so a later lookup does not propose it again; only an explicit ``reopen``
    (with a reason, recorded on the event) lifts it, because a provider that reports no revision at discovery
    would otherwise keep the object suppressed forever.
    """
    command = RemoteSourceDecision.model_validate(command)
    if not actor or len(actor) > 128 or "\0" in actor:
        raise ValueError("Decision requires a bounded actor")
    decision_lock = int.from_bytes(hashlib.sha256(f"remote-decision:{command.decision_id}".encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": decision_lock})
    observation = await session.get(ProviderSourceObservation, command.observation_id)
    source = await session.get(ProviderSourceObject, observation.object_id) if observation else None
    if observation is None or source is None or source.channel != "remote":
        raise ValueError("Decision requires a retained remote source")
    await lock_source_bucket(session, SourceIdentity(provider_id=source.provider_id, native_id=source.native_id))
    media = await session.scalar(
        select(FileRecord).where(FileRecord.id == command.media_id).with_for_update().execution_options(populate_existing=True)
    )
    if media is None or media.missing_at is not None or media.file_type not in MEDIA_FILE_TYPES:
        raise ValueError("Recording is unavailable")
    if media.sha256_hash != command.expected_media_sha256:
        raise ValueError("Recording changed before decision")
    candidate = await session.scalar(
        select(ProviderRecordingCandidate)
        .where(ProviderRecordingCandidate.media_id == media.id, ProviderRecordingCandidate.observation_id == observation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise ValueError("Observation is not a candidate for this recording")
    if observation.revision != command.expected_revision:
        raise ValueError("Source revision changed before decision")
    current = await session.scalar(
        select(ProviderRecordingSelection)
        .where(ProviderRecordingSelection.media_id == media.id, ProviderRecordingSelection.kind == "tracklist")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payload = {**command.model_dump(mode="json"), "actor": actor}
    previous = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.decision_id == command.decision_id))
    if previous is not None:
        if previous.decision_payload != payload:
            raise ValueError("Decision UUID was reused with different intent")
        replayed_select = command.action == "select" and current is not None and current.selection_token == command.decision_id
        replayed_reject = command.action == "reject" and candidate.status == "rejected"
        replayed_reopen = command.action == "reopen" and candidate.status != "rejected"
        if replayed_select or replayed_reject or replayed_reopen:
            return await get_selected_recording_source(session, media.id)
        raise ValueError("Decision replay is no longer current")
    if (selection_token(current) if current else None) != command.expected_selection_token:
        raise ValueError("Selected source changed; refresh before reviewing")
    mapping: dict[str, str | int] | None = None
    if command.action == "select":
        if source.native_id in await _rejections(session, media.id, source.provider_id):
            raise ValueError("A rejected candidate cannot be selected; reopen it first")
        snapshot = (
            Snapshot.model_validate({**observation.payload, "retrieved_at": observation.retrieved_at, "url": observation.source_url})
            if observation.status == OutcomeStatus.FOUND.value and "tracks" in observation.payload
            else None
        )
        if snapshot is None or snapshot.completeness.state != "complete" or not snapshot.tracks:
            raise ValueError("Only a complete loaded snapshot with tracks can be selected")
        text_ordinal = review_text_section(snapshot, command.text_section_ordinal, command.accept_text_warnings)
        if observation.conflict_with_id is not None and not command.accept_conflict:
            raise ValueError("Conflicting source requires explicit acknowledgement")
        if await source_availability(session, observation, media.id) != "current":
            raise ValueError("Source is not currently available for this recording")
        mapping = {"media_id": str(media.id), "media_sha256": media.sha256_hash, "source_revision": observation.revision or "unknown"}
        if text_ordinal is not None:
            mapping["text_section_ordinal"] = text_ordinal
        if current is None:
            current = ProviderRecordingSelection(
                media_id=media.id, kind="tracklist", observation_id=observation.id, actor=actor, selected_at=datetime.now(UTC)
            )
            session.add(current)
        current.observation_id = observation.id
        current.actor = actor
        current.selected_at = datetime.now(UTC)
        current.selection_token = command.decision_id
        current.target_mapping = mapping
        current.tracklist_id = None
        current.version_id = None
        candidate.status = "accepted"
    elif command.action == "reject":
        if current is not None and current.observation_id == observation.id:
            raise ValueError("Rejecting an active selected source requires choosing a replacement")
        candidate.status = "rejected"
    else:
        if candidate.status != "rejected":
            raise ValueError("Only a rejected candidate can be reopened")
        # The rejection covers the provider object, so reopening returns every rejected row of it to review.
        await session.execute(
            update(ProviderRecordingCandidate)
            .where(
                ProviderRecordingCandidate.media_id == media.id,
                ProviderRecordingCandidate.status == "rejected",
                ProviderRecordingCandidate.observation_id.in_(
                    select(ProviderSourceObservation.id).where(ProviderSourceObservation.object_id == source.id)
                ),
            )
            .values(status="pending")
            .execution_options(synchronize_session=False)
        )
        candidate.status = "pending"
    session.add(
        ProviderSelectionEvent(
            media_id=media.id,
            kind="tracklist",
            observation_id=observation.id,
            actor=actor,
            decision_id=command.decision_id,
            action=command.action,
            target_mapping=mapping,
            decision_payload=payload,
        )
    )
    await session.flush()
    return await get_selected_recording_source(session, media.id)
