"""phaze-6bkk: the AGENT-side companion read -- containment, bounded read, off-loop I/O.

These cases complement ``tests/review/capabilities/proposal_context/test_service.py::TestLoadCompanionContents``
along with the code. The containment guard (phaze-eycl) and the bounded read (phaze-cycw) are
unchanged in substance; what changed is WHERE they run. The controller worker is fileless under
DIST-01, so ``Path.resolve()`` there normalizes lexically against a filesystem that does not hold
the archive -- it can neither follow a symlink nor prove one is absent. Only the agent can.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phaze.tasks.companion_read import read_companion_files


if TYPE_CHECKING:
    from pathlib import Path


def _agent_settings(scan_roots: list[str]) -> Any:
    """A spec'd AgentSettings stand-in (``isinstance`` gates the scan_roots read)."""
    from phaze.config import AgentSettings

    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


async def _run(companions: list[dict[str, str]], scan_roots: list[str], max_chars: int = 3000) -> list[dict[str, str]]:
    with patch("phaze.tasks.companion_read.get_settings", return_value=_agent_settings(scan_roots)):
        result = await read_companion_files({}, agent_id="fileserver-01", companions=companions, max_chars=max_chars)
    return result["contents"]


class TestReadCompanionFilesTask:
    @pytest.mark.asyncio
    async def test_reads_the_companion_text(self, tmp_path: Path) -> None:
        companion = tmp_path / "info.nfo"
        companion.write_text("Artist: DJ Test\nVenue: Club", encoding="utf-8")

        contents = await _run([{"filename": "info.nfo", "path": str(companion)}], [str(tmp_path)])

        assert len(contents) == 1
        assert contents[0]["filename"] == "info.nfo"
        assert "Artist: DJ Test" in contents[0]["content"]

    @pytest.mark.asyncio
    async def test_skips_unreadable_files(self, tmp_path: Path) -> None:
        """One unreadable sidecar is skipped -- never fatal to the batch."""
        contents = await _run([{"filename": "gone.nfo", "path": str(tmp_path / "gone.nfo")}], [str(tmp_path)])

        assert contents == []

    @pytest.mark.asyncio
    async def test_bounds_the_read_without_slurping_a_huge_file(self, tmp_path: Path) -> None:
        """phaze-cycw: the read is bounded AT THE SOURCE, not truncated after a whole-file slurp.

        Peak memory for a multi-hundred-MB mis-categorized companion (a log, an oversized .nfo, an
        image matched by a COMPANION extension) stays a small fixed multiple of ``max_chars``.
        """
        from phaze.services.companion_read import COMPANION_READ_CHAR_MARGIN

        huge = tmp_path / "huge.nfo"
        huge.write_text("x" * 1_000_000, encoding="utf-8")

        contents = await _run([{"filename": "huge.nfo", "path": str(huge)}], [str(tmp_path)], max_chars=100)

        assert len(contents[0]["content"]) == 100 * COMPANION_READ_CHAR_MARGIN

    @pytest.mark.asyncio
    async def test_refuses_absolute_path_outside_scan_roots(self, tmp_path: Path) -> None:
        """phaze-eycl regression: an agent-supplied path outside every scan_root is never opened.

        The ``/proc/self/environ`` shape from that bead -- readable, but out of bounds. Returning it
        would embed it in the LLM prompt and persist it to ``proposals.context_used``.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "environ"
        secret.write_text("POSTGRES_PASSWORD=<redacted>\nREDIS_URL=redis://redis:6379/0", encoding="utf-8")
        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        contents = await _run([{"filename": "environ", "path": str(secret)}], [str(archive_root)])

        assert contents == []

    @pytest.mark.asyncio
    async def test_refuses_traversal_escape_via_dotdot(self, tmp_path: Path) -> None:
        """phaze-eycl regression: a ``..``-laden path that RESOLVES outside the roots is refused.

        A naive prefix-string check on the raw (unresolved) string would see it as nested under the
        root -- which is exactly why the check resolves first.
        """
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("LLM_API_KEY=<redacted>", encoding="utf-8")

        contents = await _run([{"filename": "secret.txt", "path": str(archive_root / ".." / "secret.txt")}], [str(archive_root)])

        assert contents == []

    @pytest.mark.asyncio
    async def test_refuses_symlink_pointing_out_of_the_scan_root(self, tmp_path: Path) -> None:
        """The check the CONTROL plane could not honestly perform: a symlink planted INSIDE a root.

        The path string is genuinely under the scan root -- only the real filesystem reveals that it
        points elsewhere. This is why containment moved to the machine holding the mount.
        """
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("LLM_API_KEY=<redacted>", encoding="utf-8")
        link = archive_root / "notes.nfo"
        link.symlink_to(secret)

        contents = await _run([{"filename": "notes.nfo", "path": str(link)}], [str(archive_root)])

        assert contents == []

    @pytest.mark.asyncio
    async def test_no_scan_roots_refuses_everything(self, tmp_path: Path) -> None:
        """An empty root list matches nothing -- 'refuse', never 'allow'."""
        companion = tmp_path / "info.nfo"
        companion.write_text("Artist: DJ Test", encoding="utf-8")

        assert await _run([{"filename": "info.nfo", "path": str(companion)}], []) == []

    @pytest.mark.asyncio
    async def test_decodes_cp437_nfo_art_instead_of_utf8_replacement(self, tmp_path: Path) -> None:
        """phaze-j9b3z: a CP437-encoded .nfo decodes as CP437, not U+FFFD replacement chars.

        Box-drawing bytes (e.g. 0xC9 '╔') are not valid UTF-8 continuation sequences, so the
        strict-UTF-8 attempt fails and the .nfo-eligible CP437 fallback takes over.
        """
        border = "╔" + "═" * 20 + "╗"
        text = f"{border}\nGROUP: EXAMPLE\n{border}\n"
        companion = tmp_path / "release.nfo"
        companion.write_bytes(text.encode("cp437"))

        contents = await _run([{"filename": "release.nfo", "path": str(companion)}], [str(tmp_path)])

        assert "�" not in contents[0]["content"]
        assert contents[0]["content"] == text

    @pytest.mark.asyncio
    async def test_decodes_cp437_txt_art_instead_of_utf8_replacement(self, tmp_path: Path) -> None:
        """phaze-j9b3z: the same scene tooling emits .txt sidecars in the same code page."""
        border = "╔" + "═" * 20 + "╗"
        text = f"{border}\nGROUP: EXAMPLE\n{border}\n"
        companion = tmp_path / "release.txt"
        companion.write_bytes(text.encode("cp437"))

        contents = await _run([{"filename": "release.txt", "path": str(companion)}], [str(tmp_path)])

        assert "�" not in contents[0]["content"]
        assert contents[0]["content"] == text

    @pytest.mark.asyncio
    async def test_utf8_nfo_content_is_unchanged_by_the_cp437_fallback(self, tmp_path: Path) -> None:
        """phaze-j9b3z acceptance: a genuinely UTF-8 companion decodes exactly as before.

        Includes non-ASCII (accented) text so a wrong decode would visibly corrupt it -- strict
        UTF-8 must succeed first and the CP437 fallback must never run.
        """
        text = "Artist: Café Del Mar\nVenue: Über Club\nDate: 2024.05.15\n"
        companion = tmp_path / "info.nfo"
        companion.write_text(text, encoding="utf-8")

        contents = await _run([{"filename": "info.nfo", "path": str(companion)}], [str(tmp_path)])

        assert contents[0]["content"] == text

    @pytest.mark.asyncio
    async def test_non_nfo_txt_extension_keeps_the_old_utf8_replacement_behavior(self, tmp_path: Path) -> None:
        """phaze-j9b3z: the CP437 fallback is scoped to .nfo/.txt -- .m3u is ASCII/UTF-8 by spec.

        Invalid-UTF-8 bytes in an .m3u still fall back to ``errors='replace'`` (U+FFFD), never
        CP437 -- guessing CP437 for a playlist would manufacture mojibake, not remove it.
        """
        companion = tmp_path / "playlist.m3u"
        companion.write_bytes(b"#EXTM3U\n\xc9\xcd\xcd track.mp3\n")

        contents = await _run([{"filename": "playlist.m3u", "path": str(companion)}], [str(tmp_path)])

        assert "�" in contents[0]["content"]

    @pytest.mark.asyncio
    async def test_cp437_nfo_through_the_rendered_proposal_context_has_no_replacement_chars(self, tmp_path: Path) -> None:
        """phaze-j9b3z rule 3: verify through read -> clean -> the context content the prompt sees.

        A CP437 NFO with box-drawing/block-art framing around a date field: the rendered companion
        content must carry zero U+FFFD, the art lines must be stripped, and the date field -- which
        sits well past the small ``max_chars`` bound used here -- must survive because
        ``clean_companion_content`` strips the art BEFORE truncating.
        """
        from phaze.services.proposal import clean_companion_content

        top = "╔" + "═" * 40 + "╗"
        shade = "░▒▓█" * 10
        bottom = "╚" + "═" * 40 + "╝"
        art = "\n".join([top, shade, bottom])
        info = "GROUP: EXAMPLE\nRELDATE: 2024.05.15\nSOURCE: SBD"
        text = f"{art}\n{info}\n"
        assert text.index("RELDATE") > 100  # the date field is past the max_chars bound below

        companion = tmp_path / "release.nfo"
        companion.write_bytes(text.encode("cp437"))

        raw_contents = await _run([{"filename": "release.nfo", "path": str(companion)}], [str(tmp_path)], max_chars=100)
        rendered = clean_companion_content(raw_contents[0]["content"], max_chars=100)

        assert "�" not in rendered
        assert "═" not in rendered  # box-drawing art line stripped
        assert "█" not in rendered  # block-element art line stripped
        assert "RELDATE: 2024.05.15" in rendered

    @pytest.mark.asyncio
    async def test_bom_in_a_written_cue_does_not_reach_the_rendered_companion_content(self, tmp_path: Path) -> None:
        """phaze-mi5y0 (seam C3): a REAL producer's BOM must not survive to the LLM-bound text.

        Round-trips a real ``.cue`` through the real PRODUCER (``cue_generator.write_cue_file``,
        which encodes ``utf-8-sig`` at cue_generator.py:340 -- so every phaze-generated CUE starts
        with a BOM) and the real CONSUMER chain: this task's ``read_companion_bounded_sync``
        followed by the control-plane ``clean_companion_content`` + ``sanitize_pg_text`` pipeline
        that ``proposal_context._read_companion_chunk`` applies to every companion before it lands
        in ``build_file_context``'s ``"companions"`` list and the prompt. Neither half re-reads
        with ``utf-8-sig`` -- doing so would strip the BOM by definition (as every prior CUE
        readback test in ``tests/review/services/test_cue_generator.py`` does) and could not
        exhibit this mismatch.

        Also stands in for the AC's second bullet (a consumer parsing companion .cue content by
        line start): no such consumer exists in this tree. Every ``.cue`` reference under
        ``src/phaze/services/tracklist_candidate*.py`` and ``tracklist_priority.py`` is a boolean
        EXISTS/flag ("does a .cue companion exist"), never content parsing, and grepping the whole
        of ``src/phaze`` for CUE-directive parsing (``INDEX 01``, ``cuesheet``, ``parse_cue``,
        ``CueSheet``) outside ``cue_generator.py`` itself returns nothing -- matching the seam
        inventory's own C2 verdict ("NO CUE parser exists in the tree"). This assertion on the
        rendered first line is the regression guard should one ever be added.
        """
        from phaze.services.cue_generator import write_cue_file
        from phaze.services.pg_text import sanitize_pg_text
        from phaze.services.proposal import clean_companion_content

        audio = tmp_path / "<track-01>.mp3"
        audio.touch()
        content = 'REM COMMENT "Generated by Phaze"\nFILE "<track-01>.mp3" MP3\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        cue_path, _version = write_cue_file(content, audio)
        assert cue_path.read_bytes()[:3] == b"\xef\xbb\xbf"  # confirm the producer really wrote a BOM

        raw_contents = await _run([{"filename": cue_path.name, "path": str(cue_path)}], [str(tmp_path)])
        assert len(raw_contents) == 1

        rendered = sanitize_pg_text(clean_companion_content(raw_contents[0]["content"]))

        assert "﻿" not in rendered
        assert rendered == content.strip()
        assert rendered.startswith("REM COMMENT")  # not "﻿REM COMMENT" -- a line-start parser's first target

    @pytest.mark.asyncio
    async def test_reads_run_off_the_event_loop(self, tmp_path: Path) -> None:
        """phaze-cycw: ONE offload for the whole list, never a blocking read on the agent's loop."""
        companion = tmp_path / "info.nfo"
        companion.write_text("Artist: DJ Test", encoding="utf-8")

        with (
            patch("phaze.tasks.companion_read.get_settings", return_value=_agent_settings([str(tmp_path)])),
            patch("phaze.tasks.companion_read.asyncio.to_thread", new_callable=AsyncMock) as to_thread,
        ):
            to_thread.return_value = []
            await read_companion_files({}, agent_id="fileserver-01", companions=[{"filename": "info.nfo", "path": str(companion)}], max_chars=3000)

        to_thread.assert_awaited_once()
        assert to_thread.await_args.args[0].__name__ == "_read_all_sync"
