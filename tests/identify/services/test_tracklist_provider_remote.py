"""Remote sources through the real registry: core mints, core authorizes, callers never do.

Specified by docs/design/0025-remote-provider-sources.md. The provider here is the fictional
``example_catalog`` from docs/design/tracklist-provider-specification.md; nothing is sent anywhere.
"""

from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import hashlib
import json

from pydantic import ValidationError
import pytest

from phaze.tracklist_providers import LocalProvider, ProviderContractError, ProviderRegistry, local_registry
from phaze.tracklist_providers.domain import (
    Capability,
    Completeness,
    DiscoveryBudget,
    DiscoveryOutcome,
    Fact,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    ProviderCandidate,
    ProviderDescriptor,
    ProviderTrack,
    RecordingContext,
    RemoteRequest,
    SearchTerm,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
    SourceValidators,
)
from phaze.tracklist_providers.host_profile import RemoteFetchRoute, RemoteHostProfile, RemoteSearchRoute
from phaze.tracklist_providers.port import AuthorizedEffects
from phaze.tracklist_providers.registry import context_key


NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROVIDER = "example_catalog"
PROFILE = RemoteHostProfile(
    provider_id=PROVIDER,
    search=RemoteSearchRoute(fields=frozenset({"artist", "event", "date"}), format="example_search_json", max_page=3),
    fetch=RemoteFetchRoute(native_id_pattern=r"set-[0-9]{2}", format="example_set_json"),
)
CONTEXT = RecordingContext(
    recording_id="recording-01",
    artist=Fact(value="Example Artist", certainty="known"),
    event=Fact(value="Example Event", certainty="inferred"),
    date=Fact(value="2026-01-01", certainty="unknown"),
)


class CoreEffects:
    """Stands in for the transport effect; records exactly what it was asked to read."""

    def __init__(self) -> None:
        self.reads: list[SourceReference] = []

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        del budget
        self.reads.append(source)
        return SourceRead(
            status="found",
            code="http_200",
            scope=source.identity.native_id,
            text='{"rows": []}',
            bytes_read=12,
            retrieved_at=NOW,
            validators=SourceValidators(etag='W/"example"', last_modified="Thu, 01 Jan 2026 00:00:00 GMT"),
        )


def tracks_snapshot(native: str) -> Snapshot:
    return Snapshot(
        identity=SourceIdentity(provider_id=PROVIDER, native_id=native),
        retrieved_at=NOW,
        source_format="example_set_json",
        parser_version="example-v1",
        tracks=(ProviderTrack(position=1, artist="Example Artist", title="Opening"), ProviderTrack(position=2)),
        completeness=Completeness(state="complete", reason="rows exhausted"),
    )


class RemoteCatalog:
    """Reads only what the registry minted; ``discover_reads``/``load_reads`` let a test forge instead."""

    descriptor = ProviderDescriptor(
        id=PROVIDER,
        display_name="Fictional remote catalog",
        capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT}),
    )

    def __init__(
        self,
        discover_reads: Callable[[AuthorizedEffects], tuple[SourceReference, ...]] | None = None,
        load_reads: Callable[[AuthorizedEffects], tuple[SourceReference, ...]] | None = None,
        *,
        swallow: bool = False,
    ) -> None:
        self.discover_reads = discover_reads
        self.load_reads = load_reads
        self.swallow = swallow
        self.seen: list[SourceRead] = []

    async def _read_all(self, effects: AuthorizedEffects, references: tuple[SourceReference, ...], budget: DiscoveryBudget) -> None:
        for reference in references:
            if self.swallow:
                with contextlib.suppress(ValueError):
                    self.seen.append(await effects.read(reference, budget))
            else:
                self.seen.append(await effects.read(reference, budget))

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del cursor
        if self.discover_reads is not None:
            references = self.discover_reads(effects)
        else:
            search = effects.minted[0]
            assert search.request is not None
            references = (search, search.model_copy(update={"request": search.request.model_copy(update={"page": 2})}))
        await self._read_all(effects, references, budget)
        return DiscoveryOutcome(
            status="incomplete",
            code="search_capped",
            scope=context.recording_id,
            candidates=(ProviderCandidate(identity=SourceIdentity(provider_id=PROVIDER, native_id="set-01"), url="https://example.invalid/set-01"),),
            completeness=Completeness(state="incomplete", reason="Search is capped and may be incomplete"),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        references = self.load_reads(effects) if self.load_reads is not None else effects.minted
        await self._read_all(effects, references, budget)
        return LoadOutcome(status="found", code="loaded", scope=candidate.identity.native_id, snapshot=tracks_snapshot(candidate.identity.native_id))


def registry(provider: RemoteCatalog | None = None) -> ProviderRegistry:
    return ProviderRegistry((provider or RemoteCatalog(), LocalProvider()), remote_profiles=(PROFILE,))


def remote(native: str, request: RemoteRequest, fmt: str = "example_set_json") -> SourceReference:
    return SourceReference(identity=SourceIdentity(provider_id=PROVIDER, native_id=native), channel="remote", format=fmt, request=request)


def assert_refused(result: DiscoveryOutcome | LoadOutcome, *, swallowed: bool) -> None:
    """A refused read is a contract error whichever way the provider handles the raise.

    Swallowed, the sticky violation is the outcome code. Propagated, the registry's exception boundary
    reports invalid_output carrying the violation, as it already did for local reads.
    """
    expected = ("unauthorized_read", "") if swallowed else ("invalid_output", "unauthorized_read")
    assert (result.status, (result.code, result.detail)) == ("contract_error", expected), result


FETCH = RemoteRequest(kind="fetch")
CANDIDATE = ProviderCandidate(identity=SourceIdentity(provider_id=PROVIDER, native_id="set-01"))


@pytest.mark.asyncio
async def test_remote_discovery_and_load_through_a_fake_provider() -> None:
    effects = CoreEffects()
    provider = RemoteCatalog()
    discovery = await registry(provider).discover(PROVIDER, CONTEXT, DiscoveryBudget(), effects)
    assert discovery.status == "incomplete" and discovery.code == "search_capped", discovery
    search = effects.reads[0]
    assert search.channel == "remote" and search.revision is None and search.format == "example_search_json"
    assert search.identity == SourceIdentity(provider_id=PROVIDER, native_id=f"search:{context_key(CONTEXT)}")
    assert search.request is not None and search.request.kind == "search" and search.request.page == 1
    # Only known/inferred facts become terms; the unknown date is not sent.
    assert search.request.terms == (SearchTerm(field="artist", value="Example Artist"), SearchTerm(field="event", value="Example Event"))
    assert [read.request.page for read in effects.reads if read.request] == [1, 2]

    loaded = await registry(provider).load(discovery.candidates[0], LoadBudget(), effects)
    assert loaded.status == "found" and loaded.snapshot is not None and loaded.snapshot.tracks[0].title == "Opening"
    fetch = effects.reads[2]
    assert fetch == remote("set-01", FETCH)
    # The candidate's URL is provider content; nothing derived from it reaches the effect.
    assert all("example.invalid" not in reference.model_dump_json() for reference in effects.reads)
    assert len(effects.reads) == 3 and all(read.validators is not None for read in provider.seen)


@pytest.mark.asyncio
async def test_nothing_is_minted_without_a_usable_term_or_a_search_route() -> None:
    unusable = RecordingContext(
        recording_id="recording-01",
        artist=Fact(value="x" * 257, certainty="known"),
        event=Fact(value="   ", certainty="inferred"),
        date=Fact(value="2026-01-01", certainty="conflicting"),
    )
    seen: list[tuple[SourceReference, ...]] = []

    def capture(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        seen.append(effects.minted)
        return ()

    effects = CoreEffects()
    await registry(RemoteCatalog(discover_reads=capture)).discover(PROVIDER, unusable, DiscoveryBudget(), effects)
    fetch_only = PROFILE.model_copy(update={"search": None})
    await ProviderRegistry((RemoteCatalog(discover_reads=capture),), remote_profiles=(fetch_only,)).discover(
        PROVIDER, CONTEXT, DiscoveryBudget(), effects
    )
    narrow = PROFILE.model_copy(update={"search": RemoteSearchRoute(fields=frozenset({"event"}), format="example_search_json")})
    await ProviderRegistry((RemoteCatalog(discover_reads=capture),), remote_profiles=(narrow,)).discover(
        PROVIDER, CONTEXT, DiscoveryBudget(), effects
    )
    assert seen[:2] == [(), ()]
    assert seen[2][0].request is not None and seen[2][0].request.terms == (SearchTerm(field="event", value="Example Event"),)
    search_only = PROFILE.model_copy(update={"fetch": None})
    result = await ProviderRegistry((RemoteCatalog(load_reads=capture),), remote_profiles=(search_only,)).load(CANDIDATE, LoadBudget(), effects)
    assert result.status == "found" and seen[3] == () and effects.reads == []


@pytest.mark.asyncio
async def test_an_over_long_fact_is_skipped_not_truncated_and_named() -> None:
    seen: list[tuple[tuple[SourceReference, ...], tuple[str, ...]]] = []

    def capture(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        seen.append((effects.minted, effects.skipped_terms))
        return ()

    long_artist = "a" * 257
    boundary_event = "e" * 256
    context = CONTEXT.model_copy(
        update={"artist": Fact(value=long_artist, certainty="known"), "event": Fact(value=boundary_event, certainty="inferred")}
    )
    all_skipped = context.model_copy(update={"event": Fact(value="e" * 300, certainty="known")})
    for value in (context, all_skipped, CONTEXT):
        await registry(RemoteCatalog(discover_reads=capture)).discover(PROVIDER, value, DiscoveryBudget(), CoreEffects())
    (minted, skipped), (none_minted, all_names), (plain, no_skips) = seen
    assert minted[0].request is not None and minted[0].request.terms == (SearchTerm(field="event", value=boundary_event),)
    assert skipped == ("artist",)
    assert none_minted == () and all_names == ("artist", "event")
    assert len(plain) == 1 and no_skips == ()
    # Field names only: no fact value reaches the provider through the skip record.
    assert all(long_artist not in name for name in skipped + all_names)
    load_skips: list[tuple[str, ...]] = []

    def load_capture(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        load_skips.append(effects.skipped_terms)
        return ()

    await registry(RemoteCatalog(load_reads=load_capture)).load(CANDIDATE, LoadBudget(), CoreEffects())
    assert load_skips == [()]


def _forgeries() -> list[tuple[str, Callable[[AuthorizedEffects], tuple[SourceReference, ...]]]]:
    def other_terms(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        minted = effects.minted[0]
        return (minted.model_copy(update={"request": RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value="Someone Else"),))}),)

    def beyond_page_bound(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        minted = effects.minted[0]
        assert minted.request is not None
        return (minted.model_copy(update={"request": minted.request.model_copy(update={"page": PROFILE.search.max_page + 1})}),)  # type: ignore[union-attr]

    def unvalidated_page(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        minted = effects.minted[0]
        assert minted.request is not None
        return (minted.model_copy(update={"request": minted.request.model_copy(update={"page": 0})}),)

    def self_built_search(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        del effects
        return (
            remote(
                f"search:{'0' * 64}", RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value="Example Artist"),)), "example_search_json"
            ),
        )

    def other_format(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        return (effects.minted[0].model_copy(update={"format": "html"}),)

    return [
        ("other_terms", other_terms),
        ("beyond_page_bound", beyond_page_bound),
        ("unvalidated_page", unvalidated_page),
        ("self_built_search", self_built_search),
        ("other_format", other_format),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("swallowed", [False, True])
@pytest.mark.parametrize(("name", "forge"), _forgeries(), ids=[name for name, _ in _forgeries()])
async def test_remote_reference_authorized_only_when_minted_by_core(
    name: str, forge: Callable[[AuthorizedEffects], tuple[SourceReference, ...]], swallowed: bool
) -> None:
    del name
    effects = CoreEffects()
    result = await registry(RemoteCatalog(discover_reads=forge, swallow=swallowed)).discover(PROVIDER, CONTEXT, DiscoveryBudget(), effects)
    assert_refused(result, swallowed=swallowed)
    assert effects.reads == []


@pytest.mark.asyncio
async def test_load_authorizes_only_the_minted_fetch() -> None:
    def forged(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        del effects
        return (remote("set-02", FETCH),)

    def paged_fetch(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        minted = effects.minted[0]
        assert minted.request is not None
        return (minted.model_copy(update={"request": minted.request.model_copy(update={"page": 2})}),)

    def unvalidated(effects: AuthorizedEffects) -> tuple[SourceReference, ...]:
        return (SourceReference.model_construct(identity=effects.minted[0].identity, channel="remote", format="example_set_json", request=None),)

    for forge in (forged, paged_fetch, unvalidated):
        for swallowed in (False, True):
            effects = CoreEffects()
            result = await registry(RemoteCatalog(load_reads=forge, swallow=swallowed)).load(CANDIDATE, LoadBudget(), effects)
            assert_refused(result, swallowed=swallowed)
            assert effects.reads == []
    # A remote registry never lets the minted search authorize a load, nor a minted fetch another identity.
    effects = CoreEffects()
    assert (await registry().load(CANDIDATE, LoadBudget(), effects)).status == "found"
    assert effects.reads == [remote("set-01", FETCH)]


@pytest.mark.asyncio
async def test_caller_supplied_url_never_authorizes_a_read() -> None:
    url = "https://example.invalid/api/sets?id=set-01"
    with pytest.raises(ValidationError):
        SourceReference.model_validate(
            {
                "identity": {"provider_id": PROVIDER, "native_id": "set-01"},
                "channel": "remote",
                "format": "json",
                "request": {"kind": "fetch"},
                "url": url,
            }
        )
    with pytest.raises(ValidationError):
        RemoteRequest.model_validate({"kind": "fetch", "host": "example.invalid", "path": "/api/sets"})
    built_from_url = remote(url, FETCH)
    effects = CoreEffects()
    # In the recording context, on a candidate, as a candidate identity, or read by the provider: always refused.
    context = CONTEXT.model_copy(update={"sources": (built_from_url,)})
    assert (await registry().discover(PROVIDER, context, DiscoveryBudget(), effects)).code == "caller_remote_reference"
    assert (await registry().discover("local", context, DiscoveryBudget(), effects)).code == "caller_remote_reference"
    carried = ProviderCandidate(identity=built_from_url.identity, source=built_from_url)
    assert (await registry().load(carried, LoadBudget(), effects)).code == "caller_remote_reference"
    as_identity = ProviderCandidate(identity=built_from_url.identity)
    assert (await registry().load(as_identity, LoadBudget(), effects)).code == "unauthorized_identity"
    read_by_provider = await registry(RemoteCatalog(load_reads=lambda _effects: (built_from_url,))).load(CANDIDATE, LoadBudget(), effects)
    assert_refused(read_by_provider, swallowed=False)
    url_term = await registry(
        RemoteCatalog(
            discover_reads=lambda minted: (
                minted.minted[0].model_copy(update={"request": RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value=url),))}),
            ),
            swallow=True,
        )
    ).discover(PROVIDER, CONTEXT, DiscoveryBudget(), effects)
    assert_refused(url_term, swallowed=True)
    assert effects.reads == []


@pytest.mark.asyncio
async def test_discovery_cannot_launder_a_minted_reference_onto_a_candidate() -> None:
    class Launder(RemoteCatalog):
        async def discover(
            self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
        ) -> DiscoveryOutcome:
            del budget, cursor
            search = effects.minted[0]
            return DiscoveryOutcome(
                status="incomplete",
                code="laundered",
                scope=context.recording_id,
                candidates=(ProviderCandidate(identity=search.identity, source=search),),
                completeness=Completeness(state="incomplete", reason="capped"),
            )

    result = await registry(Launder()).discover(PROVIDER, CONTEXT, DiscoveryBudget(), CoreEffects())
    assert (result.status, result.code) == ("contract_error", "invalid_output")
    assert "not explicitly authorized" in result.detail


@pytest.mark.asyncio
async def test_budget_is_sticky_for_remote_reads() -> None:
    class Tolerant(RemoteCatalog):
        def __init__(self, change: dict[str, object] | None, reads: int) -> None:
            super().__init__()
            self.change = change
            self.reads = reads

        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
            asked = budget.model_copy(update=self.change) if self.change else budget
            for _ in range(self.reads):
                with contextlib.suppress(ValueError):
                    await effects.read(effects.minted[0], asked)
            return LoadOutcome(
                status="found", code="ignored", scope=candidate.identity.native_id, snapshot=tracks_snapshot(candidate.identity.native_id)
            )

    effects = CoreEffects()
    changed = await registry(Tolerant({"max_bytes": LoadBudget().max_bytes + 1}, 1)).load(CANDIDATE, LoadBudget(), effects)
    assert (changed.status, changed.code) == ("contract_error", "budget_changed")
    assert effects.reads == []
    over = await registry(Tolerant(None, 3)).load(CANDIDATE, LoadBudget(max_reads=2), effects)
    assert (over.status, over.code) == ("contract_error", "read_cap")
    assert len(effects.reads) == 2
    unauthorized = await registry(RemoteCatalog(load_reads=lambda _effects: (remote("set-02", FETCH),), swallow=True)).load(
        CANDIDATE, LoadBudget(), effects
    )
    assert (unauthorized.status, unauthorized.code) == ("contract_error", "unauthorized_read")
    assert len(effects.reads) == 2


@pytest.mark.asyncio
async def test_unauthorized_identity_refused_before_any_effect() -> None:
    effects = CoreEffects()
    for native in ("set-1", "set-01/../admin", "xset-01", "set-012"):
        result = await registry().load(ProviderCandidate(identity=SourceIdentity(provider_id=PROVIDER, native_id=native)), LoadBudget(), effects)
        assert (result.status, result.code) == ("contract_error", "unauthorized_identity")
    assert effects.reads == []


def test_registry_construction_contract_errors() -> None:
    provider = RemoteCatalog()
    with pytest.raises(ProviderContractError, match="without a core host profile"):
        ProviderRegistry((provider,))
    with pytest.raises(ProviderContractError, match="without remote transport"):
        ProviderRegistry((LocalProvider(),), remote_profiles=(PROFILE.model_copy(update={"provider_id": "local"}),))
    with pytest.raises(ProviderContractError, match="unknown provider"):
        ProviderRegistry((provider,), remote_profiles=(PROFILE, PROFILE.model_copy(update={"provider_id": "missing"})))
    with pytest.raises(ProviderContractError, match="Duplicate remote host profile"):
        ProviderRegistry((provider,), remote_profiles=(PROFILE, PROFILE))
    with pytest.raises(ValidationError):
        RemoteHostProfile(provider_id=PROVIDER)
    with pytest.raises(ValidationError, match="Invalid native ID pattern"):
        RemoteFetchRoute(native_id_pattern="(", format="json")
    assert [d.id for d in registry().descriptors()] == ["example_catalog", "local"]


@pytest.mark.parametrize(
    "values",
    [
        {"channel": "remote"},
        {"channel": "companion", "request": {"kind": "fetch"}},
        {"channel": "remote", "request": {"kind": "fetch"}, "revision": "r1"},
        {"channel": "remote", "request": {"kind": "search", "terms": [{"field": "artist", "value": "A"}]}},
        {"channel": "remote", "request": {"kind": "fetch", "page": 2}},
        {"channel": "remote", "request": {"kind": "fetch", "terms": [{"field": "artist", "value": "A"}]}},
        {"channel": "remote", "request": {"kind": "search", "terms": []}, "native_id": f"search:{'a' * 64}"},
        {"channel": "remote", "request": {"kind": "search", "terms": [{"field": "artist", "value": "A"}] * 2}, "native_id": f"search:{'a' * 64}"},
        {"channel": "remote", "request": {"kind": "search", "terms": [{"field": "duration", "value": "A"}]}, "native_id": f"search:{'a' * 64}"},
        {"channel": "remote", "request": {"kind": "search", "terms": [{"field": "artist", "value": ""}]}, "native_id": f"search:{'a' * 64}"},
        {
            "channel": "remote",
            "request": {"kind": "search", "terms": [{"field": "artist", "value": "A"}], "page": 65},
            "native_id": f"search:{'a' * 64}",
        },
    ],
)
def test_remote_reference_shape_is_closed(values: dict[str, object]) -> None:
    native = values.pop("native_id", "set-01")
    with pytest.raises(ValidationError):
        SourceReference.model_validate({"identity": {"provider_id": PROVIDER, "native_id": native}, "format": "json", **values})


def test_validators_are_bounded_and_kept_verbatim() -> None:
    read = SourceRead(status="found", code="http_200", scope="set-01", retrieved_at=NOW, validators=SourceValidators(etag='W/"x"'))
    assert SourceRead.model_validate_json(read.model_dump_json()) == read
    assert read.model_dump()["validators"] == {"etag": 'W/"x"', "last_modified": None}
    with pytest.raises(ValidationError):
        SourceValidators(etag="x" * 513)
    with pytest.raises(ValidationError):
        SourceValidators(last_modified="x" * 129)


@pytest.mark.asyncio
async def test_local_provider_behaviour_is_unchanged() -> None:
    """Local discovery and load are identical with and without a remote provider registered beside them.

    The existing local-provider modules run unmodified as the other half of this criterion; the
    submit report names them.
    """
    source = SourceReference(identity=SourceIdentity(provider_id="local", native_id="companion:0"), channel="companion", format="txt")
    context = CONTEXT.model_copy(update={"sources": (source,)})
    assert LocalProvider.descriptor.contract_version == (1, 0)
    assert Capability.REMOTE_TRANSPORT not in LocalProvider.descriptor.capabilities
    # The unset request is omitted, so a local context, and therefore its context key and cursors, are
    # byte-identical to the pre-change shape, written out literally here.
    pre_change_source = {
        "identity": {"provider_id": "local", "native_id": "companion:0"},
        "channel": "companion",
        "format": "txt",
        "display_name": None,
        "revision": None,
    }
    pre_change_context = {
        "recording_id": "recording-01",
        "sources": [pre_change_source],
        "artist": {"value": "Example Artist", "certainty": "known", "evidence": []},
        "event": {"value": "Example Event", "certainty": "inferred", "evidence": []},
        "date": {"value": "2026-01-01", "certainty": "unknown", "evidence": []},
        "duration_seconds": None,
    }
    assert context.model_dump(mode="json") == pre_change_context
    assert context.model_dump_json() == json.dumps(pre_change_context, separators=(",", ":"))
    assert context_key(context) == hashlib.sha256(json.dumps(pre_change_context, sort_keys=True).encode()).hexdigest()
    assert source.model_dump() == pre_change_source
    # A remote reference keeps its request in every dump.
    fetch = remote("set-01", FETCH)
    assert fetch.model_dump(mode="json")["request"] == {"kind": "fetch", "terms": [], "page": 1}
    assert SourceReference.model_validate_json(fetch.model_dump_json()) == fetch

    outcomes = []
    for composed in (local_registry(), registry()):
        effects = CoreEffects()
        discovery = await composed.discover("local", context, DiscoveryBudget(), effects)
        load = await composed.load(discovery.candidates[0], LoadBudget(), effects)
        outcomes.append((discovery, load, effects.reads))
    assert outcomes[0] == outcomes[1]
    discovery, load, reads = outcomes[0]
    assert discovery.status == "found" and discovery.candidates[0].source == source
    assert load.status == "incomplete" and load.code == "recognition_only"
    assert reads == [source]
