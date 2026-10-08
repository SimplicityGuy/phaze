"""The junk review page: listing, approve / reject / undo / bulk per content group, and the token (phaze-l1j35).

Rows are seeded straight into ``companion_junk_review`` (and ``files`` / ``file_companions`` where a
card reads them), as the detector would have written them; every decision then goes through the real
router and the real :func:`phaze.services.companion_junk_review.decide_content_group` against real
Postgres. Only the quarantine enqueue -- phaze-lwuf6's ``enqueue_quarantine``, which talks to agents --
is replaced, at its module attribute, so these tests can see exactly when and with what it is called.
"""

from __future__ import annotations

import base64
import html
import json
import re
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.junk_review import JunkReviewStatus
from phaze.models.agent import Agent
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services import junk_quarantine
from phaze.services.junk_review_page import EXCERPT_CHARS, EXCERPT_LINES, bound_excerpt, decode_group_token, encode_group_token


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT = "test-fileserver"
_OTHER = "other-fileserver"
HASH_STAMP = "a" * 64
HASH_EMPTY = "b" * 64
HASH_DUP = "c" * 64

_TOKEN_RE = re.compile(r'name="review_tokens" value="([^"]+)"')


class _Quarantine:
    """Stands in for ``junk_quarantine.enqueue_quarantine``; records every call and the statuses it saw."""

    def __init__(self, session: AsyncSession, *, fail: bool = False) -> None:
        self.session = session
        self.fail = fail
        self.calls: list[list[uuid.UUID]] = []
        self.statuses_seen: list[set[str]] = []
        self.task_routers: list[Any] = []
        self.dispatch: int | None = None
        self.take = False

    async def __call__(self, session: AsyncSession, review_ids: Any, *, task_router: Any = None) -> int:
        ids = list(review_ids)
        self.task_routers.append(task_router)
        self.calls.append(ids)
        rows = (await session.execute(select(CompanionJunkReview.status).where(CompanionJunkReview.id.in_(ids)))).scalars()
        self.statuses_seen.append(set(rows))
        if self.fail:
            msg = "agent unreachable"
            raise RuntimeError(msg)
        if self.take:  # what the real call does first: approved -> executing, committed
            for row in (await session.execute(select(CompanionJunkReview).where(CompanionJunkReview.id.in_(ids)))).scalars():
                row.status = JunkReviewStatus.EXECUTING.value
            await session.commit()
        return len(ids) if self.dispatch is None else self.dispatch


@pytest.fixture
def quarantine(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> _Quarantine:
    fake = _Quarantine(session)
    monkeypatch.setattr(junk_quarantine, "enqueue_quarantine", fake)
    return fake


async def _file(session: AsyncSession, path: str, sha: str, *, agent_id: str = _AGENT, file_type: str = "nfo", size: int = 120) -> FileRecord:
    if await session.get(Agent, agent_id) is None:
        session.add(Agent(id=agent_id, name=agent_id, token_hash=uuid.uuid4().hex * 2, scan_roots=["/music"]))
    record = FileRecord(
        agent_id=agent_id,
        sha256_hash=sha,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type=file_type,
        file_size=size,
    )
    session.add(record)
    await session.flush()
    return record


async def _review(
    session: AsyncSession,
    path: str,
    sha: str,
    *,
    reason: str = "known_stamp",
    agent_id: str = _AGENT,
    size: int = 120,
    file_type: str = "nfo",
    with_file: bool = True,
) -> CompanionJunkReview:
    file_id = (await _file(session, path, sha, agent_id=agent_id, file_type=file_type, size=size)).id if with_file else None
    row = CompanionJunkReview(
        agent_id=agent_id,
        original_path=path,
        sha256_hash=sha,
        file_id=file_id,
        file_type=file_type,
        file_size=size,
        reason=reason,
        content_group=sha,
    )
    session.add(row)
    await session.flush()
    return row


async def _statuses(session: AsyncSession, sha: str) -> list[str]:
    statement = select(CompanionJunkReview.status).where(CompanionJunkReview.sha256_hash == sha).order_by(CompanionJunkReview.original_path)
    return list((await session.execute(statement.execution_options(populate_existing=True))).scalars())


async def _token(client: AsyncClient, sha: str) -> str:
    """The review token the live page renders for ``sha`` -- never one built by hand."""
    page = await client.get("/s/junk", headers={"HX-Request": "true", "HX-Target": "stage-workspace"})
    assert page.status_code == 200
    for token in _TOKEN_RE.findall(page.text):
        token = html.unescape(token)
        if decode_group_token(token)[0] == sha:
            return token
    raise AssertionError(f"no card rendered for {sha}")


async def _seed_three_groups(session: AsyncSession) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    await _review(session, "/music/set-b/info.nfo", HASH_STAMP)
    await _review(session, "/music/set-c/info.nfo", HASH_STAMP, agent_id=_OTHER)
    await _review(session, "/music/set-d/empty.txt", HASH_EMPTY, reason="empty", size=0, file_type="txt")
    await _review(session, "/dump/copy.cue", HASH_DUP, reason="duplicate", file_type="cue", size=900)


# Listing.


async def test_the_page_lists_pending_groups_with_signature_copies_size_and_links(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_three_groups(session)
    # The duplicate's linked copy: same content, linked to a recording, NOT under review.
    kept = await _file(session, "/music/set-e/tracks.cue", HASH_DUP, file_type="cue", size=900)
    media = await _file(session, "/music/set-e/set.mp3", "d" * 64, file_type="mp3", size=10_000)
    session.add(FileCompanion(companion_id=kept.id, media_id=media.id))
    session.add(
        CompanionContentFeatures(
            file_id=kept.id,
            agent_id=_AGENT,
            fingerprint=HASH_DUP,
            encoding="utf-8",
            byte_size=900,
            media_references=[{"name": "set.mp3", "source": "cue_file"}],
            reference_count=1,
            is_tracklist=True,
            extractor_version=1,
        )
    )
    await session.flush()

    response = await client.get("/s/junk")

    assert response.status_code == 200
    body = response.text
    for sha in (HASH_STAMP, HASH_EMPTY, HASH_DUP):
        assert f'id="junk-group-{sha}"' in body
    assert "Download-site stamp" in body
    assert "Empty file" in body
    assert "Duplicate of a linked companion" in body
    assert "3 pending" in body  # copy count, hash-wide across both agents
    assert "on 2 agents" in body
    assert "1 linked copy kept elsewhere" in body  # link state
    assert "120 Bytes each" in body  # size
    assert "/music/set-c/info.nfo" in body
    # Most copies first.
    assert body.index(f"junk-group-{HASH_STAMP}") < body.index(f"junk-group-{HASH_EMPTY}")


async def test_the_page_escapes_file_derived_text(client: AsyncClient, session: AsyncSession) -> None:
    await _review(session, "/music/<script>alert(1)</script>/info.nfo", HASH_STAMP)
    response = await client.get("/s/junk")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


async def test_decided_groups_leave_the_listing_and_an_empty_queue_says_so(client: AsyncClient, session: AsyncSession) -> None:
    row = await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    row.status, row.decided_at = JunkReviewStatus.REJECTED.value, row.created_at
    await session.flush()
    response = await client.get("/s/junk")
    assert f"junk-group-{HASH_STAMP}" not in response.text
    assert "No junk to review" in response.text


async def test_rendering_the_page_queues_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    """Nothing is quarantined without approval AND the run step: a visit, a page change, an excerpt queue nothing."""
    await _seed_three_groups(session)
    await client.get("/s/junk")
    await client.get("/s/junk?page=2")
    await client.get(f"/junk-review/{HASH_EMPTY}/excerpt")
    assert quarantine.calls == []
    assert set(await _statuses(session, HASH_STAMP)) == {"pending"}


async def test_the_page_is_paged_and_a_nonsense_page_is_clamped(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.routers.shell import stage_context

    monkeypatch.setattr(stage_context, "JUNK_GROUP_PAGE_SIZE", 2)
    await _seed_three_groups(session)
    first = await client.get("/s/junk")
    assert first.text.count("data-junk-group ") == 2
    assert 'href="/s/junk?page=2"' in first.text
    second = await client.get("/s/junk?page=2")
    assert second.text.count("data-junk-group ") == 1
    assert f"junk-group-{HASH_DUP}" in second.text
    assert 'href="/s/junk?page=1"' in second.text
    nonsense = await client.get("/s/junk?page=banana")
    assert nonsense.status_code == 200
    assert nonsense.text.count("data-junk-group ") == 2


# Approve, then run (the operator's 2026-10-07 amendment on phaze-l1j35: "Approve, then a separate Run step").


def _card_vals(response_text: str) -> dict[str, str]:
    """The review token the card in a decision response carries -- what its next button posts."""
    match = re.search(r"hx-vals='([^']+)'", response_text)
    assert match is not None, "the response rendered no card actions"
    return json.loads(html.unescape(match.group(1)))


async def _approve(client: AsyncClient, sha: str) -> Any:
    response = await client.post(f"/junk-review/{sha}/approve", data={"review_token": await _token(client, sha)})
    assert response.status_code == 200
    return response


async def test_approve_marks_the_group_approved_and_queues_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)

    response = await _approve(client, HASH_STAMP)

    assert await _statuses(session, HASH_STAMP) == ["approved"] * 3
    assert quarantine.calls == [], "approval alone must never queue a quarantine"
    assert "Approved 3 copies. Nothing moves until you run the quarantine; until then this can be undone." in response.text
    assert 'hx-swap-oob="innerHTML:#junk-review-status"' in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/quarantine"' in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' in response.text
    assert await _statuses(session, HASH_EMPTY) == ["pending"]
    assert await _statuses(session, HASH_DUP) == ["pending"]


async def test_an_approved_group_stays_on_the_page_until_it_is_run(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    await _approve(client, HASH_STAMP)
    page = await client.get("/s/junk")
    assert f'id="junk-group-{HASH_STAMP}"' in page.text
    assert "1 approved, not run" in page.text
    assert quarantine.calls == []


async def test_approval_records_a_time_and_no_actor(client: AsyncClient, session: AsyncSession) -> None:
    """Operator decision 7 of 2026-10-07, "Timestamp only" (epic phaze-4x319): a time, nobody's name."""
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    await _approve(client, HASH_STAMP)
    row = (await session.execute(select(CompanionJunkReview).execution_options(populate_existing=True))).scalar_one()
    assert row.decided_at is not None


async def test_approve_then_undo_returns_the_group_to_pending(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    approved = await _approve(client, HASH_STAMP)

    response = await client.post(f"/junk-review/{HASH_STAMP}/undo", data=_card_vals(approved.text))

    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["pending"]
    assert "Undone: 1 copy back to pending review." in response.text
    assert quarantine.calls == []


async def test_running_the_quarantine_dispatches_only_approved_copies(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    approved = await _approve(client, HASH_STAMP)
    approved_ids = set((await session.execute(select(CompanionJunkReview.id).where(CompanionJunkReview.sha256_hash == HASH_STAMP))).scalars())

    response = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data=_card_vals(approved.text))

    assert response.status_code == 200
    assert len(quarantine.calls) == 1
    assert set(quarantine.calls[0]) == approved_ids
    assert quarantine.statuses_seen == [{"approved"}], "only rows whose approval already stands are handed over"
    assert quarantine.task_routers == [client._transport.app.state.task_router]  # type: ignore[attr-defined]
    assert "Quarantine queued for 3 copies of 3 copies approved." in response.text
    assert await _statuses(session, HASH_EMPTY) == ["pending"], "a run touches only its own group"


async def test_approve_then_run_after_which_undo_is_refused(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    quarantine.take = True
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    approved = await _approve(client, HASH_STAMP)

    ran = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data=_card_vals(approved.text))

    assert ran.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["executing"]
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' not in ran.text, "a group handed to quarantine must not offer Undo"
    assert "This can no longer be undone." in ran.text

    undo = await client.post(f"/junk-review/{HASH_STAMP}/undo", data={"review_token": await _fresh_token(session)})
    assert undo.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["executing"]
    assert "Nothing to undo: this group was already handed to quarantine, which cannot be undone." in undo.text


async def _fresh_token(session: AsyncSession) -> str:
    from phaze.services.junk_review_page import load_groups

    return (await load_groups(session, [HASH_STAMP]))[HASH_STAMP].token


async def test_running_a_group_with_nothing_approved_queues_nothing_and_says_why(
    client: AsyncClient, session: AsyncSession, quarantine: _Quarantine
) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    response = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data={"review_token": await _token(client, HASH_STAMP)})
    assert response.status_code == 200
    assert quarantine.calls == []
    assert await _statuses(session, HASH_STAMP) == ["pending"]
    assert "Nothing was quarantined: no copy is approved. Approve the group first, then run the quarantine." in response.text


async def test_a_failed_quarantine_run_keeps_the_approval_and_says_so(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    quarantine.fail = True
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    approved = await _approve(client, HASH_STAMP)
    response = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data=_card_vals(approved.text))
    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["approved"]
    assert "It could not be queued (agent unreachable): those copies stay approved and can be run again or undone." in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' in response.text


async def test_a_partial_dispatch_is_reported_not_rounded_up(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    quarantine.dispatch = 1
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    await _review(session, "/music/set-b/info.nfo", HASH_STAMP)
    approved = await _approve(client, HASH_STAMP)
    response = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data=_card_vals(approved.text))
    assert "Quarantine queued for 1 copy of 2 copies approved. 1 copy could not be handed to an agent: unsent copies stay approved" in response.text


async def test_approve_withdraws_a_duplicate_that_no_longer_qualifies(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    """The service re-judges pending duplicates on the links stored now; the page reuses it, it does not bypass it."""
    await _review(session, "/dump/copy.cue", HASH_DUP, reason="duplicate", file_type="cue")
    response = await _approve(client, HASH_DUP)
    # No fresh features and no linked copy: the row is no longer a candidate, so it is withdrawn, not approved.
    assert await _statuses(session, HASH_DUP) == []
    assert quarantine.calls == []
    assert "Nothing was pending, so nothing was approved." in response.text


# Reject.


async def test_reject_covers_every_identical_copy_on_every_agent(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    """Operator decision 5 of 2026-10-07, "Every identical copy" (epic phaze-4x319)."""
    await _seed_three_groups(session)
    # A copy approved earlier and not yet run is covered by the rejection too.
    approved = await _review(session, "/music/set-z/info.nfo", HASH_STAMP)
    approved.status, approved.decided_at = JunkReviewStatus.APPROVED.value, approved.created_at
    await session.flush()

    response = await client.post(f"/junk-review/{HASH_STAMP}/reject", data={"review_token": await _token(client, HASH_STAMP)})

    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["rejected"] * 4
    assert quarantine.calls == []
    assert "Rejected 4 copies" in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' in response.text
    assert await _statuses(session, HASH_EMPTY) == ["pending"]


# Undo.


async def test_undo_returns_a_rejected_group_to_pending(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    rejected = await client.post(f"/junk-review/{HASH_STAMP}/reject", data={"review_token": await _token(client, HASH_STAMP)})

    response = await client.post(f"/junk-review/{HASH_STAMP}/undo", data=_card_vals(rejected.text))

    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 3
    rows = (await session.execute(select(CompanionJunkReview.decided_at).where(CompanionJunkReview.sha256_hash == HASH_STAMP))).scalars()
    assert set(rows) == {None}
    assert "Undone: 3 copies back to pending review." in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/approve"' in response.text
    assert quarantine.calls == []


async def test_undo_cannot_reach_a_group_already_handed_to_quarantine(client: AsyncClient, session: AsyncSession) -> None:
    row = await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    row.status, row.decided_at = JunkReviewStatus.EXECUTING.value, row.created_at
    await session.flush()

    response = await client.post(f"/junk-review/{HASH_STAMP}/undo", data={"review_token": await _fresh_token(session)})
    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["executing"]
    assert "already handed to quarantine" in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' not in response.text


# The token.


async def test_a_stale_token_is_a_conflict_and_changes_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    token = await _token(client, HASH_STAMP)
    # A new copy of the same content is detected after the operator loaded the page.
    await _review(session, "/music/set-new/info.nfo", HASH_STAMP)

    response = await client.post(f"/junk-review/{HASH_STAMP}/approve", data={"review_token": token})

    assert response.status_code == 409
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 4
    assert quarantine.calls == []
    assert "changed after you reviewed it, so nothing was applied" in response.text
    assert "4 pending" in response.text  # the refreshed card shows what is really there
    assert "data-conflict-swap" in response.text


async def test_a_decision_from_another_tab_makes_the_first_tabs_token_stale(
    client: AsyncClient, session: AsyncSession, quarantine: _Quarantine
) -> None:
    await _seed_three_groups(session)
    token = await _token(client, HASH_STAMP)
    assert (await client.post(f"/junk-review/{HASH_STAMP}/reject", data={"review_token": token})).status_code == 200
    response = await client.post(f"/junk-review/{HASH_STAMP}/approve", data={"review_token": token})
    assert response.status_code == 409
    assert await _statuses(session, HASH_STAMP) == ["rejected"] * 3
    assert quarantine.calls == []


@pytest.mark.parametrize(
    "review_token",
    [
        "not base64 at all!!",
        base64.urlsafe_b64encode(b"[1, 2]").decode(),
        base64.urlsafe_b64encode(b'{"sha256_hash": "zz", "state": "x"}').decode(),
        encode_group_token(HASH_EMPTY, "0" * 64),  # a real-shaped token for ANOTHER group
    ],
)
async def test_a_malformed_or_foreign_token_is_refused(client: AsyncClient, session: AsyncSession, review_token: str) -> None:
    await _seed_three_groups(session)
    response = await client.post(f"/junk-review/{HASH_STAMP}/approve", data={"review_token": review_token})
    assert response.status_code == 422
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 3


async def test_the_group_and_action_come_from_the_route_and_are_validated(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_three_groups(session)
    token = await _token(client, HASH_STAMP)
    assert (await client.post("/junk-review/not-a-hash/approve", data={"review_token": token})).status_code == 422
    assert (await client.post(f"/junk-review/{HASH_STAMP}/delete", data={"review_token": token})).status_code == 422
    assert (await client.get(f"/junk-review/{HASH_STAMP}/approve")).status_code == 405
    # A status smuggled into the form is ignored: the route fixes the target.
    response = await client.post(f"/junk-review/{HASH_STAMP}/reject", data={"review_token": token, "status": "quarantined"})
    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["rejected"] * 3


# Bulk.


async def test_bulk_approve_decides_every_selected_group_and_queues_nothing(
    client: AsyncClient, session: AsyncSession, quarantine: _Quarantine
) -> None:
    await _seed_three_groups(session)
    tokens = [await _token(client, HASH_STAMP), await _token(client, HASH_EMPTY)]

    response = await client.post("/junk-review/bulk", data={"action": "approve", "review_tokens": tokens})

    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["approved"] * 3
    assert await _statuses(session, HASH_EMPTY) == ["approved"]
    assert await _statuses(session, HASH_DUP) == ["pending"], "an unselected group is untouched"
    assert quarantine.calls == []
    assert "Approved 4 copies across 2 groups." in response.text
    assert response.text.count('hx-swap-oob="true"') == 2


async def test_bulk_run_quarantines_the_approved_copies_of_every_selected_group(
    client: AsyncClient, session: AsyncSession, quarantine: _Quarantine
) -> None:
    await _seed_three_groups(session)
    approve_tokens = [await _token(client, HASH_STAMP), await _token(client, HASH_EMPTY)]
    assert (await client.post("/junk-review/bulk", data={"action": "approve", "review_tokens": approve_tokens})).status_code == 200
    run_tokens = [await _token(client, HASH_STAMP), await _token(client, HASH_EMPTY), await _token(client, HASH_DUP)]

    response = await client.post("/junk-review/bulk", data={"action": "quarantine", "review_tokens": run_tokens})

    assert response.status_code == 200
    assert len(quarantine.calls) == 2, "the selected group with nothing approved is not run"
    assert all(seen == {"approved"} for seen in quarantine.statuses_seen)
    assert "Quarantine queued for 4 copies of 4 copies approved." in response.text
    assert await _statuses(session, HASH_DUP) == ["pending"]


async def test_bulk_reject_is_hash_wide_and_queues_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    response = await client.post("/junk-review/bulk", data={"action": "reject", "review_tokens": [await _token(client, HASH_STAMP)]})
    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["rejected"] * 3
    assert quarantine.calls == []


async def test_bulk_with_one_stale_token_applies_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    tokens = [await _token(client, HASH_STAMP), await _token(client, HASH_EMPTY)]
    await _review(session, "/music/set-new/empty.txt", HASH_EMPTY, reason="empty", size=0, file_type="txt")

    response = await client.post("/junk-review/bulk", data={"action": "approve", "review_tokens": tokens})

    assert response.status_code == 409
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 3
    assert await _statuses(session, HASH_EMPTY) == ["pending"] * 2
    assert quarantine.calls == []
    assert "nothing was applied" in response.text


async def test_bulk_with_nothing_selected_does_nothing(client: AsyncClient, session: AsyncSession, quarantine: _Quarantine) -> None:
    await _seed_three_groups(session)
    response = await client.post("/junk-review/bulk", data={"action": "approve"})
    assert response.status_code == 200
    assert "Select at least one group first." in response.text
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 3
    assert quarantine.calls == []


async def test_bulk_refuses_an_unknown_action_and_an_oversized_selection(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_three_groups(session)
    token = await _token(client, HASH_STAMP)
    assert (await client.post("/junk-review/bulk", data={"action": "undo", "review_tokens": [token]})).status_code == 422
    assert (await client.post("/junk-review/bulk", data={"action": "approve", "review_tokens": ["x" * 513]})).status_code == 422
    assert (await client.post("/junk-review/bulk", data={"action": "approve", "review_tokens": [token] * 51})).status_code == 422
    assert await _statuses(session, HASH_STAMP) == ["pending"] * 3


# The excerpt.


async def test_the_excerpt_of_an_empty_file_is_described_without_a_read(client: AsyncClient, session: AsyncSession) -> None:
    await _review(session, "/music/set-d/empty.txt", HASH_EMPTY, reason="empty", size=0, file_type="txt")
    response = await client.get(f"/junk-review/{HASH_EMPTY}/excerpt")
    assert response.status_code == 200
    assert "The file is empty (0 bytes)" in response.text


async def test_the_excerpt_is_a_bounded_escaped_read_on_the_owning_agent(client: AsyncClient, session: AsyncSession) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def apply(task: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((task, kwargs))
        return {"contents": [{"filename": "info.nfo", "content": "<script>alert(1)</script>\n" + "x" * 5000}]}

    queue = client._transport.app.state.task_router.queue_for(_AGENT, "meta")  # type: ignore[attr-defined]
    router = client._transport.app.state.task_router  # type: ignore[attr-defined]
    router.queue_for = lambda agent_id, lane=None: queue  # noqa: ARG005
    queue.apply = apply

    response = await client.get(f"/junk-review/{HASH_STAMP}/excerpt")

    assert response.status_code == 200
    assert calls and calls[0][0] == "read_companion_files"
    assert calls[0][1]["max_chars"] == EXCERPT_CHARS
    assert calls[0][1]["companions"] == [{"filename": "info.nfo", "path": "/music/set-a/info.nfo"}]
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "x" * (EXCERPT_CHARS + 1) not in response.text
    assert "The file continues past this point." in response.text


async def test_an_unreachable_agent_degrades_the_excerpt_to_a_note(client: AsyncClient, session: AsyncSession) -> None:
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)

    async def apply(task: str, **kwargs: Any) -> dict[str, Any]:
        msg = "timeout"
        raise TimeoutError(msg)

    queue = client._transport.app.state.task_router.queue_for(_AGENT, "meta")  # type: ignore[attr-defined]
    client._transport.app.state.task_router.queue_for = lambda agent_id, lane=None: queue  # type: ignore[attr-defined]  # noqa: ARG005
    queue.apply = apply
    response = await client.get(f"/junk-review/{HASH_STAMP}/excerpt")
    assert response.status_code == 200
    assert "did not return an excerpt" in response.text


async def test_no_live_copy_and_no_reachable_copy_are_said_plainly(client: AsyncClient, session: AsyncSession) -> None:
    assert "No live copy" in (await client.get(f"/junk-review/{HASH_STAMP}/excerpt")).text
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP, with_file=False)
    assert "No copy of this content is reachable" in (await client.get(f"/junk-review/{HASH_STAMP}/excerpt")).text


def test_bound_excerpt_caps_lines_and_characters() -> None:
    many_lines = "\n".join(f"line {n}" for n in range(EXCERPT_LINES * 3))
    text, cut = bound_excerpt(many_lines)
    assert cut
    assert len(text.splitlines()) == EXCERPT_LINES
    text, cut = bound_excerpt("y" * (EXCERPT_CHARS * 3))
    assert cut
    assert len(text) == EXCERPT_CHARS
    assert bound_excerpt("short\x00text") == ("shorttext", False)


# The real run step, end to end.


async def test_the_run_step_dispatches_through_the_real_enqueue_quarantine(client: AsyncClient, session: AsyncSession) -> None:
    """No stand-in: phaze-lwuf6's enqueue_quarantine claims the approved rows and enqueues one job per copy on its agent."""
    await _review(session, "/music/set-a/info.nfo", HASH_STAMP)
    await _review(session, "/music/set-b/info.nfo", HASH_STAMP)
    approved = await _approve(client, HASH_STAMP)
    task_router = client._transport.app.state.task_router  # type: ignore[attr-defined]

    response = await client.post(f"/junk-review/{HASH_STAMP}/quarantine", data=_card_vals(approved.text))

    assert response.status_code == 200
    assert await _statuses(session, HASH_STAMP) == ["executing", "executing"]
    tasks = [task for _queue, task, _kwargs in task_router.captures]
    assert tasks == ["quarantine_companion", "quarantine_companion"]
    assert "Quarantine queued for 2 copies of 2 copies approved." in response.text
    assert f'hx-post="/junk-review/{HASH_STAMP}/undo"' not in response.text
