"""Behavioral tests for the v7.0 Identify workspaces (Phase 59, IDENT-01/IDENT-02 + R-5 / WORK-05).

This is the single Phase-59 test file (Wave 0, Plan 59-01 / 59-VALIDATION.md). It mirrors the
Phase-58 ``tests/test_enrich_analyze_workspaces.py`` model and defines the full Phase-59 test
surface up front:

* The two **foundation** tests are FILLED here (they pass against the ``_STAGE_PLACEHOLDER``
  fragments today and guard the contract Plans 02/03 must preserve):
    - ``test_identify_fragments_are_bare``       -> R-5    (``/s/trackid`` & ``/s/tracklist`` HX
      responses are bare fragments -- no ``<html>``/``<head>`` document wrapper).
    - ``test_identify_single_poll_discipline``   -> WORK-05 / R-2 (the shell fires EXACTLY ONE
      ``/pipeline/stats`` poll; neither new fragment starts a second ``hx-trigger="every"`` /
      ``setInterval`` loop).

* The four **workspace** behavior tests are ``xfail`` stubs that COLLECT cleanly now and are
  converted to real assertions by their owning plan/task (Plans 59-02 / 59-03):
    - ``test_trackid_table_signals``             -> IDENT-01 (Plan 59-02)
    - ``test_trackid_success_renders_done``      -> IDENT-01 neg / Pitfall 1 (Plan 59-02)
    - ``test_tracklist_step_cards_and_triggers`` -> IDENT-02 (Plan 59-03)
    - ``test_tracklist_per_set_coverage``        -> IDENT-02 (Plan 59-03)

The module-level ``_seed_*`` helpers below are test fixtures (ORM inserts only -- never a backend
change). Plans 59-02/03 use them to seed the fingerprint + tracklist rows the two workspaces
render. They live here (not conftest) because they are Phase-59-specific shapes; ``conftest.py``
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
from phaze.models.fingerprint import FingerprintResult
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from phaze.services.pipeline import (
    _trackid_linked_conf_subq,
    _trackid_page_stmt,
    _tracklist_sets_page_stmt,
    get_match_pending_tracklists,
    get_scrape_pending_tracklists,
    get_trackid_files_page,
    get_tracklist_sets_page,
    get_untracked_files,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# The two redesigned Identify workspace stages whose HX fragments must ride the ONE chrome poll
# (no per-fragment ``hx-trigger="every"`` / ``setInterval``).
_WORKSPACE_STAGES = ["trackid", "tracklist"]


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

    The parent row every ``_seed_fingerprint_result`` / ``_seed_tracklist`` FK points at.
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


async def _seed_fingerprint_result(
    session: AsyncSession,
    file_id: uuid.UUID,
    engine: str,
    status: str,
) -> FingerprintResult:
    """Insert one per-engine ``fingerprint_results`` row for ``file_id``.

    ``engine`` is the PERSISTED lowercase vocab (``"audfprint"`` / ``"panako"``); ``status`` is
    the PERSISTED vocab (``"success"`` / ``"failed"`` -- never ``"completed"``, Pitfall 1). The
    unique ``(file_id, engine)`` index means at most one row per file per engine (absence = pending).
    """
    row = FingerprintResult(id=uuid.uuid4(), file_id=file_id, engine=engine, status=status)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _seed_tracklist(
    session: AsyncSession,
    *,
    file_id: uuid.UUID | None = None,
    match_confidence: int | None = None,
    external_id: str | None = None,
) -> Tracklist:
    """Insert one ``tracklists`` row.

    ``file_id`` non-NULL models a LINKED ("matched") tracklist (D-04); NULL models a candidate.
    ``match_confidence`` is the rapidfuzz int surfaced as the Track-ID confidence.
    """
    tl = Tracklist(
        id=uuid.uuid4(),
        external_id=external_id or f"ext-{uuid.uuid4().hex[:12]}",
        source_url="https://example.test/tracklist",
        file_id=file_id,
        match_confidence=match_confidence,
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
    """R-5 -- the ``/s/trackid`` & ``/s/tracklist`` HX responses are bare fragments.

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
async def test_trackid_table_signals(client: AsyncClient, session: AsyncSession) -> None:
    """IDENT-01 / D-01 / D-03 / D-04 -- one combined Track-ID table of per-file identity signals.

    Seeds a LINKED file (audfprint ``success`` + panako ``failed`` + a linked ``Tracklist``) and a
    CANDIDATE-only file (audfprint ``success``, no linked tracklist, but a system-wide unlinked
    candidate exists), then asserts the ``/s/trackid`` fragment renders ONE combined table (D-03)
    whose rows carry the per-engine status words (done/failed/pending, D-01), the tracklist
    match-state words (matched/candidate, D-04), and the linked confidence as a percent (D-02 -- not
    a fabricated fingerprint score) -- with inert (no ``hx-get``) rows.
    """
    matched = await _seed_file(session, original_filename="matched.mp3")
    await _seed_fingerprint_result(session, matched.id, "audfprint", "success")
    await _seed_fingerprint_result(session, matched.id, "panako", "failed")
    await _seed_tracklist(session, file_id=matched.id, match_confidence=90)

    candidate = await _seed_file(session, original_filename="candidate.mp3")
    await _seed_fingerprint_result(session, candidate.id, "audfprint", "success")
    await _seed_tracklist(session, file_id=None, match_confidence=77)

    # phaze-1wvb: the rows live in the BOUNDED fragment now -- /s/trackid ships an empty host div.
    resp = await client.get("/pipeline/trackid-files")
    assert resp.status_code == 200
    body = resp.text
    # D-03: exactly ONE combined per-file table (not two sub-sections).
    assert body.count('id="trackid-file-table"') == 1
    tbl = body[body.index('id="trackid-file-table"') :]
    # D-01: per-engine status words -- matched file -> audfprint "done" + panako "failed"; the
    # candidate file's panako has no row -> "pending" (absence == pending, Pitfall 2).
    assert "done" in tbl
    assert "failed" in tbl
    assert "pending" in tbl
    # D-04: tracklist match-state words for the linked + candidate files.
    assert "matched" in tbl
    assert "candidate" in tbl
    # D-02/D-04: the linked tracklist confidence renders as a percent, not a fabricated score.
    assert "90%" in tbl
    # R-1 / D-06: ROWS are inert -- scoped to <tbody> because phaze-a6hm.1 gave the <thead> its own
    # hx-get sort buttons (the sortable-column contract). An unscoped "hx-get" not in tbl would now
    # be asserting that the table is UNSORTABLE, which is the opposite of what this test means.
    assert "hx-get" not in tbl[tbl.index("<tbody") :]


@pytest.mark.asyncio
async def test_trackid_success_renders_done(client: AsyncClient, session: AsyncSession) -> None:
    """IDENT-01 (neg) / Pitfall 1 -- a ``status="success"`` row renders "done" (NOT "pending").

    Guards the Pitfall-1 vocabulary trap -- the done badge must key on ``FingerprintResult.status
    == "success"`` (the value the engine adapters actually write), NOT on ``"completed"`` (which
    ``get_stage_progress`` filters and which is never persisted here). Both engines are seeded
    ``success`` so a regression that maps success -> pending makes "done" vanish and "pending"
    appear; with no tracklist the only other cells are "no match" + "—", so the table carries no
    "pending" word when the mapping is correct.
    """
    file = await _seed_file(session, original_filename="done.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")
    await _seed_fingerprint_result(session, file.id, "panako", "success")
    resp = await client.get("/pipeline/trackid-files")
    assert resp.status_code == 200
    tbl = resp.text[resp.text.index('id="trackid-file-table"') :]
    assert "done" in tbl
    assert "pending" not in tbl


@pytest.mark.asyncio
async def test_tracklist_step_cards_and_triggers(client: AsyncClient) -> None:
    """IDENT-02 / D-05 / D-06 -- three sequential step cards with per-step ALL triggers.

    The ``/s/tracklist`` fragment renders Search/Scrape/Match step cards, each with its own ALL
    trigger wired VERBATIM to the existing endpoint (``/pipeline/search-tracklists`` /
    ``scrape-tracklists`` / ``match-tracklists``) under the R-4 guard (``hx-confirm`` + ``:disabled``
    on the matching ``*Busy``), with NO single chain button.
    """
    resp = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    # D-06: each step card posts to its own existing bulk endpoint (no single run-chain button).
    assert 'hx-post="/pipeline/search-tracklists"' in body
    assert 'hx-post="/pipeline/scrape-tracklists"' in body
    assert 'hx-post="/pipeline/match-tracklists"' in body
    # R-4: every ALL trigger carries an hx-confirm + a :disabled busy-gate on its matching *Busy key.
    assert body.count("hx-confirm=") >= 3
    assert ':disabled="$store.pipeline.searchBusy > 0"' in body
    assert ':disabled="$store.pipeline.scrapeBusy > 0"' in body
    assert ':disabled="$store.pipeline.matchBusy > 0"' in body
    # D-06: the three ALL-trigger labels are present (Search/Scrape/Match cards).
    assert "SEARCH ALL" in body
    assert "SCRAPE ALL" in body
    assert "MATCH ALL" in body
    # D-05: NO single run-chain orchestrator button (no backend endpoint runs all three).
    assert "run-chain" not in body
    assert "RUN CHAIN" not in body


@pytest.mark.asyncio
async def test_tracklist_per_set_coverage(client: AsyncClient, session: AsyncSession) -> None:
    """IDENT-02 / D-07 / D-08 -- per-set table renders N/M track coverage; inert rows.

    Seed a linked tracklist with a version + N confident / M total tracks, then assert the per-set
    table below the step cards renders the ``N/M`` coverage from ``TracklistTrack.confidence`` with
    inert (no ``hx-get``) rows.
    """
    file = await _seed_file(session, original_filename="set.mp3")
    tl = await _seed_tracklist(session, file_id=file.id, match_confidence=88)
    version = await _seed_tracklist_version(session, tl.id)
    await _seed_tracklist_track(session, version.id, position=1, confidence=0.9)
    await _seed_tracklist_track(session, version.id, position=2, confidence=None)
    # D-08: the per-set table's HOST still sits below the three step cards (aggregate on top,
    # detail below) -- phaze-1wvb moved the ROWS into the bounded fragment, not the layout.
    shell = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert shell.status_code == 200
    assert shell.text.index("grid grid-cols-3") < shell.text.index('id="tracklist-sets-view"')

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
async def test_get_trackid_files_page_shape(session: AsyncSession) -> None:
    """IDENT-01 / D-01 / D-04 -- the Track-ID row carries per-engine badges + tracklist match/conf.

    A file with audfprint ``success`` + panako ``failed`` + a LINKED tracklist (match_confidence)
    yields one dict: audfprint_status "done", panako_status "failed", tracklist_state "matched",
    confidence = the linked value.
    """
    file = await _seed_file(session, original_filename="full.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")
    await _seed_fingerprint_result(session, file.id, "panako", "failed")
    await _seed_tracklist(session, file_id=file.id, match_confidence=90)

    rows = (await get_trackid_files_page(session)).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "full.mp3"
    assert row["path"] == "/test/music/full.mp3"
    assert row["audfprint_status"] == "done"
    assert row["panako_status"] == "failed"
    assert row["tracklist_state"] == "matched"
    assert row["confidence"] == 90


@pytest.mark.asyncio
async def test_get_trackid_files_page_success_renders_done(session: AsyncSession) -> None:
    """Pitfall 1 (the load-bearing guard) -- a ``status="success"`` row maps to "done", NOT "pending".

    Guards against keying the done badge on ``"completed"`` (which ``get_stage_progress`` filters and
    which is NEVER persisted by the engine adapter path) -- that bug would render every engine pending.
    """
    file = await _seed_file(session, original_filename="success.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")

    rows = (await get_trackid_files_page(session)).rows
    assert len(rows) == 1
    assert rows[0]["audfprint_status"] == "done"
    # panako has no row -> pending (Pitfall 2: absence == pending).
    assert rows[0]["panako_status"] == "pending"


@pytest.mark.asyncio
async def test_get_trackid_files_page_candidate_and_no_match(session: AsyncSession) -> None:
    """D-04 -- the candidate fallback + the no-match branch.

    A file with only a fingerprint and NO linked tracklist surfaces "candidate" + the system-wide
    best candidate confidence when an unlinked candidate exists; with no candidate at all it is
    "no match" + None.
    """
    # No-match: a fingerprinted file, no tracklists anywhere.
    nomatch = await _seed_file(session, original_filename="nomatch.mp3")
    await _seed_fingerprint_result(session, nomatch.id, "audfprint", "success")
    rows = (await get_trackid_files_page(session)).rows
    assert len(rows) == 1
    assert rows[0]["tracklist_state"] == "no match"
    assert rows[0]["confidence"] is None

    # Introduce an unlinked candidate tracklist -> the fingerprinted file now reads "candidate".
    await _seed_tracklist(session, file_id=None, match_confidence=77)
    rows = (await get_trackid_files_page(session)).rows
    by_name = {r["filename"]: r for r in rows}
    assert by_name["nomatch.mp3"]["tracklist_state"] == "candidate"
    assert by_name["nomatch.mp3"]["confidence"] == 77


@pytest.mark.asyncio
async def test_get_trackid_files_page_degrades_to_empty() -> None:
    """T-59-DOS / paging contract rule 6 -- a DB error degrades to an EMPTY Page, never raises."""
    page = await get_trackid_files_page(_ExplodingSession())  # type: ignore[arg-type]
    assert page.rows == []
    assert page.has_next is False


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
    stmts = {
        "trackid": _trackid_page_stmt(_trackid_linked_conf_subq(), page=3, page_size=DEFAULT_PAGE_SIZE),
        "tracklist": _tracklist_sets_page_stmt(page=3, page_size=DEFAULT_PAGE_SIZE),
    }
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
async def test_trackid_page_is_bounded_regardless_of_corpus_size(session: AsyncSession) -> None:
    """phaze-1wvb -- a backlog larger than one page does NOT grow the Track-ID read.

    The pre-fix ``get_trackid_stage_files`` returned EVERY signal-bearing file, so this seeding would
    have yielded ``_OVER_A_PAGE`` rows. The bounded read returns exactly ``page_size`` rows and flags
    ``has_next`` off the +1 sentinel -- never a whole-corpus COUNT (paging contract rule 2).
    """
    for i in range(_OVER_A_PAGE):
        file = await _seed_file(session, original_filename=f"corpus-{i:03d}.mp3")
        await _seed_fingerprint_result(session, file.id, "audfprint", "success")

    page = await get_trackid_files_page(session)
    assert len(page.rows) == DEFAULT_PAGE_SIZE, "the render read must be bounded to one page"
    assert page.has_next is True
    assert page.has_prev is False

    # The tail is reachable, and the page size itself is capped (rule 3) -- asking for more than
    # MAX_PAGE_SIZE cannot widen the read back out to the corpus.
    tail = await get_trackid_files_page(session, page=2, page_size=MAX_PAGE_SIZE * 10)
    assert len(tail.rows) <= MAX_PAGE_SIZE
    assert tail.has_next is False


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
    assert (await get_trackid_files_page(session, page_size=-5)).page_size >= 1
    assert (await get_trackid_files_page(session, page_size=100_000)).page_size <= MAX_PAGE_SIZE


@pytest.mark.asyncio
async def test_identify_workspaces_server_render_zero_rows(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-1wvb -- neither Identify workspace server-renders a file row inline any more.

    The workspace fragments ship an EMPTY host div that hx-gets the bounded fragment on load (the
    same shape phaze-5462 gave the analyze/metadata/fingerprint tabs). This is the structural guard:
    a regression that re-inlines the row loop puts the table markup back into the workspace response
    and fails here even before the payload grows.
    """
    file = await _seed_file(session, original_filename="inline-check.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")
    await _seed_tracklist(session, file_id=file.id, match_confidence=95)

    trackid = await client.get("/s/trackid", headers={"HX-Request": "true"})
    assert trackid.status_code == 200
    assert 'id="trackid-files-view"' in trackid.text
    assert 'hx-get="/pipeline/trackid-files"' in trackid.text
    assert 'id="trackid-file-table"' not in trackid.text, "rows must NOT be server-rendered inline"
    assert "inline-check.mp3" not in trackid.text

    tracklist = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert tracklist.status_code == 200
    assert 'id="tracklist-sets-view"' in tracklist.text
    assert 'hx-get="/pipeline/tracklist-sets"' in tracklist.text
    assert 'id="tracklist-set-table"' not in tracklist.text, "rows must NOT be server-rendered inline"


@pytest.mark.asyncio
async def test_identify_render_payload_does_not_grow_with_the_corpus(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-1wvb -- the DoS regression proper: a large backlog must not grow the response.

    Renders the Track-ID surface (workspace + its bounded fragment) at a small corpus, then adds
    ``_OVER_A_PAGE`` more signal-bearing files and renders again. Pre-fix, the second response
    carried every extra row; post-fix the fragment is capped at one page, so the payload is flat.
    """
    seed = await _seed_file(session, original_filename="baseline.mp3")
    await _seed_fingerprint_result(session, seed.id, "audfprint", "success")
    small = (await client.get("/pipeline/trackid-files")).text

    for i in range(_OVER_A_PAGE):
        file = await _seed_file(session, original_filename=f"flood-{i:03d}.mp3")
        await _seed_fingerprint_result(session, file.id, "audfprint", "success")
    large_resp = await client.get("/pipeline/trackid-files")
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

    The Tracklist workspace's SEARCH / SCRAPE / MATCH ALL triggers read their own pending sets, which
    are UNBOUNDED BY DESIGN. This is the rule-7 guard: with a backlog larger than one render page,
    the enqueue readers must still return the FULL set. If someone "unifies" these with the paged
    render reader, the buttons silently under-enqueue the backlog -- a far worse bug than a long
    table -- and this test is what catches it.
    """
    for i in range(_OVER_A_PAGE):
        await _seed_file(session, original_filename=f"untracked-{i:03d}.mp3")

    # The RENDER read is bounded...
    render_page = await get_trackid_files_page(session)
    assert len(render_page.rows) <= DEFAULT_PAGE_SIZE

    # ...while the ENQUEUE read still covers every pending file (no LIMIT, ever).
    enqueue_set = await get_untracked_files(session)
    assert len(enqueue_set) == _OVER_A_PAGE, "SEARCH ALL must enqueue the FULL backlog, not one page"
    assert len(enqueue_set) > DEFAULT_PAGE_SIZE

    # The sibling scrape/match enqueue readers are likewise unpaged (they take no paging args at all).
    assert isinstance(await get_scrape_pending_tracklists(session), list)
    assert isinstance(await get_match_pending_tracklists(session), list)


# --- phaze-a6hm.1: the sortable-column contract, END TO END through a real handler ----------------
#
# tests/shared/routers/test_column_sort.py proves the contract object in isolation. These prove the
# WIRING: that a header click actually reorders the SET (not the page), that the whitelist holds at
# the HTTP boundary, and that the header announces itself. Without these, a contract with perfect
# unit tests could still be connected to nothing.


@pytest.mark.asyncio
async def test_trackid_headers_are_sortable_and_announce_state(client: AsyncClient, session: AsyncSession) -> None:
    """The shared _file_table renders a sort button + aria-sort for a whitelisted header (rules 1/5).

    ``File`` is whitelisted on the Track-ID contract; ``Panako`` is not. Asserting BOTH is what makes
    this a test of the label-recognition mechanism rather than of "the template emits buttons" -- a
    partial that made every header sortable would pass a one-sided check and then 500 on click.
    """
    file = await _seed_file(session, original_filename="a.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")

    body = (await client.get("/pipeline/trackid-files")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # The whitelisted header is a real server-side sort control aimed at its own endpoint.
    assert 'hx-get="/pipeline/trackid-files?' in head
    assert "sort=filename" in head
    # Rule 5: the ACTIVE column announces its direction; the caret is decorative only.
    assert 'aria-sort="ascending"' in head
    assert 'aria-hidden="true"' in head
    # A non-whitelisted header stays plain text -- no button, no aria-sort.
    panako = head[head.index("Panako") - 200 : head.index("Panako")]
    assert "hx-get" not in panako


@pytest.mark.asyncio
async def test_trackid_sort_reorders_the_set_server_side(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 1: the ORDER BY lands in SQL, so asc and desc return genuinely different row orders.

    Seeded out of alphabetical order so a handler that ignored ``sort`` entirely (returning
    insertion/newest-first order) fails at least one of the two direction assertions.
    """
    for name in ("banana.mp3", "apple.mp3", "cherry.mp3"):
        seeded = await _seed_file(session, original_filename=name)
        await _seed_fingerprint_result(session, seeded.id, "audfprint", "success")

    asc = (await client.get("/pipeline/trackid-files?sort=filename&order=asc")).text
    desc = (await client.get("/pipeline/trackid-files?sort=filename&order=desc")).text

    def order_of(body: str) -> list[str]:
        rows = body[body.index("<tbody") :]
        return sorted(("apple.mp3", "banana.mp3", "cherry.mp3"), key=rows.index)

    assert order_of(asc) == ["apple.mp3", "banana.mp3", "cherry.mp3"]
    assert order_of(desc) == ["cherry.mp3", "banana.mp3", "apple.mp3"]


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
    for name in ("banana.mp3", "apple.mp3"):
        seeded = await _seed_file(session, original_filename=name)
        await _seed_fingerprint_result(session, seeded.id, "audfprint", "success")

    resp = await client.get(f"/pipeline/trackid-files?sort={hostile}&order=asc")
    assert resp.status_code == 200
    rows = resp.text[resp.text.index("<tbody") :]
    assert rows.index("apple.mp3") < rows.index("banana.mp3")  # the default (filename asc) order
    assert f"sort={hostile}" not in resp.text


@pytest.mark.asyncio
async def test_sorting_preserves_view_state_and_the_pager_preserves_the_sort(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 4, both directions: a sort keeps the other view state, and a pager keeps the sort.

    The second half is the one that rots silently -- Prev/Next dropping the sort looks fine on page 1
    and only misbehaves once the operator scrolls, which is exactly when they are relying on it.
    """
    for index in range(12):
        seeded = await _seed_file(session, original_filename=f"file-{index:02d}.mp3")
        await _seed_fingerprint_result(session, seeded.id, "audfprint", "success")

    body = (await client.get("/pipeline/trackid-files?sort=filename&order=desc&page_size=10")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # A header click re-emits page_size, and resets to page 1 rather than holding a stale offset.
    assert "page_size=10" in head
    assert "page=" not in head

    # The pager carries the ACTIVE sort forward, so Next stays inside the chosen order.
    pager = body[body.index("</table>") :]
    assert "sort=filename" in pager
    assert "order=desc" in pager
