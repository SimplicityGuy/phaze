"""Integration tests for duplicate resolution router."""

import html as html_mod
import json
import re
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata


HASH_A = "a" * 64
HASH_B = "b" * 64

# Extract the server-rendered ``name="file_states"`` hidden-input value from a resolve response.
# The value is Jinja-autoescaped JSON (``&#34;`` for the quotes), so it never contains a raw ``"`` --
# the regex stops at the closing attribute quote, and ``html.unescape`` recovers the real JSON string.
_FILE_STATES_RE = re.compile(r'name="file_states"\s+value="([^"]*)"')


def _extract_server_file_states(response_text: str) -> str:
    """Return the ACTUAL server-rendered ``file_states`` payload (HTML-unescaped), never a hand-crafted dict.

    This is the exact value the browser would POST back on Undo. The round-trip tests extract THIS so a
    regression in the resolve->undo payload contract (or the PR-B id-only shape) is caught end-to-end.
    """
    match = _FILE_STATES_RE.search(response_text)
    assert match is not None, "resolve response did not render a file_states hidden input"
    return html_mod.unescape(match.group(1))


def _make_file(
    original_path: str,
    file_type: str,
    sha256_hash: str,
    file_size: int = 1000,
) -> FileRecord:
    """Helper to create a FileRecord with explicit hash."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
    )


def _make_metadata(file_id: uuid.UUID, **kwargs) -> FileMetadata:
    """Helper to create a FileMetadata row."""
    return FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_list_duplicates_returns_html(session: AsyncSession, client: AsyncClient) -> None:
    """Phase 57 (SHELL-05): a plain GET /duplicates/ 302-redirects into the shell.

    The "Duplicate Resolution" heading + stats header are full-page chrome on the dedupe
    workspace node (a Phase-57 placeholder; real content lands in 58-61). The in-page HX
    group-list partial stays usable (test_list_duplicates_htmx_returns_partial covers it).
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get("/duplicates/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/dedupe"


@pytest.mark.asyncio
async def test_list_duplicates_htmx_returns_partial(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/ with HX-Request header returns partial without full base.html."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get("/duplicates/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    # Partial should NOT contain full base.html elements
    assert "<!DOCTYPE html>" not in response.text
    # But should have group content
    assert HASH_A[:12] in response.text


@pytest.mark.asyncio
async def test_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/ with no duplicate files returns empty state message."""
    # Add a single unique file (no duplicates)
    f1 = _make_file("/dir/unique.mp3", "mp3", HASH_A)
    session.add(f1)
    await session.flush()

    response = await client.get("/duplicates/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "No duplicates found" in response.text


@pytest.mark.asyncio
async def test_compare_endpoint(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/{hash}/compare returns comparison table with Resolve Group button."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    session.add_all([f1, f2])
    await session.flush()

    m1 = _make_metadata(f1.id, bitrate=320, artist="Artist A")
    m2 = _make_metadata(f2.id, bitrate=128, artist="Artist B")
    session.add_all([m1, m2])
    await session.flush()

    response = await client.get(f"/duplicates/{HASH_A}/compare")

    assert response.status_code == 200
    assert "Resolve Group" in response.text
    assert "Artist A" in response.text
    assert "Artist B" in response.text


@pytest.mark.asyncio
async def test_resolve_group(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/{hash}/resolve writes the DedupResolution marker for non-canonical files.

    Phase 90 (D-09): the DUPLICATE_RESOLVED files.state dual-write was removed; the marker
    (dedup_resolved_clause) is the sole derived authority.
    """
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution

    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )

    assert response.status_code == 200
    assert "Group resolved" in response.text

    # Verify the marker was written for the non-canonical file only (the canonical keeper has none).
    marker_ids = set((await session.execute(select(DedupResolution.file_id))).scalars().all())
    assert marker_ids == {f2.id}


@pytest.mark.asyncio
async def test_undo_resolve(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/{hash}/undo restores files to previous state."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # First resolve
    resolve_response = await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )
    assert resolve_response.status_code == 200

    # Construct file_states for undo
    file_states = [{"id": str(f2.id)}]

    # Undo
    undo_response = await client.post(
        f"/duplicates/{HASH_A}/undo",
        data={"file_states": json.dumps(file_states)},
    )

    assert undo_response.status_code == 200

    # Verify file restored
    await session.refresh(f2)


@pytest.mark.asyncio
async def test_bulk_resolve(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/resolve-all resolves all groups on page."""
    # Group A
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    # Group B
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=1500)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"page": "1", "page_size": "20"},
    )

    assert response.status_code == 200
    assert "Resolved" in response.text
    assert "groups" in response.text.lower()


@pytest.mark.asyncio
async def test_bulk_undo(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/undo-all restores all bulk-resolved files."""
    # Group A
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Bulk resolve first
    await client.post("/duplicates/resolve-all", data={"page": "1", "page_size": "20"})

    # Build undo states
    file_states = [{"id": str(f2.id)}]

    response = await client.post(
        "/duplicates/undo-all",
        data={
            "file_states": json.dumps(file_states),
            "page": "1",
            "page_size": "20",
        },
    )

    assert response.status_code == 200

    # Verify file restored
    await session.refresh(f2)


@pytest.mark.asyncio
async def test_resolved_groups_not_shown(session: AsyncSession, client: AsyncClient) -> None:
    """After resolving a group, GET /duplicates/ no longer shows that group."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Resolve the group
    await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )

    # Check listing
    response = await client.get("/duplicates/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert HASH_A[:12] not in response.text
    assert "No duplicates found" in response.text


@pytest.mark.asyncio
async def test_stats_header_values(session: AsyncSession, client: AsyncClient) -> None:
    """Stats response includes correct group count and total files."""
    # Create 2 groups: A (2 files) and B (2 files)
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=1000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=2000)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=4000)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    # Phase 57 (SHELL-05): the "Groups"/"Total Files" stats header is full-page chrome on
    # the dedupe workspace node (a Phase-57 placeholder), so a plain GET /duplicates/ now
    # 302-redirects into the shell.
    response = await client.get("/duplicates/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/dedupe"


# ---------------------------------------------------------------------------
# UAT regression (Phase 84): the resolve/undo endpoints must COMMIT.
#
# `get_session` (database.py:48-51) yields the session and never commits, and `services/dedup.py`
# only `flush()`es (caller-owned transaction). Before this fix the router committed nothing, so a
# resolve returned HTTP 200, rendered a success partial, and was rolled back on session close --
# the dedup feature never persisted anything in production.
#
# Every pre-existing test missed it because `conftest.client` overrides `get_session` with the
# test's OWN session, so assertions read uncommitted rows from inside the same transaction. These
# tests assert from an INDEPENDENT session, which by definition sees only committed data.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_endpoint_commits_marker(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """After POST /resolve, a SEPARATE session sees the committed DedupResolution marker (the sole authority)."""
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    response = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert response.status_code == 200

    # 92-04 (CLEAN-02): read through the shared ``verify`` fixture -- an INDEPENDENT session bound to the
    # per-test ``_db_connection`` -- so it sees the router's in-test commit under create_savepoint isolation.
    markers = (await verify.execute(select(DedupResolution.file_id))).scalars().all()
    assert list(markers) == [dup.id], "resolve did not COMMIT the dedup marker"

    canonical = (await verify.execute(select(DedupResolution.canonical_file_id))).scalar_one()
    assert canonical == keeper.id, "canonical_file_id must carry the operator's pick (D-03)"
    # Phase 90 (D-09): the DUPLICATE_RESOLVED files.state dual-write was removed; only the marker persists.


@pytest.mark.asyncio
async def test_undo_endpoint_commits_marker_delete_and_restore(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """After POST /undo, a SEPARATE session sees the marker gone and previous_state restored."""
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert resolve.status_code == 200

    payload = json.dumps([{"id": str(dup.id)}])
    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": payload})
    assert undo.status_code == 200

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees the undo's committed DELETE.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)))).scalar_one()
    assert remaining == 0, "undo did not COMMIT the marker DELETE"


# ---------------------------------------------------------------------------
# Phase 90 (PR-A, BLOCKER FIX): dedup-undo is DECOUPLED from any scalar state. The marker DELETE + early-return
# gate derive from the payload id-set ALONE, so PR-B stripping previous_state can NEVER no-op the undo.
# These round-trips use the ACTUAL server-rendered file_states payload (never a hand-crafted dict).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_roundtrip_deletes_marker_from_server_payload(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """Real /resolve -> extract server file_states -> /undo: the DedupResolution marker is DELETED.

    Mirrors ``test_undo_endpoint_commits_marker_delete_and_restore`` but the undo payload is the ACTUAL
    value the resolve response rendered into ``name="file_states"`` (HTML-unescaped), NOT a literal dict.
    """
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert resolve.status_code == 200
    payload = _extract_server_file_states(resolve.text)
    # Sanity: the extracted payload is the real server JSON naming the resolved dup.
    assert str(dup.id) in payload

    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": payload})
    assert undo.status_code == 200

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees the committed marker DELETE.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)).where(DedupResolution.file_id == dup.id))).scalar_one()
    assert remaining == 0, "round-trip undo (server payload) did not delete the DedupResolution marker"


@pytest.mark.asyncio
async def test_bulk_undo_roundtrip_deletes_markers_from_server_payload(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """Real /resolve-all -> extract server file_states -> /undo-all: every DedupResolution marker DELETED."""
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper_a = _make_file("/m/keepA.mp3", "mp3", HASH_A)
    dup_a = _make_file("/m/dupA.mp3", "mp3", HASH_A)
    keeper_b = _make_file("/m/keepB.mp3", "mp3", HASH_B)
    dup_b = _make_file("/m/dupB.mp3", "mp3", HASH_B)
    session.add_all([keeper_a, dup_a, keeper_b, dup_b])
    await session.flush()

    resolve = await client.post("/duplicates/resolve-all", data={"page": "1", "page_size": "20"})
    assert resolve.status_code == 200
    payload = _extract_server_file_states(resolve.text)

    undo = await client.post("/duplicates/undo-all", data={"file_states": payload, "page": "1", "page_size": "20"})
    assert undo.status_code == 200

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees every committed marker DELETE.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)))).scalar_one()
    assert remaining == 0, "bulk round-trip undo (server payload) did not delete every DedupResolution marker"


@pytest.mark.asyncio
async def test_undo_toast_targets_a_dom_id_that_exists_in_the_dedupe_shell(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-be1j): the toast's Undo form must hx-target a REAL element.

    Before the fix, ``toast.html`` hardcoded ``hx-target="#duplicates-list"`` -- a DOM id whose
    sole definer (``templates/duplicates/list.html``) was deleted in the v7 cutover. Under htmx
    2.0.10, ``issueAjaxRequest`` resolves ``hx-target`` BEFORE opening the XHR and aborts with
    ``htmx:targetError`` on a null target -- while the Alpine ``@click="show = false"`` has
    already dismissed the toast, so clicking Undo silently issued NO request. The toast must
    instead target the resolve response's OWN placeholder id, which is guaranteed to exist in
    the DOM the instant the toast is shown.
    """
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    assert resolve.status_code == 200

    placeholder_match = re.search(r'<div id="(group-[a-f0-9]+)" style="display:none;">', resolve.text)
    assert placeholder_match is not None, "resolve response no longer renders its group placeholder"
    placeholder_id = placeholder_match.group(1)

    toast_target_match = re.search(
        r'<form hx-post="/duplicates/[a-f0-9]+/undo"\s+hx-target="#([^"]+)"',
        resolve.text,
    )
    assert toast_target_match is not None, "undo form is missing an hx-target"
    assert toast_target_match.group(1) != "duplicates-list", "toast still targets the dead #duplicates-list id"
    assert toast_target_match.group(1) == placeholder_id, "toast must target the resolve response's OWN placeholder id"


@pytest.mark.asyncio
async def test_undo_endpoint_returns_shell_shaped_dupe_group_card(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-be1j): the /undo response must fit back into the Dedupe workspace shell.

    ``undo_response.html`` used to render ``duplicates/partials/group_card.html`` -- the legacy
    accordion-row shape (deleted-page style). Retargeting the toast alone is insufficient: the
    restored element must be the SAME ``_dupe_group.html`` shape (``id="dupe-group-{hash}"``,
    keeper-select radios) every other card in the live workspace uses, or the group would render
    with the wrong shape and lose its resolve wiring.
    """
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    assert resolve.status_code == 200
    file_states = _extract_server_file_states(resolve.text)

    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": file_states})
    assert undo.status_code == 200
    assert f'id="dupe-group-{HASH_A}"' in undo.text, "undo did not restore the shell-shaped _dupe_group.html card"
    assert f'hx-post="/duplicates/{HASH_A}/resolve"' in undo.text, "restored card lost its keeper-select resolve wiring"
    assert f'name="group-{HASH_A}"' in undo.text
    assert "Compare" not in undo.text, "undo rendered the legacy accordion row shape (group_card.html), not the shell shape"


@pytest.mark.asyncio
async def test_bulk_undo_toast_targets_a_dom_id_that_exists_in_the_dedupe_shell(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-be1j): the "Undo All" toast must also hx-target a REAL, persistent element.

    Same dead-id defect as the single-group Undo, on the bulk path. ``#dedupe-bulk-response`` is
    the persistent status div ``dedupe_workspace.html`` renders for the bulk-resolve/undo flow, so
    it is a valid, always-present target inside the live Dedupe workspace shell.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    resolve_all = await client.post("/duplicates/resolve-all", data={"page": "1", "page_size": "20"})
    assert resolve_all.status_code == 200

    toast_target_match = re.search(
        r'<form hx-post="/duplicates/undo-all"\s+hx-target="#([^"]+)"',
        resolve_all.text,
    )
    assert toast_target_match is not None, "bulk undo form is missing an hx-target"
    target_id = toast_target_match.group(1)
    assert target_id != "duplicates-list", "bulk toast still targets the dead #duplicates-list id"

    workspace = await client.get("/s/dedupe")
    assert workspace.status_code == 200
    assert f'id="{target_id}"' in workspace.text, "bulk toast target does not exist in the live Dedupe workspace shell"


@pytest.mark.asyncio
async def test_undo_roundtrip_id_only_payload_still_deletes_marker(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """THE BLOCKER GUARD: an id-only payload (PR-B shape, no previous_state) STILL deletes the marker.

    Take the real server-rendered payload, STRIP ``previous_state`` from every entry (emulating the shape
    PR-B produces once the capture is removed), POST to /undo, and assert the marker is deleted. Under the
    OLD ``if not restore_by_id: return 0`` gate this no-op'd silently; the decoupled gate must delete it.
    """
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert resolve.status_code == 200

    # Strip previous_state from every entry -> the PR-B id-only shape [{"id": ...}].
    server_payload = json.loads(_extract_server_file_states(resolve.text))
    id_only = [{"id": entry["id"]} for entry in server_payload]
    assert all("previous_state" not in e for e in id_only)

    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": json.dumps(id_only)})
    assert undo.status_code == 200

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees the committed marker DELETE.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)).where(DedupResolution.file_id == dup.id))).scalar_one()
    assert remaining == 0, "id-only (PR-B shape) undo did NOT delete the marker -- the blocker regressed"
