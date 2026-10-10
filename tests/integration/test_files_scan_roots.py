"""Files sections use the owner's configured roots while retaining full-path tooltips."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.agent import Agent
from tests.integration.test_files_resize import _make_file


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_files_group_by_scan_root_with_filename_only_rows(client: AsyncClient, session: AsyncSession) -> None:
    agent = await session.get(Agent, "test-fileserver")
    assert agent is not None
    agent.scan_roots = ["/music", "/music/live", "/other"]
    paths = ("/music/album/song.mp3", "/music/live/set.mp3", "/other/album/second.mp3")
    for path in paths:
        session.add(_make_file(path))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    assert body.count('scope="rowgroup"') == 3
    for root in ("/music", "/music/live", "/other"):
        assert f'class="font-mono">{root}</span>' in body
    for path in paths:
        assert f'title="{path}">{path.rsplit("/", 1)[-1]}' in body
        assert f">{path}<" not in body
