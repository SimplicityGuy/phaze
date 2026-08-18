"""Tests for `services/pipeline/analyze.py` (split from test_pipeline.py, phaze-7l8jh).

get_analyze_working_set, get_analyze_files_page, analyze_lanes_content_hash, derive_file_lane -- `services/pipeline/analyze.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_get_analyze_working_set_derives_flags_from_markers(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 90 (PR-A) + COMPUTE-03 + Phase 95 (phaze-zqvh.2): working-set rows + flags DERIVE from markers/cloud_job, not ``files.state``.

    Seeds four files whose ``files.state`` is deliberately a neutral ``DISCOVERED`` (proving the read no
    longer consults it): a completed analysis (``analysis_completed_at`` set -- surfaced via the bounded
    recent-completions window), a failed analysis (``failed_at`` set -- working set), an awaiting-cloud
    sidecar with NO ``backend_id`` stamped yet (``cloud_job(status='awaiting')`` -- working set), and an
    off-stage file with NO analysis/cloud_job (must be absent). Asserts membership + the derived
    ``completed`` / ``analysis_failed`` / ``awaiting_cloud`` flags, that no raw ``state`` key is exposed,
    and that the unattributed cloud_job (no backend_id) gets the truthful ``lane="cloud"`` fallback --
    never the stale ``"a1"`` heuristic label.
    """
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings(_backend("vox", "kueue")))

    done_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    off_stage = _make_pipeline_file()
    session.add_all([done_f, failed_f, awaiting_f, off_stage])
    await session.flush()
    session.add(_completed_analysis_for(done_f.id, fine_done=40, fine_total=40))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    by_id = {r["file_id"]: r for r in rows}

    assert str(off_stage.id) not in by_id, "a file with no analysis/cloud_job marker is not in the Analyze stage"
    assert "state" not in by_id[str(done_f.id)], "the raw scalar-state key must be gone (derived flags replace it)"

    assert by_id[str(done_f.id)]["completed"] is True
    assert by_id[str(done_f.id)]["analysis_failed"] is False
    assert by_id[str(done_f.id)]["awaiting_cloud"] is False
    assert by_id[str(done_f.id)]["lane"] == "local", "no cloud_job row -> local lane"
    assert by_id[str(done_f.id)]["lane_kind"] == "local"

    assert by_id[str(failed_f.id)]["analysis_failed"] is True
    assert by_id[str(failed_f.id)]["completed"] is False

    assert by_id[str(awaiting_f.id)]["awaiting_cloud"] is True
    assert by_id[str(awaiting_f.id)]["completed"] is False
    assert by_id[str(awaiting_f.id)]["lane"] == "cloud", "NULL backend_id -> truthful unattributed 'cloud' fallback, never 'a1'"
    assert by_id[str(awaiting_f.id)]["lane_kind"] == "cloud"


@pytest.mark.asyncio
async def test_get_analyze_working_set_lane_derives_from_backend_id(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPUTE-03: a stamped ``backend_id`` drives both ``lane`` (the id) and ``lane_kind`` (registry kind).

    A kueue-registered backend id ("vox") yields ``lane="vox"`` / ``lane_kind="kueue"``; a backend id
    that is no longer in the registry falls back to ``lane_kind="cloud"`` (deregistered cluster, still
    truthfully labeled as cloud rather than crashing or reintroducing a heuristic).
    """
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings(_backend("vox", "kueue")))

    kueue_f = _make_pipeline_file()
    stale_backend_f = _make_pipeline_file()
    session.add_all([kueue_f, stale_backend_f])
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=kueue_f.id, status=CloudJobStatus.RUNNING.value, backend_id="vox", cloud_phase="running"))
    session.add(
        CloudJob(
            id=uuid.uuid4(), file_id=stale_backend_f.id, status=CloudJobStatus.RUNNING.value, backend_id="retired-cluster", cloud_phase="running"
        )
    )
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    by_id = {r["file_id"]: r for r in rows}

    assert by_id[str(kueue_f.id)]["lane"] == "vox"
    assert by_id[str(kueue_f.id)]["lane_kind"] == "kueue"

    assert by_id[str(stale_backend_f.id)]["lane"] == "retired-cluster"
    assert by_id[str(stale_backend_f.id)]["lane_kind"] == "cloud", "a backend_id no longer in the registry falls back to 'cloud', never crashes"


@pytest.mark.asyncio
async def test_get_analyze_working_set_bounds_completions_window(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: the DEFAULT view never returns the whole completed corpus -- completions are LIMIT-ed.

    Seeds MANY completed files (more than the window) plus the active working set (in-flight + failed +
    awaiting-cloud). phaze-5462: the active working set is now PAGED rather than "returned IN FULL
    because it is naturally bounded" -- that claim was false and is the bug this bead fixed. Here the
    3 active rows fit one page, so the total is ``active (3) + window (2)`` and the completed set is
    still capped at ``completions_limit``.
    """
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings())

    # Active working set: 1 in-flight + 1 failed + 1 awaiting-cloud (all must appear, unbounded).
    inflight_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    # Completed set: 6 finished files -- MORE than the window of 2 below.
    completed = [_make_pipeline_file() for _ in range(6)]
    session.add_all([inflight_f, failed_f, awaiting_f, *completed])
    await session.flush()
    session.add(_inflight_analysis_for(inflight_f.id))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    for f in completed:
        session.add(_completed_analysis_for(f.id, fine_done=40, fine_total=40))
    await session.commit()

    rows = (await get_analyze_working_set(session, completions_limit=2)).rows
    by_id = {r["file_id"]: r for r in rows}

    # The full active working set is present (in-flight / failed / awaiting-cloud), regardless of window.
    assert str(inflight_f.id) in by_id and not by_id[str(inflight_f.id)]["completed"]
    assert str(failed_f.id) in by_id and by_id[str(failed_f.id)]["analysis_failed"]
    assert str(awaiting_f.id) in by_id and by_id[str(awaiting_f.id)]["awaiting_cloud"]
    # The completed set is BOUNDED to the window (2), not all 6 -- the corpus never lands in the DOM.
    completed_returned = [r for r in rows if r["completed"]]
    assert len(completed_returned) == 2, "completed files beyond the window must NOT be returned by the default view"
    # Total is working-set (3) + window (2) == 5, never the full membership (9).
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_get_analyze_working_set_is_active_first(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: active work renders BEFORE the recent-completions window (active-first ordering)."""
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings())

    inflight_f = _make_pipeline_file()
    completed_f = _make_pipeline_file()
    session.add_all([inflight_f, completed_f])
    await session.flush()
    session.add(_inflight_analysis_for(inflight_f.id))
    session.add(_completed_analysis_for(completed_f.id, fine_done=40, fine_total=40))
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    order = [r["file_id"] for r in rows]
    assert order.index(str(inflight_f.id)) < order.index(str(completed_f.id)), "active work must render before completions"


@pytest.mark.asyncio
async def test_get_analyze_files_page_paginates_without_overlap(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: the full-listing pager is bounded (page_size) with a +1 sentinel has_next, no overlap.

    ``page_size`` is clamped to a 10-row floor (parity with ``get_files_page``), so 25 completed files at
    ``page_size=10`` partition into 10 + 10 + 5: page1/page2 carry ``has_next`` (the +1 sentinel), page3 is
    the last. The three pages partition the corpus with NO duplicated or skipped id (the id tiebreaker).
    """
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings())

    files = [_make_pipeline_file() for _ in range(25)]
    session.add_all(files)
    await session.flush()
    for f in files:
        session.add(_completed_analysis_for(f.id, fine_done=1, fine_total=1))
    await session.commit()

    seen: list[str] = []
    page1 = await get_analyze_files_page(session, page=1, page_size=10, status="completed")
    assert len(page1.rows) == 10 and page1.has_next is True
    page2 = await get_analyze_files_page(session, page=2, page_size=10, status="completed")
    assert len(page2.rows) == 10 and page2.has_next is True
    page3 = await get_analyze_files_page(session, page=3, page_size=10, status="completed")
    assert len(page3.rows) == 5 and page3.has_next is False
    for pg in (page1, page2, page3):
        seen.extend(r["file_id"] for r in pg.rows)
    assert sorted(seen) == sorted(str(f.id) for f in files), "pages must partition the corpus with no overlap/gap"


@pytest.mark.asyncio
async def test_get_analyze_files_page_status_filter_lens(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: each status lens returns ONLY its bucket; unknown status degrades to the full membership.

    Seeds one completed + one failed + one awaiting-cloud + one off-stage file. ``completed`` /
    ``failed`` / ``awaiting_cloud`` each return exactly their file; an unknown status degrades to the
    full analyze-stage membership (all three in-stage files, off-stage excluded) -- never a 422.
    """
    monkeypatch.setattr(pipeline_analyze_mod, "get_settings", lambda: _backend_settings())

    done_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    off_stage = _make_pipeline_file()
    session.add_all([done_f, failed_f, awaiting_f, off_stage])
    await session.flush()
    session.add(_completed_analysis_for(done_f.id, fine_done=1, fine_total=1))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    completed = await get_analyze_files_page(session, status="completed")
    assert {r["file_id"] for r in completed.rows} == {str(done_f.id)}
    failed = await get_analyze_files_page(session, status="failed")
    assert {r["file_id"] for r in failed.rows} == {str(failed_f.id)}
    awaiting = await get_analyze_files_page(session, status="awaiting_cloud")
    assert {r["file_id"] for r in awaiting.rows} == {str(awaiting_f.id)}
    # Unknown status -> full analyze-stage membership (all three in-stage), off-stage excluded, never 422.
    unknown = await get_analyze_files_page(session, status="not-a-real-status")
    ids = {r["file_id"] for r in unknown.rows}
    assert ids == {str(done_f.id), str(failed_f.id), str(awaiting_f.id)}
    assert str(off_stage.id) not in ids
    assert unknown.status is None, "an unknown status degrades to the unfiltered listing"


def test_analyze_lanes_content_hash_is_stable_and_state_sensitive() -> None:
    """phaze-zqvh.3: the grid content hash is deterministic, changes with lane state / selection, degrade-safe.

    The hash is the server side of the idempotent-swap: identical inputs MUST yield an identical digest
    (so an unchanged tick is byte-identical and the client skips the swap), while ANY change to the lane
    snapshot OR the selected-lane highlight MUST change it (so a real update still swaps). A
    non-serializable input degrades to ``""`` (fail-safe: an empty hash never matches -> always swap).
    """
    lanes_a = [{"id": "a1", "kind": "compute", "in_flight": 2, "cap": 4, "available": True}]
    lanes_b = [{"id": "a1", "kind": "compute", "in_flight": 3, "cap": 4, "available": True}]

    # Deterministic + non-empty for identical inputs.
    h1 = analyze_lanes_content_hash(lanes_a, None)
    assert h1 and h1 == analyze_lanes_content_hash(lanes_a, None)
    # A changed datum (in_flight) changes the hash.
    assert analyze_lanes_content_hash(lanes_b, None) != h1
    # A changed selection changes the hash (the highlight is part of the rendered grid).
    assert analyze_lanes_content_hash(lanes_a, "a1") != h1
    # Empty snapshot is stable + non-crashing.
    assert analyze_lanes_content_hash([], None) == analyze_lanes_content_hash([], None)
    # Degrade-safe: an un-serializable input (a circular reference -- unhandled even by default=str)
    # collapses to "" (fail-safe -> an empty hash never matches, so the grid always swaps).
    circular: dict[str, object] = {}
    circular["self"] = circular
    assert analyze_lanes_content_hash([circular], None) == ""
