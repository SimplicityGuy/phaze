"""phaze-x1qr3.13: the poster -- one printable, self-contained SVG per set.

Every acceptance criterion is discharged here, against the real ``GET /files/{id}/poster.svg``
route rather than against the template's source: ``image/svg+xml`` content type, well-formed XML
with no ``<script>`` and no external ``href``/``url(`` reference (asserted on the PARSED tree, not
a substring search), Jinja-escaped artist/event text, an honest stated-in-text answer for a
fine-only file and for a file with no tracklist, and the record page's download link gated on a
``SetProfile`` existing.

Consumes the DB fixtures, so ``conftest.py`` auto-marks this module ``integration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
from defusedxml import ElementTree as DefusedET
import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.models.metadata import FileMetadata
from phaze.models.set_profile import SetProfile
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord

SVG_NS = "{http://www.w3.org/2000/svg}"


def _local_name(tag: str) -> str:
    return tag.removeprefix(SVG_NS) if tag.startswith(SVG_NS) else tag


async def _seed_poster_windows(session: AsyncSession, file: FileRecord) -> None:
    """Three fine windows (bpm + camelot) and two coarse windows (energy + mood/style)."""
    windows = [
        AnalysisWindow(id=uuid.uuid4(), file_id=file.id, tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, bpm=128.0, camelot="8A"),
        AnalysisWindow(id=uuid.uuid4(), file_id=file.id, tier="fine", window_index=1, start_sec=30.0, end_sec=60.0, bpm=130.0, camelot="8A"),
        AnalysisWindow(id=uuid.uuid4(), file_id=file.id, tier="fine", window_index=2, start_sec=60.0, end_sec=90.0, bpm=132.0, camelot="9A"),
        AnalysisWindow(
            id=uuid.uuid4(), file_id=file.id, tier="coarse", window_index=0, start_sec=0.0, end_sec=45.0, energy=0.3, mood="energetic", style="techno"
        ),
        AnalysisWindow(
            id=uuid.uuid4(), file_id=file.id, tier="coarse", window_index=1, start_sec=45.0, end_sec=90.0, energy=0.9, mood="dark", style="techno"
        ),
    ]
    session.add_all(windows)
    await session.commit()


async def _seed_tracklist(session: AsyncSession, file: FileRecord, *, artist: str | None, event: str | None = None) -> Tracklist:
    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"ext-{uuid.uuid4().hex[:12]}",
        source_url="https://example.test/tracklist",
        file_id=file.id,
        status="approved",
        artist=artist,
        event=event,
    )
    session.add(tracklist)
    await session.commit()
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.commit()
    session.add_all(
        [
            TracklistTrack(id=uuid.uuid4(), version_id=version.id, position=1, title="Opener", timestamp="00:00:00"),
            TracklistTrack(id=uuid.uuid4(), version_id=version.id, position=2, title="Closer", timestamp="00:00:45"),
        ]
    )
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file.id, duration=90.0))
    tracklist.latest_version_id = version.id
    await session.commit()
    return tracklist


def _parse_svg(body: str):  # type: ignore[no-untyped-def]
    return DefusedET.fromstring(body)


@pytest.mark.asyncio
async def test_the_poster_is_svg_and_well_formed_xml_with_no_script_or_external_reference(  # type: ignore[no-untyped-def]
    client: AsyncClient, session: AsyncSession, seed_file_with_windows, make_file
) -> None:
    """Acceptance 1: ``image/svg+xml``, well-formed XML, no ``<script>``, no external ``href``/``url(``."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")
    await _seed_poster_windows(session, file)
    session.add(SetProfile(file_id=file.id))
    await session.commit()

    response = await client.get(f"/files/{file.id}/poster.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    root = _parse_svg(response.text)
    assert _local_name(root.tag) == "svg"

    for element in root.iter():
        assert _local_name(element.tag) != "script", "no <script> anywhere in the poster"
        for key, value in element.attrib.items():
            assert not key.endswith("href"), f"no href reference ({key!r} on <{_local_name(element.tag)}>)"
            assert "url(" not in value, f"no url() reference (attribute {key!r}={value!r})"


@pytest.mark.asyncio
async def test_the_title_line_is_jinja_escaped(  # type: ignore[no-untyped-def]
    client: AsyncClient, session: AsyncSession, seed_file_with_windows
) -> None:
    """Acceptance 1: an artist/event containing ``<`` renders as ``&lt;``, never raw markup."""
    file, _result, _windows = await seed_file_with_windows(original_filename="dangerous.mp3")
    await _seed_tracklist(session, file, artist="A < B", event="Warehouse & Co")

    response = await client.get(f"/files/{file.id}/poster.svg")

    assert response.status_code == 200
    assert "A < B" not in response.text, "the raw artist text must not appear unescaped"
    assert "A &lt; B" in response.text
    assert "Warehouse &amp; Co" in response.text
    # And the document still parses -- an unescaped "<" would have broken this.
    _parse_svg(response.text)


@pytest.mark.asyncio
async def test_a_fine_only_file_states_the_missing_energy_section_in_text(  # type: ignore[no-untyped-def]
    client: AsyncClient, seed_file_with_windows
) -> None:
    """Acceptance 2: no coarse windows -> the energy section is a stated sentence, not blank."""
    file, _result, _windows = await seed_file_with_windows(original_filename="fine-only.mp3", coarse_count=0)

    response = await client.get(f"/files/{file.id}/poster.svg")

    assert response.status_code == 200
    assert "No coarse windows analyzed for this file." in response.text
    _parse_svg(response.text)


@pytest.mark.asyncio
async def test_a_file_with_no_tracklist_states_the_missing_tracklist_section_in_text(  # type: ignore[no-untyped-def]
    client: AsyncClient, session: AsyncSession, seed_file_with_windows
) -> None:
    """Acceptance 2: no tracklist -> the tracklist section is a stated sentence, not blank."""
    file, _result, _windows = await seed_file_with_windows(original_filename="no-tracklist.mp3")
    await _seed_poster_windows(session, file)

    response = await client.get(f"/files/{file.id}/poster.svg")

    assert response.status_code == 200
    assert "No tracklist available for this file." in response.text
    _parse_svg(response.text)


@pytest.mark.asyncio
async def test_a_missing_file_id_is_a_404(client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    response = await client.get(f"/files/{uuid.uuid4()}/poster.svg")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_record_page_shows_the_download_link_only_when_a_profile_exists(  # type: ignore[no-untyped-def]
    client: AsyncClient, session: AsyncSession, seed_file_with_windows
) -> None:
    """Acceptance 3: the poster's download link is gated on a ``SetProfile`` existing."""
    file, _result, _windows = await seed_file_with_windows(original_filename="<set-01>.mp3")

    without_profile = BeautifulSoup((await client.get(f"/files/{file.id}")).text, "html.parser")
    assert without_profile.select_one("[data-poster-download]") is None

    session.add(SetProfile(file_id=file.id))
    await session.commit()

    with_profile = BeautifulSoup((await client.get(f"/files/{file.id}")).text, "html.parser")
    link = with_profile.select_one("[data-poster-download]")
    assert link is not None
    assert link["href"] == f"/files/{file.id}/poster.svg"
