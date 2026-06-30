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

from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.pipeline import get_trackid_stage_files, get_tracklist_set_rows


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
    state: str = FileState.FINGERPRINTED,
    original_filename: str = "set.mp3",
    file_type: str = "mp3",
    file_size: int = 1024,
) -> FileRecord:
    """Insert one FileRecord (legacy-agent default) and return it.

    The parent row every ``_seed_fingerprint_result`` / ``_seed_tracklist`` FK points at.
    """
    file_id = uuid.uuid4()
    record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex chars
        original_path=f"/test/music/{original_filename}",
        original_filename=original_filename,
        current_path=f"/test/music/{original_filename}",
        file_type=file_type,
        file_size=file_size,
        state=state,
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
) -> TracklistVersion:
    """Insert one ``tracklist_versions`` row for ``tracklist_id`` (the per-set track container)."""
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist_id, version_number=version_number)
    session.add(version)
    await session.commit()
    await session.refresh(version)
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

    resp = await client.get("/s/trackid", headers={"HX-Request": "true"})
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
    # R-1 / D-06: rows are inert this phase (row->record is Phase 61; no row-click fetch wired).
    assert "hx-get" not in tbl


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
    resp = await client.get("/s/trackid", headers={"HX-Request": "true"})
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
    resp = await client.get("/s/tracklist", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    # D-08: the per-set table sits below the three step cards (aggregate on top, detail below).
    assert "tracklist-set-table" in body
    assert body.index("grid grid-cols-3") < body.index('id="tracklist-set-table"')
    tbl = body[body.index('id="tracklist-set-table"') :]
    # D-07: N/M track-level coverage from TracklistTrack.confidence (1 confident of 2 total).
    assert "1/2" in tbl
    # D-04/D-08: a linked tracklist reads "matched" to its file.
    assert "matched" in tbl
    # R-1 / D-06: rows are inert this phase (row->record is Phase 61; no row-click fetch wired).
    assert "hx-get" not in tbl


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
async def test_get_trackid_stage_files_shape(session: AsyncSession) -> None:
    """IDENT-01 / D-01 / D-04 -- the Track-ID row carries per-engine badges + tracklist match/conf.

    A file with audfprint ``success`` + panako ``failed`` + a LINKED tracklist (match_confidence)
    yields one dict: audfprint_status "done", panako_status "failed", tracklist_state "matched",
    confidence = the linked value.
    """
    file = await _seed_file(session, original_filename="full.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")
    await _seed_fingerprint_result(session, file.id, "panako", "failed")
    await _seed_tracklist(session, file_id=file.id, match_confidence=90)

    rows = await get_trackid_stage_files(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "full.mp3"
    assert row["path"] == "/test/music/full.mp3"
    assert row["audfprint_status"] == "done"
    assert row["panako_status"] == "failed"
    assert row["tracklist_state"] == "matched"
    assert row["confidence"] == 90


@pytest.mark.asyncio
async def test_get_trackid_stage_files_success_renders_done(session: AsyncSession) -> None:
    """Pitfall 1 (the load-bearing guard) -- a ``status="success"`` row maps to "done", NOT "pending".

    Guards against keying the done badge on ``"completed"`` (which ``get_stage_progress`` filters and
    which is NEVER persisted by the engine adapter path) -- that bug would render every engine pending.
    """
    file = await _seed_file(session, original_filename="success.mp3")
    await _seed_fingerprint_result(session, file.id, "audfprint", "success")

    rows = await get_trackid_stage_files(session)
    assert len(rows) == 1
    assert rows[0]["audfprint_status"] == "done"
    # panako has no row -> pending (Pitfall 2: absence == pending).
    assert rows[0]["panako_status"] == "pending"


@pytest.mark.asyncio
async def test_get_trackid_stage_files_candidate_and_no_match(session: AsyncSession) -> None:
    """D-04 -- the candidate fallback + the no-match branch.

    A file with only a fingerprint and NO linked tracklist surfaces "candidate" + the system-wide
    best candidate confidence when an unlinked candidate exists; with no candidate at all it is
    "no match" + None.
    """
    # No-match: a fingerprinted file, no tracklists anywhere.
    nomatch = await _seed_file(session, original_filename="nomatch.mp3")
    await _seed_fingerprint_result(session, nomatch.id, "audfprint", "success")
    rows = await get_trackid_stage_files(session)
    assert len(rows) == 1
    assert rows[0]["tracklist_state"] == "no match"
    assert rows[0]["confidence"] is None

    # Introduce an unlinked candidate tracklist -> the fingerprinted file now reads "candidate".
    await _seed_tracklist(session, file_id=None, match_confidence=77)
    rows = await get_trackid_stage_files(session)
    by_name = {r["filename"]: r for r in rows}
    assert by_name["nomatch.mp3"]["tracklist_state"] == "candidate"
    assert by_name["nomatch.mp3"]["confidence"] == 77


@pytest.mark.asyncio
async def test_get_trackid_stage_files_degrades_to_empty() -> None:
    """T-59-DOS -- a DB error degrades to ``[]`` (never raises into the render/poll)."""
    assert await get_trackid_stage_files(_ExplodingSession()) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_tracklist_set_rows_shape(session: AsyncSession) -> None:
    """IDENT-02 / D-07 -- the per-set row carries N/M track coverage + match state.

    A LINKED tracklist with a version of 1 confident (confidence set) + 1 unconfident (confidence
    NULL) track yields tracks_confident=1, tracks_total=2, matched_to_file=True, state "matched".
    """
    file = await _seed_file(session, original_filename="set.mp3")
    tl = await _seed_tracklist(session, file_id=file.id, match_confidence=88)
    version = await _seed_tracklist_version(session, tl.id)
    await _seed_tracklist_track(session, version.id, position=1, confidence=0.9)
    await _seed_tracklist_track(session, version.id, position=2, confidence=None)

    rows = await get_tracklist_set_rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["set_name"] == "set.mp3"
    assert row["path"] == "/test/music/set.mp3"
    assert row["tracklist_state"] == "matched"
    assert row["matched_to_file"] is True
    assert row["tracks_confident"] == 1
    assert row["tracks_total"] == 2


@pytest.mark.asyncio
async def test_get_tracklist_set_rows_candidate(session: AsyncSession) -> None:
    """D-04/D-08 -- an unlinked tracklist is a "candidate" set with no file path and zero counts."""
    await _seed_tracklist(session, file_id=None, match_confidence=None, external_id="cand-1")
    rows = await get_tracklist_set_rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["tracklist_state"] == "candidate"
    assert row["matched_to_file"] is False
    assert row["path"] is None
    assert row["tracks_confident"] == 0
    assert row["tracks_total"] == 0


@pytest.mark.asyncio
async def test_get_tracklist_set_rows_degrades_to_empty() -> None:
    """T-59-DOS -- a DB error degrades to ``[]`` (never raises into the render/poll)."""
    assert await get_tracklist_set_rows(_ExplodingSession()) == []  # type: ignore[arg-type]
