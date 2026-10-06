"""phaze-bx7wf: the /s/files table sits in the same inner framed card as the stage pages' Eligible files.

Before this bead the matrix ran edge to edge in the workspace with no frame, while the Metadata stage
wrapped its paged table in an inset, rounded, bordered card. Both now compose ONE shared class string
(``ui.table_card_class`` in ``ui/primitives.html``), so border, radius and background -- and the dark
theme's counterparts -- cannot drift between the two. The pager lives INSIDE the card with the same
``px-6`` / ``pb-4`` inset the stage-page pagers carry (phaze-rkikk), so Next is no longer flush with
the bottom edge.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration

_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_CARD_CLASSES = ("rounded-lg", "border", "border-gray-200", "bg-white", "dark:border-phaze-border", "dark:bg-phaze-panel")


def _shared_card_class() -> str:
    match = re.search(r'set table_card_class = "([^"]+)"', (_TEMPLATES / "ui" / "primitives.html").read_text())
    assert match, "ui/primitives.html no longer exports table_card_class"
    return match.group(1)


def _file(i: int) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/card-{i}-{uid.hex}.mp3",
        original_filename=f"card-{i}-{uid.hex}.mp3",
        current_path=f"/music/card-{i}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _card_open_tags(body: str) -> list[str]:
    return [m for m in re.findall(r'<div class="([^"]*)"', body) if all(token in m.split() for token in _CARD_CLASSES)]


class _Descendants(HTMLParser):
    def __init__(self, card_class: str) -> None:
        super().__init__()
        self.card_class = card_class
        self.depth = 0  # >0 while inside the card
        self.seen: set[str] = set()
        self._stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"input", "br", "col", "img", "meta", "link", "hr"}:
            return
        is_card = tag == "div" and dict(attrs).get("class") == self.card_class and self.depth == 0
        if self.depth:
            self.seen.add(tag)
        if is_card or self.depth:
            self.depth += 1
        self._stack.append(is_card or self.depth > 0)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"input", "br", "col", "img", "meta", "link", "hr"} or not self._stack:
            return
        if self._stack.pop():
            self.depth -= 1


def _descendants_of_card(body: str, card_class: str) -> set[str]:
    parser = _Descendants(card_class)
    parser.feed(body)
    return parser.seen


def test_the_shared_card_class_carries_the_stage_page_frame() -> None:
    tokens = _shared_card_class().split()
    for token in _CARD_CLASSES:
        assert token in tokens


@pytest.mark.asyncio
async def test_files_table_and_pager_render_inside_the_shared_card(client: AsyncClient, session: AsyncSession) -> None:
    session.add_all([_file(i) for i in range(11)])
    await session.commit()

    resp = await client.get("/pipeline/files?page_size=10", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    cards = [c for c in _card_open_tags(body) if "mx-6" in c.split()]
    assert len(cards) == 1, "the Files matrix must render exactly one inner card, inset to the 24px page gutter"
    assert _shared_card_class() in cards[0]

    start = body.index(f'<div class="{cards[0]}"')
    table_pos = body.index("<table", start)
    nav_pos = body.index('<nav aria-label="Files pagination"', start)
    assert start < table_pos < nav_pos
    assert _descendants_of_card(body, cards[0]) >= {"table", "nav"}
    nav = re.search(r'<nav aria-label="Files pagination" class="([^"]*)"', body)
    assert nav
    tokens = nav.group(1).split()
    assert "px-6" in tokens and "pb-4" in tokens and "mt-6" not in tokens


@pytest.mark.asyncio
async def test_the_empty_files_state_sits_inside_the_card_too(client: AsyncClient) -> None:
    resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    cards = [c for c in _card_open_tags(resp.text) if "mx-6" in c.split()]
    assert len(cards) == 1
    assert resp.text.index("No files yet") > resp.text.index(f'<div class="{cards[0]}"')


@pytest.mark.asyncio
async def test_metadata_eligible_files_host_uses_the_same_card_class(client: AsyncClient) -> None:
    resp = await client.get("/s/metadata")
    assert resp.status_code == 200
    host = re.search(r'<div id="metadata-files-view"[^>]*class="([^"]*)"', resp.text, re.S)
    assert host
    assert _shared_card_class() in host.group(1)
