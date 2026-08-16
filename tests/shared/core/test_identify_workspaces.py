"""Behavioral tests for the v7.0 Identify workspaces (Phase 59, IDENT-01/IDENT-02 + R-5 / WORK-05).

This is the single Phase-59 test file (Wave 0, Plan 59-01 / 59-VALIDATION.md). It mirrors the
Phase-58 ``tests/test_enrich_analyze_workspaces.py`` model and defines the full Phase-59 test
surface up front:

* The two **foundation** tests are FILLED here (they pass against the ``_STAGE_PLACEHOLDER``
  fragments today and guard the contract Plans 02/03 must preserve):
    - ``test_identify_fragments_are_bare``       -> R-5    (the ``/s/tracklist`` HX response is a
      bare fragment -- no ``<html>``/``<head>`` document wrapper).
    - ``test_identify_single_poll_discipline``   -> WORK-05 / R-2 (the shell fires EXACTLY ONE
      ``/pipeline/stats`` poll; neither new fragment starts a second ``hx-trigger="every"`` /
      ``setInterval`` loop).

* The **workspace** behavior tests were ``xfail`` stubs converted to real assertions by their
  owning plan/task (Plan 59-03):
    - ``test_tracklist_step_cards_and_triggers`` -> IDENT-02 (Plan 59-03)
    - ``test_tracklist_per_set_coverage``        -> IDENT-02 (Plan 59-03)

  phaze-0jpe removed the Track-ID workspace along with audio fingerprinting -- its whole purpose was
  per-engine audfprint/panako match status -- so its two tests (``test_trackid_table_signals`` /
  ``test_trackid_success_renders_done``) are gone with it. The paging- and sort-contract wiring they
  shared with the Tracklist surface is still covered below, exercised through
  ``/pipeline/tracklist-sets``.

The module-level ``_seed_*`` helpers below are test fixtures (ORM inserts only -- never a backend
change). Plan 59-03 uses them to seed the tracklist rows the workspace renders. They live here (not conftest) because they are Phase-59-specific shapes; ``conftest.py``
already seeds the legacy agent so a bare ``FileRecord`` satisfies its NOT NULL + FK ``agent_id``
default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from phaze.services.pipeline import (
    _tracklist_sets_page_stmt,
    get_match_pending_tracklists,
    get_tracklist_sets_page,
    get_untracked_files,
)
from tests._queue_fakes import wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# The redesigned Identify workspace stage whose HX fragment must ride the ONE chrome poll
# (no per-fragment ``hx-trigger="every"`` / ``setInterval``).
_WORKSPACE_STAGES = ["tracklist"]


# ---------------------------------------------------------------------------
# Module-level async seed helpers (test fixtures -- ORM inserts only, no backend change).
# Plans 59-02/03 build their workspace assertions on these.
# ---------------------------------------------------------------------------


async def _seed_file(
    session: AsyncSession,
    *,
    original_filename: str = "set.mp3",
    file_type: str = "mp3",
    file_size: int = 1024,
) -> FileRecord:
    """Insert one FileRecord (legacy-agent default) and return it.

    The parent row every ``_seed_tracklist`` FK points at.
    """
    file_id = uuid.uuid4()
    record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex chars
        original_path=f"/test/music/{original_filename}",
        original_filename=original_filename,
        current_path=f"/test/music/{original_filename}",
        file_type=file_type,
        file_size=file_size,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def _seed_tracklist(
    session: AsyncSession,
    *,
    file_id: uuid.UUID | None = None,
    match_confidence: int | None = None,
    external_id: str | None = None,
    artist: str | None = None,
    event: str | None = None,
) -> Tracklist:
    """Insert one ``tracklists`` row.

    ``file_id`` non-NULL models a LINKED ("matched") tracklist (D-04); NULL models a candidate.
    ``match_confidence`` is the rapidfuzz int surfaced as the per-set confidence. ``artist`` /
    ``event`` are the ``Tracklist`` columns the phaze-6not3 fix's ``TRACKLIST_SETS_SORT`` NO LONGER
    orders "Set" / "Tracklist" by directly (those headers now order by the DISPLAYED value --
    see ``get_tracklist_sets_page``) but both remain part of the fallback chain for an unmatched
    candidate's displayed set name (``artist or event or external_id``).
    """
    tl = Tracklist(
        id=uuid.uuid4(),
        external_id=external_id or f"ext-{uuid.uuid4().hex[:12]}",
        source_url="https://example.test/tracklist",
        file_id=file_id,
        match_confidence=match_confidence,
        artist=artist,
        event=event,
    )
    session.add(tl)
    await session.commit()
    await session.refresh(tl)
    return tl


async def _seed_tracklist_version(
    session: AsyncSession,
    tracklist_id: uuid.UUID,
    *,
    version_number: int = 1,
    set_latest: bool = True,
) -> TracklistVersion:
    """Insert one ``tracklist_versions`` row for ``tracklist_id`` (the per-set track container).

    ``set_latest`` (default True) points the parent ``Tracklist.latest_version_id`` at this version,
    mirroring the scraper task (``tasks/tracklist.py`` sets ``latest_version_id`` on each new version).
    Per-set coverage is scoped to the latest version only (D-07), so tests seeding multiple versions
    pass ``set_latest=False`` on the stale ones.
    """
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist_id, version_number=version_number)
    session.add(version)
    await session.commit()
    await session.refresh(version)
    if set_latest:
        await session.execute(update(Tracklist).where(Tracklist.id == tracklist_id).values(latest_version_id=version.id))
        await session.commit()
    return version


async def _seed_tracklist_track(
    session: AsyncSession,
    version_id: uuid.UUID,
    *,
    position: int = 1,
    confidence: float | None = None,
) -> TracklistTrack:
    """Insert one ``tracklist_tracks`` row.

    ``confidence`` (Float, nullable) is the basis for the D-07 per-set N/M track coverage.
    """
    track = TracklistTrack(id=uuid.uuid4(), version_id=version_id, position=position, confidence=confidence)
    session.add(track)
    await session.commit()
    await session.refresh(track)
    return track


# ---------------------------------------------------------------------------
# Foundation tests (FILLED in Plan 59-01).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identify_fragments_are_bare(client: AsyncClient) -> None:
    """R-5 -- the ``/s/tracklist`` HX response is a bare fragment.

    Mirrors ``test_enrich_analyze_workspaces.py::test_stage_fragment_is_bare``: a swapped
    workspace fragment NEVER carries ``<html>``/``<head>`` (no duplicate landmarks/skip-links).
    The chrome -- including the single poll element -- persists across swaps. Passes against the
    ``_STAGE_PLACEHOLDER`` fragments today and must stay green once Plans 02/03 supersede them.
    """
    for stage in _WORKSPACE_STAGES:
        hx = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert hx.status_code == 200, f"{stage} fragment must render 200"
        assert "<html" not in hx.text, f"{stage} fragment must not carry <html>"
        assert "<head" not in hx.text, f"{stage} fragment must not carry <head>"


@pytest.mark.asyncio
async def test_identify_single_poll_discipline(client: AsyncClient) -> None:
    """WORK-05 / R-2 -- exactly one chrome poll; no second loop in either Identify fragment.

    The full shell (``GET /``) fires the live refresh from persistent chrome: EXACTLY ONE
    ``hx-get="/pipeline/stats"`` element. No swappable Identify workspace fragment may carry its
    own ``hx-trigger="every"`` poll or a ``setInterval`` loop -- every workspace's live values
    ride the one chrome poll via ``hx-swap-oob`` against the existing seeds.
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    body = shell.text
    # Exactly one persistent poll element in chrome (R-2).
    assert body.count('hx-get="/pipeline/stats"') == 1, "shell must fire exactly one /pipeline/stats poll"

    # No Identify workspace fragment starts a second poll loop (R-2 / WORK-05).
    for stage in _WORKSPACE_STAGES:
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        assert 'hx-trigger="every' not in frag.text, f"{stage} fragment must not start a second poll loop"
        assert "setInterval" not in frag.text, f"{stage} fragment must not use setInterval"


# ---------------------------------------------------------------------------
# Workspace tests -- xfail stubs converted to real assertions by their owning plan/task.
# (names + reasons per 59-VALIDATION.md / 59-RESEARCH.md Test Map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracklist_workspace_is_the_drain_plus_match(client: AsyncClient) -> None:
    """IDENT-02 / D-05 / D-06, reworked by phaze-2akf -- LOOKUP (the drain) then MATCH.

    This used to assert THREE step cards (Search / Scrape / Match), each posting to its own bulk
    endpoint. Two of those three are gone with the legacy scrape path:

    * SEARCH and SCRAPE were separate only because the legacy path searched in one job and fetched
      the detail page in another. The drain collapses derive -> search -> score -> render -> parse
      -> persist into ONE operation, so neither the split nor its two bulk fan-out triggers
      describe anything the system does.
    * Their endpoints (``/pipeline/search-tracklists``, ``/pipeline/scrape-tracklists``) are
      deleted, so this test now asserts their ABSENCE -- re-adding a bulk fan-out at a host that
      publishes ~1 request / 8 s must fail loudly rather than pass silently.

    What remains is the drain panel (phaze-fq9h.8's status fragment, which carries its own bounded
    "run a slice" trigger) and the MATCH card, which is still a real, separate stage.
    """
    resp = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    # The LOOKUP stage IS the drain fragment -- one status surface, not a third one.
    assert 'hx-get="/pipeline/tracklist-drain-status"' in body
    assert body.index("1 · Lookup") < body.index("2 · Match") < body.index("Matched-set coverage")
    assert 'aria-label="Tracklist preparation workflow"' in body
    assert body.count("Prerequisite") == 2 and body.count("Next action") == 2 and body.count("Current state") == 2
    assert "classified as a live set from duration first, then repaired filename and track-number evidence" in body
    assert "embedded tags, a .cue companion, or a pending/approved lookup" in body
    assert "lookup queries derived from the repaired filename" in body
    assert "artist and event metadata" not in body
    assert "Any stored tracklist with no Discogs link is queued" in body
    assert "tracks on its latest version that have both artist and title" in body
    assert "skipped or unmatched tracks leave the tracklist pending" in body
    # MATCH survives as the one remaining bulk trigger, still R-4 guarded.
    assert 'hx-post="/pipeline/match-tracklists"' in body
    assert ':disabled="$store.pipeline.matchBusy > 0"' in body
    assert "MATCH ALL" in body
    assert body.count("hx-confirm=") >= 1
    # phaze-2akf: the retired bulk fan-outs must not come back.
    assert "/pipeline/search-tracklists" not in body
    assert "/pipeline/scrape-tracklists" not in body
    assert "SEARCH ALL" not in body
    assert "SCRAPE ALL" not in body
    assert "searchBusy" not in body
    assert "scrapeBusy" not in body
    # D-05: NO single run-chain orchestrator button (no backend endpoint runs all three).
    assert "run-chain" not in body
    assert "RUN CHAIN" not in body


@pytest.mark.asyncio
async def test_tracklist_queue_diagnostics_are_defined_and_progressively_disclosed(client: AsyncClient) -> None:
    """Preparation leads with Lookup/Match while provider queue mechanics stay available on demand."""
    response = await client.get("/pipeline/tracklist-drain-status")
    assert response.status_code == 200
    body = response.text

    assert "<details" in body and "Operator queue diagnostics" in body
    for term in ("Drain:", "Parked:", "Collapse ratio:", "Crawl delay and throughput ceiling:"):
        assert term in body
    assert 'hx-post="/pipeline/run-tracklist-drain"' in body
    assert 'hx-post="/pipeline/arm-tracklist-drain"' in body
    assert "1001Tracklists" in body


@pytest.mark.asyncio
async def test_tracklist_drain_status_refreshes_after_running_a_slice(client: AsyncClient) -> None:
    """phaze-k2ob4: the drain-status panel refreshes after "Run a drain slice", not just on mount.

    The panel's host div is `hx-trigger="load"`-only (fetched exactly once) and the panel itself
    carries no self-poll (WORK-05/R-2 forbids a second `hx-trigger="every"` loop on this surface --
    see `test_identify_single_poll_discipline`), so before this fix Queued/Answered-by-cache/
    Prioritized/ETA froze at first render for the life of the tab even though
    `_run_drain_response.html` promised the operator checks the outcome "on its own refresh". The
    sanctioned fix is an event-driven, bounded, ONE-shot re-fetch per click -- never an interval:
    POST /pipeline/run-tracklist-drain sets `HX-Trigger: drain-refresh` on its response, and the
    host div listens for that event bubbling to `<body>`.
    """
    workspace = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert workspace.status_code == 200
    ws_body = workspace.text
    assert 'hx-trigger="load, drain-refresh from:body"' in ws_body
    # WORK-05 / R-2: still no interval anywhere on this fragment (the sanctioned fix, not the
    # rejected one -- see the router endpoint's docstring).
    assert 'hx-trigger="every' not in ws_body
    assert "setInterval" not in ws_body

    wire_fakes(client)
    response = await client.post("/pipeline/run-tracklist-drain")
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "drain-refresh"


@pytest.mark.asyncio
async def test_tracklist_per_set_coverage(client: AsyncClient, session: AsyncSession) -> None:
    """IDENT-02 / D-07 / D-08 -- per-set table renders N/M track coverage; inert rows.

    Seed a linked tracklist with a version + N confident / M total tracks, then assert the per-set
    table below the aggregate cards renders the ``N/M`` coverage from ``TracklistTrack.confidence``
    with inert (no ``hx-get``) rows.
    """
    file = await _seed_file(session, original_filename="set.mp3")
    tl = await _seed_tracklist(session, file_id=file.id, match_confidence=88)
    version = await _seed_tracklist_version(session, tl.id)
    await _seed_tracklist_track(session, version.id, position=1, confidence=0.9)
    await _seed_tracklist_track(session, version.id, position=2, confidence=None)
    # D-08: the per-set table's HOST still sits below the aggregates (aggregate on top, detail
    # below) -- phaze-1wvb moved the ROWS into the bounded fragment, not the layout, and phaze-2akf
    # replaced the three-card grid with the drain panel + the MATCH card without moving the table.
    shell = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert shell.status_code == 200
    assert shell.text.index('id="tracklist-drain-status-view"') < shell.text.index('id="tracklist-sets-view"')
    assert shell.text.index("MATCH ALL") < shell.text.index('id="tracklist-sets-view"')

    resp = await client.get("/pipeline/tracklist-sets")
    assert resp.status_code == 200
    body = resp.text
    assert "tracklist-set-table" in body
    tbl = body[body.index('id="tracklist-set-table"') :]
    # D-07: N/M track-level coverage from TracklistTrack.confidence (1 confident of 2 total).
    assert "1/2" in tbl
    # D-04/D-08: a linked tracklist reads "matched" to its file.
    assert "matched" in tbl
    # R-1 / D-06: ROWS are inert -- scoped to <tbody> because phaze-a6hm.1 gave the <thead> its own
    # hx-get sort buttons (the sortable-column contract). An unscoped "hx-get" not in tbl would now
    # be asserting that the table is UNSORTABLE, which is the opposite of what this test means.
    assert "hx-get" not in tbl[tbl.index("<tbody") :]


# ---------------------------------------------------------------------------
# Read-only row-assembly helper unit tests (Plan 59-01 Task 2).
# These exercise the service helpers directly (no template wiring) so the data
# contract Plans 02/03 render against is locked + degrade-safe NOW.
# ---------------------------------------------------------------------------


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` in the fake-session tests.

    ``__aexit__`` returns ``False`` so an exception raised inside the ``async with`` block
    propagates out to the helper's degrade ``except`` -- exactly as a real SAVEPOINT does after
    ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ExplodingSession:
    """A fake session whose every ``execute`` raises -- exercises the helper degrade path."""

    def begin_nested(self) -> _NullSavepoint:
        return _NullSavepoint()

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("forced DB error")


@pytest.mark.asyncio
async def test_get_tracklist_sets_page_shape(session: AsyncSession) -> None:
    """IDENT-02 / D-07 -- the per-set row carries N/M track coverage + match state.

    A LINKED tracklist with a version of 1 confident (confidence set) + 1 unconfident (confidence
    NULL) track yields tracks_confident=1, tracks_total=2, matched_to_file=True, state "matched".
    """
    file = await _seed_file(session, original_filename="set.mp3")
    tl = await _seed_tracklist(session, file_id=file.id, match_confidence=88)
    version = await _seed_tracklist_version(session, tl.id)
    await _seed_tracklist_track(session, version.id, position=1, confidence=0.9)
    await _seed_tracklist_track(session, version.id, position=2, confidence=None)

    rows = (await get_tracklist_sets_page(session)).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["set_name"] == "set.mp3"
    assert row["path"] == "/test/music/set.mp3"
    assert row["tracklist_state"] == "matched"
    assert row["matched_to_file"] is True
    assert row["tracks_confident"] == 1
    assert row["tracks_total"] == 2


@pytest.mark.asyncio
async def test_get_tracklist_sets_page_counts_latest_version_only(session: AsyncSession) -> None:
    """WR-01 regression -- a re-scraped (multi-version) tracklist counts ONLY its latest version.

    Coverage must NOT sum tracks across versions: a stale v1 (3 tracks) plus a latest v2 (2 tracks,
    1 confident) yields tracks_confident=1, tracks_total=2 -- not 4/5 across both versions.
    """
    file = await _seed_file(session, original_filename="reset.mp3")
    tl = await _seed_tracklist(session, file_id=file.id, match_confidence=91)
    stale = await _seed_tracklist_version(session, tl.id, version_number=1, set_latest=False)
    for pos in (1, 2, 3):
        await _seed_tracklist_track(session, stale.id, position=pos, confidence=0.8)
    latest = await _seed_tracklist_version(session, tl.id, version_number=2, set_latest=True)
    await _seed_tracklist_track(session, latest.id, position=1, confidence=0.95)
    await _seed_tracklist_track(session, latest.id, position=2, confidence=None)

    rows = (await get_tracklist_sets_page(session)).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["tracks_confident"] == 1
    assert row["tracks_total"] == 2


@pytest.mark.asyncio
async def test_get_tracklist_sets_page_candidate(session: AsyncSession) -> None:
    """D-04/D-08 -- an unlinked tracklist is a "candidate" set with no file path and zero counts."""
    await _seed_tracklist(session, file_id=None, match_confidence=None, external_id="cand-1")
    rows = (await get_tracklist_sets_page(session)).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["tracklist_state"] == "candidate"
    assert row["matched_to_file"] is False
    assert row["path"] is None
    assert row["tracks_confident"] == 0
    assert row["tracks_total"] == 0


@pytest.mark.asyncio
async def test_get_tracklist_sets_page_degrades_to_empty() -> None:
    """T-59-DOS / paging contract rule 6 -- a DB error degrades to an EMPTY Page, never raises."""
    page = await get_tracklist_sets_page(_ExplodingSession())  # type: ignore[arg-type]
    assert page.rows == []
    assert page.has_next is False


# ---------------------------------------------------------------------------
# phaze-1wvb -- the bound. These are the tests that would have caught the bug: both Identify reads
# were whole-corpus (no LIMIT, `.all()`-materialised, server-rendered inline), so the render grew
# without limit as the archive converged. Every assertion below fails against the pre-fix code.
# ---------------------------------------------------------------------------


# Seeding a full DEFAULT_PAGE_SIZE + a few is enough to prove the bound: an UNBOUNDED read returns
# all of them, a bounded one returns exactly one page plus has_next. Keeping this small keeps the
# test fast while still being decisive.
_OVER_A_PAGE = DEFAULT_PAGE_SIZE + 3


def test_identify_page_statements_carry_a_sql_limit() -> None:
    """phaze-1wvb -- THE decisive guard: the bound must live in the SQL, not just in Python.

    ``split_sentinel`` truncates the result list, so a test that only asserts ``len(page.rows) ==
    page_size`` still passes when the underlying SELECT has NO ``LIMIT`` -- the read would keep
    dragging the whole corpus into memory (the actual DoS) while looking perfectly bounded from the
    outside. This was verified by mutation: removing ``paged_stmt`` from the reader left every
    row-count assertion green. So compile the statements and assert on the emitted SQL.
    """
    stmts = {"tracklist": _tracklist_sets_page_stmt(page=3, page_size=DEFAULT_PAGE_SIZE)}
    for name, stmt in stmts.items():
        sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert "LIMIT" in sql.upper(), f"the {name} render read has NO SQL LIMIT -- it is a whole-corpus read"
        assert "OFFSET" in sql.upper(), f"the {name} render read does not page"
        # rule 2: has_next rides the +1 sentinel, so the LIMIT is page_size + 1 and there is NO COUNT.
        assert str(DEFAULT_PAGE_SIZE + 1) in sql, f"the {name} read must LIMIT page_size + 1 (the sentinel)"
        assert "COUNT(*)" not in sql.upper().replace(" ", ""), f"the {name} read must never emit a whole-corpus COUNT"
        # rule 4: the ORDER BY must end on the unique primary-key tiebreaker, not just created_at.
        order_by = sql.upper().rsplit("ORDER BY", 1)[-1]
        assert ".ID DESC" in order_by, f"the {name} read lost its unique tiebreaker -- paging can skip/duplicate rows"


@pytest.mark.asyncio
async def test_tracklist_sets_page_is_bounded_regardless_of_corpus_size(session: AsyncSession) -> None:
    """phaze-1wvb -- a large tracklist corpus does NOT grow the per-set read (was one row per Tracklist)."""
    for i in range(_OVER_A_PAGE):
        await _seed_tracklist(session, file_id=None, match_confidence=i, external_id=f"cand-{i:03d}")

    page = await get_tracklist_sets_page(session)
    assert len(page.rows) == DEFAULT_PAGE_SIZE
    assert page.has_next is True

    tail = await get_tracklist_sets_page(session, page=2, page_size=MAX_PAGE_SIZE * 10)
    assert len(tail.rows) <= MAX_PAGE_SIZE
    assert tail.has_next is False


@pytest.mark.asyncio
async def test_identify_paging_never_skips_or_duplicates_a_row(session: AsyncSession) -> None:
    """Paging contract rule 4 -- the unique tiebreaker gives tied rows a TOTAL order.

    Every row here is inserted in its own transaction but ``created_at`` is a Postgres timestamp
    default, so ties are routine; without the ``id`` tiebreaker OFFSET paging could silently skip or
    duplicate rows between page 1 and page 2. Walking the whole set page-by-page must reconstruct it
    exactly once each.
    """
    total = 25
    page_size = 10
    for i in range(total):
        await _seed_tracklist(session, file_id=None, match_confidence=i, external_id=f"page-{i:03d}")

    seen: list[str] = []
    for page_num in (1, 2, 3):
        page = await get_tracklist_sets_page(session, page=page_num, page_size=page_size)
        seen.extend(str(row["set_name"]) for row in page.rows)
    assert len(seen) == total
    assert len(set(seen)) == total, "a row was duplicated across pages -- the tiebreaker is broken"


@pytest.mark.asyncio
async def test_identify_pages_clamp_instead_of_raising(session: AsyncSession) -> None:
    """Paging contract rule 5 -- nonsense paging inputs CLAMP; they never raise into the render."""
    await _seed_tracklist(session, file_id=None, external_id="only-one")

    for page_num in (0, -1, -9999):
        page = await get_tracklist_sets_page(session, page=page_num)
        assert page.page == 1
        assert len(page.rows) == 1

    # A page far past the end is an EMPTY page, not an error (the total is deliberately unknown).
    beyond = await get_tracklist_sets_page(session, page=9999)
    assert beyond.rows == []
    assert beyond.has_next is False

    # Page size clamps into [MIN, MAX] at both extremes.
    assert (await get_tracklist_sets_page(session, page_size=-5)).page_size >= 1
    assert (await get_tracklist_sets_page(session, page_size=100_000)).page_size <= MAX_PAGE_SIZE


@pytest.mark.asyncio
async def test_identify_workspaces_server_render_zero_rows(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-1wvb -- the Identify workspace does not server-render a file row inline any more.

    The workspace fragment ships an EMPTY host div that hx-gets the bounded fragment on load (the
    same shape phaze-5462 gave the analyze/metadata tabs). This is the structural guard: a
    regression that re-inlines the row loop puts the table markup back into the workspace response
    and fails here even before the payload grows.
    """
    file = await _seed_file(session, original_filename="inline-check.mp3")
    await _seed_tracklist(session, file_id=file.id, match_confidence=95)

    tracklist = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert tracklist.status_code == 200
    assert 'id="tracklist-sets-view"' in tracklist.text
    assert 'hx-get="/pipeline/tracklist-sets"' in tracklist.text
    assert 'id="tracklist-set-table"' not in tracklist.text, "rows must NOT be server-rendered inline"
    assert "inline-check.mp3" not in tracklist.text


@pytest.mark.asyncio
async def test_identify_render_payload_does_not_grow_with_the_corpus(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-1wvb -- the DoS regression proper: a large backlog must not grow the response.

    Renders the Tracklist per-set fragment at a small corpus, then adds ``_OVER_A_PAGE`` more
    tracklists and renders again. Pre-fix, the second response carried every extra row; post-fix the
    fragment is capped at one page, so the payload is flat.
    """
    await _seed_tracklist(session, file_id=None, match_confidence=1, external_id="baseline")
    small = (await client.get("/pipeline/tracklist-sets")).text

    for i in range(_OVER_A_PAGE):
        await _seed_tracklist(session, file_id=None, match_confidence=i, external_id=f"flood-{i:03d}")
    large_resp = await client.get("/pipeline/tracklist-sets")
    assert large_resp.status_code == 200
    large = large_resp.text

    assert large.count("<tr") <= DEFAULT_PAGE_SIZE + 1, "the rendered table must stay bounded to one page"
    # Bounded, not merely "smaller": the flood adds at most one page of rows, never the whole backlog.
    assert len(large) < len(small) * (DEFAULT_PAGE_SIZE + 5)
    # Rule 2: the pager offers Prev/Next, never a "page X of Y" total (that needs a whole-corpus COUNT).
    assert "Next" in large
    assert " of " not in large.split("Page ", 1)[-1][:40]


@pytest.mark.asyncio
async def test_tracklist_bulk_actions_still_cover_the_full_set(session: AsyncSession) -> None:
    """PAGING CONTRACT RULE 7 -- bounding the render must NOT bound the enqueue sets.

    The Tracklist workspace's MATCH ALL trigger reads its own pending set, which is UNBOUNDED BY
    DESIGN. This is the rule-7 guard: with a backlog larger than one render page, the enqueue reader
    must still return the FULL set. If someone "unifies" it with the paged render reader, the button
    silently under-enqueues the backlog -- a far worse bug than a long table -- and this test is
    what catches it.

    phaze-2akf: ``get_untracked_files`` is still asserted here even though its SEARCH ALL trigger is
    gone. It remains the canonical unbounded READ of "files with no tracklist" (the counterpart of
    ``stage_status.done_clause(Stage.TRACKLIST)``), and keeping the unpaged assertion is what stops
    a future edit from quietly giving it a LIMIT and then wiring a trigger back onto it.
    """
    for i in range(_OVER_A_PAGE):
        await _seed_file(session, original_filename=f"untracked-{i:03d}.mp3")

    # The RENDER read is bounded...
    render_page = await get_tracklist_sets_page(session)
    assert len(render_page.rows) <= DEFAULT_PAGE_SIZE

    # ...while the ENQUEUE read still covers every pending file (no LIMIT, ever).
    enqueue_set = await get_untracked_files(session)
    assert len(enqueue_set) == _OVER_A_PAGE, "the un-tracklisted read must cover the FULL backlog, not one page"
    assert len(enqueue_set) > DEFAULT_PAGE_SIZE

    # The MATCH ALL enqueue reader is likewise unpaged (it takes no paging args at all).
    assert isinstance(await get_match_pending_tracklists(session), list)


# --- phaze-a6hm.1: the sortable-column contract, END TO END through a real handler ----------------
#
# tests/shared/routers/test_column_sort.py proves the contract object in isolation. These prove the
# WIRING: that a header click actually reorders the SET (not the page), that the whitelist holds at
# the HTTP boundary, and that the header announces itself. Without these, a contract with perfect
# unit tests could still be connected to nothing.
#
# phaze-0jpe: these were authored against ``/pipeline/trackid-files`` and moved to
# ``/pipeline/tracklist-sets`` when the Track-ID workspace was removed with fingerprinting. The
# ``SortContract`` under test is the sibling of the one they were written for, wired through the same
# shared ``_file_table.html`` header machinery, so the wiring they guard is unchanged.


@pytest.mark.asyncio
async def test_tracklist_headers_are_sortable_and_announce_state(client: AsyncClient, session: AsyncSession) -> None:
    """The shared _file_table renders a sort button + aria-sort for a whitelisted header (rules 1/5).

    ``Set`` is whitelisted on the Tracklist-sets contract; ``Tracks`` is not. Asserting BOTH is what
    makes this a test of the label-recognition mechanism rather than of "the template emits buttons"
    -- a partial that made every header sortable would pass a one-sided check and then 500 on click.
    """
    await _seed_tracklist(session, file_id=None, artist="a", external_id="hdr-0")

    body = (await client.get("/pipeline/tracklist-sets")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # The whitelisted header is a real server-side sort control aimed at its own endpoint.
    assert 'hx-get="/pipeline/tracklist-sets?' in head
    assert "sort=artist" in head
    # Rule 5: the ACTIVE column announces its direction; the caret is decorative only.
    assert 'aria-sort="ascending"' in head
    assert 'aria-hidden="true"' in head
    # A non-whitelisted header stays plain text -- no button, no aria-sort.
    tracks = head[head.index("Tracks") - 200 : head.index("Tracks")]
    assert "hx-get" not in tracks


@pytest.mark.asyncio
async def test_tracklist_sort_reorders_the_set_server_side(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 1: the ORDER BY lands in SQL, so asc and desc return genuinely different row orders.

    Seeded out of alphabetical order so a handler that ignored ``sort`` entirely (returning
    insertion/newest-first order) fails at least one of the two direction assertions.
    """
    for index, name in enumerate(("banana", "apple", "cherry")):
        await _seed_tracklist(session, file_id=None, artist=name, external_id=f"sort-{index}")

    asc = (await client.get("/pipeline/tracklist-sets?sort=artist&order=asc")).text
    desc = (await client.get("/pipeline/tracklist-sets?sort=artist&order=desc")).text

    def order_of(body: str) -> list[str]:
        rows = body[body.index("<tbody") :]
        return sorted(("apple", "banana", "cherry"), key=rows.index)

    assert order_of(asc) == ["apple", "banana", "cherry"]
    assert order_of(desc) == ["cherry", "banana", "apple"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", ["original_path", "__class__", "id", "file_size; DROP TABLE files", "1) OR 1=1 --"])
async def test_unwhitelisted_sort_is_rejected_at_the_http_boundary(client: AsyncClient, session: AsyncSession, hostile: str) -> None:
    """THE regression the bead requires, at the boundary: an unwhitelisted sort cannot reach a column.

    Asserts three things, because any one alone is too weak:
      * the request does not 500 (a getattr-based implementation would, on ``__class__``),
      * the response is the DEFAULT order (the hostile value was discarded, not honoured), and
      * the hostile value is not echoed back as a ``sort=`` parameter in any header URL, which would
        make the operator's next click carry it further.

    That last assertion is deliberately scoped to ``sort=<value>`` rather than to the bare string:
    short keys like ``id`` occur legitimately in every ``id="..."`` attribute on the page, so an
    unscoped substring check would fail on markup that is entirely correct.

    Rule 3: this degrades rather than 422-ing, matching every other render-path allowlist in phaze.
    """
    for index, name in enumerate(("banana", "apple")):
        await _seed_tracklist(session, file_id=None, artist=name, external_id=f"hostile-{index}")

    resp = await client.get(f"/pipeline/tracklist-sets?sort={hostile}&order=asc")
    assert resp.status_code == 200
    rows = resp.text[resp.text.index("<tbody") :]
    assert rows.index("apple") < rows.index("banana")  # the default (artist asc) order
    assert f"sort={hostile}" not in resp.text


@pytest.mark.asyncio
async def test_sorting_preserves_view_state_and_the_pager_preserves_the_sort(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 4, both directions: a sort keeps the other view state, and a pager keeps the sort.

    The second half is the one that rots silently -- Prev/Next dropping the sort looks fine on page 1
    and only misbehaves once the operator scrolls, which is exactly when they are relying on it.
    """
    for index in range(12):
        await _seed_tracklist(session, file_id=None, artist=f"artist-{index:02d}", external_id=f"view-{index:02d}")

    body = (await client.get("/pipeline/tracklist-sets?sort=artist&order=desc&page_size=10")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # A header click re-emits page_size, and resets to page 1 rather than holding a stale offset.
    assert "page_size=10" in head
    assert "page=" not in head

    # The pager carries the ACTIVE sort forward, so Next stays inside the chosen order.
    pager = body[body.index("</table>") :]
    assert "sort=artist" in pager
    assert "order=desc" in pager


# ---------------------------------------------------------------------------
# phaze-6not3: TRACKLIST_SETS_SORT must order by what the cell actually RENDERS, not merely a
# same-named Tracklist column (FILES_SORT's own "sort what you show" precedent, violated on both
# of this contract's columns before the fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_column_sorts_by_the_displayed_set_name_not_bare_artist(client: AsyncClient, session: AsyncSession) -> None:
    """The 'Set' header must order by ``set_name`` (filename when matched, else artist/event/id).

    Before the fix it ordered by bare ``Tracklist.artist`` unconditionally -- a value that is
    NEVER shown for a matched row (the cell renders the linked file's name instead). Seeded so the
    two orderings DISAGREE: by bare artist, the candidate ("bbb-artist") sorts before the matched
    row ("zzz-artist"); by displayed set_name, the matched row's filename ("aaa-file.mp3") sorts
    first. Only the fixed expression can produce the second order.
    """
    matched_file = await _seed_file(session, original_filename="aaa-file.mp3")
    await _seed_tracklist(session, file_id=matched_file.id, artist="zzz-artist", external_id="set-matched")
    await _seed_tracklist(session, file_id=None, artist="bbb-artist", external_id="set-candidate")

    asc = (await client.get("/pipeline/tracklist-sets?sort=artist&order=asc")).text
    rows = asc[asc.index("<tbody") :]
    assert rows.index("aaa-file.mp3") < rows.index("bbb-artist"), (
        "ascending 'Set' sort must follow the DISPLAYED set_name (matched row's filename), not the invisible Tracklist.artist column"
    )


@pytest.mark.asyncio
async def test_tracklist_column_groups_matched_apart_from_candidate(client: AsyncClient, session: AsyncSession) -> None:
    """The 'Tracklist' header must group by the matched/candidate STATE actually shown.

    Before the fix it ordered by bare ``Tracklist.event`` -- a column that appears nowhere on
    screen (the cell renders only "matched"/"candidate"). ``event`` values are chosen so
    alphabetical order would INTERLEAVE matched and candidate rows; the fixed expression
    (``Tracklist.file_id IS NOT NULL``) must instead put every candidate on one side and every
    matched row on the other, with no interleaving, regardless of event.
    """
    file_a = await _seed_file(session, original_filename="matched-a.mp3")
    file_b = await _seed_file(session, original_filename="matched-b.mp3")
    await _seed_tracklist(session, file_id=file_a.id, artist="m1", event="aaa-event", external_id="ev-m1")
    await _seed_tracklist(session, file_id=None, artist="c1", event="bbb-event", external_id="ev-c1")
    await _seed_tracklist(session, file_id=file_b.id, artist="m2", event="ccc-event", external_id="ev-m2")
    await _seed_tracklist(session, file_id=None, artist="c2", event="ddd-event", external_id="ev-c2")

    asc = (await client.get("/pipeline/tracklist-sets?sort=event&order=asc")).text
    rows = asc[asc.index("<tbody") :]

    positions = {name: rows.index(name) for name in ("matched-a.mp3", "matched-b.mp3", "c1", "c2")}
    candidates_last = max(positions["c1"], positions["c2"])
    matched_first = min(positions["matched-a.mp3"], positions["matched-b.mp3"])
    assert candidates_last < matched_first, "ascending 'Tracklist' sort must group ALL candidates before ALL matched rows, never interleaved"
