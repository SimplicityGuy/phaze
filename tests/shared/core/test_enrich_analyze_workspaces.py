"""Behavioral tests for the v7.0 Enrich + Analyze workspaces (Phase 58, WORK-01..05 + R-5).

This is the single Phase-58 test file (Wave 0, task 58-01-00 / 58-VALIDATION.md). It defines
the full Phase-58 test surface up front:

* The two **foundation** tests are FILLED here:
    - ``test_stage_fragment_is_bare``   -> R-5    (fragment correctness; mirrors
      ``test_shell_routes.py::test_stage_fragment_is_bare``)
    - ``test_single_poll_discipline``   -> WORK-05 / R-2 / R-3 (one chrome poll +
      ``visibilitychange`` shed; no second loop in any workspace fragment). This test FAILS
      until Plan 58-01 Task 1 wires the chrome poll into ``shell/shell.html`` -- that is the
      expected RED state at task 58-01-00.

* The four **workspace** tests are ``xfail`` stubs that COLLECT cleanly now and are converted
  to real assertions by their owning task (see 58-VALIDATION.md Per-Task Verification Map):
    - ``test_discover_workspace``                 -> WORK-01 (Plan 58-02, task 58-02-03)
    - ``test_metadata_trigger_all_wired``         -> WORK-02 (Plan 58-03, task 58-03-02)
    - ``test_lane_cards_states``                  -> WORK-03 (Plan 58-04, task 58-04-02)
    - ``test_analyze_file_table_lane_and_windows``-> WORK-04 (Plan 58-04, task 58-04-03)

The module-level ``_seed_*`` helpers below are test fixtures (ORM inserts only -- never a
backend change). Plans 58-02..04 use them to seed the analyze-stage rows (incl. partial-window
in-flight rows for the WORK-04 mid-flight assertion) and the cloud_job lane/admission states.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# The four redesigned workspace stages whose HX fragments must ride the ONE chrome poll
# (no per-fragment ``hx-trigger="every"`` / ``setInterval``).
_WORKSPACE_STAGES = ["discover", "metadata", "fingerprint", "analyze"]


# ---------------------------------------------------------------------------
# Module-level async seed helpers (test fixtures -- ORM inserts only, no backend change).
# Plans 58-02..04 build their workspace assertions on these. They live here (not conftest)
# because they are Phase-58-specific shapes; ``conftest.py`` already seeds the legacy agent
# so a bare FileRecord satisfies its NOT NULL + FK ``agent_id`` default.
# ---------------------------------------------------------------------------


async def _seed_file(
    session: AsyncSession,
    *,
    original_filename: str = "set.mp3",
    file_type: str = "mp3",
    file_size: int = 1024,
) -> FileRecord:
    """Insert one FileRecord (legacy-agent default) and return it.

    The parent row every ``_seed_analysis`` / ``_seed_cloud_job`` FK points at.
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


async def _seed_analysis(
    session: AsyncSession,
    file_id: uuid.UUID,
    fine_done: int | None,
    fine_total: int | None,
    *,
    completed: bool = False,
) -> AnalysisResult:
    """Insert the 1:1 ``analysis`` aggregate row for ``file_id``.

    ``fine_done < fine_total`` models an in-flight file (the 57.1 PR #184 mid-flight
    ``fine_windows_analyzed/total`` signal); ``fine_done == fine_total`` models a completed
    file's full window coverage (WORK-04). Phase 90 (PR-A): the workspace's ``completed`` flag now
    DERIVES from ``analysis_completed_at`` (``done_clause(ANALYZE)``), not ``files.state == ANALYZED`` --
    so pass ``completed=True`` to stamp the completion timestamp on a done row.
    """
    result = AnalysisResult(
        file_id=file_id,
        fine_windows_analyzed=fine_done,
        fine_windows_total=fine_total,
        analysis_completed_at=datetime.now(UTC) if completed else None,
    )
    session.add(result)
    await session.commit()
    await session.refresh(result)
    return result


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    cloud_phase: str | None,
    *,
    status: str = CloudJobStatus.RUNNING,
    inadmissible: bool = False,
    backend_id: str | None = None,
) -> CloudJob:
    """Insert the per-file ``cloud_job`` sidecar for ``file_id``.

    ``cloud_phase`` drives the k8s admission-state surfaces (WORK-03). ``inadmissible`` flags the
    Kueue fault path (the ``role="alert"`` banner). ``backend_id`` (COMPUTE-03) is the registry
    cluster id stamped at dispatch; NULL models an unattributed cloud row (no cluster stamped yet).
    """
    job = CloudJob(
        file_id=file_id,
        s3_key=f"staging/{file_id}",
        status=status,
        cloud_phase=cloud_phase,
        inadmissible=inadmissible,
        backend_id=backend_id,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Foundation tests (FILLED in task 58-01-00).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_fragment_is_bare(client: AsyncClient) -> None:
    """R-5 -- an HX ``/s/<stage>`` response is a bare fragment (no document wrapper/head).

    Mirrors ``test_shell_routes.py::test_stage_fragment_is_bare``: a swapped workspace
    fragment NEVER carries ``<html>``/``<head>`` (no duplicate landmarks/skip-links). The
    chrome -- including the single poll element -- persists across swaps, so the dead-template
    AST guard stays green and the fragment never re-injects the poll.
    """
    hx = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    assert "<html" not in hx.text
    assert "<head" not in hx.text


@pytest.mark.asyncio
async def test_single_poll_discipline(client: AsyncClient) -> None:
    """WORK-05 / R-2 / R-3 -- exactly one chrome poll + ``visibilitychange`` shed; no 2nd loop.

    The full shell (``GET /``) fires the live refresh from persistent chrome: EXACTLY ONE
    ``hx-get="/pipeline/stats"`` element, and a ``visibilitychange`` listener that sheds
    polling while the tab is backgrounded. No swappable workspace fragment may carry its own
    ``hx-trigger="every"`` poll or a ``setInterval`` loop -- every workspace's live values ride
    the one chrome poll via ``hx-swap-oob`` against the existing ``stats_bar.html`` seeds.
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    body = shell.text
    # Exactly one persistent poll element in chrome (R-2).
    assert body.count('hx-get="/pipeline/stats"') == 1, "shell must fire exactly one /pipeline/stats poll"
    # The foreground/background shed (R-3).
    assert "visibilitychange" in body, "shell must shed polling on visibilitychange"

    # No workspace fragment starts a second poll loop (R-2).
    for stage in _WORKSPACE_STAGES:
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        assert 'hx-trigger="every' not in frag.text, f"{stage} fragment must not start a second poll loop"
        assert "setInterval" not in frag.text, f"{stage} fragment must not use setInterval"


@pytest.mark.asyncio
async def test_shell_store_seeds_phase58_keys(client: AsyncClient) -> None:
    """WORK-01/03/05 -- the v7.0 shell's OWN Alpine store seeds the Phase-58 derived keys.

    The shell (``shell/shell.html``) carries a standalone ``Alpine.store('pipeline', {...})``
    independent of the legacy ``base.html`` store. Phase-58 x-text bindings (Discover
    ``notYetEnriched`` sub-count, A1 lane ``computeOnline`` capacity) must be seeded to int 0
    there too, or they render ``undefined`` on initial paint until the first ~5s poll. Guards
    the base.html/shell.html store-divergence defect (verification W-1).
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    body = shell.text
    assert "notYetEnriched: 0" in body, "shell store must seed notYetEnriched to int 0 (no undefined flash)"
    assert "computeOnline: 0" in body, "shell store must seed computeOnline to int 0 (no undefined flash)"


@pytest.mark.asyncio
async def test_shell_sinks_legacy_oob_fragments(client: AsyncClient) -> None:
    """WORK-05 -- the shell carries a target for every OOB fragment the shared poll re-emits.

    ``stats_bar.html`` re-renders the Phase-44 ``#straggler-failed-card`` out-of-band on every
    ``/pipeline/stats`` tick. The v7.0 shell has no Analysis Health surface (Phase 61 owns
    observability), but once 58-01 wired the shared poll the card arrives each tick; without a
    landing target htmx logs ``htmx:oobErrorNoTarget`` every 5s. The seed host provides a hidden
    sink so the single poll stays clean (found in 58-04 live UAT).
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    assert 'id="straggler-failed-card"' in shell.text, "shell must carry a sink for the legacy straggler-failed-card OOB fragment"


@pytest.mark.asyncio
async def test_workspaces_sink_cloud_card_oob_fragments(client: AsyncClient) -> None:
    """WORK-05 -- every workspace has exactly one target for each cloud-state OOB card.

    The shared `/pipeline/stats` poll re-emits the six v6.0 cloud-state cards OOB every tick.
    Only the Analyze workspace renders their REAL targets; Discover/Metadata/Fingerprint must
    carry a hidden sink for each or htmx logs `htmx:oobErrorNoTarget` 6x/poll (found in 58-04
    live UAT). Analyze must have EXACTLY ONE of each id (the real card) -- the sink is skipped
    there via `cloud_cards=true` so a duplicate id can't steal the live swap.
    """
    cloud_ids = [
        "admission-state-card",
        "inadmissible-card",
        "localqueue-card",
        "awaiting-cloud-card",
        "analyzing-cloud-card",
        "staged-pushing-card",
    ]
    for stage in ("discover", "metadata", "fingerprint", "analyze"):
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        for cid in cloud_ids:
            count = frag.text.count(f'id="{cid}"')
            assert count == 1, f"{stage} fragment must have exactly one #{cid} target (found {count})"


@pytest.mark.asyncio
async def test_workspaces_sink_analyze_lanes_oob_grid(client: AsyncClient) -> None:
    """BEUI-01 -- every workspace has exactly one target for the #analyze-lanes N-lane grid OOB swap.

    The shared `/pipeline/stats` poll re-emits the BEUI-01 N-lane grid OOB every tick (stats_bar.html,
    `oob=True`). Its REAL host lives only in analyze_workspace.html; Discover/Metadata/Fingerprint (and
    the first-run empty state) must carry a hidden sink or htmx logs `htmx:oobErrorNoTarget` every 5s
    (found in 71 live UAT -- the same class as the six cloud-state cards above). Analyze must have
    EXACTLY ONE (the real grid) -- the sink is skipped there via `cloud_cards=true` so a duplicate id
    can't steal the live swap. With no files seeded the analyze fragment is the empty-state guide, which
    still carries the sink; either way the count is exactly one.
    """
    for stage in ("discover", "metadata", "fingerprint", "analyze"):
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        count = frag.text.count('id="analyze-lanes"')
        assert count == 1, f"{stage} fragment must have exactly one #analyze-lanes target (found {count})"


# ---------------------------------------------------------------------------
# Workspace tests -- xfail stubs converted to real assertions by their owning plan/task.
# (names + reasons per 58-VALIDATION.md Per-Task Verification Map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_workspace(client: AsyncClient) -> None:
    """WORK-01 -- Discover workspace: recent-scans surface + not-yet-enriched sub-count + SCAN/RECOVER.

    The HX fragment composes the shared scaffold (one ``tabindex="-1"`` h1 focus target), the
    live sub-count bound to ``$store.pipeline`` (refreshed by the single chrome poll), the
    recent-scans surface (or the "No scans yet" empty state with no rows seeded), and the
    SCAN + RECOVER actions. It carries NO second poll loop (the reused recent-scans self-poll
    is stripped, A3/Pitfall 4) and pre-mounts the ``dag-seed-notYetEnriched`` OOB target so the
    derived sub-count seed has a landing spot.
    """
    resp = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # Scaffold focus target (exactly one h1 with tabindex=-1).
    assert body.count('tabindex="-1"') == 1
    # Recent-scans surface OR the locked empty-state copy (no scans seeded here -> empty state).
    assert "No scans yet" in body
    # Live sub-count present (the x-text frame binds discovered + notYetEnriched).
    assert "not yet enriched" in body
    # SCAN + RECOVER actions, wired to the existing endpoints (no new backend).
    assert "SCAN" in body
    assert "RECOVER" in body
    assert "/pipeline/scans" in body
    assert "/pipeline/recover" in body
    # R-4: RECOVER carries a confirm + a busy-disable gate.
    assert "hx-confirm" in body
    assert ":disabled" in body
    # WORK-05 / Pitfall 4: no second poll loop (the reused recent-scans self-poll is stripped).
    assert 'hx-trigger="every' not in body
    assert "setInterval" not in body
    # T-58-SEED: the derived sub-count seed has a pre-mounted OOB landing target.
    assert "dag-seed-notYetEnriched" in body


@pytest.mark.asyncio
async def test_metadata_trigger_all_wired(client: AsyncClient) -> None:
    """WORK-02 / D-01 / D-02 -- Metadata + Fingerprint ALL buttons post to the EXISTING endpoints.

    Each enrich workspace is a sibling fragment: a single ALL-only bulk trigger wired VERBATIM to
    its existing endpoint (D-01: ``POST /pipeline/extract-metadata`` / ``POST /pipeline/fingerprint``)
    with the R-4 guard (hx-confirm + a ``:disabled`` busy-gate). D-02: there is NO ``EXTRACT SELECTED``
    button and NO per-row checkbox/selection state anywhere. WORK-05: neither fragment starts a second
    poll loop -- live values ride the single chrome poll.
    """
    # --- Metadata workspace (D-01 verbatim endpoint + R-4 guard) ---
    md = await client.get("/s/metadata", headers={"HX-Request": "true"})
    assert md.status_code == 200
    md_body = md.text
    # Scaffold focus target (exactly one h1 with tabindex=-1).
    assert md_body.count('tabindex="-1"') == 1
    # EXTRACT ALL wired VERBATIM to the existing endpoint (D-01).
    assert 'hx-post="/pipeline/extract-metadata"' in md_body
    assert "EXTRACT ALL" in md_body
    # Trigger-response landing target present.
    assert 'id="metadata-trigger-response"' in md_body
    # R-4 bulk-enqueue guard: confirm + busy-disable on metadataBusy.
    assert "hx-confirm" in md_body
    assert "$store.pipeline.metadataBusy" in md_body
    # D-02: NO EXTRACT SELECTED, NO row-selection / checkbox state.
    assert "EXTRACT SELECTED" not in md_body
    assert 'type="checkbox"' not in md_body
    # WORK-05 / R-2: no second poll loop.
    assert 'hx-trigger="every' not in md_body
    assert "setInterval" not in md_body

    # --- Fingerprint workspace (the sibling) ---
    fp = await client.get("/s/fingerprint", headers={"HX-Request": "true"})
    assert fp.status_code == 200
    fp_body = fp.text
    assert fp_body.count('tabindex="-1"') == 1
    assert 'hx-post="/pipeline/fingerprint"' in fp_body
    assert "FINGERPRINT ALL" in fp_body
    assert 'id="fingerprint-trigger-response"' in fp_body
    assert "hx-confirm" in fp_body
    assert "$store.pipeline.fingerprintBusy" in fp_body
    # D-02: no selection affordance on the sibling either.
    assert 'type="checkbox"' not in fp_body
    # WORK-05 / R-2: no second poll loop.
    assert 'hx-trigger="every' not in fp_body
    assert "setInterval" not in fp_body


@pytest.mark.asyncio
async def test_lane_cards_states(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """BEUI-01 / D-04/D-05/D-06 -- N registry lane cards render rank-ascending; word-labelled states; global roll-up intact.

    The Analyze workspace loops ``_lane_card.html`` over the seeded ``get_backend_lane_snapshot`` list
    (D-04): one card per registry backend, rank-ascending (D-06), each showing ``RANK {n}`` + the
    ``{in_flight}/{cap}`` numeral + the per-lane Kueue quota-wait-vs-Inadmissible caption (D-03). A down
    lane (``available=False``) is NEVER hidden -- it greys with the explicit word ``offline`` (WCAG 1.4.1);
    the Phase-58 ``not configured`` path is retired for the N-lane grid. The 6 global cloud-state cards stay
    VERBATIM as a cross-lane roll-up below the grid (D-07) with the load-bearing WORK-03 distinction: the
    Inadmissible FAULT card carries ``role="alert"`` while the healthy admission-state card does NOT.
    """
    import phaze.routers.pipeline as pipeline_mod

    # phaze-xd8k: "nox" is offline yet still carries 5 real in-flight files (a lane can be unreachable
    # for NEW dispatch while still draining work it already accepted) -- the card must render that TRUE
    # count, never a fabricated 0 that hides real in-flight work behind "offline".
    lanes = [
        {"id": "a1", "kind": "compute", "rank": 10, "cap": 4, "in_flight": 2, "available": True, "quota_wait": 0, "inadmissible": 0},
        {"id": "k8s", "kind": "kueue", "rank": 20, "cap": 3, "in_flight": 1, "available": True, "quota_wait": 2, "inadmissible": 1},
        {"id": "nox", "kind": "local", "rank": 99, "cap": 8, "in_flight": 5, "available": False, "quota_wait": 0, "inadmissible": 0},
    ]

    async def _snapshot(_session: AsyncSession) -> list[dict[str, object]]:
        return lanes

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _snapshot)

    # Seed a k8s cloud_job flagged Inadmissible (status RUNNING so get_inadmissible_count counts it) so the
    # GLOBAL inadmissible fault card renders (role="alert"), AND its cloud_phase makes the admission card render.
    fault = await _seed_file(session, original_filename="fault.mp3")
    await _seed_cloud_job(
        session,
        fault.id,
        CloudPhase.QUEUED_BEHIND_QUOTA.value,
        status=CloudJobStatus.RUNNING,
        inadmissible=True,
    )

    resp = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # Bare fragment + focus targets: the scaffold <h1 tabindex="-1"> PLUS the Phase-88 (88-01, DRILL-03
    # / D-09) shared #detail-pane header <h2 tabindex="-1">, which the pane focuses on drill-in open to
    # park focus off the poll-doomed lane trigger (Pitfall 1). Both are intentional focus targets.
    assert "<html" not in body
    assert "<head" not in body
    assert body.count('tabindex="-1"') == 2
    assert 'id="detail-pane-heading"' in body  # the pane's D-09 focus target is hosted in the fragment

    # D-05: the lane grid host id is stable; one card per seeded lane.
    assert 'id="analyze-lanes"' in body
    # Lane identities carry a word + glyph -- {KIND · id} (never hue-only).
    assert "COMPUTE · a1" in body
    assert "KUEUE · k8s" in body
    assert "LOCAL · nox" in body
    # D-06: rank-ascending render order (a1 rank10 -> k8s rank20 -> nox rank99).
    assert body.index("COMPUTE · a1") < body.index("KUEUE · k8s") < body.index("LOCAL · nox")
    # RANK micro-labels + {in_flight}/{cap} numerals.
    assert "RANK 10" in body and "RANK 20" in body and "RANK 99" in body
    assert "2/4" in body  # a1 in_flight/cap
    assert "1/3" in body  # k8s in_flight/cap
    # D-03 per-lane Kueue admission caption: quota-wait vs Inadmissible, word-labelled.
    assert "2 waiting" in body
    assert "1 inadmissible" in body
    # local lane available=False -> explicit "offline" word (never hidden). "not configured" is retired.
    assert "offline" in body
    assert "not configured" not in body
    # phaze-xd8k: the offline nox lane renders its TRUE in-flight count (5/8), never a literal "0" that
    # would hide the 5 files it is still draining.
    assert "5/8" in body

    # D-07 / WORK-03 load-bearing distinction: the Inadmissible FAULT carries role="alert"; the HEALTHY
    # admission-state card does NOT (the fault can never be collapsed into healthy progression).
    assert 'role="alert"' in body
    assert "K8s Jobs not admitting" in body  # inadmissible_card copy
    assert 'id="admission-state-card"' in body
    admission = body[body.index('id="admission-state-card"') :]
    admission_section = admission[: admission.index("</section>")]
    assert 'role="alert"' not in admission_section

    # No leaked legacy routing key.
    assert "cloud_target" not in body

    # WORK-05 / R-2: no second poll loop in the workspace fragment.
    assert 'hx-trigger="every' not in body
    assert "setInterval" not in body


@pytest.mark.asyncio
async def test_lane_grid_subcount_makes_no_across_lanes_claim(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-xd8k -- the ANALYZE sub-count no longer claims to be a sum "across M lanes".

    The header's ``$store.pipeline.analyzeActive`` (scheduling-ledger-derived, ALL routes) and the lane
    cards' ``in_flight`` (cloud_job-derived, per-backend) are DIFFERENT populations from DIFFERENT
    tables -- "N files in flight across M lanes" falsely claimed the header was the sum of the lane
    numerators (2929 vs 2 in the confirmed bug scenario). The reworded copy drops the "across M lanes"
    summation claim entirely; this test locks that the old phrasing (with ANY lane count) can never
    return, regardless of how many backends are registered. Also proves a compute lane still renders
    configured off the snapshot (no ``not configured``, no ``cloud_target`` leak, no template exception).
    """
    import phaze.routers.pipeline as pipeline_mod

    lanes = [
        {"id": "a1", "kind": "compute", "rank": 10, "cap": 2, "in_flight": 0, "available": True, "quota_wait": 0, "inadmissible": 0},
        {"id": "nox", "kind": "local", "rank": 99, "cap": 1, "in_flight": 0, "available": True, "quota_wait": 0, "inadmissible": 0},
    ]

    async def _snapshot(_session: AsyncSession) -> list[dict[str, object]]:
        return lanes

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _snapshot)
    # A file must exist or the analyze node swaps to the first-run empty-state (shell.py:176) instead of lanes.
    await _seed_file(session, original_filename="held.mp3")

    resp = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    assert 'id="analyze-lanes"' in body
    # phaze-xd8k: no "across N lanes" summation claim survives, for ANY N.
    assert "flight across" not in body
    assert "3 lanes" not in body
    assert "2 lanes" not in body
    assert "files in the analyze stage" in body
    # The compute lane renders configured + available off the snapshot; "not configured" is retired.
    assert "COMPUTE · a1" in body
    assert "not configured" not in body
    assert "cloud_target" not in body


@pytest.mark.asyncio
async def test_analyze_file_table_lane_and_windows(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """WORK-04 / D-03 / D-04 / COMPUTE-03 -- per-file lane badge + mid-flight N/M (in-flight) / full coverage (completed).

    ONE table covers ALL in-stage files (D-03). Per-file lane is DERIVED from the stamped
    ``CloudJob.backend_id`` (COMPUTE-03), through the Phase-96 ``non_local_backend_kinds`` registry
    projection -- never the retired 'a1' heuristic: no cloud_job row -> local; a stamped
    ``backend_id`` -> the backend id itself, glyph/color from its registry kind (kueue -> ⎈); a
    cloud_job with NO ``backend_id`` yet -> the truthful neutral "cloud, unattributed" fallback.
    Windowed progress reads the analysis aggregate: a completed (ANALYZED) row shows full
    ``window {a}/{total}`` coverage; an in-flight row shows the merged 57.1 mid-flight ``N/M windows``
    signal ALONGSIDE ``running`` -- a render that emits only a bare ``running`` MUST fail this test
    (B2 / 57.1 PROG-03 read). Phase 61 (RECORD-01) supersedes the Phase-58 click-unbound invariant:
    each row now opens the full-record slide-in (``hx-get="/record/{file_id}"`` + a ``record:open``
    dispatch).
    """
    import phaze.services.pipeline as services_pipeline_mod

    monkeypatch.setattr(
        services_pipeline_mod,
        "get_settings",
        lambda: SimpleNamespace(backends=[SimpleNamespace(id="vox", kind="kueue")]),
    )

    # Completed LOCAL file (no cloud_job): full window coverage from the aggregate.
    done = await _seed_file(session, original_filename="done.mp3")
    await _seed_analysis(session, done.id, fine_done=41, fine_total=41, completed=True)
    # IN-FLIGHT file: a partial 57.1 analysis row (fine_done < fine_total), NOT yet ANALYZED.
    inflight = await _seed_file(session, original_filename="inflight.mp3")
    await _seed_analysis(session, inflight.id, fine_done=14, fine_total=41)
    # kueue-stamped lane: cloud_job with backend_id='vox' (registered kind=kueue) + cloud_phase set.
    kueue_file = await _seed_file(session, original_filename="kueued.mp3")
    await _seed_cloud_job(session, kueue_file.id, CloudPhase.RUNNING.value, backend_id="vox")
    # NULL-backend_id cloud row: a cloud_job exists but no cluster has been stamped yet -- the
    # truthful "cloud, unattributed" fallback, never the stale 'a1' heuristic label.
    unattributed = await _seed_file(session, original_filename="unattributed.mp3")
    await _seed_cloud_job(session, unattributed.id, None)

    resp = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # A1 regression (COMPUTE-03): the stale heuristic label must appear NOWHERE in the rendered page.
    assert "A1" not in body

    # Scope to the file table (below the lane-card grid, which carries its own lane glyphs).
    assert 'id="analyze-file-table"' in body
    tbl = body[body.index('id="analyze-file-table"') :]

    # D-03: one table listing ALL in-stage files.
    assert "done.mp3" in tbl
    assert "inflight.mp3" in tbl

    # Completed row: full window coverage from the aggregate.
    assert "window 41/41" in tbl

    # In-flight row: the mid-flight N/M signal AND running -- NOT a bare "running" (B2 / D-04).
    assert "running" in tbl
    assert "14/41" in tbl

    # Per-file lane badge derivation (COMPUTE-03): no cloud_job -> local; backend_id='vox' (kueue) ->
    # its own glyph + id; NULL backend_id -> the neutral unattributed cloud fallback.
    assert "local" in tbl
    assert "⎈ vox" in tbl
    assert "☁️ vox" not in tbl, "a kueue-kind backend must NOT render the compute glyph"
    assert "▪ cloud" in tbl, "an unattributed cloud_job (no backend_id) renders the neutral fallback badge"

    # Phase 61 (RECORD-01 / D-06 supersession): rows now open the full-record slide-in --
    # hx-get="/record/{file_id}" into #record-body + a record:open dispatch. (The Phase-58
    # click-unbound invariant is intentionally superseded; no selected-state is introduced.)
    assert 'hx-get="/record/' in tbl
    assert 'hx-target="#record-body"' in tbl
    assert "record:open" in tbl
    assert "aria-selected" not in tbl

    # CR-01 regression: the @click="$dispatch('record:open')" rows MUST sit inside an Alpine
    # x-data scope or the dispatch never fires (the shell root has no x-data). A bare string
    # match on "record:open" passes even when the click is inert, so assert the enclosing
    # x-data on the clickable table wrapper (the wrapper div precedes the table id, so check body).
    assert 'overflow-x-auto" x-data' in body

    # UAT focus/a11y: clickable record rows are keyboard-operable — tabindex="0" (so Esc can
    # return focus to the opening row) + Enter opens the record via both the Alpine dispatch
    # and the htmx load (keyup[key=='Enter']). Verified live in Phase 61 UAT.
    assert 'tabindex="0"' in tbl
    assert "@keydown.enter" in tbl
    assert "keyup[key==&#39;Enter&#39;]" in tbl or "keyup[key=='Enter']" in tbl

    # WORK-05 / R-2: no second poll loop in the fragment.
    assert 'hx-trigger="every' not in body
    assert "setInterval" not in body


# ---------------------------------------------------------------------------
# Phase 95 (phaze-zqvh.2, CONSOLE-04): the BOUNDED analyze file grid + its status-filter/pager fragment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_files_fragment_default_view(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zqvh.2 -- GET /pipeline/analyze-files (no filter) renders the bounded working-set view + filter bar.

    The default (no ``status``) fragment shows the active-first working set PLUS the recent-completions
    window through the SAME row projection the workspace uses, carries the URL-carried status-filter bar
    (hx-get /pipeline/analyze-files into #analyze-files-view), and -- being the default view -- shows NO
    pager. It never starts a second poll loop (WORK-05 / R-2).
    """
    inflight = await _seed_file(session, original_filename="inflight.mp3")
    await _seed_analysis(session, inflight.id, fine_done=3, fine_total=10)
    done = await _seed_file(session, original_filename="done.mp3")
    await _seed_analysis(session, done.id, fine_done=10, fine_total=10, completed=True)

    resp = await client.get("/pipeline/analyze-files")
    assert resp.status_code == 200
    body = resp.text
    # The swap-target container + the status-filter lens (mirrors the _status_filter_bar idiom).
    assert 'id="analyze-files-view"' in body
    assert 'id="analyze-filter-bar"' in body
    assert 'hx-get="/pipeline/analyze-files"' in body
    assert 'hx-target="#analyze-files-view"' in body
    # Working set (in-flight) + completions window (completed) both render, per-row record slide-in intact.
    assert "inflight.mp3" in body
    assert "done.mp3" in body
    assert 'hx-get="/record/' in body
    # Default view carries NO pager (only the filtered/paged view does).
    assert "Analyze files pagination" not in body
    # WORK-05 / R-2: no second poll loop.
    assert 'hx-trigger="every' not in body
    assert "setInterval" not in body


@pytest.mark.asyncio
async def test_analyze_files_fragment_filtered_and_paged(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zqvh.2 -- a status lens serves the full listing paged; the pager carries the lens, no whole-corpus COUNT.

    ``page_size`` is clamped to a 10-row floor (parity with ``/pipeline/files``), so 11 completed files under
    the ``completed`` lens at ``page_size=10`` yield page 1 bounded to 10 record rows with a Next control (the
    +1 sentinel ``has_next``); the pager links carry ``status=completed`` and the filter select reflects the lens.
    """
    for i in range(11):
        f = await _seed_file(session, original_filename=f"c{i}.mp3")
        await _seed_analysis(session, f.id, fine_done=1, fine_total=1, completed=True)

    resp = await client.get("/pipeline/analyze-files?status=completed&page_size=10")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="analyze-files-view"' in body
    # Bounded page: exactly page_size record rows.
    assert body.count('hx-get="/record/') == 10
    # Pager present (11 > 10 => has_next) and carrying the status lens on its links.
    assert "Analyze files pagination" in body
    assert "page=2&amp;page_size=10&amp;status=completed" in body  # &-autoescaped in the href (XSS-safe)
    # The select reflects the active lens (Clear filter affordance appears too).
    assert 'value="completed" selected' in body
    assert "Clear filter" in body


@pytest.mark.asyncio
async def test_analyze_file_grid_stays_outside_poll_oob(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zqvh.2 -- the 5s /pipeline/stats poll NEVER re-emits the analyze file grid.

    The file grid (its filter bar + pager) lives in #analyze-files-view, a SIBLING of the poll's OOB
    targets. If the poll re-emitted it, a tick would reset the operator's page position / filter selection.
    Assert the poll fragment carries none of the file-grid ids/endpoint -- so filter + page state survive
    every tick (the file grid stays outside the poll's OOB fan-out).
    """
    stats = await client.get("/pipeline/stats")
    assert stats.status_code == 200
    body = stats.text
    assert 'id="analyze-files-view"' not in body
    assert 'id="analyze-filter-bar"' not in body
    assert "/pipeline/analyze-files" not in body


@pytest.mark.asyncio
async def test_analyze_files_fragment_unknown_status_degrades(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zqvh.2 / T-57-01 -- an unknown ``status`` degrades to the default view, never a 422 or a path splice."""
    inflight = await _seed_file(session, original_filename="inflight.mp3")
    await _seed_analysis(session, inflight.id, fine_done=2, fine_total=10)

    resp = await client.get("/pipeline/analyze-files?status=../../etc/passwd")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="analyze-files-view"' in body
    assert "inflight.mp3" in body
    # Degraded to the default view: no pager, the default select option is active.
    assert "Analyze files pagination" not in body


# ---------------------------------------------------------------------------
# Phase 95 (phaze-zqvh.3): bound the 5s poll's per-tick #analyze-lanes swap churn (idempotent OOB swap).
# ---------------------------------------------------------------------------


def _analyze_lanes_hash(body: str) -> str | None:
    """Extract the #analyze-lanes grid's data-lanes-hash attribute value from a rendered body."""
    m = re.search(r'id="analyze-lanes"[^>]*data-lanes-hash="([0-9a-f]*)"', body)
    return m.group(1) if m else None


@pytest.mark.asyncio
async def test_analyze_lanes_grid_carries_content_hash(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.3 -- both the initial /s/analyze render and the 5s /pipeline/stats OOB grid carry data-lanes-hash.

    The client htmx:oobBeforeSwap skip hook compares the incoming grid hash to what is mounted, so BOTH
    render paths must emit a non-empty ``data-lanes-hash`` computed over the SAME inputs -- else the first
    tick can never be a no-op and the churn bound never engages.
    """
    import phaze.routers.pipeline as pipeline_mod

    lanes = [{"id": "a1", "kind": "compute", "rank": 10, "cap": 4, "in_flight": 2, "available": True, "quota_wait": 0, "inadmissible": 0}]

    async def _snapshot(_session: AsyncSession) -> list[dict[str, object]]:
        return lanes

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _snapshot)
    await _seed_file(session, original_filename="held.mp3")  # a file must exist or the empty-state renders

    initial = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert initial.status_code == 200
    initial_hash = _analyze_lanes_hash(initial.text)
    assert initial_hash, "the initial #analyze-lanes render must carry a non-empty data-lanes-hash"

    poll = await client.get("/pipeline/stats")
    assert poll.status_code == 200
    poll_hash = _analyze_lanes_hash(poll.text)
    assert poll_hash, "the /pipeline/stats OOB grid must carry a non-empty data-lanes-hash"
    # Same lane state on both paths => same hash => the first poll tick is already a no-op swap.
    assert initial_hash == poll_hash


@pytest.mark.asyncio
async def test_analyze_lanes_poll_hash_stable_when_unchanged_changes_on_state(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.3 -- the poll grid hash is byte-identical across unchanged ticks and changes on a real update.

    Two /pipeline/stats ticks with an UNCHANGED lane snapshot yield an identical data-lanes-hash (the
    client skips the destroy-and-recreate); a changed snapshot (an in_flight bump) yields a different
    hash (a real update still swaps). This is the server-verifiable core of the idempotent-swap churn bound.
    """
    import phaze.routers.pipeline as pipeline_mod

    state = {"in_flight": 2}

    async def _snapshot(_session: AsyncSession) -> list[dict[str, object]]:
        return [
            {
                "id": "a1",
                "kind": "compute",
                "rank": 10,
                "cap": 4,
                "in_flight": state["in_flight"],
                "available": True,
                "quota_wait": 0,
                "inadmissible": 0,
            }
        ]

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _snapshot)

    tick1 = _analyze_lanes_hash((await client.get("/pipeline/stats")).text)
    tick2 = _analyze_lanes_hash((await client.get("/pipeline/stats")).text)
    assert tick1 and tick1 == tick2, "an unchanged lane state must produce a byte-identical grid hash across ticks"

    state["in_flight"] = 3
    tick3 = _analyze_lanes_hash((await client.get("/pipeline/stats")).text)
    assert tick3 and tick3 != tick1, "a real lane-state change must change the hash so the grid still swaps"


@pytest.mark.asyncio
async def test_shell_carries_analyze_lanes_idempotent_skip_hook(client: AsyncClient) -> None:
    """phaze-zqvh.3 / WORK-05 -- the shell wires the oobBeforeSwap skip hook WITHOUT adding a second poll loop.

    The churn bound is a client htmx:oobBeforeSwap handler that skips the #analyze-lanes OOB swap when the
    incoming data-lanes-hash matches what is mounted. Assert the hook is present (scoped to #analyze-lanes,
    setting shouldSwap=false) and that the single-poll discipline is intact (exactly one /pipeline/stats
    poll; no setInterval / hx-trigger="every" second loop was introduced).
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    body = shell.text
    assert "htmx:oobBeforeSwap" in body
    assert "analyze-lanes" in body
    assert "shouldSwap = false" in body
    # WORK-05 / R-2: still exactly one poll, no second loop.
    assert body.count('hx-get="/pipeline/stats"') == 1
    assert "setInterval" not in body


@pytest.mark.asyncio
async def test_poll_still_seeds_store_and_lands_analyze_lanes_oob(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.3 preservation -- the churn bound changes NOTHING about the OOB fan-out or the store seeds.

    The idempotent-swap hook is purely client-side; the poll fragment must still carry the ~store-seed OOB
    writes ($store.pipeline keys update every tick) AND the #analyze-lanes OOB grid (hx-swap-oob) so all
    targets still land. Asserts the store writes + the OOB grid are still emitted on the tick.
    """
    import phaze.routers.pipeline as pipeline_mod

    async def _snapshot(_session: AsyncSession) -> list[dict[str, object]]:
        return [{"id": "a1", "kind": "compute", "rank": 10, "cap": 4, "in_flight": 1, "available": True, "quota_wait": 0, "inadmissible": 0}]

    monkeypatch.setattr(pipeline_mod, "get_backend_lane_snapshot", _snapshot)
    poll = await client.get("/pipeline/stats")
    assert poll.status_code == 200
    body = poll.text
    # Store seeds still fire every tick (unchanged OOB store-write contract).
    assert 'id="analyze-files-ready" hx-swap-oob="true"' in body
    assert "$store.pipeline.discovered =" in body
    assert "$store.pipeline.agentBusy =" in body
    # The #analyze-lanes grid still OOB-swaps (with its new content hash).
    assert 'id="analyze-lanes"' in body
    assert 'hx-swap-oob="true"' in body
    assert _analyze_lanes_hash(body), "the OOB grid must still carry its content hash"
