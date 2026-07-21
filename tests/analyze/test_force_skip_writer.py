"""Phase 87 (87-06, UI-04 / D-08/D-09/D-10): the force-skip writer endpoint.

``POST /pipeline/files/{file_id}/skip/{stage}`` writes a distinct ``stage_skip`` marker for an ENRICH
stage so the ``failed`` bucket can converge for genuinely-unprocessable files -- honestly (``skipped``,
never counterfeit ``done``). This suite locks the five correctness behaviors the writer must hold:

1. A non-enrich stage (``propose``/``review``/``apply``) returns 422 with NO row written (D-10,
   T-87-18 -- the approval-bypass hazard; backstopped by the Plan-01 DB CHECK).
2. A blank / whitespace-only reason returns the inline "A reason is required." validation fragment
   with NO row written (D-09, T-87-22).
3. A valid reason commits a ``StageSkip(file_id, stage, reason)`` row that is readable from an
   INDEPENDENT session (Pitfall 7 -- ``get_session`` NEVER auto-commits; a flush-only writer would
   pass a same-session read but fail this).
4. A NUL byte in the reason is sanitized before persist (T-87-19 -- a NUL passes pydantic then aborts
   the PG txn) and the sanitized text round-trips.
5. The writer is ADDITIVE-ONLY (T-87-20 -- behavior 6): a terminally-failed analyze keeps its
   ``analysis.failed_at`` marker after a skip, so the Phase-79 shadow-compare gate stays green.

Every DB assertion reads from an INDEPENDENT session (the ``client`` fixture overrides ``get_session``
with the shared test session, which sees UNCOMMITTED rows -- so a same-session read cannot prove the
writer committed). Must pass in the ``analyze`` bucket in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.stage_skip import StageSkip


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a FileRecord so the StageSkip.file_id FK (files.id) is satisfied, then COMMIT it."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _read_skip(file_id: uuid.UUID, stage: str) -> StageSkip | None:
    """Read the stage_skip marker from an INDEPENDENT session (proves the writer COMMITTED).

    92-04 (CLEAN-02): the read session is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (create_savepoint) -- so it shares the one outer-transaction connection and SEES in-test commits (a fresh
    ``async_sessionmaker(a fresh engine)`` would open a DIFFERENT pool connection and read ZERO/STALE).
    """
    from phaze.database import async_session

    async with async_session() as independent:
        return (await independent.execute(select(StageSkip).where(StageSkip.file_id == file_id, StageSkip.stage == stage))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_non_enrich_stage_returns_422_and_writes_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """A ``propose`` force-skip is rejected 422 before any write (D-10 enrich-only, T-87-18)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/propose", data={"reason": "trying to bypass approval"})

    assert response.status_code == 422
    assert response.json()["detail"] == "stage not force-skippable"
    assert await _read_skip(file_id, "propose") is None


@pytest.mark.asyncio
async def test_empty_reason_returns_validation_fragment_and_writes_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """A whitespace-only reason returns the inline validation fragment with NO write (D-09, T-87-22)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "   "})

    assert response.status_code == 422
    assert "A reason is required." in response.text
    assert await _read_skip(file_id, "analyze") is None


@pytest.mark.asyncio
async def test_valid_skip_is_committed_and_readable_from_independent_session(client: AsyncClient, session: AsyncSession) -> None:
    """A valid reason COMMITS the marker (readable from an independent session, Pitfall 7)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "corrupt source file"})

    assert response.status_code == 200
    marker = await _read_skip(file_id, "metadata")
    assert marker is not None
    assert marker.reason == "corrupt source file"


@pytest.mark.asyncio
async def test_duplicate_force_skip_is_idempotent_not_500(client: AsyncClient, session: AsyncSession) -> None:
    """CR-01: re-submitting a force-skip for the same (file, stage) is a no-op success, never a 500.

    ``_force_skip_dialog.html`` is not hidden after a successful skip, so a re-submit is a NORMAL path.
    A bare INSERT would hit UNIQUE(file_id, stage) and raise an unhandled IntegrityError → HTTP 500;
    ``on_conflict_do_nothing`` makes it idempotent. The first-writer's reason is preserved (do-nothing).
    """
    file_id = await _seed_file(session)

    first = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "first reason"})
    second = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "second reason"})

    assert first.status_code == 200
    assert second.status_code == 200  # would be 500 with a bare INSERT

    # Exactly one row survives (scalar_one_or_none raises MultipleResultsFound if the conflict duplicated).
    marker = await _read_skip(file_id, "fingerprint")
    assert marker is not None
    assert marker.reason == "first reason"  # do-nothing keeps the original, does not overwrite


@pytest.mark.asyncio
async def test_nul_only_reason_returns_422_and_writes_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """WR-01: a NUL/control-only reason is empty AFTER sanitize, so it must fail the D-09 gate with NO write.

    ``str.strip()`` does not remove NUL, so a raw-input blank check would let ``"\\x00"`` through and then
    persist ``""``. The gate now validates the SANITIZED value, so a NUL-only reason returns 422.
    """
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "\x00\x00"})

    assert response.status_code == 422
    assert "A reason is required." in response.text
    assert await _read_skip(file_id, "metadata") is None


@pytest.mark.asyncio
async def test_nul_in_reason_is_sanitized_and_round_trips(client: AsyncClient, session: AsyncSession) -> None:
    """A NUL byte is stripped before persist (no PG txn abort) and the sanitized text round-trips (T-87-19)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "corrupt\x00source"})

    assert response.status_code == 200
    marker = await _read_skip(file_id, "fingerprint")
    assert marker is not None
    assert marker.reason == "corruptsource"  # NUL removed; no lost text around it


@pytest.mark.asyncio
async def test_skip_never_clears_analysis_failed_at(client: AsyncClient, session: AsyncSession, verify: AsyncSession) -> None:
    """ADDITIVE-ONLY (behavior 6, T-87-20): a terminally-failed analyze keeps ``failed_at`` after a skip."""
    file_id = await _seed_file(session)
    failed_at = datetime.now(UTC)
    session.add(AnalysisResult(file_id=file_id, failed_at=failed_at, error_message="analyze crashed on this set"))
    await session.commit()

    response = await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "analyze crashes on this set"})
    assert response.status_code == 200

    # The skip marker exists AND the failure marker is untouched -- read both from an independent session.
    # 92-04 (CLEAN-02): the failure-marker read goes through the shared ``verify`` fixture (per-test connection).
    assert await _read_skip(file_id, "analyze") is not None
    row = (await verify.execute(select(AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))).first()
    assert row is not None
    assert row[0] is not None  # failed_at was NOT cleared by the additive writer


@pytest.mark.asyncio
async def test_skip_unknown_file_id_is_no_op_not_500(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-yx6s: an unknown well-formed ``file_id`` is a clean no-op ack, never a 500 (T-87-27).

    Mirrors the sibling per-file endpoints' contract. The pre-check (``_force_skip_file_exists``)
    intercepts this case before any INSERT is attempted, so no StageSkip row is written.
    """
    file_id = uuid.uuid4()  # genuinely nonexistent -- no FileRecord was ever seeded for it

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "no such file"})

    assert response.status_code == 200
    assert "File not found" in response.text
    assert await _read_skip(file_id, "metadata") is None


@pytest.mark.asyncio
async def test_skip_race_deleted_between_precheck_and_insert_is_no_op_and_session_survives(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-yx6s (request_guards.py rule 4 + rule 5): the pre-check alone is a TOCTOU hole.

    Forces the race the pre-check cannot close: ``_force_skip_file_exists`` is monkeypatched to report
    "exists" for a ``file_id`` that has NO backing ``FileRecord`` row, reproducing "file existed when
    checked, was deleted before the INSERT ran" without a second concurrent connection. The subsequent
    INSERT hits the real FK violation on ``files.id`` and must be caught -- not surfaced as a 500.

    Also proves rule 5: the caught error unwinds only the nested SAVEPOINT (``session.begin_nested()``),
    NOT a full ``session.rollback()`` -- so the session used by the request (the same object the
    ``client`` fixture's ``get_session`` override hands back on every call, per ``tests/conftest.py``)
    is still usable for a query issued immediately afterward, rather than raising
    ``PendingRollbackError``/expiring already-loaded ORM state.
    """
    import phaze.routers.pipeline as pipeline_module

    async def _fake_exists(_session: AsyncSession, _file_id: uuid.UUID) -> bool:
        return True  # lies: no FileRecord backs this file_id, simulating the post-check delete

    monkeypatch.setattr(pipeline_module, "_force_skip_file_exists", _fake_exists)

    file_id = uuid.uuid4()  # genuinely nonexistent FK referent -- the real race condition

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "raced out"})

    assert response.status_code == 200, response.text
    assert "File not found" in response.text
    assert await _read_skip(file_id, "metadata") is None

    # Rule 5 proof: the outer request transaction on `session` must still be usable -- a full
    # session.rollback() would expire every loaded object and 500 the NEXT statement on this session.
    still_usable = await session.execute(select(FileRecord.id).limit(1))
    still_usable.scalar_one_or_none()  # does not raise PendingRollbackError / InvalidRequestError

    # And the session can still commit further real work afterward.
    other_file_id = await _seed_file(session)
    assert other_file_id is not None


# --------------------------------------------------------------------------------------------------
# phaze-5p43: the success ack must ALSO refresh the record's stage pill.
#
# ``_force_skip_dialog.html``'s own header contract promises "the pill flips to ⊘ skipped on the NEXT
# poll tick". That tick never comes: the dialog ships ONLY inside ``record_body.html``, a deliberate
# SNAPSHOT (D-02 — renders once, no ``hx-trigger="every"``, and a full-body poll is forbidden because
# it would clobber in-progress inline edits). So the writer pushes the ONE pill it invalidated as an
# ``hx-swap-oob`` fragment addressed to that (file, stage). These lock the OOB shape, the honest
# re-derived bucket, and — load-bearing, given this repo's duplicate-id OOB history (phaze-gzrd,
# phaze-op6f, phaze-7j50) — that the OOB target id is UNIQUE in the composed record document.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_skip_returns_oob_pill_showing_skipped(client: AsyncClient, session: AsyncSession) -> None:
    """The 200 ack carries an OOB ``⊘ skipped`` pill for that (file, stage) — not a toast alone.

    Regression for phaze-5p43: before the fix the response body was ONLY the toast, so the record's
    Analyze pill kept reading ``✗ failed`` for the life of the open record and the operator had no
    durable confirmation once the 5s toast auto-dismissed.
    """
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC), error_message="analyze crashed"))
    await session.commit()

    # BEFORE: the open record shows the pre-skip bucket.
    assert 'aria-label="Analyze: failed"' in (await client.get(f"/record/{file_id}")).text

    response = await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "corrupt source file"})

    assert response.status_code == 200
    body = response.text
    assert f'id="stage-pill-analyze-{file_id}"' in body, "the ack must address the record's per-(file, stage) pill"
    assert 'hx-swap-oob="true"' in body, "the pill fragment must be an out-of-band swap (the ack's own target is the error box)"
    assert 'aria-label="Analyze: skipped (force-completed)"' in body, "the pushed pill must carry the honest SKIPPED bucket"
    assert "⊘" in body
    assert "Skipped analyze — reason recorded." in body, "the existing toast ack must survive alongside the pill"


@pytest.mark.asyncio
async def test_oob_pill_id_is_unique_in_the_composed_record(client: AsyncClient, session: AsyncSession) -> None:
    """The OOB target id occurs EXACTLY once in the record document and once in the ack fragment.

    A duplicate id would make HTMX swap an arbitrary one of them (the phaze-gzrd / op6f / 7j50 shape).
    The id lives on a wrapper span emitted from ONE place — ``record_body.html``'s stage loop, once per
    (stage, file) — while ``_stage_pill.html`` itself stays id-less and shared verbatim with the Files
    matrix, so no second element in the composed document can collide.
    """
    file_id = await _seed_file(session)
    marker = f'id="stage-pill-analyze-{file_id}"'

    record = (await client.get(f"/record/{file_id}")).text
    assert record.count(marker) == 1, "the OOB target id must be unique in the composed record document"
    # ... and no OTHER stage on the same record reuses it (the id is per (file, stage), not per file).
    for stage_value in ("metadata", "fingerprint", "propose", "review", "apply"):
        assert record.count(f'id="stage-pill-{stage_value}-{file_id}"') == 1

    ack = (await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "corrupt source file"})).text
    assert ack.count(marker) == 1, "the ack must push exactly one pill, not a duplicate set"


@pytest.mark.asyncio
async def test_oob_pill_reports_done_not_skipped_when_precedence_says_done(client: AsyncClient, session: AsyncSession) -> None:
    """The pushed bucket is RE-DERIVED, never hardcoded ``skipped``.

    Precedence is ``in_flight ≻ done ≻ skipped ≻ failed``, so skipping an already-done metadata stage
    must still render ``✓ done``. Hardcoding the pill would make the record lie in the other direction.
    """
    file_id = await _seed_file(session)
    session.add(FileMetadata(file_id=file_id, failed_at=None))  # metadata derives DONE
    await session.commit()

    body = (await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "belt and braces"})).text

    assert f'id="stage-pill-metadata-{file_id}"' in body
    assert 'aria-label="Meta: done"' in body, "precedence keeps a genuinely-done stage reading done, even with a skip marker"
    assert "skipped (force-completed)" not in body
