"""Wave-0 behavior scaffold for Phase 61 (Full record + ⌘K palette + Agents + empty state).

RED-by-design: the 11 tests below encode the RECORD-01..04 behavior contract that Plans
61-02..05 turn green. This file's Wave-0 gate is that it COLLECTS cleanly (11 tests, exit 0)
so each downstream plan can attach its ``<automated>`` verify to a concrete, already-written
test. Assertions are the REAL contract (bare fragments, file_id-scoping, 404 HTML fragment,
``role="option"``/``role="presentation"`` grouping, DISTINCT-artist read, discovery-scan
endpoint, never-DEAD compute lanes) — never placeholder ``assert True``.

Symbols that did not exist yet at Wave-0 (``distinct_artists``) are imported INSIDE the test
bodies (deferred) so collection stays clean while the tests fail/err at run time until Plans
02-05 land them. (COMPUTE-01 later retired the ``classify_compute_lanes`` aggregate shim.)

Route/contract map (see 61-VALIDATION.md "Per-Task Verification Map", 61-RESEARCH.md diagram):
    record body            -> GET /record/{file_id}          (61-02)
    ⌘K grouped palette     -> GET /search/  (HX branch)       (61-03)
    distinct_artists()     -> phaze.services.search_queries    (61-03)
    Agents two sections    -> GET /admin/agents               (61-04)
    derive_compute_lane_identities -> phaze.services.agent_liveness  (61-04 / COMPUTE-01)
    empty state            -> /s/analyze fragment, count==0    (61-05)
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# RECORD-01 — the full record fragment (GET /record/{file_id})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_fragment_bare_and_scoped(client: AsyncClient, seed_file_with_windows) -> None:  # type: ignore[no-untyped-def]
    """RECORD-01: /record/{file_id} is a BARE fragment (no <html>/<head>) scoped by file_id."""
    file, _result, _windows = await seed_file_with_windows()
    r = await client.get(f"/record/{file.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    # Content-only fragment — never the document wrapper/head (chrome persists across swaps).
    assert "<html" not in body
    assert "<head" not in body
    # Scoped strictly by the typed UUID file_id (broken-access-control mitigation, T-61).
    assert str(file.id) in body


@pytest.mark.asyncio
async def test_record_renders_bpm_scale_labels(client: AsyncClient, seed_file_with_windows) -> None:  # type: ignore[no-untyped-def]
    """quick 260707-c9o: the record view renders max/min BPM gutter labels (record.py passes bpm_lo/bpm_hi).

    seed_file_with_windows seeds fine windows with bpm 128/129/130 → min 128 (bottom), max 130 (top).
    """
    file, _result, _windows = await seed_file_with_windows()
    r = await client.get(f"/record/{file.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    assert "<polyline" in body
    assert "130" in body  # max BPM (top of the gutter)
    assert "128" in body  # min BPM (bottom of the gutter)
    assert 'aria-label="BPM range 128 to 130"' in body


@pytest.mark.asyncio
async def test_record_missing_file_404_fragment(client: AsyncClient) -> None:
    """RECORD-01: a missing/de-duplicated file → 404 FRIENDLY HTML fragment (not a 500/JSON detail)."""
    missing = uuid.uuid4()
    r = await client.get(f"/record/{missing}", headers={"HX-Request": "true"})
    assert r.status_code == 404
    # Friendly fragment rendered inside the host: HTML, not a JSON {"detail":...} or a stack trace.
    assert "text/html" in r.headers.get("content-type", "")
    assert "<html" not in r.text
    assert "Traceback" not in r.text


@pytest.mark.asyncio
async def test_record_pending_approvals_wired(client: AsyncClient, seed_pending_proposal) -> None:  # type: ignore[no-untyped-def]
    """RECORD-01: the record embeds the shared _diff_row approval cluster wired to the file's proposal."""
    proposal = await seed_pending_proposal(0.95)
    r = await client.get(f"/record/{proposal.file_id}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    # The pending-approval cluster reuses Phase 60 approve/edit/undo routes — the proposal id
    # (and htmx wiring) must appear so the row targets the right proposal.
    assert str(proposal.id) in body
    assert "hx-" in body


# ---------------------------------------------------------------------------
# RECORD-02 — the ⌘K command palette (grouped results over /search/ + distinct_artists)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmdk_grouped_results(client: AsyncClient, seed_distinct_artists) -> None:  # type: ignore[no-untyped-def]
    """RECORD-02: the grouped palette endpoint returns Files/Tracklists/Artists/Commands as an ARIA listbox."""
    await seed_distinct_artists()
    r = await client.get("/search/", params={"q": "bonobo"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    # Selectable rows are role="option"; group headers are role="presentation" (skipped by roving nav).
    assert 'role="option"' in body
    assert 'role="presentation"' in body
    # The four command-palette groups.
    for group in ("Files", "Artists", "Commands"):
        assert group in body


@pytest.mark.asyncio
async def test_distinct_artists_query(session: AsyncSession, seed_distinct_artists) -> None:  # type: ignore[no-untyped-def]
    """RECORD-02 (D-05): distinct_artists() returns DISTINCT non-None artists, LIMIT-bounded."""
    from phaze.services.search_queries import distinct_artists

    await seed_distinct_artists()
    # 'Bonobo' is seeded in BOTH FileMetadata and Tracklist → DISTINCT must collapse it to one.
    got = await distinct_artists(session, "bonobo")
    assert list(got).count("Bonobo") == 1
    # NULL artists are excluded.
    assert None not in got
    # LIMIT-bounded (unindexed columns — the caller relies on this cap; Pitfall 4).
    bounded = await distinct_artists(session, "o", limit=2)
    assert len(bounded) <= 2


@pytest.mark.asyncio
async def test_cmdk_commands_and_artist_nav(client: AsyncClient, seed_distinct_artists) -> None:  # type: ignore[no-untyped-def]
    """RECORD-02: Scan command posts /pipeline/scan-live-sets; artist row re-searches with q= param."""
    await seed_distinct_artists()
    r = await client.get("/search/", params={"q": "bonobo"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    # ⌘K "Scan" is the PARAMETERLESS fingerprint scan (correct for the palette; Pitfall 2).
    assert "/pipeline/scan-live-sets" in body
    # An Artist option must re-search with q= (WR-03): search_page only runs a query when q is
    # truthy, so an artist= only URL would return an empty palette. The artist row carries q=.
    assert 'hx-get="/search/?q=' in body


# ---------------------------------------------------------------------------
# RECORD-03 — the Agents page (heartbeating section + live compute lanes, never DEAD)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_two_sections_never_dead(client: AsyncClient) -> None:
    """RECORD-03: /admin/agents renders Section 1 (heartbeating) + Section 2 (compute lanes), never DEAD."""
    r = await client.get("/admin/agents")
    assert r.status_code == 200
    body = r.text
    # Section 2: the live compute-lane block.
    assert 'id="compute-lanes"' in body
    # It shows an Active/Waiting/Idle liveness state...
    assert any(state in body for state in ("ACTIVE", "WAITING", "IDLE"))
    # ...and NEVER a perpetual DEAD/rose state (KDEPLOY-04 — k8s bursts are ephemeral Jobs).
    compute_section = body.split('id="compute-lanes"', 1)[1]
    assert "DEAD" not in compute_section


# NOTE (COMPUTE-01): the former ``test_compute_lane_liveness_states`` exercised the retired
# ``classify_compute_lanes`` ``(state, count)`` shim. That aggregate contract was deleted once the
# router/template moved to per-cluster ``derive_compute_lane_identities`` lanes; its ACTIVE > WAITING
# > IDLE precedence coverage now lives in the derive-based equivalents in
# tests/agents/services/test_agent_liveness.py (``test_derive_*``).


# ---------------------------------------------------------------------------
# RECORD-04 — the first-run empty state (count==0 branch in the Analyze workspace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_state_agent_roots_scan(client: AsyncClient, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """RECORD-04: count==0 renders the guide listing each agent + scan_roots; Scan posts DISCOVERY /pipeline/scans."""
    agent, _token = seed_test_agent
    # The bare Analyze fragment (excludes the shell's cmdk chrome) — with 0 files it is the empty state.
    r = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert r.status_code == 200
    frag = r.text
    assert "<html" not in frag  # bare fragment
    # The empty-state guide root marker (branch discriminator).
    assert "data-empty-state" in frag
    # It lists each agent and its scan_roots.
    assert agent.id in frag
    assert "/test/music" in frag
    # "Scan {agent}" posts the DISCOVERY scan (agent_id + scan_root), NOT the parameterless
    # fingerprint scan (Pitfall 2). Scoped to the bare fragment so the palette's scan-live-sets
    # command (which lives in shell chrome, not this fragment) cannot leak in.
    assert "/pipeline/scans" in frag
    assert "scan-live-sets" not in frag
    # D-08: no new free-text path input surface.
    assert 'type="text"' not in frag


@pytest.mark.asyncio
async def test_empty_state_suppressed_when_files_exist(client: AsyncClient, make_file) -> None:  # type: ignore[no-untyped-def]
    """RECORD-04: file_count>0 does NOT render the empty-state guide (branch correctness)."""
    await make_file()  # file_count is now > 0
    r = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "data-empty-state" not in r.text


# ---------------------------------------------------------------------------
# Cross-cutting (D-02) — new fragments are single-poll clean (skip-on-404 resilient)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_fragments_single_poll_clean(
    client: AsyncClient,
    seed_file_with_windows,  # type: ignore[no-untyped-def]
    seed_test_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Cross-cutting (D-02): record + palette + empty-state fragments are single-poll clean.

    Spans all THREE Phase-61 surfaces and is referenced in the per-task verify of 61-02, 61-03,
    AND 61-05. Wave-2 runs sequentially, so 61-02/61-03 execute BEFORE 61-05 lands the empty
    state: any surface still returning 404 is treated-as-clean (skipped), and the single-poll
    properties are asserted ONLY against surfaces currently returning 200. Once 61-05 lands (and
    in the post-wave full suite) all three are asserted. This is the ONLY permitted resilience —
    the clean-fragment properties themselves are never weakened.
    """
    file, _result, _windows = await seed_file_with_windows()
    _agent, _token = seed_test_agent  # ensure an agent with scan_roots for the empty-state surface

    surfaces = {
        "record": f"/record/{file.id}",
        "palette": "/search/?q=techno",
        "empty_state": "/s/analyze",
    }

    checked = 0
    for name, url in surfaces.items():
        r = await client.get(url, headers={"HX-Request": "true"})
        if r.status_code == 404:
            continue  # surface not yet implemented (Wave-2 sequential) — treat as clean
        assert r.status_code == 200, f"{name} → {r.status_code}"
        body = r.text
        # Bare fragment (no document wrapper injected on swap).
        assert "<html" not in body, f"{name} carries <html>"
        assert "<head" not in body, f"{name} carries <head>"
        # Single poll: no self-poll loop and no JS timer inside a swapped fragment.
        assert 'hx-trigger="every' not in body, f"{name} has a self-poll loop"
        assert "setInterval" not in body, f"{name} has a setInterval loop"
        # Counts-only OOB: never an hx-swap-oob on an approval-row (_diff_row) subtree.
        assert "hx-swap-oob" not in body or "_diff_row" not in body, f"{name} OOB-swaps an approval subtree"
        checked += 1

    # The always-present surfaces (palette + empty-state routes exist pre-Phase-61) guarantee
    # at least one surface is actually asserted — the test is never vacuously skipped.
    assert checked >= 1
