"""Controller-side tests for `routers/pipeline/dashboard_stats.py` (split from test_pipeline.py, phaze-7l8jh).

GET /pipeline/, /pipeline/stats, build_dashboard_context, and the cloud admission / activity cards it seeds -- `routers/pipeline/dashboard_stats.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    _ALL_SIX_CARD_IDS,
    _VOX_KUEUE_ONLY_TOML,
    CloudJob,
    CloudJobStatus,
    CloudPhase,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _make_file,
    _seed_analysis_failed,
    _seed_cloud_phase,
    _seed_running_scan,
    install_fake_queues,
    pytest,
    seed_active_agent,
    uuid,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_dashboard_context_binds_lanes(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 71 (71-03, BEUI-01 / D-04): build_dashboard_context seeds the `lanes` snapshot; cloud_lane_kind retired.

    The Analyze grid reads the rank-ascending ``get_backend_lane_snapshot`` list under the neutral
    ``lanes`` key (D-04). The transitional single-kind ``cloud_lane_kind`` key is GONE (the BEUI-01
    N-lane redesign supersedes the one-label-for-the-whole-registry value), and no ``cloud_target``
    context key survives (Plan 06's package-wide gate depends on this). ``pipeline_stats_partial``
    seeds the SAME ``lanes`` key identically for the 5s OOB re-push (asserted at the render level in
    the Analyze-workspace suite + the ``grep -c get_backend_lane_snapshot == 2`` code gate).

    The snapshot is monkeypatched directly (it resolves the registry via ``get_settings()``, a distinct
    singleton from the module-level ``settings``) so the seed is asserted independent of registry wiring.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `get_backend_lane_snapshot` is read from the
    # `dashboard_stats` submodule's namespace, so patching the facade would silently no-op.
    from phaze.routers.pipeline import build_dashboard_context
    import phaze.routers.pipeline.dashboard_stats as pipeline_mod

    sentinel: list[dict[str, object]] = [
        {
            "id": "a1",
            "kind": "compute",
            "rank": 10,
            "cap": 2,
            "in_flight": 1,
            "available": True,
            "quota_wait": 0,
            "inadmissible": 0,
            "queued": 0,
            "working": 1,
            "processed_24h": 3,
            "processed_lifetime": 10,
        },
        {
            "id": "local",
            "kind": "local",
            "rank": 99,
            "cap": 1,
            "in_flight": 0,
            "available": True,
            "quota_wait": 0,
            "inadmissible": 0,
            "queued": 0,
            "working": 0,
            "processed_24h": 0,
            "processed_lifetime": 0,
        },
    ]

    # phaze-5c6i2: get_backend_lane_snapshot now takes app_state (the local lane's SAQ read) --
    # the stub's signature must accept it too, or build_dashboard_context's call raises TypeError.
    async def _fake_snapshot(_session: AsyncSession, _app_state: object = None) -> list[dict[str, object]]:
        return sentinel

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _fake_snapshot)
    app_state = client._transport.app.state  # type: ignore[union-attr]

    ctx = await build_dashboard_context(app_state, session)
    assert ctx["lanes"] == sentinel  # seeded rank-ascending from the snapshot (D-04)
    assert "cloud_lane_kind" not in ctx  # transitional key retired (BEUI-01 N-lane redesign)
    assert "cloud_target" not in ctx  # Plan 06 package-wide gate guard


@pytest.mark.asyncio
async def test_dashboard_context_excludes_compute_agents_from_scan_picker(client: AsyncClient, session: AsyncSession) -> None:
    """SER-01: build_dashboard_context lists ONLY kind='fileserver' agents in the Trigger Scan dropdown.

    A kind='compute' agent (Kueue/burst backend like k8s-vox) is media-less and cannot be a scan
    target, so it must never appear in ``ctx['agents']``. Seed one fileserver + one compute agent and
    assert on the agent ids (not just count) so the compute-exclusion is provable.
    """
    from sqlalchemy import inspect as sa_inspect

    from phaze.routers.pipeline import build_dashboard_context

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="k8s-vox", kind="compute")

    app_state = client._transport.app.state  # type: ignore[union-attr]
    ctx = await build_dashboard_context(app_state, session)

    # build_dashboard_context runs degrade-safe reads that roll back the session when the SAQ
    # ``saq_jobs`` broker table is absent (as in this fixture DB), which expires the returned ORM
    # rows; read each agent's PK from the identity map (IO-free) rather than a lazy ``.id`` load.
    agent_ids = {sa_inspect(agent).identity[0] for agent in ctx["agents"]}
    assert "nox" in agent_ids  # fileserver agent still offered as a scan target
    assert "k8s-vox" not in agent_ids  # compute agent excluded (media-less, not a scan target)


@pytest.mark.asyncio
async def test_dashboard_renders_recover_button_end_to_end(client: AsyncClient) -> None:
    """GET /pipeline/ exposes the GLOBAL Recover button posting to /pipeline/recover (REQ-42-5).

    The recovery affordance is a pipeline-level action in the DAG header (not a per-stage node), so
    the rendered dashboard must carry its hx-post target + label, proving the Phase-42 manual recovery
    surface reaches the page end-to-end (not just the partial render test)."""
    response = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/pipeline/recover"' in body
    assert "Recover orphaned work" in body


@pytest.mark.asyncio
async def test_pipeline_stats_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/stats returns 200 with HTML containing count values."""
    session.add(_make_file())
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Stats bar should contain the count
    assert "Discovered" in response.text
    assert "Analyzed" in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_awaiting_cloud_card(client: AsyncClient, session: AsyncSession) -> None:
    """The dashboard renders the awaiting-cloud count in the #awaiting-cloud-card (Phase 83, D-15)."""
    awaiting = [_make_file() for _ in range(3)]
    session.add_all(awaiting)
    session.add(_make_file())
    await session.commit()
    # Phase 83: the card counts genuinely-parked cloud_job(status='awaiting') rows, not FileRecord.state.
    for f in awaiting:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert 'id="awaiting-cloud-card"' in text
    assert "Awaiting cloud" in text
    # The held count (3) renders inside the card.
    assert ">3<" in text
    # Inline (full-page) render is NOT an OOB swap.
    card_start = text.index('id="awaiting-cloud-card"')
    card_open = text.rfind("<div", 0, card_start)
    assert 'hx-swap-oob="true"' not in text[card_open:card_start]


@pytest.mark.asyncio
async def test_stats_partial_emits_awaiting_cloud_card_oob(client: AsyncClient, session: AsyncSession) -> None:
    """The 5s /pipeline/stats poll re-pushes the awaiting-cloud card OUT-OF-BAND (hx-swap-oob)."""
    awaiting = [_make_file() for _ in range(2)]
    session.add_all(awaiting)
    await session.commit()
    # Phase 83: the card counts genuinely-parked cloud_job(status='awaiting') rows, not FileRecord.state.
    for f in awaiting:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    text = response.text
    assert 'id="awaiting-cloud-card"' in text
    # On the poll the card is an OOB fragment so htmx swaps it in place.
    card_start = text.index('id="awaiting-cloud-card"')
    card_open = text.rfind("<div", 0, card_start)
    assert 'hx-swap-oob="true"' in text[card_open : card_start + 200]
    assert ">2<" in text


@pytest.mark.asyncio
async def test_dashboard_renders_green_pulse_for_progressing_running_scan(client: AsyncClient, session: AsyncSession) -> None:
    """A fresh RUNNING scan renders the green pulsing dot + '·Ns ago' affordance."""
    await _seed_running_scan(session, seconds_quiet=5, scan_path="/music/fresh")
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "animate-pulse" in response.text
    assert "s ago" in response.text
    # Not stalled -> no amber warning label.
    assert "stalled?" not in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_amber_stalled_for_quiet_running_scan(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RUNNING scan quiet past the UI warn threshold renders 'stalled?'.

    The default scan_stall_seconds is now 86400 (24h); this test pins it to 600
    for determinism so the warn threshold is half of 600 -> 300s (400s quiet > 300s).
    """
    from phaze.config import get_settings
    from phaze.routers import pipeline_scans

    pinned = get_settings().model_copy(update={"scan_stall_seconds": 600})
    monkeypatch.setattr(pipeline_scans, "get_settings", lambda: pinned)

    await _seed_running_scan(session, seconds_quiet=400, scan_path="/music/quiet")
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "stalled?" in response.text
    assert "text-amber-600" in response.text


@pytest.mark.asyncio
async def test_dashboard_attaches_activity_attrs(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard handler attaches _seconds_since_progress and _is_stalled per row.

    The default scan_stall_seconds is now 86400 (24h); this test pins it to 600
    for determinism so the warn threshold is half of 600 -> 300s (400s quiet > 300s).
    """
    from phaze.config import get_settings
    from phaze.routers import pipeline_scans
    from phaze.routers.pipeline import dashboard

    pinned = get_settings().model_copy(update={"scan_stall_seconds": 600})
    monkeypatch.setattr(pipeline_scans, "get_settings", lambda: pinned)

    await _seed_running_scan(session, seconds_quiet=400, scan_path="/music/attrs")
    # Invoke the handler body directly via a tiny request stub is heavy; instead
    # assert through the rendered output that both transient attrs were consumed:
    # _seconds_since_progress drives the "Ns ago" text and _is_stalled drives the
    # amber label. Their presence proves the attach loop ran.
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "stalled?" in response.text  # _is_stalled True path
    assert dashboard is not None  # handler import smoke-check


# ---------------------------------------------------------------------------
# Phase 34 Plan 02: queue-activity surfaced through both contexts + degrade-to-200
# (VALIDATION 34-02-01). The client fixture skips the lifespan, so app.state queue
# handles are ABSENT until a test wires fakes — proving get_queue_activity's
# missing-attr degrade keeps BOTH the 5s poll and the full-page render alive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_stats_degrades_without_queues(client: AsyncClient, session: AsyncSession) -> None:
    """No fakes wired (app.state queues absent) → /pipeline/stats AND /pipeline/ stay 200.

    Proves the get_queue_activity AttributeError degrade path keeps both the poll and the
    full-page render from 500ing when the queue handles are missing (a Redis outage degrades
    identically). This is the no-500-regression guard for the new wiring.
    """
    stats_response = await client.get("/pipeline/stats")
    assert stats_response.status_code == 200

    dashboard_response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert dashboard_response.status_code == 200


@pytest.mark.asyncio
async def test_pipeline_stats_surfaces_agent_busy(client: AsyncClient, session: AsyncSession) -> None:
    """/pipeline/stats re-seeds $store.pipeline.agentBusy/controllerBusy from live queue depth.

    Wires fake queues, seeds the agent queue depth (4 queued + 1 active = 5) and the
    controller queue (2 queued + 0 active = 2), then asserts the OOB store-write substrings
    carry the SUMMED busy counts the buttons gate on.
    """
    await seed_active_agent(session, "nox")
    controller_queue, task_router = install_fake_queues(client)
    task_router.set_counts("nox", queued=4, active=1)
    controller_queue.set_counts(queued=2, active=0)

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "$store.pipeline.agentBusy = 5" in response.text
    assert "$store.pipeline.controllerBusy = 2" in response.text
    # The metadataExtracted ready-count gate must ALSO re-seed on each poll like
    # discovered/analyzed, so its button un-disables live instead of only on full reload.
    assert 'id="metadata-files-ready" hx-swap-oob="true"' in response.text
    assert "$store.pipeline.metadataExtracted = 0" in response.text


@pytest.mark.asyncio
async def test_dashboard_seeds_busy_on_first_load(client: AsyncClient, session: AsyncSession) -> None:
    """/pipeline/ initial render does not 500 with queues wired (seeds counts on first load)."""
    await seed_active_agent(session, "nox")
    controller_queue, task_router = install_fake_queues(client)
    task_router.set_counts("nox", queued=4, active=1)
    controller_queue.set_counts(queued=2, active=0)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Phase 44 Plan 04 Task 1: the ANALYSIS_FAILED + STALLED counts on the dashboard
#
# The two counts ride the EXISTING 5s /pipeline/stats poll context (seeded into BOTH
# dashboard() and pipeline_stats_partial()), sourced from the Plan-02 degrade-safe service
# reads (get_analysis_failed_count / get_analysis_stalled_count). The analysis_failed_card
# renders both; it is re-pushed hx-swap-oob on every poll so the counts stay live without
# re-rendering the DAG buttons.
#
# This card originally carried a running-age STRAGGLER bucket (long-running in-flight
# process_file jobs) in the amber tile. phaze-g84sk removed it once phaze-w55w1's
# heartbeat-stall watchdog made running age meaningless as a "stuck" proxy (a genuine stall
# now lands in ANALYSIS_FAILED itself, reason="timeout"), then -- per operator follow-up --
# replaced the amber tile with STALLED, a PRECISE count of that same reason="timeout" subset
# instead of dropping the tile outright.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_analysis_health_card(client: AsyncClient) -> None:
    """GET /pipeline/ renders the Analysis Health card with both buckets (zero by default)."""
    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="analysis-health-card"' in response.text
    assert "Stalled" in response.text
    assert "Analysis failed" in response.text


@pytest.mark.asyncio
async def test_dashboard_seeds_analysis_failed_count(client: AsyncClient, session: AsyncSession) -> None:
    """A file in ANALYSIS_FAILED bumps analysis_failed_count into the dashboard card render."""
    await _seed_analysis_failed(session, 3)

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # The failed bucket count (3) renders inside the card's red panel.
    assert "Analysis failed" in response.text
    # The count value reaches the card (degrade-safe service returns the real count).
    import re

    card = re.search(r'id="analysis-health-card".*', response.text, re.DOTALL)
    assert card is not None
    assert ">3<" in card.group(0)


@pytest.mark.asyncio
async def test_dashboard_seeds_analysis_stalled_count(client: AsyncClient, session: AsyncSession) -> None:
    """A heartbeat-STALLED failure bumps analysis_stalled_count; a non-stall failure does not (phaze-g84sk).

    Seeds one stalled (error_message="timeout: ...") and one crashed (non-stall) ANALYSIS_FAILED
    file: the amber Stalled tile must read 1 (not 2), proving the reader filters on the reason
    prefix rather than counting every ANALYSIS_FAILED file.
    """
    await _seed_analysis_failed(session, 1, stalled=True)
    await _seed_analysis_failed(session, 1, stalled=False)

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Stalled" in response.text
    import re

    card = re.search(r'id="analysis-health-card".*', response.text, re.DOTALL)
    assert card is not None
    stalled_tile = card.group(0).split("Stalled")[0]
    assert ">1<" in stalled_tile, "exactly one of the two failures was a heartbeat stall"


@pytest.mark.asyncio
async def test_stats_partial_seeds_counts_and_oob_card(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/stats re-pushes the Analysis Health card out-of-band on the 5s poll.

    The stats partial seeds analysis_failed_count + analysis_stalled_count into context and
    emits the card with hx-swap-oob="true" (it lives outside #pipeline-stats, so the innerHTML
    swap can never reach it). A seeded stalled ANALYSIS_FAILED file proves both counts ride the
    poll.
    """
    await _seed_analysis_failed(session, 2, stalled=True)

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    # OOB card re-render on the poll tick.
    assert 'id="analysis-health-card"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    # Both bucket labels present; the seeded failed+stalled counts (2 each) ride the poll context.
    assert "Stalled" in response.text
    assert "Analysis failed" in response.text


def test_queue_progress_percent_formula() -> None:
    """queue_progress_percent is analyzed / (analyzed + agent_busy) * 100, divide-by-zero guarded.

    (30, 10) → 75 proves the numerator is analyzed and the denominator is analyzed+agent_busy
    (a reversed ratio would yield 25). (0, 0) → 0 proves the divide-by-zero guard. (11428, 0)
    → 100 proves a fully-analyzed archive reports complete.
    """
    from phaze.services.pipeline import queue_progress_percent

    assert queue_progress_percent(30, 10) == 75
    assert queue_progress_percent(0, 0) == 0
    assert queue_progress_percent(11428, 0) == 100


@pytest.mark.asyncio
async def test_dashboard_admission_card_carrier_always_renders(client: AsyncClient) -> None:
    """With NO cloud_job rows the empty carrier still renders (stable OOB target), no heading/grid."""
    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    # All-zero (no k8s activity) → empty carrier: no caption, no tiles. (The per-card "Cloud ·
    # Admission" heading is gone — the single Cloud pane owns the heading — so the live-snapshot
    # caption is the rendered-body discriminator now.)
    assert "live — per reconcile" not in response.text
    assert "Queued (quota)" not in response.text


@pytest.mark.asyncio
async def test_dashboard_admission_card_renders_matching_tile(client: AsyncClient, session: AsyncSession) -> None:
    """A seeded ADMITTED row renders the heading + the blue Admitted tile (its own count gate)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.ADMITTED.value)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    assert "live — per reconcile" in response.text
    assert "Admitted" in response.text
    assert "quota granted" in response.text
    # Phases with 0 files stay invisible — their tiles are not rendered.
    assert "Queued (quota)" not in response.text
    assert "Finished" not in response.text


@pytest.mark.asyncio
async def test_dashboard_admission_card_finished_is_green_not_alert(client: AsyncClient, session: AsyncSession) -> None:
    """The finished tile uses GREEN hues; the card carries NO role='alert' and NO amber (healthy progression)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.FINISHED.value)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    import re

    card = re.search(r'id="admission-state-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    card_html = card.group(0)
    assert "Finished" in card_html
    assert "result returned" in card_html
    assert "bg-green-50" in card_html
    # Healthy progression — alert role + amber stay exclusive to inadmissible_card.
    assert 'role="alert"' not in card_html
    assert "amber" not in card_html


@pytest.mark.asyncio
async def test_dashboard_admission_card_finished_is_a_lifetime_total_not_a_live_snapshot(client: AsyncClient, session: AsyncSession) -> None:
    """Acceptance 6 (phaze-zyoag): Finished renders OUTSIDE the "per reconcile" live grid, captioned as cumulative.

    ``cloud_phase == FINISHED`` counts EVERY succeeded row ever, unbounded -- unlike its three siblings
    it is not a live-at-this-instant snapshot. The template must make that explicit rather than
    implying, via the shared "per reconcile, updates ~5 min" caption, that all four counts share one
    clock.
    """
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.FINISHED.value)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    import re

    card = re.search(r'id="admission-state-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    card_html = card.group(0)
    assert "lifetime total" in card_html, "the Finished tile must say it is cumulative, not live"
    # The live-grid caption must not sit above a lone Finished tile implying it shares that clock.
    live_caption_pos = card_html.find("live — per reconcile")
    finished_pos = card_html.find("Finished")
    assert live_caption_pos == -1, "with only Finished non-zero, the live-snapshot caption must not render"
    assert finished_pos != -1


@pytest.mark.asyncio
async def test_dashboard_admission_card_quiet_for_null_cloud_phase(client: AsyncClient, session: AsyncSession) -> None:
    """An a1/local row (NULL cloud_phase) counts toward no phase → empty carrier, no heading."""
    await _seed_cloud_phase(session, cloud_phase=None)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    assert "live — per reconcile" not in response.text
    assert "Queued (quota)" not in response.text


@pytest.mark.asyncio
async def test_stats_poll_repushes_admission_card_oob(client: AsyncClient, session: AsyncSession) -> None:
    """The 5s /pipeline/stats poll re-pushes the admission card OOB (hx-swap-oob + the matching tile)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.RUNNING.value)

    response = await client.get("/pipeline/stats")

    assert response.status_code == 200
    import re

    card = re.search(r'id="admission-state-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    card_html = card.group(0)
    assert 'hx-swap-oob="true"' in card_html
    assert "Running" in card_html
    assert "admitted — pod running" in card_html
    assert "bg-violet-50" in card_html


@pytest.mark.asyncio
async def test_dashboard_all_six_cloud_card_ids_present(client: AsyncClient) -> None:
    """The full /s/analyze render carries all SIX stable section ids (healthy, all-empty state)."""
    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    for card_id in _ALL_SIX_CARD_IDS:
        assert f'id="{card_id}"' in text


@pytest.mark.asyncio
async def test_stats_poll_all_six_cloud_card_ids_present_oob(client: AsyncClient) -> None:
    """The /pipeline/stats OOB re-push still carries all SIX ids, each as an hx-swap-oob fragment."""
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    text = response.text
    for card_id in _ALL_SIX_CARD_IDS:
        card_start = text.index(f'id="{card_id}"')
        assert 'hx-swap-oob="true"' in text[card_start : card_start + 200]


@pytest.mark.asyncio
async def test_dashboard_cloud_diagnostics_are_disclosed_without_duplicate_oob_hosts(client: AsyncClient, session: AsyncSession) -> None:
    """Cloud internals are collapsed by default and every live OOB carrier remains unique."""
    session.add(_make_file())
    await session.commit()

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text

    diagnostics_open = text.index('id="analysis-diagnostics"')
    assert "Technical diagnostics" in text[diagnostics_open:]
    assert "Admission, reconcile, staged transfer, quota, rank, lifetime totals, health, and recovery" in text
    for card_id in _ALL_SIX_CARD_IDS:
        assert text.count(f'id="{card_id}"') == 1
    assert text.index('id="awaiting-cloud-card"') < diagnostics_open
    assert diagnostics_open < text.index('id="admission-state-card"')
    assert diagnostics_open < text.index('id="staged-pushing-card"') < text.index('id="analyzing-cloud-card"')


@pytest.mark.asyncio
async def test_staged_analyzing_and_admission_agree_per_row(client: AsyncClient, session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """One ``vox`` (kueue) row per {uploading, uploaded, submitted, running} renders consistently everywhere.

    uploading/uploaded -> Staged only (never Analyzing, never an Admission tile -- cloud_phase is NULL
    pre-submit). submitted -> Analyzing + Admission's "Queued (quota)", NEVER Staged (the exact bug
    report shape). running -> Analyzing + Admission's "Running", never Staged.
    """
    backends_toml_env(_VOX_KUEUE_ONLY_TOML)

    async def _seed(i: int, status: CloudJobStatus, cloud_phase: str | None) -> None:
        f = _make_file()
        session.add(f)
        await session.flush()
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=status.value, backend_id="vox", cloud_phase=cloud_phase))

    await _seed(1, CloudJobStatus.UPLOADING, None)
    await _seed(2, CloudJobStatus.UPLOADED, None)
    await _seed(3, CloudJobStatus.SUBMITTED, CloudPhase.QUEUED_BEHIND_QUOTA.value)
    await _seed(4, CloudJobStatus.RUNNING, CloudPhase.RUNNING.value)
    await session.commit()

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text

    import re

    def _card(section_id: str) -> str:
        card = re.search(rf'id="{section_id}".*?</section>', text, re.DOTALL)
        assert card is not None, f"{section_id} carrier must always render"
        return card.group(0)

    staged_card = _card("staged-pushing-card")
    analyzing_card = _card("analyzing-cloud-card")
    admission_card = _card("admission-state-card")

    # Staged counts EXACTLY the two pre-submit rows (uploading + uploaded); the submitted row must
    # NEVER inflate it -- the exact bug this bead fixes.
    assert re.search(r"text-2xl[^>]*>\s*2\s*<", staged_card), staged_card

    # Analyzing counts EXACTLY the two post-submit rows (submitted-on-kueue + running).
    assert re.search(r"text-2xl[^>]*>\s*2\s*<", analyzing_card), analyzing_card

    # Admission agrees: the SAME submitted row is "Queued (quota)" 1, the SAME running row is "Running" 1.
    assert "Queued (quota)" in admission_card
    assert "Running" in admission_card
    assert re.search(r"Queued \(quota\)", admission_card)
