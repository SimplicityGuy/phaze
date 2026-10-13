"""MixesDB adapter: REST search and REST page fetch only, through core-minted references. No I/O here.

Measured basis: docs/spikes/phaze-6ehg6-mixesdb-rest-access.md (routes, page JSON, revision fields),
docs/spikes/phaze-s19tp-mixesdb-search-semantics.md (body matches, non-tracklist namespaces, capped search,
the artist-only fallback that cost 118 requests for zero new matches) and
docs/spikes/phaze-ue40z-mixesdb-slash-key-pagination-conditional.md (slash keys, pagination, conditional
requests). Access scope (epic phaze-ypcx6, 2026-10-08): REST and the bare Action help endpoint only, robots
respected; this adapter needs neither the help endpoint nor any Action call.

- **Discovery** reads the one search the registry minted from ``RecordingContext`` facts. It never builds a
  second, narrower query: an artist-only fallback is not a default. The documented search returns at most
  ``limit`` records with no total, cursor or continuation, so every discovery is ``incomplete`` with no
  cursor, whatever the count. Non-tracklist namespaces are evidence, never candidates, and a candidate is a
  native key to review, never an identity claim about the recording.
- **Load** fetches the page by the search-returned key, verbatim. A key containing a slash cannot be served
  by the documented page route (probe phaze-ue40z: percent-encoded, an edge HTML 404; literal, a REST
  ``rest-no-match``), so it is reported ``unavailable`` with that evidence and no request is spent: never
  ``absent``, and no encoding, category or HTML workaround. A 404 on an ordinary key is absent for that page
  only. Conditional requests are never sent (docs/design/0025-remote-provider-sources.md, 2026-10-12
  amendment).
"""

import json
import re
from urllib.parse import quote

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
    SourceIdentity,
    SourceRead,
)
from phaze.tracklist_providers.host_profile import RemoteFetchRoute, RemoteHostProfile, RemoteSearchRoute
from phaze.tracklist_providers.mixesdb_parser import PROVIDER_ID, MixesDbInterpreter, mixesdb_revision
from phaze.tracklist_providers.port import AuthorizedEffects


HOST = "www.mixesdb.com"
SEARCH_LIMIT = 100
SEARCH_FORMAT = "mixesdb_rest_search_json"
PAGE_FORMAT = "mixesdb_rest_page_json"
USER_AGENT = "phaze-tracklist-lookup/2026.10 (+https://github.com/SimplicityGuy/phaze)"
SLASH_KEY_PROBE = "docs/spikes/phaze-ue40z-mixesdb-slash-key-pagination-conditional.md"
# Linear by construction: one negated character class with a bounded repeat, anchored by fullmatch. The
# excluded characters are those MediaWiki forbids in a title, plus space (a key spells it as an underscore).
NATIVE_KEY_PATTERN = r"[^\x00-\x20\x7f#<>\[\]|{}]{1,255}"

# Operator record, 2026-10-12, bead phaze-gyr3l (selected option "Permitted, private use only (Recommended)").
RIGHTS_EVIDENCE: tuple[str, ...] = (
    "Source statement: MixesDB:Legal_stuff, read 2026-10-12 through the REST page route: community tracklists are released under "
    "Creative Commons Attribution-Share Alike 3.0 United States 'if not stated otherwise'.",
    "Operator decision 2026-10-12 on phaze-gyr3l: automated reads through the REST search and page routes and long-term private "
    "storage by the single operator are permitted.",
    "Redistribution or sharing of stored tracklists is not decided and stays gated (share-alike); page URL, native key and revision "
    "are retained for attribution.",
    "Per-page licence exceptions are invisible to the import; the REST licence object is empty; the legal page may be revised.",
)

# MediaWiki's canonical and MixesDB's project namespaces. Main-namespace pages are the tracklists.
_NON_TRACKLIST_NAMESPACES = frozenset(
    {
        "category",
        "file",
        "help",
        "image",
        "media",
        "mediawiki",
        "mixesdb",
        "module",
        "project",
        "special",
        "talk",
        "template",
        "user",
    }
)
_NAMESPACE = re.compile(r"([^:]{1,64}):")
_TAG = re.compile(r"<[^<>]*>")
_EXCERPT = 512
_LISTED = 32

_FETCH = RemoteFetchRoute(native_id_pattern=NATIVE_KEY_PATTERN, format=PAGE_FORMAT, path_template="/w/rest.php/v1/page/{native_id}")
PROFILE = RemoteHostProfile(
    provider_id=PROVIDER_ID,
    host=HOST,
    user_agent=USER_AGENT,
    search=RemoteSearchRoute(
        fields=frozenset({"artist", "event", "date"}),
        format=SEARCH_FORMAT,
        path="/w/rest.php/v1/search/page",
        term_parameters=(("artist", "q"), ("event", "q"), ("date", "q")),
        fixed_parameters=(("limit", str(SEARCH_LIMIT)),),
    ),
    fetch=_FETCH,
    # A lookup is robots + one search; a load is one page (robots is cached for a day).
    per_run_budget=4,
    robots="respect",
)


def page_url(key: str) -> str:
    """The page's public address, for attribution only. It is never fetched."""
    return f"https://{HOST}/w/{quote(key, safe='/:')}"


def _namespace(key: str) -> str | None:
    match = _NAMESPACE.match(key)
    if match is None:
        return None
    name = match.group(1).replace("_", " ").strip().casefold()
    base = name.removesuffix(" talk")
    return name if name in _NON_TRACKLIST_NAMESPACES or base in _NON_TRACKLIST_NAMESPACES else None


def _revision(latest: object) -> str | None:
    """``latest.id`` and ``latest.timestamp`` as the interpreter's opaque revision; None when either is missing."""
    revision_id = latest.get("id") if isinstance(latest, dict) else None
    timestamp = latest.get("timestamp") if isinstance(latest, dict) else None
    if isinstance(revision_id, int) and not isinstance(revision_id, bool) and isinstance(timestamp, str) and timestamp:
        return mixesdb_revision(revision_id, timestamp)
    return None


class MixesDbProvider:
    descriptor = ProviderDescriptor(
        id=PROVIDER_ID,
        display_name="MixesDB",
        capabilities=frozenset(
            {
                Capability.LOAD,
                Capability.DISCOVERY,
                Capability.REMOTE_TRANSPORT,
                Capability.REVISION_EVIDENCE,
                Capability.TRACK_PARSING,
                Capability.TIMESTAMP_EVIDENCE,
            }
        ),
    )

    def __init__(self, interpreter: MixesDbInterpreter | None = None) -> None:
        # No offset origin is evidenced by a search or a page, so cues stay unusable (the interpreter's default).
        self.interpreter = interpreter or MixesDbInterpreter(rights_evidence=RIGHTS_EVIDENCE)

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        scope = context.recording_id
        if cursor is not None:
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="cursor_unsupported", scope=scope)
        skipped = tuple(f"skipped_overlong_fact={field}" for field in effects.skipped_terms)
        searches = [(reference, reference.request) for reference in effects.minted if reference.request and reference.request.kind == "search"]
        if not searches:
            reason = "No artist, event or date fact was usable for a search, so nothing was searched"
            return DiscoveryOutcome(
                status=OutcomeStatus.INCOMPLETE,
                code="no_search_facts",
                scope=scope,
                evidence=skipped,
                completeness=Completeness(state="incomplete", reason=reason, evidence=skipped),
            )
        search, request = searches[0]
        read = await effects.read(search, budget)
        if read.status != OutcomeStatus.FOUND or read.text is None:
            # A failed search says nothing about any tracklist; a 404 on the search route is not absence.
            status = (
                read.status
                if read.status in {OutcomeStatus.RETRY, OutcomeStatus.UNSUPPORTED, OutcomeStatus.CONTRACT_ERROR}
                else OutcomeStatus.UNAVAILABLE
            )
            return DiscoveryOutcome(status=status, code=read.code, scope=scope, evidence=read.evidence)
        pages = _search_pages(read.text)
        if pages is None:
            return DiscoveryOutcome(status=OutcomeStatus.UNSUPPORTED, code="unrecognized_search_response", scope=scope, evidence=read.evidence)
        terms = request.terms
        candidates: list[ProviderCandidate] = []
        other_namespaces: list[str] = []
        unloadable = namespaced = unexamined = 0
        for rank, item in enumerate(pages, 1):
            if len(candidates) >= budget.max_candidates:
                # Bounded work whatever the server sends: the rest is counted, never interpreted.
                unexamined = len(pages) - rank + 1
                break
            entry: dict[str, object] = item if isinstance(item, dict) else {}
            key = entry.get("key")
            if not isinstance(key, str) or not _FETCH.accepts(key) or key in {".", ".."}:
                unloadable += 1
                continue
            if _namespace(key) is not None:
                namespaced += 1
                if len(other_namespaces) < _LISTED:
                    other_namespaces.append(key)
                continue
            title = entry.get("title")
            title = title if isinstance(title, str) and 0 < len(title) <= 4096 else None
            folded = (title or key.replace("_", " ")).casefold()
            presence = [f"title_{term.field}={'present' if term.value.casefold() in folded else 'absent'}" for term in terms]
            evidence = [f"search_rank={rank}", *presence]
            if any(value.endswith("=absent") for value in presence):
                evidence.append("match_basis=body_or_fuzzy")
            page_id = entry.get("id")
            if isinstance(page_id, int) and not isinstance(page_id, bool):
                evidence.append(f"page_id={page_id}")
            if "/" in key:
                evidence.append("slash_key_route_unproven")
            excerpt = entry.get("excerpt")
            if isinstance(excerpt, str) and excerpt:
                evidence.append("search_excerpt:" + _TAG.sub("", excerpt[: _EXCERPT * 4])[:_EXCERPT])
            candidates.append(
                ProviderCandidate(
                    identity=SourceIdentity(provider_id=PROVIDER_ID, native_id=key),
                    # Search records were observed without ``latest``; a revision is observed only when one is carried.
                    observed_revision=_revision(entry.get("latest")),
                    url=page_url(key),
                    display_name=title,
                    evidence=tuple(evidence),
                )
            )
        evidence = [
            f"search_limit={SEARCH_LIMIT}",
            f"search_returned={len(pages)}",
            "query_fields=" + ",".join(term.field for term in terms),
            *skipped,
        ]
        if namespaced:
            evidence.append(f"non_tracklist_namespace_count={namespaced}")
            evidence.extend(f"non_tracklist_namespace={key}" for key in other_namespaces)
        if unloadable:
            evidence.append(f"unloadable_record_count={unloadable}")
        if unexamined:
            evidence.extend((f"candidate_cap={budget.max_candidates}", f"records_not_examined={unexamined}"))
        if len(pages) >= SEARCH_LIMIT:
            code = "search_at_cap"
            reason = (
                f"Search returned {len(pages)} records at its limit of {SEARCH_LIMIT}; the documented search has no total, cursor or continuation"
            )
        else:
            code = "search_exhaustion_unknown"
            reason = (
                f"Search returned {len(pages)} records under its limit of {SEARCH_LIMIT}; with no total or exhaustion evidence, that proves nothing"
            )
        return DiscoveryOutcome(
            status=OutcomeStatus.INCOMPLETE,
            code=code,
            scope=scope,
            evidence=tuple(evidence[:64]),
            candidates=tuple(candidates),
            completeness=Completeness(state="incomplete", reason=reason, evidence=tuple(evidence[:64])),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        key = candidate.identity.native_id
        url = page_url(key)
        if "/" in key:
            return LoadOutcome(
                status=OutcomeStatus.UNAVAILABLE,
                code="slash_key_route_unproven",
                scope=key,
                detail="The documented page route cannot serve a key containing a slash; the page is not known to be absent",
                evidence=(
                    f"native_key:{key}",
                    f"page_url:{url}",
                    "observed:percent_encoded_slash=http_404_edge_html",
                    "observed:literal_slash=http_404_rest_no_match",
                    f"probe:{SLASH_KEY_PROBE}",
                    "no_request_sent",
                ),
            )
        fetches = [reference for reference in effects.minted if reference.request is not None and reference.request.kind == "fetch"]
        if not fetches:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="fetch_not_minted", scope=key)
        read = await effects.read(fetches[0], budget)
        if read.status != OutcomeStatus.FOUND or read.text is None:
            # A 404 here is absent for this page only, scoped to its key.
            return LoadOutcome(status=read.status, code=read.code, scope=key, evidence=read.evidence)
        page = _page(read, key)
        if isinstance(page, LoadOutcome):
            return page
        if candidate.observed_revision is not None and page.revision != candidate.observed_revision:
            return LoadOutcome(
                status=OutcomeStatus.RETRY,
                code="revision_changed",
                scope=key,
                evidence=(f"observed_revision={candidate.observed_revision}", f"loaded_revision={page.revision or 'unknown'}"),
            )
        return self.interpreter.interpret(candidate.model_copy(update={"url": url}), page, budget)


def _search_pages(text: str) -> list[object] | None:
    try:
        body = json.loads(text)
    except ValueError:
        return None
    pages = body.get("pages") if isinstance(body, dict) else None
    return pages if isinstance(pages, list) else None


def _page(read: SourceRead, key: str) -> SourceRead | LoadOutcome:
    """The page JSON as a wikitext read, carrying its revision; or the typed reason it is not one."""
    try:
        body = json.loads(read.text or "")
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("source"), str):
        return LoadOutcome(status=OutcomeStatus.UNSUPPORTED, code="unrecognized_page_response", scope=key, evidence=read.evidence)
    if body.get("key") != key:
        returned = body.get("key")
        mismatch = (*read.evidence, f"requested_key:{key}", f"returned_key:{returned if isinstance(returned, str) else 'missing'}"[:4096])
        return LoadOutcome(status=OutcomeStatus.UNAVAILABLE, code="page_key_mismatch", scope=key, evidence=mismatch)
    if body.get("content_model") != "wikitext":
        return LoadOutcome(status=OutcomeStatus.UNSUPPORTED, code="unsupported_content_model", scope=key, evidence=read.evidence)
    revision = _revision(body.get("latest"))
    evidence = [*read.evidence, f"revision={revision}"[:4096] if revision else "revision_unknown"]
    page_id = body.get("id")
    if isinstance(page_id, int) and not isinstance(page_id, bool):
        evidence.append(f"page_id={page_id}")
    licence = body.get("license")
    if isinstance(licence, dict) and any(isinstance(value, str) and value for value in licence.values()):
        evidence.append(f"rest_license={json.dumps(licence, sort_keys=True)}"[:512])
    else:
        evidence.append("rest_license=empty")
    values = {"text": body["source"], "encoding": "utf-8", "revision": revision, "revision_scope": "full" if revision else "unknown"}
    return SourceRead.model_validate({**read.model_dump(), **values, "evidence": tuple(evidence[:64])})
