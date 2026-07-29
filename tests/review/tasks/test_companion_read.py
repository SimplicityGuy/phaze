"""phaze-6bkk: the AGENT-side companion read -- containment, bounded read, off-loop I/O.

These cases moved here from ``tests/review/services/test_proposal.py::TestLoadCompanionContents``
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
