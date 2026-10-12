"""The core remote transport effect: one paced, budgeted, stoppable GET per authorized remote read.

Providers do no I/O. This effect satisfies ``ProviderEffects.read`` for a remote ``SourceReference`` the
registry minted, and builds the request only from the provider's core-held ``RemoteHostProfile`` (its
``https`` host and route) plus the reference's percent-encoded native ID (one path segment) or encoded
search terms and page. A reference has no field for a URL, host, path, method or header, and this effect
re-checks its shape against the profile before any socket opens (docs/design/0025-remote-provider-sources.md).

Durable state lives in Postgres (``phaze.models.remote_transport``), never Valkey, and generalizes the
investigation runner of docs/spikes/phaze-6ehg6-mixesdb-rest-access.md:

- **Serial.** A per-provider session advisory lock, keyed by the provider ID, is held across reservation
  and send on a dedicated unpooled connection, so two workers, in one process or many, never have
  requests to one host in flight at once, and two providers never wait on each other. No transaction is
  open while a request is in flight.
- **Reserved before send.** The attempt row and counter are committed before the request leaves, so an
  interrupted send still consumes budget. Every request is charged, retries included: this effect never
  retries by itself, and a retry by core is a new, charged reservation.
- **Paced.** Consecutive reservations are at least ``min_gap_seconds`` apart (wider if robots asks), and
  none is made before a Retry-After the host sent.
- **Budgeted.** At most ``per_run_budget`` requests per run and ``durable_budget`` in any rolling
  ``durable_window_seconds``, counted from the attempt log across restarts and workers.
- **Fails closed on corrupt state.** A ledger whose counter or last reservation disagrees with the attempt
  log is refused, never reset.
- **Fails closed on a forward clock jump.** The wall clock is checked against the monotonic clock at every
  observation; a forward jump beyond the profile tolerance refuses the read and restarts the quarantine
  below. Each transport also waits one full gap of monotonic time before its first send, so a jump that
  happened while no worker was alive cannot shorten a gap either. This closes the limitation the
  investigation runner documented.
- **Stops persistently** on 403, 429, a genuine challenge interstitial or a robots disallow, until an
  operator calls :func:`clear_stop`. A readable page that merely embeds a challenge-platform script is not
  a challenge.

Status mapping: 200 found; 400 contract_error; 401 and 403 unavailable; 404 absent, for that object only;
408, 429, 5xx and timeouts retry, with any Retry-After honoured; a body over the limit is rejected after
reading at most one chunk past it. No redirect is followed, so none can leave the profile host.

Conditional requests are not sent. That is an implementer's decision, recorded in the 2026-10-12
amendment to docs/design/0025-remote-provider-sources.md section 5. A 304 that arrives anyway is charged
and maps to a typed non-found outcome.

The credential is a ``SecretStr`` from settings. It is unwrapped only inside :class:`HttpxSocket` when
the request is handed to httpx. It never appears in a log, repr, error or ``SourceRead``.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
import hashlib
import time
from typing import Literal, Protocol
from urllib.parse import quote, urlencode
from urllib.robotparser import RobotFileParser
import uuid

import httpx
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
import structlog

from phaze.models.remote_transport import RemoteTransportAttempt, RemoteTransportLedger
from phaze.tracklist_providers.domain import DiscoveryBudget, OutcomeStatus, SourceRead, SourceReference, SourceValidators
from phaze.tracklist_providers.host_profile import NATIVE_ID_PLACEHOLDER, RemoteFetchRoute, RemoteHostProfile, RemoteSearchRoute


logger = structlog.get_logger(__name__)

ROBOTS_TTL_SECONDS = 86400.0
MAX_RETRY_AFTER_SECONDS = 3600.0
CHUNK_BYTES = 65536
_FIELD_ORDER = ("artist", "event", "date")

RequestKind = Literal["search", "fetch", "robots"]


class Clock(Protocol):
    """Wall time stamps the ledger; monotonic time detects wall-clock jumps."""

    def wall(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def wall(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class OutboundRequest:
    """One GET, already built from the profile. The credential stays a SecretStr until the socket."""

    url: str
    host: str
    headers: tuple[tuple[str, str], ...]
    timeout_seconds: float
    credential: tuple[str, SecretStr] | None = None


class InboundResponse(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def chunks(self) -> AsyncIterator[bytes]: ...


class RemoteSocket(Protocol):
    """The only layer that touches the network. Raises TimeoutError on a timeout, ConnectionError otherwise."""

    def open(self, request: OutboundRequest) -> AbstractAsyncContextManager[InboundResponse]: ...


class _HttpxResponse:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return {key.lower(): value for key, value in self._response.headers.items()}

    def chunks(self) -> AsyncIterator[bytes]:
        return self._response.aiter_raw(CHUNK_BYTES)


class HttpxSocket:
    """The production socket: one streamed GET, no redirects followed, no proxy or environment settings."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)

    def __repr__(self) -> str:
        return "HttpxSocket()"

    @asynccontextmanager
    async def open(self, request: OutboundRequest) -> AsyncIterator[InboundResponse]:
        headers = dict(request.headers)
        if request.credential is not None:
            headers[request.credential[0]] = request.credential[1].get_secret_value()
        try:
            async with self._client.stream("GET", request.url, headers=headers, timeout=request.timeout_seconds, follow_redirects=False) as response:
                if response.url.scheme != "https" or response.url.host != request.host:
                    raise ConnectionError("response_host_mismatch")
                yield _HttpxResponse(response)
        # Only the class name survives: an httpx message or cause is never forwarded.
        except httpx.TimeoutException as exc:
            raise TimeoutError(type(exc).__name__) from None
        except httpx.HTTPError as exc:
            raise ConnectionError(type(exc).__name__) from None

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class _Plan:
    kind: RequestKind
    path: str
    query: tuple[tuple[str, str], ...]
    route: RemoteSearchRoute | RemoteFetchRoute | None

    def url(self, host: str) -> str:
        suffix = "?" + urlencode(self.query, quote_via=quote, safe="") if self.query else ""
        return f"https://{host}{self.path}{suffix}"


@dataclass(frozen=True)
class _Exchange:
    read: SourceRead
    http_status: int | None
    not_before: datetime | None = None


@dataclass
class _Robots:
    fetched_at: float
    parser: RobotFileParser | None


@dataclass(frozen=True)
class _Ledger:
    ordinal: int
    last_reserved_at: datetime | None
    not_before: datetime | None
    stop_code: str | None


class _CorruptLedger(Exception):
    pass


def _lock_key(provider_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"phaze.remote_transport:{provider_id}".encode()).digest()[:8], "big", signed=True)


def _seconds(delta: timedelta) -> float:
    return delta.total_seconds()


def _retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER_SECONDS)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return min(max(_seconds(moment - now), 0.0), MAX_RETRY_AFTER_SECONDS)


def _content_type(headers: Mapping[str, str]) -> tuple[str, str | None]:
    message = Message()
    message["content-type"] = headers.get("content-type", "")
    charset = message.get_param("charset")
    return message.get_content_type(), charset if isinstance(charset, str) else None


def _http_outcome(status: int) -> tuple[OutcomeStatus, str]:
    code = f"http_{status}"
    if status == 200:
        return OutcomeStatus.FOUND, code
    if status == 304:
        return OutcomeStatus.UNAVAILABLE, "http_304_not_requested"
    if 200 <= status < 300:
        return OutcomeStatus.UNSUPPORTED, code
    if 300 <= status < 400:
        return OutcomeStatus.UNAVAILABLE, "redirect_not_followed"
    if status == 400:
        return OutcomeStatus.CONTRACT_ERROR, code
    if status == 404:
        return OutcomeStatus.ABSENT, code
    if status in {408, 429} or 500 <= status < 600:
        return OutcomeStatus.RETRY, code
    if 400 <= status < 500:
        return OutcomeStatus.UNAVAILABLE, code
    return OutcomeStatus.UNSUPPORTED, code


class RemoteTransportRun:
    """The effect one operator-triggered run hands the registry. Holds the per-run budget."""

    def __init__(self, transport: "RemoteTransport") -> None:
        self._transport = transport
        self.run_id = uuid.uuid4()
        self.spent: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"RemoteTransportRun(run_id={self.run_id}, spent={self.spent})"

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        return await self._transport.read(self, source, budget)


class RemoteTransport:
    """Process-level transport: profiles, the ledger engine, the socket, the clock and its anchor."""

    def __init__(
        self,
        *,
        profiles: Iterable[RemoteHostProfile],
        engine: AsyncEngine,
        socket: RemoteSocket,
        clock: Clock | None = None,
        credentials: Mapping[str, SecretStr] | None = None,
    ) -> None:
        self._profiles: dict[str, RemoteHostProfile] = {}
        for value in profiles:
            profile = RemoteHostProfile.model_validate(value)
            if profile.provider_id in self._profiles:
                raise ValueError(f"Duplicate remote host profile: {profile.provider_id}")
            self._profiles[profile.provider_id] = profile
        self._credentials = dict(credentials or {})
        for provider_id in self._credentials:
            profile_or_none = self._profiles.get(provider_id)
            if profile_or_none is None or profile_or_none.credential is None:
                raise ValueError(f"A credential was supplied for a provider without a credential slot: {provider_id}")
        # The pacing lock is held across a send of up to the profile timeout, so it lives on a DEDICATED,
        # unpooled connection: never one borrowed from the app's request pool, and closing it on exit is what
        # releases the lock even if the unlock itself never runs.
        self._engine = create_async_engine(engine.url, poolclass=NullPool)
        self._socket = socket
        self._clock = clock or SystemClock()
        now, mono = self._clock.wall(), self._clock.monotonic()
        self._observed = (now, mono)
        # No send before one full gap of this process's own monotonic time: covers a jump while no worker ran.
        self._quarantine_from = mono
        self._robots: dict[str, _Robots] = {}

    def __repr__(self) -> str:
        return f"RemoteTransport(providers={sorted(self._profiles)})"

    def run(self) -> RemoteTransportRun:
        return RemoteTransportRun(self)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def read(self, run: RemoteTransportRun, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        try:
            reference = SourceReference.model_validate(source)
        except ValidationError:
            return self._result(OutcomeStatus.CONTRACT_ERROR, "invalid_reference", "unknown")
        scope = reference.identity.native_id
        plan = self._plan(reference)
        if isinstance(plan, str):
            logger.warning("remote_transport.refused", provider_id=reference.identity.provider_id, code=plan)
            return self._result(OutcomeStatus.CONTRACT_ERROR, plan, scope)
        profile = self._profiles[reference.identity.provider_id]
        gap = profile.min_gap_seconds
        if profile.robots == "respect":
            robots = await self._robots_for(run, profile, budget)
            if isinstance(robots, SourceRead):
                return robots.model_copy(update={"scope": scope})
            if robots.parser is not None:
                if not robots.parser.can_fetch(profile.user_agent, plan.url(profile.host)):
                    await self._stop(profile.provider_id, "robots_disallow")
                    return self._result(OutcomeStatus.UNAVAILABLE, "robots_disallow", scope)
                delay = robots.parser.crawl_delay(profile.user_agent)
                if delay is not None:
                    gap = max(gap, min(float(delay), MAX_RETRY_AFTER_SECONDS))
        return (await self._exchange(run, profile, plan, scope, budget, gap)).read

    def _plan(self, reference: SourceReference) -> _Plan | str:
        """Build the request from the profile alone, or name why the reference is not allowed."""
        profile = self._profiles.get(reference.identity.provider_id)
        request = reference.request
        if reference.channel != "remote" or request is None:
            return "not_remote"
        if profile is None:
            return "unknown_host_profile"
        if request.kind == "fetch":
            fetch = profile.fetch
            native_id = reference.identity.native_id
            if fetch is None:
                return "route_not_allowed"
            if reference.format != fetch.format:
                return "format_mismatch"
            # A dot segment would be resolved away by URL normalization, so it can never name an object.
            if not fetch.accepts(native_id) or native_id in {".", ".."}:
                return "unauthorized_identity"
            path = fetch.path_template.replace(NATIVE_ID_PLACEHOLDER, quote(native_id, safe=""))
            return _Plan("fetch", path, tuple(fetch.fixed_parameters), fetch)
        search = profile.search
        if search is None:
            return "route_not_allowed"
        if reference.format != search.format:
            return "format_mismatch"
        if any(term.field not in search.fields for term in request.terms) or request.page > search.max_page:
            return "search_not_allowed"
        names = dict(search.term_parameters)
        grouped: dict[str, list[str]] = {}
        for term in sorted(request.terms, key=lambda term: _FIELD_ORDER.index(term.field)):
            grouped.setdefault(names[term.field], []).append(term.value)
        query = [*search.fixed_parameters, *((name, " ".join(parts)) for name, parts in grouped.items())]
        if search.page_parameter is not None:
            query.append((search.page_parameter, str(request.page)))
        return _Plan("search", search.path, tuple(query), search)

    async def _robots_for(self, run: RemoteTransportRun, profile: RemoteHostProfile, budget: DiscoveryBudget) -> _Robots | SourceRead:
        cached = self._robots.get(profile.provider_id)
        if cached is not None and self._clock.monotonic() - cached.fetched_at < ROBOTS_TTL_SECONDS:
            return cached
        exchange = await self._exchange(run, profile, _Plan("robots", "/robots.txt", (), None), "robots.txt", budget, profile.min_gap_seconds)
        read, status = exchange.read, exchange.http_status
        if read.status == OutcomeStatus.FOUND:
            parser = RobotFileParser()
            parser.parse((read.text or "").splitlines())
            robots = _Robots(self._clock.monotonic(), parser)
        elif status is not None and 400 <= status < 500 and status not in profile.stop_statuses:
            # RFC 9309: an unavailable robots.txt (4xx) places no restriction.
            robots = _Robots(self._clock.monotonic(), None)
        else:
            # Name robots only where a robots request was actually made; a refusal keeps its own code.
            sent = status is not None or read.code in {"timeout", "transport_error"}
            return read.model_copy(update={"code": f"robots_{read.code}" if sent else read.code})
        self._robots[profile.provider_id] = robots
        return robots

    def _clock_ok(self, profile: RemoteHostProfile) -> bool:
        now, mono = self._clock.wall(), self._clock.monotonic()
        last_wall, last_mono = self._observed
        self._observed = (now, mono)
        drift = _seconds(now - last_wall) - (mono - last_mono)
        if drift > profile.clock_tolerance_seconds:
            self._quarantine_from = mono
            logger.error("remote_transport.clock_discontinuity", provider_id=profile.provider_id, drift_seconds=round(drift, 3))
            return False
        return True

    def _result(self, status: OutcomeStatus, code: str, scope: str, *evidence: str, **values: object) -> SourceRead:
        return SourceRead.model_validate(
            {"status": status, "code": code, "scope": scope, "evidence": evidence, "retrieved_at": self._clock.wall(), **values}
        )

    async def _exchange(
        self, run: RemoteTransportRun, profile: RemoteHostProfile, plan: _Plan, scope: str, budget: DiscoveryBudget, gap: float
    ) -> _Exchange:
        provider_id = profile.provider_id
        if run.spent.get(provider_id, 0) >= profile.per_run_budget:
            return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "run_budget_exhausted", scope), None)
        key = _lock_key(provider_id)
        async with self._engine.connect() as conn:
            await conn.execute(select(func.pg_advisory_lock(key)))
            await conn.commit()
            try:
                return await self._locked_exchange(conn, run, profile, plan, scope, budget, gap)
            finally:
                try:
                    await conn.rollback()
                    await conn.execute(select(func.pg_advisory_unlock(key)))
                    await conn.commit()
                except BaseException:
                    # Closing the session is what releases a lock this connection may still hold.
                    await conn.invalidate()
                    raise

    async def _locked_exchange(
        self, conn: AsyncConnection, run: RemoteTransportRun, profile: RemoteHostProfile, plan: _Plan, scope: str, budget: DiscoveryBudget, gap: float
    ) -> _Exchange:
        provider_id = profile.provider_id
        if not self._clock_ok(profile):
            return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "clock_discontinuity", scope), None)
        try:
            ledger = await self._load(conn, provider_id)
        except _CorruptLedger as exc:
            return _Exchange(self._corrupt(provider_id, scope, str(exc)), None)
        await conn.commit()
        if ledger.stop_code is not None:
            return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "transport_stopped", scope, f"stop={ledger.stop_code}"), None)
        now, mono = self._clock.wall(), self._clock.monotonic()
        waits = [0.0, self._quarantine_from + gap - mono]
        if ledger.last_reserved_at is not None:
            waits.append(_seconds(ledger.last_reserved_at - now) + gap)
        if ledger.not_before is not None:
            waits.append(_seconds(ledger.not_before - now))
        delay = max(waits)
        if delay > budget.deadline_seconds:
            return _Exchange(self._result(OutcomeStatus.RETRY, "paced", scope, f"retry_after_seconds={delay:g}"), None)
        if delay > 0:
            await self._clock.sleep(delay)
            if not self._clock_ok(profile):
                return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "clock_discontinuity", scope), None)
            try:
                ledger = await self._load(conn, provider_id)
            except _CorruptLedger as exc:
                return _Exchange(self._corrupt(provider_id, scope, str(exc)), None)
            if ledger.stop_code is not None:
                return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "transport_stopped", scope, f"stop={ledger.stop_code}"), None)
        now = self._clock.wall()
        window_start = now - timedelta(seconds=profile.durable_window_seconds)
        in_window = (
            await conn.execute(
                select(func.count(), func.min(RemoteTransportAttempt.reserved_at)).where(
                    RemoteTransportAttempt.provider_id == provider_id, RemoteTransportAttempt.reserved_at > window_start
                )
            )
        ).one()
        if in_window[0] >= profile.durable_budget:
            await conn.commit()
            # The oldest reservation still in the window frees the next slot when it leaves it.
            frees = _seconds(in_window[1] - window_start)
            return _Exchange(self._result(OutcomeStatus.RETRY, "durable_budget_exhausted", scope, f"retry_after_seconds={frees:g}"), None)
        if run.spent.get(provider_id, 0) >= profile.per_run_budget:
            await conn.commit()
            return _Exchange(self._result(OutcomeStatus.UNAVAILABLE, "run_budget_exhausted", scope), None)
        ordinal = ledger.ordinal + 1
        await conn.execute(
            insert(RemoteTransportAttempt).values(
                provider_id=provider_id, ordinal=ordinal, reserved_at=now, request_kind=plan.kind, run_id=run.run_id
            )
        )
        await conn.execute(
            update(RemoteTransportLedger).where(RemoteTransportLedger.provider_id == provider_id).values(ordinal=ordinal, last_reserved_at=now)
        )
        run.spent[provider_id] = run.spent.get(provider_id, 0) + 1
        # The reservation is durable before a byte is sent: an interrupted send still consumes budget.
        await conn.commit()
        exchange: _Exchange | None = None
        stop: str | None = None
        try:
            exchange, stop = await self._send(profile, plan, scope, budget)
            return exchange
        finally:
            await self._record(conn, provider_id, ordinal, exchange, stop)

    async def _load(self, conn: AsyncConnection, provider_id: str) -> _Ledger:
        """Read the ledger and prove it against the attempt log; create it only if there is no history."""
        row = (
            await conn.execute(
                select(
                    RemoteTransportLedger.ordinal,
                    RemoteTransportLedger.last_reserved_at,
                    RemoteTransportLedger.not_before,
                    RemoteTransportLedger.stop_code,
                )
                .where(RemoteTransportLedger.provider_id == provider_id)
                .with_for_update()
            )
        ).one_or_none()
        count, highest = (
            await conn.execute(
                select(func.count(), func.max(RemoteTransportAttempt.ordinal)).where(RemoteTransportAttempt.provider_id == provider_id)
            )
        ).one()
        latest = (
            await conn.execute(
                select(RemoteTransportAttempt.reserved_at)
                .where(RemoteTransportAttempt.provider_id == provider_id)
                .order_by(RemoteTransportAttempt.ordinal.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            if count:
                raise _CorruptLedger("ledger_missing")
            await conn.execute(insert(RemoteTransportLedger).values(provider_id=provider_id, ordinal=0).on_conflict_do_nothing())
            return _Ledger(0, None, None, None)
        if row.ordinal != count or (highest or 0) != row.ordinal or row.last_reserved_at != latest:
            raise _CorruptLedger("ledger_mismatch")
        return _Ledger(row.ordinal, row.last_reserved_at, row.not_before, row.stop_code)

    def _corrupt(self, provider_id: str, scope: str, reason: str) -> SourceRead:
        logger.error("remote_transport.ledger_corrupt", provider_id=provider_id, reason=reason)
        return self._result(OutcomeStatus.UNAVAILABLE, "ledger_corrupt", scope, reason)

    def _request(self, profile: RemoteHostProfile, plan: _Plan) -> OutboundRequest:
        secret = self._credentials.get(profile.provider_id)
        credential = None
        if secret is not None and profile.credential is not None:
            credential = (profile.credential.header, SecretStr(profile.credential.prefix + secret.get_secret_value()))
        return OutboundRequest(
            url=plan.url(profile.host),
            host=profile.host,
            headers=(("User-Agent", profile.user_agent), ("Accept-Encoding", "identity")),
            timeout_seconds=profile.request_timeout_seconds,
            credential=credential,
        )

    async def _send(self, profile: RemoteHostProfile, plan: _Plan, scope: str, budget: DiscoveryBudget) -> tuple[_Exchange, str | None]:
        limit = min(profile.max_response_bytes, budget.max_bytes)
        try:
            async with asyncio.timeout(profile.request_timeout_seconds), self._socket.open(self._request(profile, plan)) as response:
                status = response.status
                headers = {key.lower(): value for key, value in response.headers.items()}
                body, consumed = await _bounded_body(response, headers, limit)
        except TimeoutError:
            return _Exchange(self._result(OutcomeStatus.RETRY, "timeout", scope), None), None
        except OSError as exc:
            return _Exchange(self._result(OutcomeStatus.RETRY, "transport_error", scope, type(exc).__name__), None), None
        now = self._clock.wall()
        outcome, code = _http_outcome(status)
        evidence = [f"http_status={status}"]
        content_type, charset = _content_type(headers)
        stop: str | None = None
        if headers.get("cf-mitigated", "").strip().lower() == "challenge" or (
            content_type == "text/html"
            and body is not None
            and any(marker in body.decode("utf-8", "replace") for marker in profile.challenge_markers)
        ):
            outcome, code, stop = OutcomeStatus.UNAVAILABLE, "challenge", "challenge"
        elif status in profile.stop_statuses:
            stop = f"http_{status}"
        values: dict[str, object] = {"bytes_read": consumed}
        not_before: datetime | None = None
        if outcome == OutcomeStatus.RETRY and (wait := _retry_after(headers.get("retry-after"), now)) is not None:
            evidence.append(f"retry_after_seconds={wait:g}")
            not_before = now + timedelta(seconds=wait)
        validators = _validators(headers)
        if outcome == OutcomeStatus.FOUND:
            if body is None:
                return _Exchange(
                    self._result(OutcomeStatus.UNAVAILABLE, "response_too_large", scope, *evidence, truncated=True, bytes_read=consumed), status
                ), stop
            encoding = charset or "utf-8"
            try:
                text = body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                return _Exchange(self._result(OutcomeStatus.UNSUPPORTED, "undecodable_response", scope, *evidence, bytes_read=consumed), status), stop
            values.update(text=text, encoding=encoding, validators=validators)
            route = plan.route
            if route is not None and route.etag_is_content_validator and validators and validators.etag and not validators.etag.startswith("W/"):
                values.update(revision=validators.etag, revision_scope="full")
        read = self._result(outcome, code, scope, *evidence, **values)
        return _Exchange(read, status, not_before), stop

    async def _record(self, conn: AsyncConnection, provider_id: str, ordinal: int, exchange: _Exchange | None, stop: str | None) -> None:
        now = self._clock.wall()
        read = exchange.read if exchange is not None else None
        await conn.rollback()
        await conn.execute(
            update(RemoteTransportAttempt)
            .where(RemoteTransportAttempt.provider_id == provider_id, RemoteTransportAttempt.ordinal == ordinal)
            .values(completed_at=now, http_status=exchange.http_status if exchange else None, outcome=(read.code if read else "interrupted")[:64])
        )
        not_before = exchange.not_before if exchange is not None else None
        if not_before is not None:
            await conn.execute(
                update(RemoteTransportLedger)
                .where(RemoteTransportLedger.provider_id == provider_id)
                .values(not_before=func.greatest(func.coalesce(RemoteTransportLedger.not_before, not_before), not_before))
            )
        if stop is not None:
            await conn.execute(
                update(RemoteTransportLedger)
                .where(RemoteTransportLedger.provider_id == provider_id, RemoteTransportLedger.stopped_at.is_(None))
                .values(stopped_at=now, stop_code=stop)
            )
            logger.warning("remote_transport.stopped", provider_id=provider_id, stop=stop)
        await conn.commit()
        logger.info(
            "remote_transport.exchange",
            provider_id=provider_id,
            ordinal=ordinal,
            http_status=exchange.http_status if exchange else None,
            outcome=read.code if read else "interrupted",
        )

    async def _stop(self, provider_id: str, code: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(RemoteTransportLedger)
                .where(RemoteTransportLedger.provider_id == provider_id, RemoteTransportLedger.stopped_at.is_(None))
                .values(stopped_at=self._clock.wall(), stop_code=code)
            )
        logger.warning("remote_transport.stopped", provider_id=provider_id, stop=code)


def _validators(headers: Mapping[str, str]) -> SourceValidators | None:
    etag, modified = headers.get("etag"), headers.get("last-modified")
    # Recorded exactly as sent or not at all: a value over its bound is dropped, never cut.
    etag = etag if etag is not None and len(etag) <= 512 else None
    modified = modified if modified is not None and len(modified) <= 128 else None
    if etag is None and modified is None:
        return None
    return SourceValidators(etag=etag, last_modified=modified)


async def _bounded_body(response: InboundResponse, headers: Mapping[str, str], limit: int) -> tuple[bytes | None, int]:
    """Read at most one chunk past ``limit``; ``None`` means the body was over it and was not kept."""
    declared = headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > limit:
        return None, 0
    buffer = bytearray()
    async for chunk in response.chunks():
        buffer += chunk
        if len(buffer) > limit:
            return None, limit
    return bytes(buffer), len(buffer)


async def clear_stop(engine: AsyncEngine, provider_id: str, *, operator: str, reason: str, clock: Clock | None = None) -> bool:
    """The explicit operator action that lifts a persistent stop. Returns whether a stop was lifted."""
    if not operator.strip() or not reason.strip():
        raise ValueError("Clearing a stop names the operator and the reason")
    now = (clock or SystemClock()).wall()
    async with engine.begin() as conn:
        result = await conn.execute(
            update(RemoteTransportLedger)
            .where(RemoteTransportLedger.provider_id == provider_id, RemoteTransportLedger.stopped_at.is_not(None))
            .values(stopped_at=None, stop_code=None, cleared_at=now, cleared_by=operator[:128], clear_reason=reason[:512])
        )
    cleared = bool(result.rowcount)
    logger.warning("remote_transport.stop_cleared", provider_id=provider_id, cleared=cleared)
    return cleared


__all__ = [
    "Clock",
    "HttpxSocket",
    "InboundResponse",
    "OutboundRequest",
    "RemoteSocket",
    "RemoteTransport",
    "RemoteTransportRun",
    "SystemClock",
    "clear_stop",
]
