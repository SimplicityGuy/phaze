"""The TrackId provider through the real registry and the real interpreter, with scripted core effects.

Nothing here opens a socket: ``ScriptedEffects`` stands in for the transport and hands back synthetic
``SourceRead`` values shaped like the documented responses. The transport itself, with a fake socket and a
real ledger, is exercised in tests/integration/test_trackid_provider_transport.py. Every slug, title and track
name below is invented.
"""

from pathlib import Path
import subprocess
import sys
import textwrap
import time
from typing import Any

import pytest

from phaze.config_remote_providers import load_remote_provider_settings
from phaze.services.remote_providers import REMOTE_PROVIDERS
from phaze.tracklist_providers import local_registry
from phaze.tracklist_providers.domain import (
    Capability,
    DiscoveryBudget,
    Fact,
    LoadBudget,
    OutcomeStatus,
    ProviderCandidate,
    RecordingContext,
    SearchTerm,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.registry import ProviderRegistry
from phaze.tracklist_providers.trackid import (
    PAGE_SIZE,
    RIGHTS_EVIDENCE,
    TRACKID_PROFILE,
    TrackIdProvider,
    check_trackid_credential,
)
from tests._child_process_budget import CHILD_PROCESS_HANG_GUARD_SEC
from tests.identify.services.trackid_fixtures import NOW, ScriptedEffects, detail_document, found, list_page, process, row


CONTEXT = RecordingContext(
    recording_id="recording-01",
    artist=Fact(value="Example Artist", certainty="known"),
    event=Fact(value="Example Event", certainty="inferred"),
    date=Fact(value="2026-01-01", certainty="inferred"),
)
CANDIDATE = ProviderCandidate(identity=SourceIdentity(provider_id="trackid", native_id="set-slug-01"))


def registry() -> ProviderRegistry:
    return ProviderRegistry((TrackIdProvider(),), remote_profiles=(TRACKID_PROFILE,))


# --- registration -------------------------------------------------------------------------------------------------


def test_registration_is_explicit_and_has_no_import_time_io() -> None:
    assert [d.id for d in local_registry().descriptors()] == ["local"]
    # A fresh interpreter with every way out closed: importing the modules must not resolve a name, connect or
    # read the provider's settings, even when they are set.
    script = textwrap.dedent(
        """
        import os, socket
        os.environ["PHAZE_PROVIDER_TRACKID_ENABLED"] = "true"
        def refuse(*args, **kwargs):
            raise AssertionError("import-time network access")
        socket.socket.connect = refuse
        socket.getaddrinfo = refuse
        socket.create_connection = refuse
        import phaze.tracklist_providers as package
        import phaze.tracklist_providers.trackid
        import phaze.services.remote_providers
        assert [d.id for d in package.local_registry().descriptors()] == ["local"]
        print("ok")
        """
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=CHILD_PROCESS_HANG_GUARD_SEC, check=False)  # noqa: S603
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr


def test_registration_uses_no_entry_points() -> None:
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    assert "entry-points" not in pyproject.read_text() and "entry_points" not in pyproject.read_text()


def test_the_provider_is_registered_through_one_binding_and_is_off_by_default() -> None:
    (binding,) = [b for b in REMOTE_PROVIDERS if b.profile.provider_id == "trackid"]
    assert isinstance(binding.provider, TrackIdProvider) and binding.profile is TRACKID_PROFILE
    assert binding.credential_name == "client_credentials" and binding.spec.credential_names == ("client_credentials",)
    off = load_remote_provider_settings([binding.spec], {})
    assert off["trackid"].enabled is False


def test_descriptor_declares_only_valid_capabilities() -> None:
    caps = TrackIdProvider.descriptor.capabilities
    assert caps == {
        Capability.LOAD,
        Capability.DISCOVERY,
        Capability.REVISION_EVIDENCE,
        Capability.TRACK_PARSING,
        Capability.TIMESTAMP_EVIDENCE,
        Capability.REMOTE_TRANSPORT,
    }
    assert Capability.LOCAL_SIDECAR not in caps and Capability.EMBEDDED_METADATA not in caps


# --- discovery ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_queries_use_only_recording_context_facts() -> None:
    # A local source whose display name and identity look like a path must never reach a query.
    local = SourceReference(
        identity=SourceIdentity(provider_id="local", native_id="companion-01"),
        channel="companion",
        format="text",
        display_name="/archive-mount/private/set-01.mp3",
    )
    context = CONTEXT.model_copy(update={"sources": (local,)})
    effects = ScriptedEffects(found(list_page([], 0)))
    outcome = await registry().discover("trackid", context, DiscoveryBudget(), effects)
    assert outcome.status == OutcomeStatus.FOUND
    (search,) = effects.reads
    assert search.request is not None
    # The date is a fact, but the profile sends artist and event only; nothing else can become a term.
    assert search.request.terms == (SearchTerm(field="artist", value="Example Artist"), SearchTerm(field="event", value="Example Event"))
    assert "archive-mount" not in search.model_dump_json() and "companion-01" not in search.model_dump_json()
    assert TRACKID_PROFILE.search is not None and TRACKID_PROFILE.search.fields == {"artist", "event"}


@pytest.mark.asyncio
async def test_no_search_is_made_without_a_usable_fact() -> None:
    effects = ScriptedEffects()
    only_date = RecordingContext(recording_id="recording-01", date=Fact(value="2026-01-01", certainty="known"))
    outcome = await registry().discover("trackid", only_date, DiscoveryBudget(), effects)
    assert (outcome.status, outcome.code) == (OutcomeStatus.UNSUPPORTED, "no_search_hint")
    assert effects.reads == [] and not outcome.candidates
    too_long = CONTEXT.model_copy(update={"artist": Fact(value="a" * 300, certainty="known"), "event": Fact()})
    outcome = await registry().discover("trackid", too_long, DiscoveryBudget(), effects)
    assert outcome.code == "no_search_hint" and "artist" in outcome.detail and "a" * 300 not in outcome.detail


@pytest.mark.asyncio
async def test_exhausted_paging_is_complete_and_unknown_exhaustion_is_incomplete() -> None:
    # 25 results over 20 + 5: the pages cover rowCount exactly, so exhaustion is demonstrated.
    effects = ScriptedEffects(found(list_page(range(0, 20), 25)), found(list_page(range(20, 25), 25)))
    complete = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), effects)
    assert (complete.status, complete.code) == (OutcomeStatus.FOUND, "search_exhausted")
    assert complete.completeness is not None and complete.completeness.state == "complete" and complete.cursor is None
    assert [c.identity.native_id for c in complete.candidates] == [f"set-slug-{i:02d}" for i in range(25)]
    assert [r.request.page for r in effects.reads if r.request] == [1, 2]

    # 500 results: three pages cover 60 of them. Not exhausted, and there is no cursor to continue with.
    pages = [found(list_page(range(n * 20, n * 20 + 20), 500)) for n in range(3)]
    capped = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(*pages))
    assert (capped.status, capped.code) == (OutcomeStatus.INCOMPLETE, "page_cap")
    assert capped.cursor is None and len(capped.candidates) == 60
    assert capped.completeness is not None and capped.completeness.state == "incomplete"


@pytest.mark.asyncio
async def test_zero_results_is_an_exhausted_empty_scope_never_a_statement_about_the_recording() -> None:
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found(list_page([], 0))))
    assert (outcome.status, outcome.code, outcome.candidates) == (OutcomeStatus.FOUND, "search_exhausted", ())
    # rowCount says 3 but the page is empty: the index moved under us, so that is not an exhausted scope.
    odd = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found(list_page([], 3))))
    assert (odd.status, odd.code) == (OutcomeStatus.INCOMPLETE, "row_count_mismatch")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "code"),
    [
        ([list_page(range(20), 40), list_page(range(20, 30), 41)], "row_count_changed"),
        ([list_page(range(20), 40), list_page(range(20, 30), 40)], "row_count_mismatch"),
        ([list_page(range(20), 40), list_page([*range(20, 39), 0], 40)], "duplicate_items"),
    ],
)
async def test_inconsistent_paging_never_claims_exhaustion(pages: list[dict[str, Any]], code: str) -> None:
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(*(found(page) for page in pages)))
    assert (outcome.status, outcome.code) == (OutcomeStatus.INCOMPLETE, code)


@pytest.mark.asyncio
async def test_a_failed_later_page_keeps_the_candidates_already_read_as_incomplete() -> None:
    stopped = SourceRead(status=OutcomeStatus.UNAVAILABLE, code="transport_stopped", scope="search:x", retrieved_at=NOW)
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found(list_page(range(20), 40)), stopped))
    assert (outcome.status, outcome.code) == (OutcomeStatus.INCOMPLETE, "page_2_transport_stopped")
    assert len(outcome.candidates) == 20


@pytest.mark.asyncio
async def test_candidate_cap_and_read_budget_stop_paging_before_an_exception_loses_the_candidates() -> None:
    capped = await registry().discover(
        "trackid", CONTEXT, DiscoveryBudget(max_candidates=5), ScriptedEffects(found(list_page(range(20), 40)), found(list_page(range(20, 40), 40)))
    )
    assert (capped.status, capped.code, len(capped.candidates)) == (OutcomeStatus.INCOMPLETE, "candidate_cap", 5)
    first = found(list_page(range(20), 60), size=200_000)
    tight = await registry().discover("trackid", CONTEXT, DiscoveryBudget(max_bytes=262144), ScriptedEffects(first, found(list_page([], 60))))
    assert (tight.status, tight.code, len(tight.candidates)) == (OutcomeStatus.INCOMPLETE, "read_budget", 20)
    one_read = await registry().discover("trackid", CONTEXT, DiscoveryBudget(max_reads=1), ScriptedEffects(found(list_page(range(20), 40))))
    assert (one_read.status, len(one_read.candidates)) == (OutcomeStatus.INCOMPLETE, 20)


@pytest.mark.asyncio
async def test_candidates_carry_the_slug_and_no_revision_and_no_guessed_metadata() -> None:
    items = [{"slug": "set-slug-00", "id": 7, "title": "Example Title", "trackCount": 12, "status": 3}]
    outcome = await registry().discover(
        "trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found({"result": {"audiostreams": items, "rowCount": 1}}))
    )
    (candidate,) = outcome.candidates
    assert candidate.identity == SourceIdentity(provider_id="trackid", native_id="set-slug-00")
    # The list endpoint cannot see a reprocess, so a revision taken from it could never change.
    assert candidate.observed_revision is None
    assert candidate.display_name == "Example Title" and candidate.url is None
    assert any("trackCount=12" in line and "unverified" in line for line in candidate.evidence)
    assert not hasattr(candidate, "artist") and "Example Title" not in " ".join(candidate.evidence)


@pytest.mark.asyncio
async def test_items_without_a_usable_slug_make_the_scope_incomplete_not_silently_smaller() -> None:
    page = {"result": {"audiostreams": [{"slug": "ok-slug"}, {"slug": "../etc"}, {"slug": None}, "text", {"id": 1}], "rowCount": 5}}
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found(page)))
    assert (outcome.status, outcome.code) == (OutcomeStatus.INCOMPLETE, "unusable_items")
    assert [c.identity.native_id for c in outcome.candidates] == ["ok-slug"]
    assert "items_without_a_usable_slug=4" in outcome.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["not json", "[]", "{}", '{"result": {"audiostreams": {}, "rowCount": 1}}', '{"result": {"audiostreams": [], "rowCount": true}}']
)
async def test_an_unreadable_list_is_a_contract_error_never_an_empty_result(body: str) -> None:
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(found(body)))
    assert (outcome.status, outcome.code, outcome.candidates) == (OutcomeStatus.CONTRACT_ERROR, "invalid_list_json", ())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OutcomeStatus.ABSENT, OutcomeStatus.UNAVAILABLE, OutcomeStatus.RETRY, OutcomeStatus.CONTRACT_ERROR])
async def test_a_failed_list_read_is_never_absence_and_never_found(status: OutcomeStatus) -> None:
    read = SourceRead(status=status, code="http_404" if status == OutcomeStatus.ABSENT else "x", scope="search:x", retrieved_at=NOW)
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(read))
    assert outcome.status not in {OutcomeStatus.FOUND, OutcomeStatus.ABSENT} and not outcome.candidates


@pytest.mark.asyncio
async def test_a_foreign_cursor_is_refused() -> None:
    from phaze.tracklist_providers.domain import OpaqueCursor
    from phaze.tracklist_providers.registry import context_key

    cursor = OpaqueCursor(provider_id="trackid", context_key=context_key(CONTEXT), token="1")  # noqa: S106
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects(), cursor=cursor)
    assert (outcome.status, outcome.code) == (OutcomeStatus.CONTRACT_ERROR, "invalid_cursor")


# --- load ---------------------------------------------------------------------------------------------------------


async def load(read: SourceRead, candidate: ProviderCandidate = CANDIDATE) -> Any:
    return await registry().load(candidate, LoadBudget(), ScriptedEffects(read))


@pytest.mark.asyncio
async def test_load_interprets_the_detail_with_ordered_approximate_offsets_and_rights() -> None:
    document = detail_document([process(10, [row("00:00:05", title="First"), row("00:03:10", title="Second")])])
    outcome = await load(found(document, scope="set-slug-01"))
    assert (outcome.status, outcome.code) == (OutcomeStatus.FOUND, "parsed_trackid_detail")
    assert outcome.snapshot is not None
    assert [t.title for t in outcome.snapshot.tracks] == ["First", "Second"]
    assert all(t.timestamp and t.timestamp.offset_usability == "approximate" for t in outcome.snapshot.tracks)
    assert outcome.snapshot.revision is None
    assert any("highest detection process id=10" in line for line in outcome.snapshot.provenance)


@pytest.mark.asyncio
async def test_404_is_scoped_to_the_slug_and_never_recording_wide() -> None:
    gone = SourceRead(status=OutcomeStatus.ABSENT, code="http_404", scope="set-slug-01", retrieved_at=NOW)
    outcome = await load(gone)
    assert (outcome.status, outcome.code, outcome.scope, outcome.snapshot) == (OutcomeStatus.ABSENT, "stream_not_found", "set-slug-01", None)
    assert any("this stream slug only" in line for line in outcome.evidence)
    # An absence that did not come from a 404 is not believed.
    odd = await load(gone.model_copy(update={"code": "http_410"}))
    assert odd.status == OutcomeStatus.CONTRACT_ERROR
    # Another slug in the same lookup is unaffected: the registry mints per candidate.
    other = ProviderCandidate(identity=SourceIdentity(provider_id="trackid", native_id="set-slug-02"))
    effects = ScriptedEffects(found(detail_document([process(1, [row()])]), scope="set-slug-02"))
    assert (await registry().load(other, LoadBudget(), effects)).status == OutcomeStatus.FOUND
    assert effects.reads[0].identity.native_id == "set-slug-02"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (OutcomeStatus.UNAVAILABLE, "response_too_large"),
        (OutcomeStatus.UNAVAILABLE, "transport_stopped"),
        (OutcomeStatus.UNAVAILABLE, "run_budget_exhausted"),
        (OutcomeStatus.RETRY, "durable_budget_exhausted"),
        (OutcomeStatus.UNAVAILABLE, "robots_disallow"),
        (OutcomeStatus.UNAVAILABLE, "challenge"),
        (OutcomeStatus.UNAVAILABLE, "clock_discontinuity"),
        (OutcomeStatus.UNAVAILABLE, "ledger_corrupt"),
        (OutcomeStatus.RETRY, "timeout"),
        (OutcomeStatus.RETRY, "http_503"),
        (OutcomeStatus.UNAVAILABLE, "http_401"),
        (OutcomeStatus.CONTRACT_ERROR, "http_400"),
        (OutcomeStatus.UNSUPPORTED, "undecodable_response"),
    ],
)
async def test_no_transport_failure_becomes_empty_found(status: OutcomeStatus, code: str) -> None:
    outcome = await load(SourceRead(status=status, code=code, scope="set-slug-01", retrieved_at=NOW))
    assert (outcome.status, outcome.code) == (status, code)
    assert outcome.snapshot is None
    if code == "response_too_large":
        assert "says nothing about the tracklist" in outcome.detail


@pytest.mark.asyncio
async def test_no_exception_becomes_empty_found() -> None:
    class Exploding:
        def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> Any:
            del candidate, content, budget
            raise RuntimeError("boom")

    guarded = ProviderRegistry((TrackIdProvider(Exploding()),), remote_profiles=(TRACKID_PROFILE,))
    outcome = await guarded.load(CANDIDATE, LoadBudget(), ScriptedEffects(found(detail_document([process(1, [row()])]))))
    assert (outcome.status, outcome.code) == (OutcomeStatus.CONTRACT_ERROR, "invalid_output")
    for text in ("not json", "[]", "null", ""):
        bad = await load(found(text))
        assert bad.status == OutcomeStatus.CONTRACT_ERROR and bad.snapshot is None
    # A body-less "found" read is not a tracklist either.
    empty = await load(SourceRead(status=OutcomeStatus.FOUND, code="http_200", scope="set-slug-01", retrieved_at=NOW))
    assert (empty.status, empty.code) == (OutcomeStatus.CONTRACT_ERROR, "empty_body")


@pytest.mark.asyncio
async def test_a_ready_stream_with_no_rows_is_incomplete_never_found_empty() -> None:
    outcome = await load(found(detail_document([process(10, [])])))
    assert outcome.status == OutcomeStatus.INCOMPLETE and outcome.snapshot is not None and not outcome.snapshot.tracks
    assert outcome.snapshot.completeness.state == "incomplete"
    none_at_all = await load(found(detail_document([])))
    assert none_at_all.status == OutcomeStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_an_in_flight_reprocess_is_incomplete_and_says_so() -> None:
    document = detail_document([process(10, [row()]), process(11, [], end_date=None)])
    outcome = await load(found(document))
    assert outcome.status == OutcomeStatus.INCOMPLETE and outcome.snapshot is not None
    assert "reprocess" in outcome.detail and "still running" in outcome.detail
    assert "still in flight" in outcome.snapshot.completeness.reason
    assert len(outcome.snapshot.tracks) == 1


@pytest.mark.asyncio
async def test_a_truncated_read_is_never_promoted_to_a_complete_tracklist() -> None:
    read = found(detail_document([process(10, [row()])])).model_copy(update={"truncated": True})
    outcome = await load(read)
    assert (outcome.status, outcome.code) == (OutcomeStatus.INCOMPLETE, "read_truncated")
    assert outcome.snapshot is not None and outcome.snapshot.completeness.state == "incomplete"


@pytest.mark.asyncio
async def test_revision_change_between_discovery_and_load_is_retry() -> None:
    # Discovery leaves the revision unset, so a plain load never trips the check ...
    plain = await load(found(detail_document([process(10, [row()])])))
    assert plain.status == OutcomeStatus.FOUND
    # ... and a candidate that DID carry an observed revision differing from the loaded one is a RETRY, not a tracklist.
    observed = CANDIDATE.model_copy(update={"observed_revision": "process:9"})
    outcome = await load(found(detail_document([process(10, [row()])])), observed)
    assert (outcome.status, outcome.code) == (OutcomeStatus.RETRY, "revision_changed")


@pytest.mark.asyncio
async def test_rights_evidence_matches_the_recorded_operator_answer() -> None:
    # Literal copy of the tuple recorded on phaze-gyr3l (2026-10-12), kept independent of the module under test.
    recorded = (
        "Operator decision 2026-10-12 on phaze-gyr3l: automated use through the documented, authenticated TrackId Integration API "
        "and long-term storage of the returned snapshots are permitted.",
        "The TrackId terms text was not read and the API returns no licence data; this is not a provider licence verdict.",
        "Redistribution, export, tag-writing and CUE-writing of the stored data are not addressed by this decision.",
    )
    assert recorded == RIGHTS_EVIDENCE
    complete = await load(found(detail_document([process(10, [row()])])))
    incomplete = await load(found(detail_document([process(10, [])])))
    for outcome in (complete, incomplete):
        assert outcome.snapshot is not None and outcome.snapshot.rights_evidence == recorded


# --- the native-id pattern ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("identity", ["a" * 4095 + "!", "a" * 4096, "a-" * 2048, "a." * 2047 + "~!", "a" * 255 + "!"])
def test_the_native_id_pattern_is_linear_on_an_adversarial_identity(identity: str) -> None:
    fetch = TRACKID_PROFILE.fetch
    assert fetch is not None
    for _ in range(5):
        started = time.perf_counter()
        assert fetch.accepts(identity) is False
        assert time.perf_counter() - started < 0.05
    # The same identity through the registry is refused before any effect runs.


@pytest.mark.asyncio
async def test_a_4096_character_identity_is_refused_by_the_registry_before_any_read() -> None:
    effects = ScriptedEffects()
    hostile = ProviderCandidate(identity=SourceIdentity(provider_id="trackid", native_id="a" * 4095 + "!"))
    started = time.perf_counter()
    outcome = await registry().load(hostile, LoadBudget(), effects)
    assert time.perf_counter() - started < 1.0
    assert (outcome.status, outcome.code) == (OutcomeStatus.CONTRACT_ERROR, "unauthorized_identity") and effects.reads == []


@pytest.mark.parametrize(
    ("slug", "accepted"),
    [
        ("set-slug-01", True),
        ("A1._~-", True),
        ("a" * 255, True),
        ("a" * 256, False),
        (".", False),
        ("..", False),
        ("-a", False),
        ("a/b", False),
        ("a b", False),
        ("a?b", False),
        ("", False),
    ],
)
def test_the_native_id_pattern_accepts_slugs_and_nothing_that_could_leave_the_segment(slug: str, accepted: bool) -> None:
    fetch = TRACKID_PROFILE.fetch
    assert fetch is not None and fetch.accepts(slug) is accepted


# --- profile, pacing and credentials ------------------------------------------------------------------------------


def test_the_profile_is_pinned_to_the_documented_api_and_never_looser_than_the_effect_defaults() -> None:
    from phaze.tracklist_providers import host_profile as hp

    assert TRACKID_PROFILE.host == "trackid.net" and TRACKID_PROFILE.robots == "not_applicable"
    assert TRACKID_PROFILE.min_gap_seconds >= hp.DEFAULT_MIN_GAP_SECONDS
    assert TRACKID_PROFILE.per_run_budget <= hp.DEFAULT_PER_RUN_BUDGET and TRACKID_PROFILE.durable_budget <= hp.DEFAULT_DURABLE_BUDGET
    assert TRACKID_PROFILE.max_response_bytes <= hp.MAX_RESPONSE_BYTES == 262144
    assert TRACKID_PROFILE.credential is not None and (TRACKID_PROFILE.credential.header, TRACKID_PROFILE.credential.prefix) == (
        "Authorization",
        "Bearer ",
    )
    search = TRACKID_PROFILE.search
    assert search is not None and search.page_parameter == "currentPage" and search.page_origin == 0
    assert dict(search.fixed_parameters) == {"status": "Ready", "pageSize": str(PAGE_SIZE)} and PAGE_SIZE <= 100
    assert search.max_page * PAGE_SIZE <= DiscoveryBudget().max_candidates


SECRET = "synthetic-secret-8d41"
CLIENT = "tkid_synthetic01"


@pytest.mark.parametrize("bad", ["no-separator-value", ":only-secret", "only-id:", "a:b:c", "has space:x", "id:se cret", "id:sec\nret"])
def test_a_malformed_credential_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        check_trackid_credential(bad)
    message = str(excinfo.value)
    assert "PHAZE_PROVIDER_TRACKID_CLIENT_CREDENTIALS" in message
    assert all(part not in message for part in bad.replace("\n", ":").split(":") if len(part) > 3)


def test_a_well_formed_credential_is_accepted() -> None:
    check_trackid_credential(f"{CLIENT}:{SECRET}")


@pytest.mark.asyncio
async def test_a_disabled_provider_is_listed_and_refuses_both_calls() -> None:
    disabled = ProviderRegistry((TrackIdProvider(),), disabled=frozenset({"trackid"}), remote_profiles=(TRACKID_PROFILE,))
    assert [d.id for d in disabled.descriptors()] == ["trackid"]
    discovery = await disabled.discover("trackid", CONTEXT, DiscoveryBudget(), ScriptedEffects())
    loaded = await disabled.load(CANDIDATE, LoadBudget(), ScriptedEffects())
    assert (discovery.status, discovery.code) == (OutcomeStatus.UNAVAILABLE, "disabled_provider")
    assert (loaded.status, loaded.code) == (OutcomeStatus.UNAVAILABLE, "disabled_provider")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_count", "pages", "status"),
    [
        (0, 1, "found"),
        (1, 1, "found"),
        (20, 1, "found"),
        (21, 2, "found"),
        (40, 2, "found"),
        (41, 3, "found"),
        (60, 3, "found"),
        (61, 3, "incomplete"),
    ],
)
async def test_exhaustion_boundary_at_the_page_size(row_count: int, pages: int, status: str) -> None:
    script = [found(list_page(range(n * PAGE_SIZE, min(row_count, (n + 1) * PAGE_SIZE)), row_count)) for n in range(pages)]
    effects = ScriptedEffects(*script)
    outcome = await registry().discover("trackid", CONTEXT, DiscoveryBudget(), effects)
    assert outcome.status == status and len(effects.reads) == pages and not effects.script
    assert len(outcome.candidates) == min(row_count, 60) and outcome.cursor is None
