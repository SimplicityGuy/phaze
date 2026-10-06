"""Canonical full-page file details share the record drawer's scoped read contract."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest

from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _shared_text(body: str) -> str:
    page = BeautifulSoup(body, "html.parser")
    content = page.select_one("[data-record-content]")
    assert content is not None
    return content.get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_drawer_and_full_page_share_record_facts_and_states(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    session: AsyncSession,
    seed_file_with_windows,
) -> None:
    """Both presentations expose one builder/partial contract, not parallel implementations."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename="Artist - Event - Title (2024).mp3",
            proposed_path="/organized/Artist/Event/Artist - Event - Title (2024).mp3",
            confidence=0.94,
            status=ProposalStatus.PENDING.value,
        )
    )
    await session.commit()

    drawer = await client.get(f"/record/{file.id}", headers={"HX-Request": "true"})
    full_page = await client.get(f"/files/{file.id}")

    assert drawer.status_code == full_page.status_code == 200
    assert "<html" not in drawer.text
    assert "<html" in full_page.text
    assert _shared_text(drawer.text) == _shared_text(full_page.text)
    assert f'href="/files/{file.id}"' in drawer.text
    assert "Open full page" in drawer.text

    drawer_page = BeautifulSoup(drawer.text, "html.parser")
    canonical_page = BeautifulSoup(full_page.text, "html.parser")
    for anchor in ("analysis", "tracklist", "history"):
        assert drawer_page.select_one(f"#{anchor}") is not None
        assert canonical_page.select_one(f"#{anchor}") is not None
        assert canonical_page.select_one(f'a[href="#{anchor}"]') is not None

    # The full document's Changes Review link must remain useful without htmx; only the drawer
    # attaches shell-specific swap wiring to this otherwise ordinary href.
    drawer_review_link = drawer_page.select_one('a[href="/s/rename?status=needs_review"]')
    page_review_link = canonical_page.select_one('a[href="/s/rename?status=needs_review"]')
    assert drawer_review_link is not None and drawer_review_link.get("hx-target") == "#stage-workspace"
    assert page_review_link is not None and page_review_link.get("hx-target") is None


@pytest.mark.asyncio
async def test_full_page_is_strictly_scoped_to_the_requested_file(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    seed_file_with_windows,
) -> None:
    """A page can never expose a different file's analysis or identity facts."""
    requested, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    other, _other_result, _other_windows = await seed_file_with_windows(original_filename="<set-02>.mp3")

    response = await client.get(f"/files/{requested.id}")
    page_text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)

    assert response.status_code == 200
    assert requested.original_filename in page_text
    assert requested.current_path in page_text
    assert other.original_filename not in page_text
    assert other.current_path not in page_text


@pytest.mark.asyncio
async def test_full_page_missing_and_malformed_ids_do_not_leak(client: AsyncClient) -> None:
    """Missing UUIDs get a friendly document; FastAPI rejects malformed UUIDs before any query."""
    missing = uuid.uuid4()

    absent = await client.get(f"/files/{missing}")
    malformed = await client.get("/files/not-a-uuid")

    assert absent.status_code == 404
    assert "text/html" in absent.headers.get("content-type", "")
    assert "<html" in absent.text
    assert "That file no longer exists" in absent.text
    assert str(missing) not in absent.text
    assert "Traceback" not in absent.text

    assert malformed.status_code == 422
    assert "Traceback" not in malformed.text
    assert "/test/music" not in malformed.text


def _assert_app_shell(body: str) -> BeautifulSoup:
    """The shell landmarks a deep link must not lose: the banner header, the Pipeline rail and the workspace."""
    page = BeautifulSoup(body, "html.parser")
    # The shell's own <header> is a direct child of <body> (a banner landmark); the record
    # article's inner <header> is scoped to the <article> and is not one.
    assert len(page.select("body > header")) == 1, "the shell header (banner) must be present"
    assert page.select_one('nav[aria-label="Pipeline stages"]') is not None, "the Pipeline navigation rail must be present"
    workspace = page.select_one("main #stage-workspace")
    assert workspace is not None, "the record must render inside the shell's workspace"
    assert page.select_one("#toast-container") is not None
    return page


@pytest.mark.asyncio
async def test_full_page_renders_inside_the_app_shell(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    seed_file_with_windows,
) -> None:
    """phaze-dnmew: /files/<id> keeps the banner and Pipeline navigation, with the record in the workspace."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")

    response = await client.get(f"/files/{file.id}")

    assert response.status_code == 200
    page = _assert_app_shell(response.text)
    workspace = page.select_one("#stage-workspace")
    assert workspace is not None
    assert workspace.select_one(f'article[data-file-id="{file.id}"]') is not None
    assert workspace.select_one("[data-record-content]") is not None
    assert file.original_filename in (page.title.get_text() if page.title else "")
    # The drawer host is part of the shell, so the page and the drawer share one chrome.
    assert page.select_one("#record-body") is not None


@pytest.mark.asyncio
async def test_full_page_not_found_also_renders_inside_the_app_shell(client: AsyncClient) -> None:
    """phaze-dnmew: the 404 keeps the shell too, so a dead deep link still offers a way out."""
    response = await client.get(f"/files/{uuid.uuid4()}")

    assert response.status_code == 404
    page = _assert_app_shell(response.text)
    not_found = page.select_one("#stage-workspace [data-record-not-found]")
    assert not_found is not None
    assert "That file no longer exists" in not_found.get_text()
