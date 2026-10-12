"""The MixesDB adapter through the real registry and the real interpreter, with a scripted core effect.

Nothing is sent anywhere: ``ScriptedCore`` stands in for the transport and answers each minted reference from a
script. The transport itself is exercised in tests/integration/test_mixesdb_provider.py. Every key, title,
artist and identifier here is synthetic.
"""

import importlib
import json
from pathlib import Path
import socket
import sys
import time

import pytest

from phaze.config_remote_providers import RemoteProviderSettings, disabled_provider_ids, load_remote_provider_settings
from phaze.services.remote_lookup import effective_profile
from phaze.services.remote_providers import REMOTE_PROVIDERS
from phaze.tracklist_providers import ProviderRegistry, local_registry, mixesdb
from phaze.tracklist_providers.domain import (
    Capability,
    DiscoveryBudget,
    Fact,
    LoadBudget,
    OpaqueCursor,
    OutcomeStatus,
    ProviderCandidate,
    RecordingContext,
    RemoteRequest,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.mixesdb import (
    NATIVE_KEY_PATTERN,
    PROFILE,
    RIGHTS_EVIDENCE,
    MixesDbProvider,
    page_url,
)
from phaze.tracklist_providers.registry import context_key


NOW = "2026-01-01T00:00:00Z"
(BINDING,) = (binding for binding in REMOTE_PROVIDERS if binding.profile.provider_id == "mixesdb")
KEY = "2026-01-01_-_Example_Artist_@_Example_Festival"
CONTEXT = RecordingContext(
    recording_id="recording-01",
    artist=Fact(value="Example Artist", certainty="inferred"),
    event=Fact(value="Example Festival", certainty="inferred"),
    date=Fact(value="2026", certainty="inferred"),
)
WIKITEXT = "== Tracklist ==\n# [00] Example Artist - Opening Track\n# [05] Other Artist - Second Track (Example Remix)\n"


def search_body(*entries: object) -> str:
    return json.dumps({"pages": list(entries)})


def page_body(key: str = KEY, *, revision: int | None = 101, source: object = WIKITEXT, **extra: object) -> str:
    body: dict[str, object] = {
        "id": 7,
        "key": key,
        "title": key.replace("_", " "),
        "content_model": "wikitext",
        "license": {"url": "", "title": ""},
        "source": source,
    }
    if revision is not None:
        body["latest"] = {"id": revision, "timestamp": NOW}
    return json.dumps({**body, **extra})


def found(text: str, scope: str = "scope") -> SourceRead:
    return SourceRead(status="found", code="http_200", scope=scope, evidence=("http_status=200",), text=text, bytes_read=len(text), retrieved_at=NOW)


class ScriptedCore:
    """The core effect: answers search and fetch references from a script, recording every read."""

    def __init__(self, search: SourceRead | None = None, fetch: SourceRead | None = None) -> None:
        self.search = search or found(search_body())
        self.fetch = fetch or found(page_body())
        self.reads: list[SourceReference] = []

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        del budget
        self.reads.append(source)
        assert source.request is not None
        return self.search if source.request.kind == "search" else self.fetch


def registry(provider: MixesDbProvider | None = None, **kwargs: object) -> ProviderRegistry:
    return ProviderRegistry((provider or MixesDbProvider(),), remote_profiles=(PROFILE,), **kwargs)  # type: ignore[arg-type]


def candidate(key: str = KEY, **kwargs: object) -> ProviderCandidate:
    return ProviderCandidate(identity=SourceIdentity(provider_id="mixesdb", native_id=key), **kwargs)  # type: ignore[arg-type]


def test_registration_is_explicit_and_has_no_import_time_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network I/O during import")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.delitem(sys.modules, "phaze.tracklist_providers.mixesdb")
    reloaded = importlib.import_module("phaze.tracklist_providers.mixesdb")
    # Importing builds declarations only; nothing registers itself anywhere.
    assert reloaded.PROFILE.provider_id == reloaded.MixesDbProvider.descriptor.id == "mixesdb"
    assert [descriptor.id for descriptor in local_registry().descriptors()] == ["local"]
    # No entry point and no package-level registration: the package root never names the adapter.
    root = Path(reloaded.__file__).parent
    assert "mixesdb" not in (root / "__init__.py").read_text(encoding="utf-8").casefold()
    assert "entry-points" not in (root.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    # Registration is the one explicit binding the composition root composes.
    assert [descriptor.id for descriptor in registry().descriptors()] == ["mixesdb"]
    assert isinstance(BINDING.provider, MixesDbProvider) and BINDING.profile == PROFILE and BINDING.credential_name is None


def test_descriptor_profile_and_spec_declare_remote_rest_only_with_no_credentials() -> None:
    capabilities = MixesDbProvider.descriptor.capabilities
    assert {Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT} <= capabilities
    assert capabilities <= set(Capability) - {Capability.LOCAL_SIDECAR, Capability.EMBEDDED_METADATA}
    assert PROFILE.host == "www.mixesdb.com" and PROFILE.credential is None and BINDING.spec.credential_names == ()
    assert PROFILE.robots == "respect" and PROFILE.min_gap_seconds >= 5.1
    assert "phaze" in PROFILE.user_agent and "github.com/SimplicityGuy/phaze" in PROFILE.user_agent
    assert PROFILE.search is not None and PROFILE.search.max_page == 1 and PROFILE.search.page_parameter is None
    assert dict(PROFILE.search.fixed_parameters) == {"limit": "100"}


def test_provider_is_disabled_by_default() -> None:
    settings = load_remote_provider_settings((BINDING.spec,), environ={})
    assert settings["mixesdb"].enabled is False
    disabled = disabled_provider_ids((MixesDbProvider.descriptor,), settings)
    assert disabled == frozenset({"mixesdb"})
    # Fail closed with no settings entry at all.
    assert disabled_provider_ids((MixesDbProvider.descriptor,), {}) == frozenset({"mixesdb"})
    enabled = load_remote_provider_settings((BINDING.spec,), environ={"PHAZE_PROVIDER_MIXESDB_ENABLED": "true"})
    assert disabled_provider_ids((MixesDbProvider.descriptor,), enabled) == frozenset()


async def test_disabled_provider_is_listed_and_reads_nothing() -> None:
    core = ScriptedCore()
    built = registry(disabled=frozenset({"mixesdb"}))
    assert [descriptor.id for descriptor in built.descriptors()] == ["mixesdb"]
    discovered = await built.discover("mixesdb", CONTEXT, DiscoveryBudget(), core)
    loaded = await built.load(candidate(), LoadBudget(), core)
    assert (discovered.status, discovered.code) == (OutcomeStatus.UNAVAILABLE, "disabled_provider")
    assert (loaded.status, loaded.code) == (OutcomeStatus.UNAVAILABLE, "disabled_provider")
    assert core.reads == []


@pytest.mark.parametrize(
    "identity",
    [
        "a" * 4096,
        "a" * 255 + "#",
        "_" * 4095 + "\x00",
        "a/" * 2048,
        "%" * 4096,
    ],
)
def test_native_key_pattern_is_linear_over_adversarial_identity(identity: str) -> None:
    assert PROFILE.fetch is not None
    started = time.perf_counter()
    for _ in range(200):
        assert PROFILE.fetch.accepts(identity) is False
    # Two hundred checks of a 4096-character identity; a backtracking pattern takes seconds, this takes milliseconds.
    assert time.perf_counter() - started < 0.5
    assert NATIVE_KEY_PATTERN.startswith("[^") and NATIVE_KEY_PATTERN.endswith("]{1,255}")


def test_native_key_pattern_accepts_real_key_shapes_verbatim() -> None:
    assert PROFILE.fetch is not None
    for key in (KEY, "Category:Example_Artist", "Example/Sub_Page", "Exämple_Artist_(Live)", "a" * 255):
        assert PROFILE.fetch.accepts(key)
    for key in ("", "a" * 256, "has space", "bad|pipe", "bad[bracket]", "bad#hash"):
        assert not PROFILE.fetch.accepts(key)


async def test_discovery_reads_the_minted_search_once_and_never_inherits_the_title() -> None:
    core = ScriptedCore(search=found(search_body({"id": 7, "key": KEY, "title": KEY.replace("_", " "), "excerpt": "<span>Example</span> Artist"})))
    outcome = await registry().discover("mixesdb", CONTEXT, DiscoveryBudget(), core)
    assert outcome.status == OutcomeStatus.INCOMPLETE and outcome.cursor is None
    (only,) = outcome.candidates
    assert only.identity.native_id == KEY and only.source is None and only.observed_revision is None
    assert only.url == page_url(KEY) == "https://www.mixesdb.com/w/2026-01-01_-_Example_Artist_%40_Example_Festival"
    assert "title_artist=present" in only.evidence and "page_id=7" in only.evidence and "search_excerpt:Example Artist" in only.evidence
    (read,) = core.reads
    assert read.identity.native_id == f"search:{context_key(CONTEXT)}"
    assert read.request is not None and {(term.field, term.value) for term in read.request.terms} == {
        ("artist", "Example Artist"),
        ("event", "Example Festival"),
        ("date", "2026"),
    }


async def test_cursor_and_missing_facts_are_typed_and_never_absent() -> None:
    core = ScriptedCore()
    cursor = OpaqueCursor(provider_id="mixesdb", context_key=context_key(CONTEXT), token="1")  # noqa: S106 - a cursor token, not a secret
    with_cursor = await registry().discover("mixesdb", CONTEXT, DiscoveryBudget(), core, cursor)
    assert (with_cursor.status, with_cursor.code) == (OutcomeStatus.CONTRACT_ERROR, "cursor_unsupported")
    empty = await registry().discover("mixesdb", RecordingContext(recording_id="recording-02"), DiscoveryBudget(), core)
    assert (empty.status, empty.code) == (OutcomeStatus.INCOMPLETE, "no_search_facts") and empty.completeness is not None
    overlong = RecordingContext(recording_id="recording-03", artist=Fact(value="x" * 257, certainty="inferred"))
    skipped = await registry().discover("mixesdb", overlong, DiscoveryBudget(), core)
    assert skipped.code == "no_search_facts" and skipped.evidence == ("skipped_overlong_fact=artist",)
    assert core.reads == []


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        ("absent", "http_404", OutcomeStatus.UNAVAILABLE),
        ("retry", "http_503", OutcomeStatus.RETRY),
        ("unavailable", "transport_stopped", OutcomeStatus.UNAVAILABLE),
        ("unsupported", "undecodable_response", OutcomeStatus.UNSUPPORTED),
        ("contract_error", "http_400", OutcomeStatus.CONTRACT_ERROR),
    ],
)
async def test_failed_search_is_its_own_typed_outcome_never_absent(status: str, code: str, expected: OutcomeStatus) -> None:
    failed = SourceRead(status=status, code=code, scope="search", evidence=("http_status=x",), retrieved_at=NOW)  # type: ignore[arg-type]
    outcome = await registry().discover("mixesdb", CONTEXT, DiscoveryBudget(), ScriptedCore(search=failed))
    assert (outcome.status, outcome.code, outcome.candidates) == (expected, code, ())


@pytest.mark.parametrize("text", ["not json", "[]", '{"pages": {}}', '{"other": []}'])
async def test_unrecognized_search_response_is_unsupported(text: str) -> None:
    outcome = await registry().discover("mixesdb", CONTEXT, DiscoveryBudget(), ScriptedCore(search=found(text)))
    assert (outcome.status, outcome.code) == (OutcomeStatus.UNSUPPORTED, "unrecognized_search_response")


async def test_unloadable_records_and_candidate_cap_are_evidence() -> None:
    entries: list[object] = ["not a record", {"key": 5}, {"key": "has space"}, {"key": ".."}, {"key": "User_talk:Someone"}]
    entries += [{"key": f"Example_Set_{index:02d}", "title": 5, "id": True} for index in range(5)]
    outcome = await registry().discover("mixesdb", CONTEXT, DiscoveryBudget(max_candidates=2), ScriptedCore(search=found(search_body(*entries))))
    assert outcome.code == "search_exhaustion_unknown" and len(outcome.candidates) == 2
    expected = {"unloadable_record_count=4", "non_tracklist_namespace_count=1", "candidate_cap=2", "records_not_examined=3", "search_returned=10"}
    assert expected <= set(outcome.evidence)
    first = outcome.candidates[0]
    assert first.display_name is None and not any(item.startswith("page_id=") for item in first.evidence)


async def test_load_parses_the_page_with_the_real_interpreter_and_records_rights() -> None:
    core = ScriptedCore()
    outcome = await registry().load(candidate(), LoadBudget(), core)
    assert outcome.status == OutcomeStatus.FOUND and outcome.snapshot is not None
    snapshot = outcome.snapshot
    assert snapshot.identity.native_id == KEY and snapshot.revision == f"101@{NOW}" and snapshot.revision_scope == "full"
    assert snapshot.url == page_url(KEY) and f"page_url:{page_url(KEY)}" in snapshot.provenance and f"native_key:{KEY}" in snapshot.provenance
    assert "rest_license=empty" in snapshot.provenance and "page_id=7" in snapshot.provenance
    assert snapshot.rights_evidence == RIGHTS_EVIDENCE and len(RIGHTS_EVIDENCE) == 4
    assert [(track.artist, track.title, track.remix_info) for track in snapshot.tracks] == [
        ("Example Artist", "Opening Track", None),
        ("Other Artist", "Second Track (Example Remix)", "Example Remix"),
    ]
    # No origin is evidenced, so a cue is kept verbatim and is never usable.
    assert all(track.timestamp is not None and track.timestamp.offset_usability == "unusable" for track in snapshot.tracks)
    assert snapshot.artist.certainty == snapshot.event.certainty == snapshot.date.certainty == "unknown"
    (read,) = core.reads
    assert read.identity.native_id == KEY and read.request is not None and read.request.kind == "fetch"


@pytest.mark.parametrize(
    ("text", "code", "status"),
    [
        ("not json", "unrecognized_page_response", OutcomeStatus.UNSUPPORTED),
        (page_body(source=None), "unrecognized_page_response", OutcomeStatus.UNSUPPORTED),
        (page_body("Some_Other_Page"), "page_key_mismatch", OutcomeStatus.UNAVAILABLE),
        (page_body(content_model="json"), "unsupported_content_model", OutcomeStatus.UNSUPPORTED),
    ],
)
async def test_unusable_page_responses_are_typed(text: str, code: str, status: OutcomeStatus) -> None:
    outcome = await registry().load(candidate(), LoadBudget(), ScriptedCore(fetch=found(text)))
    assert (outcome.status, outcome.code, outcome.snapshot) == (status, code, None)


async def test_page_without_revision_or_with_a_licence_is_recorded_as_such() -> None:
    text = page_body(revision=None, id=None, license={"url": "https://licence.example.invalid", "title": "Example"})
    outcome = await registry().load(candidate(), LoadBudget(), ScriptedCore(fetch=found(text)))
    assert outcome.snapshot is not None and outcome.snapshot.revision is None
    assert "revision_unknown" in outcome.snapshot.provenance and not any(item.startswith("page_id=") for item in outcome.snapshot.provenance)
    assert any(item.startswith("rest_license={") for item in outcome.snapshot.provenance)


async def test_failed_page_read_keeps_its_status_scoped_to_the_key() -> None:
    gone = SourceRead(status="absent", code="http_404", scope=KEY, evidence=("http_status=404",), retrieved_at=NOW)
    outcome = await registry().load(candidate(), LoadBudget(), ScriptedCore(fetch=gone))
    assert (outcome.status, outcome.code, outcome.scope) == (OutcomeStatus.ABSENT, "http_404", KEY)


async def test_revision_mismatch_is_retry_from_the_provider_itself() -> None:
    effects = _Minted(ScriptedCore(fetch=found(page_body(revision=102))))
    outcome = await MixesDbProvider().load(candidate(observed_revision=f"101@{NOW}"), LoadBudget(), effects)
    assert (outcome.status, outcome.code, outcome.snapshot) == (OutcomeStatus.RETRY, "revision_changed", None)
    assert outcome.evidence == (f"observed_revision=101@{NOW}", f"loaded_revision=102@{NOW}")


async def test_load_without_a_minted_fetch_is_a_contract_error() -> None:
    effects = _Minted(ScriptedCore(), minted=())
    outcome = await MixesDbProvider().load(candidate(), LoadBudget(), effects)
    assert (outcome.status, outcome.code) == (OutcomeStatus.CONTRACT_ERROR, "fetch_not_minted")
    assert effects.core.reads == []


class _Minted:
    """An AuthorizedEffects for calling the provider directly, past the registry."""

    def __init__(self, core: ScriptedCore, minted: tuple[SourceReference, ...] | None = None) -> None:
        self.core = core
        identity = SourceIdentity(provider_id="mixesdb", native_id=KEY)
        fetch = SourceReference(identity=identity, channel="remote", format="mixesdb_rest_page_json", request=RemoteRequest(kind="fetch"))
        self._minted = (fetch,) if minted is None else minted

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        return await self.core.read(source, budget)

    @property
    def minted(self) -> tuple[SourceReference, ...]:
        return self._minted

    @property
    def skipped_terms(self) -> tuple[str, ...]:
        return ()


async def test_candidate_construction_is_bounded_by_the_cap_whatever_the_server_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[ProviderCandidate] = []

    def counting(**kwargs: object) -> ProviderCandidate:
        built.append(ProviderCandidate(**kwargs))  # type: ignore[arg-type]
        return built[-1]

    monkeypatch.setattr(mixesdb, "ProviderCandidate", counting)
    flood = [{"key": f"Category:Example_{index:02d}"} for index in range(40)] + [{"key": f"Example_Set_{index:05d}"} for index in range(5000)]
    text = search_body(*flood)
    assert len(text) <= 262144
    outcome = await registry().discover(
        "mixesdb", CONTEXT, DiscoveryBudget(max_candidates=8, max_characters=262144), ScriptedCore(search=found(text))
    )
    assert len(built) == 8 and len(outcome.candidates) == 8
    assert outcome.code == "search_at_cap" and outcome.cursor is None
    assert {"search_returned=5040", "candidate_cap=8", "records_not_examined=4992", "non_tracklist_namespace_count=40"} <= set(outcome.evidence)
    # Only the first 32 namespaced keys are listed by name; the count covers them all.
    assert sum(item.startswith("non_tracklist_namespace=") for item in outcome.evidence) == 32


def test_a_pacing_override_never_loosens_the_profile_floor() -> None:
    def settings(**values: object) -> RemoteProviderSettings:
        return RemoteProviderSettings(provider_id="mixesdb", enabled=True, **values)  # type: ignore[arg-type]

    floor = effective_profile(PROFILE, settings(min_interval_seconds=0.1, max_requests_per_run=1000))
    assert (floor.min_gap_seconds, floor.per_run_budget) == (6.0, 4)
    tighter = effective_profile(PROFILE, settings(min_interval_seconds=30.0, max_requests_per_run=2))
    assert (tighter.min_gap_seconds, tighter.per_run_budget) == (30.0, 2)
