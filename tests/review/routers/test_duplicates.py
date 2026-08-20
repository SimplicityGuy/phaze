"""Integration tests for duplicate resolution router."""

import html as html_mod
import json
import re
from urllib.parse import quote
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
_PLAN_ID_RE = re.compile(r'name="plan_id"\s+value="([^"]*)"')
_PLAN_IDS_RE = re.compile(r'name="plan_ids"\s+value="([^"]*)"')


@pytest.fixture(autouse=True)
def _route_legacy_test_posts_through_review(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy resolve/undo coverage while exercising the required review handshake."""
    original_post = client.post

    async def reviewed_post(url: str, *args, **kwargs):
        data = kwargs.get("data") or {}
        if url.startswith("/duplicates/") and url.endswith("/resolve") and "canonical_id" in data:
            review = await original_post(url.removesuffix("/resolve") + "/review", data={"canonical_id": data["canonical_id"]})
            match = _PLAN_ID_RE.search(review.text)
            if match is None:
                return review
            return await original_post(url, data={"plan_id": match.group(1)})
        if url == "/duplicates/resolve-all" and "group_hashes" in data:
            review = await original_post("/duplicates/review-all", data={"group_hashes": data["group_hashes"]})
            plan_ids = _PLAN_IDS_RE.findall(review.text)
            if not plan_ids:
                return review
            return await original_post(url, data={"plan_ids": plan_ids})
        return await original_post(url, *args, **kwargs)

    monkeypatch.setattr(client, "post", reviewed_post)


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
async def test_list_duplicates_redirects_even_with_hx_request_header(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-y4s6: GET /duplicates/ redirects unconditionally now, even with an HX-Request header.

    The in-page HX group-list/pagination fragment this used to preserve
    (``duplicates/partials/group_list.html``, composed of ``group_card.html`` + ``pagination.html``)
    had no live caller left post-v7-cutover and was deleted outright -- the live Dedupe workspace
    (``pipeline/partials/dedupe_workspace.html``) renders its cards inline with no pagination and
    never hx-gets this bare path.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get("/duplicates/", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/s/dedupe"


@pytest.mark.asyncio
async def test_compare_endpoint(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/{hash}/compare returns a comparison table with the staged review action."""
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
    assert "Review decision" in response.text
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
async def test_resolve_group_with_canonical_id_outside_group_resolves_nothing(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-xasy: POST /resolve with a canonical_id that belongs to a DIFFERENT file must be a no-op.

    Regression for the reported failure scenario: a replayed form (e.g. a stale browser tab after the
    original keeper was deleted and re-scanned under a new UUID) can carry a canonical_id that still
    passes the FK because it names SOME live file -- just not a member of this group. Before the fix,
    every member of the group (including whichever file would have been the intended keeper) got a
    DedupResolution marker pointing at that unrelated file, silently orphaning the group with zero
    surviving canonical.
    """
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution
    from phaze.services.review import get_dedupe_groups

    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    outsider = _make_file("/dir/unrelated.mp3", "mp3", HASH_B)
    session.add_all([f1, f2, outsider])
    await session.flush()

    response = await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(outsider.id)},
    )

    assert response.status_code == 200

    # No member of group A -- keeper or otherwise -- got a marker.
    marker_ids = set((await session.execute(select(DedupResolution.file_id))).scalars().all())
    assert marker_ids == set()

    # The group is still visible, unresolved, in the live Dedupe workspace's own group source (it
    # did not silently vanish).
    groups = await get_dedupe_groups(session)
    assert any(g["sha256_hash"] == HASH_A for g in groups)


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

    # Construct file_states for undo. phaze-btix: the CAS DELETE now requires the payload's
    # canonical_id to match the marker's own canonical_file_id, not just the file id.
    file_states = [{"id": str(f2.id), "canonical_id": str(f1.id)}]

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
    """POST /duplicates/resolve-all resolves every operator-submitted group_hashes entry."""
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
        data={"group_hashes": [HASH_A, HASH_B]},
    )

    assert response.status_code == 200
    assert "Resolved" in response.text
    assert "groups" in response.text.lower()


@pytest.mark.asyncio
async def test_bulk_review_summarizes_without_resolving(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/review-all is a read-only boundary before the existing bulk commit."""
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution

    lower_quality = _make_file("/dir/lower.mp3", "mp3", HASH_A, file_size=1000)
    higher_quality = _make_file("/dir/higher.mp3", "mp3", HASH_A, file_size=2000)
    session.add_all([lower_quality, higher_quality])
    await session.flush()
    session.add_all(
        [
            _make_metadata(lower_quality.id, bitrate=128_000),
            _make_metadata(higher_quality.id, bitrate=320_000),
        ]
    )
    await session.commit()

    response = await client.post("/duplicates/review-all", data={"group_hashes": [HASH_A]})

    assert response.status_code == 200
    assert "No files have been archived yet" in response.text
    assert "higher.mp3" in response.text and "320 kbps" in response.text and "highest bitrate" in response.text
    assert 'hx-post="/duplicates/resolve-all"' in response.text
    assert list((await session.execute(select(DedupResolution))).scalars()) == []


@pytest.mark.asyncio
async def test_direct_single_and_bulk_bypasses_are_rejected(session: AsyncSession, client: AsyncClient) -> None:
    """Canonical IDs and hashes cannot invoke destructive endpoints without an opaque plan."""
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution

    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    single = await client.request("POST", f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    bulk = await client.request("POST", "/duplicates/resolve-all", data={"group_hashes": [HASH_A]})

    assert single.status_code == 422
    assert bulk.status_code == 422
    assert list((await session.execute(select(DedupResolution))).scalars()) == []


@pytest.mark.asyncio
async def test_single_plan_binds_canonical_and_full_membership(session: AsyncSession, client: AsyncClient) -> None:
    """A changed group rejects atomically; extra canonical form data cannot alter a valid plan."""
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution
    from phaze.models.dedup_review_plan import DedupReviewPlan

    keeper = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    duplicate = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, duplicate])
    await session.flush()
    review = await client.request("POST", f"/duplicates/{HASH_A}/review", data={"canonical_id": str(keeper.id)})
    plan_match = _PLAN_ID_RE.search(review.text)
    assert plan_match is not None

    newcomer = _make_file("/dir/new.mp3", "mp3", HASH_A)
    session.add(newcomer)
    await session.commit()
    stale = await client.request(
        "POST",
        f"/duplicates/{HASH_A}/resolve",
        data={"plan_id": plan_match.group(1), "canonical_id": str(duplicate.id)},
    )

    assert stale.status_code == 200 and "nothing to resolve" in stale.text.lower()
    assert list((await session.execute(select(DedupResolution))).scalars()) == []
    plan = await session.get(DedupReviewPlan, uuid.UUID(plan_match.group(1)))
    assert plan is not None and plan.committed_at is None


@pytest.mark.asyncio
async def test_committed_plan_audits_bound_canonical_and_keeps_undo(session: AsyncSession, client: AsyncClient) -> None:
    """Commit ignores injected canonical data, stamps the plan, and preserves server-produced undo."""
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution
    from phaze.models.dedup_review_plan import DedupReviewPlan

    keeper = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    duplicate = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, duplicate])
    await session.flush()
    review = await client.request("POST", f"/duplicates/{HASH_A}/review", data={"canonical_id": str(keeper.id)})
    plan_match = _PLAN_ID_RE.search(review.text)
    assert plan_match is not None

    resolved = await client.request(
        "POST",
        f"/duplicates/{HASH_A}/resolve",
        data={"plan_id": plan_match.group(1), "canonical_id": str(duplicate.id)},
    )
    assert resolved.status_code == 200
    markers = list((await session.execute(select(DedupResolution))).scalars())
    assert len(markers) == 1 and markers[0].file_id == duplicate.id and markers[0].canonical_file_id == keeper.id
    plan = await session.get(DedupReviewPlan, uuid.UUID(plan_match.group(1)))
    assert plan is not None and plan.committed_at is not None

    undone = await client.request(
        "POST",
        f"/duplicates/{HASH_A}/undo",
        data={"file_states": _extract_server_file_states(resolved.text)},
    )
    assert undone.status_code == 200
    assert list((await session.execute(select(DedupResolution))).scalars()) == []


@pytest.mark.asyncio
async def test_review_rejects_truncated_group_without_plan(session: AsyncSession, client: AsyncClient) -> None:
    """A 501-member group cannot archive its unseen member through single or bulk review."""
    files = [_make_file(f"/dir/copy-{index:03}.mp3", "mp3", HASH_A) for index in range(501)]
    session.add_all(files)
    await session.commit()

    single = await client.request("POST", f"/duplicates/{HASH_A}/review", data={"canonical_id": str(files[0].id)})
    bulk = await client.request("POST", "/duplicates/review-all", data={"group_hashes": [HASH_A]})

    assert 'name="plan_id"' not in single.text
    assert 'name="plan_ids"' not in bulk.text
    assert "No unseen copy can be archived" in bulk.text


@pytest.mark.asyncio
async def test_bulk_resolve_only_touches_submitted_hashes(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/resolve-all leaves a duplicate group untouched if its hash was NOT submitted.

    Regression for phaze-81bu: bulk_resolve used to re-derive "the current page" via a fresh
    LIMIT/OFFSET query instead of acting on the group hashes the operator was actually shown, so it
    could resolve a group the operator never reviewed. Submitting only HASH_A must leave HASH_B (also a
    qualifying duplicate group) completely unresolved.
    """
    from sqlalchemy import select

    from phaze.models.dedup_resolution import DedupResolution

    # Group A -- submitted
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    # Group B -- NOT submitted, but still a duplicate group the naive page/page_size re-derivation
    # would have picked up.
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"group_hashes": [HASH_A]},
    )

    assert response.status_code == 200

    marker_file_ids = set((await session.execute(select(DedupResolution.file_id))).scalars().all())
    # Exactly one of Group A's files (the non-canonical one) got a marker; Group B is untouched.
    assert marker_file_ids <= {f1.id, f2.id}
    assert marker_file_ids, "the submitted group was not resolved"
    assert f3.id not in marker_file_ids
    assert f4.id not in marker_file_ids


@pytest.mark.asyncio
async def test_bulk_resolve_oob_removes_every_submitted_group_card(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-wgse): bulk resolve used to leave every #dupe-group-{hash} card fully
    rendered on screen -- the pipeline Dedupe workspace targets #dedupe-bulk-response (a status div
    ABOVE the card list), not the card list itself, so the primary swap never touched the cards. An
    operator could then click an already-archived group's keeper radio and re-POST
    /duplicates/{hash}/resolve. The response must now OOB-delete each SUBMITTED group's card
    (mirroring the phaze-gwe1 row-removal idiom), so the cards actually disappear.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=1500)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"group_hashes": [HASH_A, HASH_B]},
    )

    assert response.status_code == 200
    for group_hash in (HASH_A, HASH_B):
        assert f'<div id="dupe-group-{group_hash}" hx-swap-oob="delete"></div>' in response.text, (
            f"expected an OOB delete swap removing the rendered card for group {group_hash}"
        )


@pytest.mark.asyncio
async def test_bulk_resolve_oob_removes_card_for_a_hash_that_resolved_to_nothing(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-wgse): a submitted hash that resolves to NO group at all (e.g. a group a
    concurrent request already fully archived, or a stale hash from a page the operator hasn't
    refreshed) must still be OOB-removed from the operator's rendered card list -- not just the
    hashes this call actually wrote a marker for. ``bulk_resolve`` only iterates
    ``find_duplicate_groups_by_hashes``' result, which silently drops a hash with nothing left to
    resolve, so the OOB removal must be driven by the SUBMITTED hashes, not the resolved subset.
    """
    unknown_hash = "c" * 64  # never backed by any FileRecord -- resolves to nothing.
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"group_hashes": [HASH_A, unknown_hash]},
    )

    assert response.status_code == 200
    assert "Resolved 1 groups" in response.text, "only the real group should count towards resolved_groups"
    assert f'<div id="dupe-group-{HASH_A}" hx-swap-oob="delete"></div>' in response.text
    assert f'<div id="dupe-group-{unknown_hash}" hx-swap-oob="delete"></div>' not in response.text, (
        "an unreviewable caller-supplied hash must not enter the committed plan or its DOM effects"
    )


@pytest.mark.asyncio
async def test_bulk_resolve_no_group_hashes_resolves_nothing(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/resolve-all without opaque reviewed plans is rejected."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.post("/duplicates/resolve-all", data={})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_bulk_undo(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/undo-all restores all bulk-resolved files."""
    # Group A
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Bulk resolve first. With no metadata/bitrate differences, score_group's tie-break falls to the
    # (stable-sorted) shortest path, so f1 -- inserted first, same path length as f2 -- is the keeper.
    await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})

    # Build undo states. phaze-btix: the CAS DELETE now requires the payload's canonical_id to match
    # the marker's own canonical_file_id, not just the file id.
    file_states = [{"id": str(f2.id), "canonical_id": str(f1.id)}]

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
    """After resolving a group, it no longer appears in the live Dedupe workspace's group source."""
    from phaze.services.review import get_dedupe_groups

    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    # Resolve the group
    await client.post(
        f"/duplicates/{HASH_A}/resolve",
        data={"canonical_id": str(f1.id)},
    )

    groups = await get_dedupe_groups(session)
    assert not any(g["sha256_hash"] == HASH_A for g in groups)


@pytest.mark.asyncio
async def test_duplicates_index_redirects_into_the_dedupe_workspace(session: AsyncSession, client: AsyncClient) -> None:
    """A seeded corpus does not change the answer: GET /duplicates/ is a bare 302 into the shell.

    phaze-nt8f renamed this from ``test_stats_header_values``. That name (and its "includes correct
    group count and total files" docstring) survived the Phase-57 rewrite that reduced the body to a
    redirect assertion, leaving a test that claimed to cover the Groups / Total Files stats header
    while asserting nothing about it -- which is precisely why the header's orphaning went unnoticed
    for a phase. There is no stats header any more (``duplicates/partials/stats_header.html`` is
    deleted); what this test actually pins is the redirect, so it now says so.
    """
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

    # phaze-btix: the CAS delete now requires the payload's canonical_id to match the marker's own
    # canonical_file_id, not just the file id.
    payload = json.dumps([{"id": str(dup.id), "canonical_id": str(keeper.id)}])
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

    resolve = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A, HASH_B]})
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
    assert f'hx-post="/duplicates/{HASH_A}/review"' in undo.text, "restored card lost its staged review wiring"
    assert 'name="canonical_id"' in undo.text
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

    resolve_all = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})
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
async def test_undo_roundtrip_server_payload_still_deletes_marker(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """The REAL server-rendered payload (id + canonical_id) still round-trips and deletes the marker.

    Historically (Phase 90 PR-B / D-09) this test pinned an id-ONLY payload shape as sufficient to
    delete the marker -- ``[{"id": ...}]`` with no other field. phaze-btix found that shape is exactly
    the defect: file_id alone cannot distinguish the marker THIS resolution wrote from a marker a
    LATER, different resolution of the same file wrote, so a stale id-only replay could silently
    revert a newer resolution despite the docstring's claimed CAS no-op. ``resolve_group`` now also
    echoes ``canonical_id`` in the payload and ``undo_resolve`` requires BOTH to match the live marker
    (see ``test_undo_roundtrip_id_only_payload_no_longer_deletes_marker`` for the id-only case). This
    test pins the happy path: the UNMODIFIED server payload (which already carries both fields) still
    round-trips end to end.
    """
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert resolve.status_code == 200

    server_payload = _extract_server_file_states(resolve.text)
    assert '"canonical_id"' in server_payload, "resolve response no longer echoes canonical_id (phaze-btix CAS anchor)"

    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": server_payload})
    assert undo.status_code == 200

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees the committed marker DELETE.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)).where(DedupResolution.file_id == dup.id))).scalar_one()
    assert remaining == 0, "round-trip undo (real server payload) did NOT delete the marker"


@pytest.mark.asyncio
async def test_undo_roundtrip_id_only_payload_no_longer_deletes_marker(session: AsyncSession, client: AsyncClient, verify: AsyncSession) -> None:
    """phaze-btix: an id-only payload (missing canonical_id) is now an UNUSABLE element, not a valid undo.

    Take the real server-rendered payload and STRIP ``canonical_id`` from every entry (emulating a
    replay of the shape the pre-fix code accepted). The CAS gate in ``undo_resolve`` now requires both
    the file id AND the canonical_id it was resolved with to match the live marker; an entry missing
    either half is skipped like any other malformed element (contract rule 2), so the DELETE matches
    nothing and the marker survives. This is the deliberate behavior change that closes the ABA hole:
    file_id alone was never a safe CAS anchor.
    """
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    keeper = _make_file("/m/keeper.mp3", "mp3", HASH_A)
    dup = _make_file("/m/dup.mp3", "mp3", HASH_A)
    session.add_all([keeper, dup])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(keeper.id)})
    assert resolve.status_code == 200

    server_payload = json.loads(_extract_server_file_states(resolve.text))
    id_only = [{"id": entry["id"]} for entry in server_payload]
    assert all("canonical_id" not in e for e in id_only)

    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": json.dumps(id_only)})
    assert undo.status_code == 200  # element-level malformed shape -> graceful no-op, never a 500/4xx

    # 92-04 (CLEAN-02): shared ``verify`` fixture (per-test connection) sees the marker was NOT deleted.
    remaining = (await verify.execute(select(func.count(DedupResolution.id)).where(DedupResolution.file_id == dup.id))).scalar_one()
    assert remaining == 1, "id-only undo deleted the marker -- the phaze-btix CAS gate regressed"


# ---------------------------------------------------------------------------
# phaze-m7ya: compare/undo must find a group NO MATTER WHERE IT SORTS.
#
# Both endpoints used to locate a single group by fetching a hardcoded first 1000 groups
# (``find_duplicate_groups_with_metadata(session, limit=1000, offset=0)``) and linear-scanning the
# result for the requested hash. Any group past that arbitrary boundary answered "Group not found"
# permanently -- while the list page, which paginates the FULL set, still rendered it with a Compare
# button. These tests therefore MUST build more than 1000 groups: a small fixture passes against the
# broken code and proves nothing. The hashes are zero-padded hex of the row index, so they sort
# lexicographically by index and ``_BEYOND_CAP_INDEX`` is genuinely past the old cap under the
# subquery's ``ORDER BY sha256_hash``.
# ---------------------------------------------------------------------------

_GROUP_COUNT = 1200
_BEYOND_CAP_INDEX = 1150  # comfortably past the old hardcoded 1000-group scan window


def _hash_for(index: int) -> str:
    """Return a synthetic sha256-shaped hash that sorts lexicographically by ``index``."""
    return f"{index:064x}"


async def _seed_many_duplicate_groups(session: AsyncSession, count: int = _GROUP_COUNT) -> None:
    """Insert ``count`` two-file duplicate groups so the corpus exceeds the old 1000-group scan cap."""
    files: list[FileRecord] = []
    for index in range(count):
        group_hash = _hash_for(index)
        files.append(_make_file(f"/dir/g{index}_keep.mp3", "mp3", group_hash, file_size=2000))
        files.append(_make_file(f"/dir/g{index}_dup.mp3", "mp3", group_hash, file_size=1000))
    session.add_all(files)
    await session.flush()


@pytest.mark.asyncio
async def test_compare_finds_group_beyond_the_old_1000_group_scan(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-m7ya): a group past the old cap is reachable by Compare, not 'Group not found'.

    Fails on the pre-fix code: the group at index 1150 never lands in the first 1000 hashes, so the
    linear scan misses it and the endpoint renders 'Group not found.' forever.
    """
    await _seed_many_duplicate_groups(session)
    target_hash = _hash_for(_BEYOND_CAP_INDEX)

    response = await client.get(f"/duplicates/{target_hash}/compare")

    assert response.status_code == 200
    assert "Group not found" not in response.text, "a real, unresolved group past the old 1000-group cap was unreachable"
    assert "Review decision" in response.text
    assert f"/duplicates/{target_hash}/review" in response.text


@pytest.mark.asyncio
async def test_undo_restores_card_for_group_beyond_the_old_1000_group_scan(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-m7ya): undo re-renders the restored card for a group past the old cap.

    Fails on the pre-fix code: the capped re-fetch scan yields ``group=None``, so the undo silently
    drops the restored group card and the operator's group vanishes from the workspace.
    """
    await _seed_many_duplicate_groups(session)
    target_hash = _hash_for(_BEYOND_CAP_INDEX)

    from sqlalchemy import select

    keeper_id = (
        (await session.execute(select(FileRecord.id).where(FileRecord.sha256_hash == target_hash).order_by(FileRecord.original_path)))
        .scalars()
        .first()
    )
    assert keeper_id is not None

    resolve = await client.post(f"/duplicates/{target_hash}/resolve", data={"canonical_id": str(keeper_id)})
    assert resolve.status_code == 200
    file_states = _extract_server_file_states(resolve.text)

    undo = await client.post(f"/duplicates/{target_hash}/undo", data={"file_states": file_states})

    assert undo.status_code == 200
    assert f'id="dupe-group-{target_hash}"' in undo.text, "undo dropped the restored card for a group past the old 1000-group cap"


@pytest.mark.asyncio
async def test_compare_reports_not_found_for_an_unknown_hash(session: AsyncSession, client: AsyncClient) -> None:
    """A hash that is genuinely not a duplicate group still renders the ordinary empty state, not a 500."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.get(f"/duplicates/{HASH_B}/compare")

    assert response.status_code == 200
    assert "Group not found" in response.text


# ---------------------------------------------------------------------------
# phaze-wkqk: the untrusted-input contract (src/phaze/routers/request_guards.py).
#
# Contract rule 6 makes a docstring that promises "no HTTP 500" a TEST obligation, so every payload
# shape the guard names is exercised here. Before the fix, `not-json` raised JSONDecodeError and
# `[1,2]` raised AttributeError on `int.get` -- both escaping as an unhandled HTTP 500 through
# handlers that documented a graceful no-op. Nothing below may ever assert a 5xx.
# ---------------------------------------------------------------------------

_UNDO_PATHS = (f"/duplicates/{HASH_A}/undo", "/duplicates/undo-all")

# Envelope failures (rule 1): unparseable, or valid JSON that is not an array. Whole request -> 422.
_MALFORMED_ENVELOPES = (
    "not-json",  # not JSON at all -> json.JSONDecodeError
    "",  # empty form value -- the truncated-payload case
    '{"id": "x"}',  # valid JSON, wrong container: iterating a non-empty dict yields str keys
    "{}",  # valid JSON, wrong container (empty -- used to no-op by luck, not by contract)
    "null",  # valid JSON, not a container at all
    "42",  # valid JSON scalar
    '"a string"',  # valid JSON string -- iterating it would yield characters
    # phaze-xe5a: nested deep enough to blow the interpreter's recursion limit inside json.loads
    # itself. This is RecursionError, not JSONDecodeError -- a sibling failure the guard didn't
    # catch before the fix, so it escaped as a 500 instead of the 422 every other envelope failure
    # here gets. Verified in this repo's Python 3.14.5 venv: `"[" * 2000` only raises
    # JSONDecodeError ("Expecting value") well before the recursion limit; the C scanner needs
    # ~50k+ levels of unclosed nesting to actually hit RecursionError, so 100_000 is used here to
    # reliably reproduce the failure class rather than the specific (env-dependent) threshold.
    "[" * 100_000,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _UNDO_PATHS)
@pytest.mark.parametrize("payload", _MALFORMED_ENVELOPES)
async def test_undo_malformed_envelope_returns_422_never_500(client: AsyncClient, path: str, payload: str) -> None:
    """A non-JSON or non-array ``file_states`` is a 422 envelope rejection, never a 500."""
    response = await client.post(path, data={"file_states": payload})

    assert response.status_code == 422, f"{path} with {payload!r} returned {response.status_code}"
    assert "file_states" in response.text


# Element failures (rule 2): the envelope is a valid array, but entries are unusable. These are
# SKIPPED, not escalated -- one stale entry must not void an otherwise valid bulk undo.
_MALFORMED_ELEMENT_ARRAYS = (
    "[1, 2]",  # ints -> (1).get("id") AttributeError before the fix
    '["a", "b"]',  # strings -> str.get AttributeError before the fix
    "[null]",  # None -> NoneType.get
    "[[]]",  # nested list
    '[{"id": "not-a-uuid"}]',  # the malformed-id case the docstring already claimed to handle
    '[{"nope": 1}]',  # dict without the id key
    "[]",  # the empty payload
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _UNDO_PATHS)
@pytest.mark.parametrize("payload", _MALFORMED_ELEMENT_ARRAYS)
async def test_undo_malformed_elements_are_skipped_not_500(client: AsyncClient, path: str, payload: str) -> None:
    """Unusable ENTRIES inside a well-formed array degrade to a graceful no-op 200."""
    response = await client.post(path, data={"file_states": payload})

    assert response.status_code == 200, f"{path} with {payload!r} returned {response.status_code}"


@pytest.mark.asyncio
async def test_undo_mixed_payload_undoes_the_valid_entries(session: AsyncSession, client: AsyncClient) -> None:
    """A payload mixing junk entries with one real id still undoes the real one (rule 2's whole point)."""
    from sqlalchemy import func, select

    from phaze.models.dedup_resolution import DedupResolution

    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    resolve = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    assert resolve.status_code == 200

    # phaze-btix: the one real, USABLE entry must carry both id and canonical_id -- junk entries
    # around it (including an id-only entry, now itself unusable) must not suppress it.
    payload = json.dumps([1, "junk", None, {"nope": 1}, {"id": "not-a-uuid"}, {"id": str(f2.id)}, {"id": str(f2.id), "canonical_id": str(f1.id)}])
    undo = await client.post(f"/duplicates/{HASH_A}/undo", data={"file_states": payload})

    assert undo.status_code == 200
    remaining = (await session.execute(select(func.count()).select_from(DedupResolution))).scalar_one()
    assert remaining == 0, "a junk entry suppressed the undo of a valid id"


# ---------------------------------------------------------------------------
# GET /duplicates/ history-restore response shape (phaze-64uy)
#
# duplicates/partials/pagination.html sets hx-push-url="true" on every /duplicates/?page=... control,
# so those URLs enter history. A Back with the snapshot evicted arrives as a restore carrying BOTH
# HX-Request and HX-History-Restore-Request, and htmx swaps a restore into <body> while ignoring
# hx-target. The handler's raw HX-Request check answered that with the group_list.html fragment --
# the starkest case in the bead: the dispatcher measured a 284-BYTE fragment replacing the entire
# document.
#
# routers/response_shape.py rule 1 bans the raw check; wants_fragment is the predicate.
# ---------------------------------------------------------------------------


_RESTORE_HEADERS = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


@pytest.mark.asyncio
async def test_duplicates_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET /duplicates/ falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- the buggy handler returned a 200 here too.
    """
    response = await client.get("/duplicates/", headers=_RESTORE_HEADERS)
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
    assert response.headers["location"] == "/s/dedupe"


@pytest.mark.asyncio
async def test_duplicates_history_restore_resolves_to_the_full_shell(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with the shell chrome intact."""
    response = await client.get("/duplicates/?page=2", headers=_RESTORE_HEADERS, follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the rail must be present after a restore"
    assert 'id="stage-workspace"' in body, "the shell's swap target must be present after a restore"


@pytest.mark.asyncio
async def test_duplicates_redirects_even_with_hx_request_header(client: AsyncClient) -> None:
    """phaze-y4s6: GET /duplicates/ redirects unconditionally now, even with an HX-Request header.

    The in-page HX group-list/pagination fragment this used to preserve had no live caller left
    post-v7-cutover and was deleted outright (see test_list_duplicates_redirects_even_with_hx_request_header).
    """
    response = await client.get("/duplicates/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/dedupe"


@pytest.mark.asyncio
async def test_duplicates_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/duplicates/", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/dedupe"


@pytest.mark.asyncio
async def test_dedupe_mutations_emit_no_dead_stats_header_oob(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-nt8f): no dedupe mutation ships the orphaned ``#stats-header`` OOB fragment.

    ``resolve_response.html`` / ``undo_response.html`` / ``bulk_resolve_response.html`` each used to
    emit ``<div id="stats-header" hx-swap-oob="true">{% include stats_header.html %}</div>``. The id's
    sole host, ``duplicates/list.html``, was deleted in the Phase-62 cutover (the same sweep that
    retargeted the toast away from ``#duplicates-list``), so htmx 2.0.10 found no match, fired
    ``htmx:oobErrorNoTarget`` and discarded the fragment -- after the router had paid THREE
    whole-corpus aggregates per mutation to render it. The included partial also still pointed its
    "Accept All" form at the equally-dead ``#duplicates-list``.

    Asserted on all three response bodies at once so re-adding the block to any single fork fails.
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

    bulk = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})
    assert bulk.status_code == 200

    for label, body in (("resolve", resolve.text), ("undo", undo.text), ("resolve-all", bulk.text)):
        assert "stats-header" not in body, f"{label} response still emits the orphaned #stats-header OOB fragment"
        assert "duplicates-list" not in body, f"{label} response still references the v7-cutover-deleted #duplicates-list id"


@pytest.mark.asyncio
async def test_bulk_undo_restores_the_group_cards_bulk_resolve_deleted(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-dpc5): a successful Undo All must put the deleted cards back.

    phaze-wgse made bulk resolve OOB-delete every ``#dupe-group-{hash}`` card the operator was shown,
    but ``bulk_undo`` still answered with status text alone -- on the stale premise, recorded in three
    places, that "bulk resolve never OOB-touches the individual cards". The Dedupe workspace body is
    only produced by a rail navigation (the chrome poll seeds hidden counters, nothing else), so the
    cards stayed gone for the life of the page: the operator was told N files were restored while
    looking at an empty duplicate set with no keeper radios to re-pick with.

    Asserts the full round trip through the SERVER-rendered payloads (never hand-crafted): resolve-all
    deletes the cards, its toast carries the submitted hashes, and undo-all appends live
    ``_dupe_group.html`` cards -- same id, same wired keeper radios -- back into ``#dedupe-group-list``.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=3000)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=1500)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    resolve_all = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A, HASH_B]})
    assert resolve_all.status_code == 200
    # The undo toast must carry the same hashes the OOB deletes name -- that is what makes the
    # reversal addressable at all.
    for group_hash in (HASH_A, HASH_B):
        assert f'<div id="dupe-group-{group_hash}" hx-swap-oob="delete"></div>' in resolve_all.text
        assert f'<input type="hidden" name="group_hashes" value="{group_hash}">' in resolve_all.text, (
            f"bulk undo toast does not carry group {group_hash}, so undo cannot restore its card"
        )

    undo_all = await client.post(
        "/duplicates/undo-all",
        data={"file_states": _extract_server_file_states(resolve_all.text), "group_hashes": [HASH_A, HASH_B]},
    )
    assert undo_all.status_code == 200
    assert "Undo All complete" in undo_all.text

    assert 'hx-swap-oob="beforeend:#dedupe-group-list"' in undo_all.text, "undo-all restored no cards into the workspace card list"
    for group_hash in (HASH_A, HASH_B):
        assert f'id="dupe-group-{group_hash}"' in undo_all.text, f"group {group_hash}'s card was not restored"
        assert f'hx-post="/duplicates/{group_hash}/review"' in undo_all.text, f"restored card for {group_hash} lost its staged review wiring"

    # The restore lands somewhere real: the workspace declares the container the OOB append targets.
    workspace = await client.get("/s/dedupe")
    assert workspace.status_code == 200
    assert 'id="dedupe-group-list"' in workspace.text, "the Dedupe workspace no longer declares the OOB append target"


@pytest.mark.asyncio
async def test_bulk_undo_replay_restores_no_duplicate_cards(session: AsyncSession, client: AsyncClient) -> None:
    """A second Undo All restores nothing, so it must not inject a duplicate copy of a live card.

    The markers are already gone on the replay (``undo_resolve`` is keyed on (file_id, canonical_id)
    pairs), so ``restored_count`` is 0 -- and the card is by then back on screen. Appending it again
    would create two elements with the same ``#dupe-group-{hash}`` id, the duplicate-id OOB hazard this
    repo has paid for four times (gzrd / op6f / 7j50 / 5p43).
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    session.add_all([f1, f2])
    await session.flush()

    resolve_all = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})
    payload = _extract_server_file_states(resolve_all.text)

    first = await client.post("/duplicates/undo-all", data={"file_states": payload, "group_hashes": [HASH_A]})
    assert f'id="dupe-group-{HASH_A}"' in first.text

    replay = await client.post("/duplicates/undo-all", data={"file_states": payload, "group_hashes": [HASH_A]})
    assert replay.status_code == 200
    assert "0 files restored" in replay.text
    assert "hx-swap-oob" not in replay.text, "a no-op replay must not re-append a card that is already on screen"


@pytest.mark.asyncio
async def test_bulk_undo_without_group_hashes_still_undoes(session: AsyncSession, client: AsyncClient) -> None:
    """A stale tab whose toast predates phaze-dpc5 posts no ``group_hashes`` -- it must still undo.

    ``group_hashes`` is optional on purpose: the operator has a 10-second window to reverse a bulk
    archive, and 422-ing that click because the page was rendered by an older build would be a worse
    failure than the missing cards this bead fixes.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    session.add_all([f1, f2])
    await session.flush()

    resolve_all = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})
    payload = _extract_server_file_states(resolve_all.text)

    undo_all = await client.post("/duplicates/undo-all", data={"file_states": payload})
    assert undo_all.status_code == 200
    assert "1 file restored" in undo_all.text


# ---------------------------------------------------------------------------
# phaze-i0jqu: PostgreSQL cannot bind a NUL (U+0000) or a lone Unicode surrogate in a UTF8 text
# parameter -- asyncpg raises CharacterNotInRepertoireError and aborts the transaction. Every
# duplicates endpoint that takes a group_hash (path or form) must reject it BEFORE it reaches any
# query or pg_advisory_xact_lock(hashtext(...)) call, never 500.
# ---------------------------------------------------------------------------

_NUL_HASH = "a" * 30 + "\x00" + "a" * 33  # 64 chars, matches every other test hash's shape
_SURROGATE_HASH = "a" * 30 + "\ud800" + "a" * 33


def _quote_hash(bad_hash: str) -> str:
    """Percent-encode a group_hash that may contain a lone surrogate.

    ``urllib.parse.quote`` UTF-8-encodes its input, which raises ``UnicodeEncodeError`` on a lone
    surrogate (by definition -- that is exactly why PostgreSQL cannot store one either). Encoding
    with ``surrogatepass`` first (WTF-8) and quoting the resulting bytes reproduces what a raw
    socket actually puts on the wire for a crafted/corrupted request; ``quote`` accepts ``bytes``
    directly and skips its own ``str.encode`` step in that case.
    """
    return quote(bad_hash.encode("utf-8", "surrogatepass"), safe="")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_hash", [_NUL_HASH, _SURROGATE_HASH])
async def test_compare_rejects_invalid_group_hash_never_500(client: AsyncClient, bad_hash: str) -> None:
    """GET /duplicates/{hash}/compare with a NUL/lone-surrogate hash degrades to 'Group not found'."""
    response = await client.get(f"/duplicates/{_quote_hash(bad_hash)}/compare")

    assert response.status_code == 200
    assert "Group not found" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_hash", [_NUL_HASH, _SURROGATE_HASH])
async def test_resolve_rejects_invalid_group_hash_never_500(client: AsyncClient, bad_hash: str) -> None:
    """POST /duplicates/{hash}/resolve with a NUL/lone-surrogate hash never reaches
    ``pg_advisory_xact_lock(hashtext(group_hash))`` -- it degrades to the honest 0-resolved no-op,
    never a 500.
    """
    response = await client.post(
        f"/duplicates/{_quote_hash(bad_hash)}/resolve",
        data={"canonical_id": str(uuid.uuid4())},
    )

    assert response.status_code == 200
    assert "Group resolved" not in response.text
    assert "Nothing to resolve" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_hash", [_NUL_HASH, _SURROGATE_HASH])
async def test_undo_rejects_invalid_group_hash_never_500(client: AsyncClient, bad_hash: str) -> None:
    """POST /duplicates/{hash}/undo with a NUL/lone-surrogate hash still undoes (the undo itself
    never references group_hash) but skips the post-undo re-fetch, never a 500.
    """
    response = await client.post(
        f"/duplicates/{_quote_hash(bad_hash)}/undo",
        data={"file_states": "[]"},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bulk_resolve_drops_invalid_hash_never_500(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/resolve-all with a NUL/lone-surrogate hash mixed into group_hashes still
    resolves the real hashes and never 500s on the whole batch.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    response = await client.post(
        "/duplicates/resolve-all",
        data={"group_hashes": [HASH_A, _NUL_HASH]},
    )

    assert response.status_code == 200
    assert "Resolved 1 groups" in response.text


@pytest.mark.asyncio
async def test_bulk_undo_drops_invalid_hash_never_500(session: AsyncSession, client: AsyncClient) -> None:
    """POST /duplicates/undo-all with a NUL/lone-surrogate hash mixed into group_hashes still
    restores the real hash's card and never 500s.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=2000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=1000)
    session.add_all([f1, f2])
    await session.flush()

    resolve_all = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})
    payload = _extract_server_file_states(resolve_all.text)

    response = await client.post(
        "/duplicates/undo-all",
        data={"file_states": payload, "group_hashes": [HASH_A, _NUL_HASH]},
    )

    assert response.status_code == 200
    assert f'id="dupe-group-{HASH_A}"' in response.text


# ---------------------------------------------------------------------------
# phaze-ptzse: resolve_group returning 0 on a NON-empty group (a stale resubmit of an
# already-resolved card, or a concurrent resolve that already claimed the canonical) must render an
# honest "nothing changed" toast, never "Group resolved" -- and must not remove the still-live card.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_of_an_already_resolved_group_is_an_honest_noop(session: AsyncSession, client: AsyncClient) -> None:
    """A second /resolve against an already-fully-resolved group (e.g. the hidden group_hashes
    inputs on a stale card surviving its own outerHTML swap and being resubmitted) must not report
    success.
    """
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    first = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    assert first.status_code == 200
    assert "Group resolved" in first.text

    replay = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})

    assert replay.status_code == 200
    assert "Group resolved" not in replay.text
    assert "Nothing to resolve" in replay.text


@pytest.mark.asyncio
async def test_resolve_reachable_without_concurrency_single_member_group_is_honest_noop(session: AsyncSession, client: AsyncClient) -> None:
    """Verifier-found repro: resolve a group singly, then resubmit its (now-stale) hidden
    ``group_hashes`` input via AUTO-KEEP -- the sha256_hash still names a real FileRecord (the
    surviving canonical), so ``find_duplicate_groups_by_hashes`` returns it as a 1-file 'group' with
    NO group-size filter, unlike its ``count > 1``-enforcing sibling. resolve_group must still no-op
    honestly (0 resolved), not silently succeed against a group of one.
    """
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    first = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(f1.id)})
    assert first.status_code == 200

    # f1 is now the sole unresolved member of HASH_A -- resubmit the same (stale) hash via bulk.
    bulk = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_A]})

    assert bulk.status_code == 200
    assert "None of the shown groups are still available" in bulk.text


# ---------------------------------------------------------------------------
# phaze-cvjby: find_duplicate_groups_by_hashes has no count>1 filter -- bulk_undo's re-hydration
# loop must not OOB-append a bogus single-file "group" card for a hash whose group is still (or is
# once again) resolved down to one file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_undo_does_not_resurrect_a_single_member_group_card(session: AsyncSession, client: AsyncClient) -> None:
    """Verifier-found repro, no concurrency needed: single-resolve group A, bulk-resolve group B,
    then Undo All with BOTH hashes (A's hidden group_hashes input survives its own card's outerHTML
    swap and is resubmitted wholesale, exactly as AUTO-KEEP HIGHEST QUALITY -> UNDO ALL would send
    it). A must NOT get a bogus 1-file card; only B's real 2-file group may be restored.
    """
    a1 = _make_file("/dir/a-keep.mp3", "mp3", HASH_A)
    a2 = _make_file("/dir/a-dup.mp3", "mp3", HASH_A)
    b1 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=2000)
    b2 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=1000)
    session.add_all([a1, a2, b1, b2])
    await session.flush()

    # A is resolved singly and stays resolved -- only its lone canonical (a1) remains unresolved.
    resolve_a = await client.post(f"/duplicates/{HASH_A}/resolve", data={"canonical_id": str(a1.id)})
    assert resolve_a.status_code == 200

    resolve_b = await client.post("/duplicates/resolve-all", data={"group_hashes": [HASH_B]})
    b_payload = _extract_server_file_states(resolve_b.text)

    undo_all = await client.post(
        "/duplicates/undo-all",
        data={"file_states": b_payload, "group_hashes": [HASH_A, HASH_B]},
    )

    assert undo_all.status_code == 200
    assert f'id="dupe-group-{HASH_B}"' in undo_all.text, "B's real 2-file group must be restored"
    assert f'id="dupe-group-{HASH_A}"' not in undo_all.text, (
        "A has only ONE unresolved member (its surviving canonical) -- it must not get a bogus keeper-select card"
    )


# ---------------------------------------------------------------------------
# phaze-4iq5t: the Dedupe workspace's group-cap MUST always be visible/escapable. Before this,
# services/review.get_dedupe_groups called find_duplicate_groups_with_metadata with no offset
# override, so a corpus with more than GROUP_PAGE_SIZE (100) duplicate-hash groups silently
# rendered only the first 100 (by, previously, arbitrary hash order) with no "showing N of M" and
# no way to reach the rest -- permanently and undetectably, since sha256 hash order gives no
# observable signal that anything was cut.
# ---------------------------------------------------------------------------


async def _seed_n_duplicate_groups(session: AsyncSession, n: int) -> list[str]:
    """Insert ``n`` distinct 2-member duplicate-hash groups; return their hashes in insertion order."""
    hashes = [f"{i:064x}" for i in range(n)]
    files = []
    for i, h in enumerate(hashes):
        files.append(_make_file(f"/dir/{i:08d}-1.mp3", "mp3", h))
        files.append(_make_file(f"/dir/{i:08d}-2.mp3", "mp3", h))
    session.add_all(files)
    await session.flush()
    return hashes


@pytest.mark.asyncio
async def test_dedupe_workspace_signals_a_bounded_render_past_the_group_cap(session: AsyncSession, client: AsyncClient) -> None:
    """AC5 regression: a corpus with MORE than GROUP_PAGE_SIZE groups must never render a bounded
    subset silently. GET /s/dedupe must show a bounded number of cards (never an unbounded render,
    AC3) AND an explicit, textual indication that more groups exist (AC1(b)/(a) minimum bar) -- a
    plain group count with no "showing X of Y" and no way to reach the rest is exactly the bug.
    """
    from phaze.services.dedup import GROUP_PAGE_SIZE

    total = GROUP_PAGE_SIZE + 1
    await _seed_n_duplicate_groups(session, total)

    workspace = await client.get("/s/dedupe")
    assert workspace.status_code == 200

    rendered_cards = workspace.text.count('id="dupe-group-')
    assert rendered_cards == GROUP_PAGE_SIZE, "first render must stay bounded to GROUP_PAGE_SIZE (AC3)"
    assert f"Showing {GROUP_PAGE_SIZE} of {total} duplicate groups" in workspace.text, (
        "operator must be told groups exist beyond what is rendered (AC1(b) minimum bar) -- a bare "
        "count with no 'showing X of Y' is silent truncation, the exact bug this regresses"
    )
    assert f'hx-get="/duplicates/groups?offset={GROUP_PAGE_SIZE}"' in workspace.text, (
        "a real 'Load more' affordance must be reachable, not just a total count (AC1(a))"
    )


@pytest.mark.asyncio
async def test_dedupe_workspace_omits_load_more_when_everything_fits(session: AsyncSession, client: AsyncClient) -> None:
    """The inverse: at or under the cap, no 'Load more' button and the plain (non-"Showing") count."""
    await _seed_n_duplicate_groups(session, 3)

    workspace = await client.get("/s/dedupe")

    assert workspace.status_code == 200
    assert "3 duplicate groups" in workspace.text
    assert "Showing" not in workspace.text
    assert "/duplicates/groups?offset=" not in workspace.text


@pytest.mark.asyncio
async def test_load_more_groups_returns_the_next_page_and_updates_the_subcount(session: AsyncSession, client: AsyncClient) -> None:
    """GET /duplicates/groups?offset=N returns the remaining groups, updates the header subcount to
    the true total (no longer "Showing X of Y" once everything is loaded), OOB-appends the new
    cards' hidden group_hashes inputs (bulk review must cover load-more'd groups too), and removes
    the 'Load more' button once exhausted.
    """
    from phaze.services.dedup import GROUP_PAGE_SIZE

    total = GROUP_PAGE_SIZE + 5
    hashes = await _seed_n_duplicate_groups(session, total)

    more = await client.get(f"/duplicates/groups?offset={GROUP_PAGE_SIZE}")
    assert more.status_code == 200

    rendered_cards = more.text.count('id="dupe-group-')
    assert rendered_cards == 5, "the remaining 5 groups, not another full page"
    for h in hashes[GROUP_PAGE_SIZE:]:
        assert f'value="{h}"' in more.text, f"hash {h} must OOB-append into #dedupe-group-hash-inputs for bulk review"
    assert f"{total} duplicate groups" in more.text, "once every group has loaded, the count is the honest, un-truncated total"
    assert "Showing" not in more.text, "nothing left to load -- the subcount must not still claim a partial view"
    assert "/duplicates/groups?offset=" not in more.text, "exhausted -- no further 'Load more' button"
    assert 'id="stage-workspace-subcount"' in more.text and 'hx-swap-oob="true"' in more.text


@pytest.mark.asyncio
async def test_load_more_groups_stays_paginated_when_more_remain(session: AsyncSession, client: AsyncClient) -> None:
    """A second page that ITSELF does not reach the total still offers its own 'Load more'."""
    from phaze.services.dedup import GROUP_PAGE_SIZE

    total = 2 * GROUP_PAGE_SIZE + 1
    await _seed_n_duplicate_groups(session, total)

    more = await client.get(f"/duplicates/groups?offset={GROUP_PAGE_SIZE}")

    assert more.status_code == 200
    assert more.text.count('id="dupe-group-') == GROUP_PAGE_SIZE
    assert f"Showing {2 * GROUP_PAGE_SIZE} of {total} duplicate groups" in more.text
    assert f'hx-get="/duplicates/groups?offset={2 * GROUP_PAGE_SIZE}"' in more.text
