"""TrackId.net provider: keyword discovery and detail load over the documented Integration API.

The provider does no I/O. Core owns the transport (``phaze.services.remote_transport``): it mints the search and
fetch references from the :data:`TRACKID_PROFILE` declared here, paces and budgets every request and holds the
credential. Importing this module registers nothing, reads no environment and opens no connection; the
composition root registers it explicitly through its binding in ``phaze.services.remote_providers``.

Decisions recorded for this adapter (implementer's decisions unless a record is cited):

* **Native ID is the slug.** The detail route is ``/audiostreams/{slug}`` and accepts nothing else, so the numeric id
  could never be fetched. Whether a title edit changes a slug is not measured. If it does, a stored slug goes stale and
  its fetch returns 404, which is scoped to that slug only and is never a statement about the recording.
* **Search sends artist and event facts only.** The date fact is left out of the query: whether TrackId keyword
  search matches an ISO date at all is unmeasured, and an unmatched term would narrow every query to nothing. The
  date stays available to the matching-evidence step. This is unmeasured, not a finding.
* **Page size 20, at most 3 pages.** At most 60 candidates and 3 list requests per discovery, inside the default
  candidate cap of 64. The API's first page is page 0 (``page_origin=0``).
* **Pacing is the effect's default and is never loosened here.** Gap 6 s, 16 requests per run, 60 per rolling hour,
  262144 response bytes: the operator asked not to inundate their servers
  (docs/design/tracklist-provider-trackid.md). A settings override may only raise the gap or lower the budget.
* **robots is not applicable**: the host is reached only through its documented, authenticated API routes.
* **Rights** are the operator decision recorded on phaze-gyr3l (2026-10-12), restated verbatim in
  :data:`RIGHTS_EVIDENCE`. It is not a licence verdict from TrackId.
"""

import json
import re
from typing import Any

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
from phaze.tracklist_providers.host_profile import (
    DEFAULT_DURABLE_BUDGET,
    DEFAULT_MIN_GAP_SECONDS,
    DEFAULT_PER_RUN_BUDGET,
    MAX_RESPONSE_BYTES,
    CredentialSlot,
    RemoteFetchRoute,
    RemoteHostProfile,
    RemoteSearchRoute,
)
from phaze.tracklist_providers.port import AuthorizedEffects, SourceInterpreter
from phaze.tracklist_providers.trackid_interpreter import SOURCE_FORMAT, TrackIdInterpreter


PROVIDER_ID = "trackid"
CREDENTIAL_NAME = "client_credentials"
HOST = "trackid.net"
LIST_FORMAT = "trackid_audiostream_list_json"
DETAIL_FORMAT = SOURCE_FORMAT
PAGE_SIZE = 20
MAX_PAGES = 3
# One bounded class repeated a bounded number of times, anchored by fullmatch: linear in the identity's length.
SLUG_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._~-]{0,254}"
_SLUG = re.compile(SLUG_PATTERN)
_CREDENTIAL_VALUE = re.compile(r"[\x21-\x7e]+")
_RETRY_AFTER = re.compile(r"retry_after_seconds=([0-9.]+)")

RIGHTS_EVIDENCE = (
    "Operator decision 2026-10-12 on phaze-gyr3l: automated use through the documented, authenticated TrackId Integration API "
    "and long-term storage of the returned snapshots are permitted.",
    "The TrackId terms text was not read and the API returns no licence data; this is not a provider licence verdict.",
    "Redistribution, export, tag-writing and CUE-writing of the stored data are not addressed by this decision.",
)


def _profile() -> RemoteHostProfile:
    return RemoteHostProfile(
        provider_id=PROVIDER_ID,
        host=HOST,
        user_agent="phaze-tracklist-lookup/1 (+https://github.com/SimplicityGuy/phaze)",
        search=RemoteSearchRoute(
            fields=frozenset({"artist", "event"}),
            format=LIST_FORMAT,
            max_page=MAX_PAGES,
            path="/api/integration/audiostreams",
            term_parameters=(("artist", "keywords"), ("event", "keywords")),
            page_parameter="currentPage",
            page_origin=0,
            fixed_parameters=(("status", "Ready"), ("pageSize", str(PAGE_SIZE))),
        ),
        fetch=RemoteFetchRoute(native_id_pattern=SLUG_PATTERN, format=DETAIL_FORMAT, path_template="/api/integration/audiostreams/{native_id}"),
        min_gap_seconds=DEFAULT_MIN_GAP_SECONDS,
        per_run_budget=DEFAULT_PER_RUN_BUDGET,
        durable_budget=DEFAULT_DURABLE_BUDGET,
        max_response_bytes=MAX_RESPONSE_BYTES,
        credential=CredentialSlot(header="Authorization", prefix="Bearer "),
        robots="not_applicable",
    )


TRACKID_PROFILE = _profile()


def check_trackid_credential(value: str) -> None:
    """Reject anything but ``<client id>:<client secret>`` in printable ASCII, naming the setting and never the value.

    The setting holds the API's own Authorization format; the transport's credential slot adds ``Bearer ``.
    """
    client_id, separator, client_secret = value.partition(":")
    if not separator or not client_id or not client_secret or ":" in client_secret or _CREDENTIAL_VALUE.fullmatch(value) is None:
        raise ValueError("PHAZE_PROVIDER_TRACKID_CLIENT_CREDENTIALS must be '<client id>:<client secret>' in printable ASCII without spaces")


def _page_items(text: str | None) -> tuple[list[Any], int] | None:
    """The list response's audiostreams and rowCount, or None when it is not that shape."""
    try:
        document = json.loads(text or "")
    except ValueError:
        return None
    result = document.get("result") if isinstance(document, dict) else None
    items = result.get("audiostreams") if isinstance(result, dict) else None
    count = result.get("rowCount") if isinstance(result, dict) else None
    if not isinstance(items, list) or not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return items, count


def _retry_seconds(read: SourceRead) -> float | None:
    for line in read.evidence:
        if (match := _RETRY_AFTER.fullmatch(line)) is not None:
            return min(float(match[1]), 3600.0)
    return None


class TrackIdProvider:
    descriptor = ProviderDescriptor(
        id=PROVIDER_ID,
        display_name="TrackId.net",
        capabilities=frozenset(
            {
                Capability.LOAD,
                Capability.DISCOVERY,
                Capability.REVISION_EVIDENCE,
                Capability.TRACK_PARSING,
                Capability.TIMESTAMP_EVIDENCE,
                Capability.REMOTE_TRANSPORT,
            }
        ),
    )

    def __init__(self, interpreter: SourceInterpreter | None = None) -> None:
        self.interpreter: SourceInterpreter = interpreter or TrackIdInterpreter()

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        scope = context.recording_id
        if cursor is not None:
            # This provider never issues a cursor, so any cursor is foreign.
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_cursor", scope=scope)
        if not effects.minted:
            skipped = f" Fields left out for length: {', '.join(effects.skipped_terms)}." if effects.skipped_terms else ""
            return DiscoveryOutcome(
                status=OutcomeStatus.UNSUPPORTED,
                code="no_search_hint",
                scope=scope,
                detail=f"No known or inferred artist or event fact is available to search with; no request was made.{skipped}",
            )
        search = effects.minted[0]
        request = search.request
        if request is None:  # pragma: no cover - a minted search always carries its request
            return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_search_request", scope=scope)
        candidates: list[ProviderCandidate] = []
        seen: set[str] = set()
        raw_total = unusable = duplicates = pages_read = 0
        row_count: int | None = None
        stop: str | None = None
        used_bytes = used_characters = used_lines = 0
        last = (0, 0, 0)
        for page in range(1, min(MAX_PAGES, budget.max_reads) + 1):
            if page > 1:
                if len(candidates) >= budget.max_candidates:
                    stop = "candidate_cap"
                elif (
                    used_bytes + last[0] > budget.max_bytes
                    or used_characters + last[1] > budget.max_characters
                    or used_lines + last[2] > budget.max_lines
                ):
                    stop = "read_budget"
                if stop:
                    break
            reference = search if page == 1 else search.model_copy(update={"request": request.model_copy(update={"page": page})})
            read = await effects.read(reference, budget)
            if read.status != OutcomeStatus.FOUND or read.text is None:
                if page == 1:
                    return self._failure(read, scope)
                stop = f"page_{page}_{read.code}"
                break
            parsed = _page_items(read.text)
            if parsed is None:
                if page == 1:
                    return DiscoveryOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_list_json", scope=scope)
                stop = f"page_{page}_invalid_list_json"
                break
            items, count = parsed
            if row_count is not None and count != row_count:
                stop = "row_count_changed"
                break
            row_count = count
            pages_read += 1
            raw_total += len(items)
            last = (read.bytes_read, len(read.text), len(read.text.splitlines()))
            used_bytes, used_characters, used_lines = used_bytes + last[0], used_characters + last[1], used_lines + last[2]
            for item in items:
                slug = item.get("slug") if isinstance(item, dict) else None
                if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
                    unusable += 1
                elif slug in seen:
                    duplicates += 1
                else:
                    seen.add(slug)
                    candidates.append(self._candidate(slug, item))
            # A short page, or pages that already cover rowCount, end the enumeration: a further request buys nothing.
            if len(items) < PAGE_SIZE or pages_read * PAGE_SIZE >= count:
                break
        return self._outcome(scope, candidates, budget, row_count, pages_read, raw_total, unusable, duplicates, stop, effects.skipped_terms)

    @staticmethod
    def _candidate(slug: str, item: dict[str, Any]) -> ProviderCandidate:
        title = item.get("title")
        hint = item.get("trackCount")
        evidence = ["Matched by keyword search; the relation to the local recording is unverified"]
        if isinstance(hint, int) and not isinstance(hint, bool):
            evidence.append(f"list trackCount={hint} (sum over detection processes; an unverified hint)")
        return ProviderCandidate(
            identity=SourceIdentity(provider_id=PROVIDER_ID, native_id=slug),
            # The list endpoint shows only the original detection process, so it can never reveal a reprocess:
            # a list-derived revision would never change. Revision evidence is taken from the detail at load.
            observed_revision=None,
            display_name=title.strip()[:4096] or None if isinstance(title, str) else None,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _failure(read: SourceRead, scope: str) -> DiscoveryOutcome:
        # A 404 on the list route says nothing about any stream, so it is never "absent".
        status = OutcomeStatus.UNAVAILABLE if read.status in {OutcomeStatus.ABSENT, OutcomeStatus.FOUND} else read.status
        return DiscoveryOutcome(status=status, code=read.code, scope=scope, evidence=read.evidence)

    @staticmethod
    def _outcome(
        scope: str,
        candidates: list[ProviderCandidate],
        budget: DiscoveryBudget,
        row_count: int | None,
        pages_read: int,
        raw_total: int,
        unusable: int,
        duplicates: int,
        stop: str | None,
        skipped_terms: tuple[str, ...],
    ) -> DiscoveryOutcome:
        reason = stop
        if reason is None and len(candidates) > budget.max_candidates:
            reason = "candidate_cap"
        elif reason is None and row_count is not None and row_count > pages_read * PAGE_SIZE:
            reason = "page_cap"
        elif reason is None and row_count is not None and raw_total != row_count:
            reason = "row_count_mismatch"
        elif reason is None and unusable:
            reason = "unusable_items"
        elif reason is None and duplicates:
            reason = "duplicate_items"
        evidence = [f"rowCount={row_count}", f"pages_read={pages_read}", f"items_returned={raw_total}"]
        if unusable:
            evidence.append(f"items_without_a_usable_slug={unusable}")
        if duplicates:
            evidence.append(f"duplicate_items={duplicates}")
        if skipped_terms:
            evidence.append(f"fields_left_out_for_length={','.join(skipped_terms)}")
        shown = tuple(candidates[: budget.max_candidates])
        if reason is None and row_count is not None:
            return DiscoveryOutcome(
                status=OutcomeStatus.FOUND,
                code="search_exhausted",
                scope=scope,
                candidates=shown,
                evidence=tuple(evidence),
                completeness=Completeness(state="complete", reason="Every result page for this keyword query was read", evidence=tuple(evidence)),
            )
        reason = reason or "row_count_unknown"
        return DiscoveryOutcome(
            status=OutcomeStatus.INCOMPLETE,
            code=reason,
            scope=scope,
            candidates=shown,
            evidence=tuple(evidence),
            completeness=Completeness(
                state="incomplete",
                reason=f"Exhaustion of the keyword results is not demonstrated ({reason}); absence is not implied",
                evidence=tuple(evidence),
            ),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        scope = candidate.identity.native_id
        if not effects.minted:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_fetch_reference", scope=scope)
        read = await effects.read(effects.minted[0], budget)
        if read.status == OutcomeStatus.ABSENT:
            if read.code != "http_404":
                return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="unexpected_absent", scope=scope, evidence=read.evidence)
            return LoadOutcome(
                status=OutcomeStatus.ABSENT,
                code="stream_not_found",
                scope=scope,
                evidence=(*read.evidence, "Not found for this stream slug only; no claim about the recording or any other stream"),
            )
        if read.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE} or read.text is None:
            if read.status == OutcomeStatus.FOUND:
                return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="empty_body", scope=scope, evidence=read.evidence)
            detail = ""
            if read.code == "response_too_large":
                detail = f"The detail response exceeded {MAX_RESPONSE_BYTES} bytes and was not read; this says nothing about the tracklist"
            return LoadOutcome(
                status=read.status, code=read.code, scope=scope, detail=detail, evidence=read.evidence, retry_after_seconds=_retry_seconds(read)
            )
        result = self.interpreter.interpret(candidate, read, budget)
        snapshot = result.snapshot
        if snapshot is None:
            return result
        update: dict[str, Any] = {"rights_evidence": RIGHTS_EVIDENCE}
        status, code, detail = result.status, result.code, result.detail
        if read.truncated or read.status == OutcomeStatus.INCOMPLETE:
            update["completeness"] = Completeness(state="incomplete", reason="read_truncated")
            status, code = OutcomeStatus.INCOMPLETE, "read_truncated"
        elif snapshot.completeness.state == "incomplete" and any("still in flight" in line for line in snapshot.completeness.evidence):
            detail = "A reprocess of this stream is still running upstream; the tracklist may change. Review it as incomplete evidence."
        return LoadOutcome(status=status, code=code, scope=result.scope, detail=detail, snapshot=snapshot.model_copy(update=update))
