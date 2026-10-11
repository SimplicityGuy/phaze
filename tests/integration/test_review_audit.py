"""REVIEW-05 audit integration tests: one audit row per apply + reversibility.

Runs against the real integration-test Postgres -- the whole ``tests/integration/`` package is
auto-marked ``integration`` by the ``tests/conftest.py`` path rule, and these tests additionally
consume the DB-backed ``client`` + ``session`` fixtures (which build tables on
``TEST_DATABASE_URL``). The SAQ queues are the shared fakes, so the DB audit trail -- the subject
under test -- is exercised end-to-end while the dispatch is captured instead of delivered.

phaze-6bkk: the on-disk write is no longer patched, because the api no longer performs one. Under
DIST-01 it has no media mount, so both routes create the ``TagWriteLog`` row in ``queued`` and hand
the write to the owning agent. The REVIEW-05 property under test is unchanged and is exactly what
survives the move: ONE audit row per apply, append-only, carrying the payload a reversal replays.

Proves REVIEW-05 over the EXISTING apply endpoints (no new audit/undo logic, D-04):
  (a) a single ``POST /tags/{id}/write`` produces exactly ONE ``TagWriteLog`` row;
  (b) ``POST /tags/{id}/undo`` re-applies ``before_tags`` via ``enqueue_tag_write`` (append-only);
  (c) a ``POST /duplicates/{hash}/resolve`` writes exactly one resolution and its undo round-trips
      the ``file_states`` blob.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re
from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest
from sqlalchemy import func, select

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.routers.tags import (
    _encode_tag_review_token,
    _get_accepted_discogs_link,
    _get_file_with_metadata,
    _get_tracklist_for_file,
    _tag_review_payload,
)
from phaze.services.review_tagwrite import get_recent_completed_tagwrite_links
from phaze.services.tag_proposal import compute_proposed_tags
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


async def _executed_file(
    session: AsyncSession,
    *,
    artist: str | None = "Old Artist",
    filename: str = "Old Artist - Old Title.mp3",
    sha256: str | None = None,
    file_size: int = 5_000_000,
) -> FileRecord:
    """Insert an APPLIED FileRecord + FileMetadata (the tag-apply target).

    READ-05 / D-01: the tag-apply guard now reads ``applied()`` — an ``executed`` ``RenameProposal``
    exists — NOT ``file.state == 'executed'`` (no ``src/`` writer produces that value). So this seeds an
    ``executed`` proposal and sets the file's own ``state`` to ``MOVED`` (the real apply-path outcome).
    The ``MOVED`` state makes the fixture mutation-sensitive: a guard reverted to ``state == EXECUTED``
    would reject it, so the audit genuinely exercises the ``proposals.status`` predicate.
    """
    file = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=sha256 or (uuid.uuid4().hex + uuid.uuid4().hex),
        original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
        original_filename=filename,
        current_path=f"/dest/{filename}",
        file_type="mp3",
        file_size=file_size,
    )
    session.add(file)
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file.id, artist=artist, title="Old Title"))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    await session.refresh(file)
    return file


async def _tag_log_count(session: AsyncSession, file_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
    return (await session.execute(stmt)).scalar_one()


@pytest.mark.parametrize("populated", [False, True])
async def test_review_and_audit_link_completed_tags_to_existing_file_history(client: AsyncClient, session: AsyncSession, populated: bool) -> None:
    """A completed write leaves the decision queue but stays discoverable in both workspace shapes."""
    file = await _executed_file(session, filename="history-example.mp3")
    if populated:
        session.add(TagWriteLog(file_id=file.id, before_tags={}, after_tags={"artist": "Example"}, source="manual", status="completed"))
        await session.commit()
    for path in ("/s/rename", "/s/rename?status=approved", "/s/audit"):
        for headers in ({}, {"HX-Request": "true"}, {"HX-Request": "true", "HX-History-Restore-Request": "true"}):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            soup = BeautifulSoup(response.text, "html.parser")
            panel = soup.select_one("[data-tagwrite-history-links]")
            assert panel is not None
            assert "ten most recent" in panel.get_text()
            assert "For older events" in panel.get_text()
            links = panel.select(f'a[href="/files/{file.id}#history"]')
            assert len(links) == int(populated)
            assert "Completed tag writes remain available in Audit" not in response.text
            if not populated:
                assert "No completed tag writes or undo actions have been recorded" in panel.get_text()
            if "/s/audit" in path:
                assert "Tag writes and undo actions do not appear in this execution table" in response.text
            if headers == {"HX-Request": "true"}:
                assert "<html" not in response.text
            else:
                assert "<html" in response.text
    if populated:
        record = await client.get(f"/files/{file.id}")
        history = BeautifulSoup(record.text, "html.parser").select_one("details#history")
        assert history is not None
        assert "tag write" in history.get_text() and "completed" in history.get_text()
        assert await _tag_log_count(session, file.id) == 1, "navigation must not write or duplicate audit records"


async def test_recent_completed_tag_links_are_bounded_ordered_and_include_undo(session: AsyncSession) -> None:
    file = await _executed_file(session)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(12):
        session.add(
            TagWriteLog(
                id=uuid.UUID(int=index + 1),
                file_id=file.id,
                before_tags={},
                after_tags={},
                source="undo" if index == 11 else "manual",
                status="completed",
                written_at=when + timedelta(seconds=index),
            )
        )
    session.add(TagWriteLog(file_id=file.id, before_tags={}, after_tags={}, source="manual", status="failed", written_at=when + timedelta(days=1)))
    await session.commit()
    result = await get_recent_completed_tagwrite_links(session)
    assert not result.degraded
    assert len(result.rows) == 10
    assert result.rows[0]["source"] == "undo"
    assert [row["written_at"] for row in result.rows] == [when + timedelta(seconds=index) for index in range(11, 1, -1)]


async def test_unavailable_recent_tag_links_do_not_claim_no_history(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.services.review_tagwrite import TagwriteHistoryLinks

    async def unavailable(session: AsyncSession) -> TagwriteHistoryLinks:
        return TagwriteHistoryLinks([], True)

    monkeypatch.setattr("phaze.routers.execution.get_recent_completed_tagwrite_links", unavailable)
    response = await client.get("/s/audit")
    assert response.status_code == 200
    assert "Recent tag-write links could not be loaded" in response.text
    assert "No completed tag writes or undo actions have been recorded" not in response.text


async def test_recent_tag_link_read_failure_is_degraded_and_keeps_session_usable(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed_read(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic history read failure")

    with monkeypatch.context() as patch:
        patch.setattr(session, "execute", failed_read)
        result = await get_recent_completed_tagwrite_links(session)
    assert result.degraded and not result.rows
    assert (await session.execute(select(func.count()).select_from(TagWriteLog))).scalar_one() == 0


async def _review_token(session: AsyncSession, file_id: uuid.UUID) -> str:
    """Mint the reviewed-payload token ``POST /tags/{id}/write`` now requires.

    phaze-tzy6s.11 / ADR-0008 (changes review approval boundary): these tests used to post a bare ``{"artist": ...}`` form, which the
    route accepted as a ``manual_edit`` tag write. That path is gone -- Changes Review is the only
    surface that authorizes a tag write, and it authorizes exactly the payload it rendered, so the
    route takes a ``review_token`` and rejects anything else with 400. The REVIEW-05 properties
    below (one audit row per apply; undo replays the recorded snapshot) are unchanged; only the way
    the write is authorized moved. Mirrors ``tests/review/routers/test_tags.py::_review_token``;
    the render-to-submit half of the chain is covered by ``tests/review/routers/test_changes_review.py``.
    """
    file_record = await _get_file_with_metadata(session, file_id)
    assert file_record is not None
    tracklist = await _get_tracklist_for_file(session, file_id)
    link = await _get_accepted_discogs_link(session, file_id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=link)
    return _encode_tag_review_token(_tag_review_payload(file_record, tracklist, link, proposed))


@pytest.mark.asyncio
async def test_tag_write_produces_exactly_one_audit_row(client: AsyncClient, session: AsyncSession) -> None:
    """(a) A single ``POST /tags/{id}/write`` appends exactly one ``TagWriteLog`` row."""
    file = await _executed_file(session, artist="Old Artist")
    install_fake_queues(client)
    resp = await client.post(f"/tags/{file.id}/write", data={"review_token": await _review_token(session, file.id)})
    assert resp.status_code == 200
    assert await _tag_log_count(session, file.id) == 1


@pytest.mark.asyncio
async def test_tag_write_without_a_review_token_is_refused_and_audits_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """(a') ADR-0008 (changes review approval boundary): an unreviewed write is refused BEFORE it can append an audit row.

    The retired ``manual_edit`` form shape is the exact request an out-of-band caller would replay,
    so this pins that the boundary is enforced server-side and leaves the audit trail untouched --
    a 400 that still wrote a row would be worse than the old behaviour, not better.
    """
    file = await _executed_file(session, artist="Old Artist")
    install_fake_queues(client)
    resp = await client.post(f"/tags/{file.id}/write", data={"artist": "New Artist"})
    assert resp.status_code == 400
    assert await _tag_log_count(session, file.id) == 0


@pytest.mark.asyncio
async def test_tag_undo_reapplies_before_tags(client: AsyncClient, session: AsyncSession) -> None:
    """(b) Undo re-applies the snapshot captured before the write, via ``enqueue_tag_write``.

    phaze-6bkk: ``before_tags`` is captured on the AGENT (only it can read the file) and reaches the
    audit row through the result callback, so this test drives the full round trip -- write ->
    agent-reports-COMPLETED-with-snapshot -> undo -- rather than patching mutagen. That the undo
    replays the recorded pre-write snapshot, and appends exactly one further row, is the REVIEW-05
    property; it is unchanged by the move.
    """
    file = await _executed_file(session, artist="Old Artist")
    _controller_queue, router = install_fake_queues(client)
    write = await client.post(f"/tags/{file.id}/write", data={"review_token": await _review_token(session, file.id)})
    assert write.status_code == 200

    # The agent reports back the snapshot it read off disk immediately before writing.
    written_log = (await session.execute(select(TagWriteLog).where(TagWriteLog.file_id == file.id))).scalar_one()
    written_log.status = TagWriteStatus.COMPLETED.value
    written_log.before_tags = {"artist": "Old Artist"}
    await session.commit()

    router.captures.clear()
    undo = await client.post(f"/tags/{file.id}/undo")
    assert undo.status_code == 200

    # Undo dispatched the pre-write snapshot ("Old Artist"), reusing enqueue_tag_write.
    dispatched = [k for _q, t, k in router.captures if t == "write_file_tags"]
    assert len(dispatched) == 1
    assert dispatched[0]["tags"].get("artist") == "Old Artist"
    # Append-only: write + undo = two audit rows.
    assert await _tag_log_count(session, file.id) == 2


@pytest.mark.asyncio
async def test_tag_undo_missing_log_returns_404(client: AsyncClient, session: AsyncSession) -> None:
    """Undo on a file with no prior write log redraws the pending row with a toast (nothing to reverse).

    phaze-y4s6: routers/tags.py's write/undo routes always return the v7 _diff_row.html shape now
    (the legacy non-v7 bare-404 fallback had no live caller left and was removed), so this is a
    200 + toast, not a bare 404.
    """
    file = await _executed_file(session)
    resp = await client.post(f"/tags/{file.id}/undo")
    assert resp.status_code == 200
    assert "No prior tag write to undo." in resp.text


@pytest.mark.asyncio
async def test_dedupe_resolve_one_resolution_and_undo_round_trips(client: AsyncClient, session: AsyncSession) -> None:
    """(c) Resolve marks exactly one non-canonical file; undo round-trips the file_states blob.

    The dedupe "audit row" is the durable ``DedupResolution`` marker (D-07 / Phase 90 D-09: the
    ``FileRecord.state`` dual-write was removed, so the marker is the sole authority): exactly one
    non-canonical file gets a marker per resolve, and undo DELETEs it from the round-tripped
    ``file_states`` JSON -- reversibility with zero new logic.
    """
    from phaze.models.dedup_resolution import DedupResolution

    shared = uuid.uuid4().hex + uuid.uuid4().hex
    canonical = await _executed_file(session, filename="keep.mp3", sha256=shared, file_size=1000)
    other = await _executed_file(session, filename="dupe.mp3", sha256=shared, file_size=2000)

    review = await client.post(f"/duplicates/{shared}/review", data={"canonical_id": str(canonical.id)})
    assert review.status_code == 200
    plan_match = re.search(r'name="plan_id" value="([^"]+)"', review.text)
    assert plan_match is not None

    resolve = await client.post(f"/duplicates/{shared}/resolve", data={"plan_id": plan_match.group(1)})
    assert resolve.status_code == 200

    resolved_stmt = select(func.count()).select_from(DedupResolution).where(DedupResolution.file_id == other.id)
    assert (await session.execute(resolved_stmt)).scalar_one() == 1, "exactly one resolution marker per resolve"

    # Undo round-trips the file_states blob the resolve response carries. phaze-btix: the CAS DELETE
    # now requires both the file id and the canonical_id the marker was written with, not id alone.
    file_states = json.dumps([{"id": str(other.id), "canonical_id": str(canonical.id)}])
    undo = await client.post(f"/duplicates/{shared}/undo", data={"file_states": file_states})
    assert undo.status_code == 200

    # The marker is gone -> the file derives ~dedup_resolved_clause() again (Phase 90 D-09).
    remaining = (await session.execute(select(func.count()).select_from(DedupResolution).where(DedupResolution.file_id == other.id))).scalar_one()
    assert remaining == 0, "undo DELETEs the resolution marker"
