"""phaze-5rfev: the agent-side ``locate-stale`` step, against real files in a temporary scan root.

The end-to-end reconcile (this step plus the controller's) runs against real Postgres in
``tests/discovery/services/test_stale_rows.py``; this module covers what only the agent decides:
existence, the content search and its bounds. Paths are invented placeholders.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from typing import TYPE_CHECKING
import unicodedata

import pytest

from phaze.agent_watcher import __main__ as wmain
from phaze.agent_watcher.locate import is_under, locate_stale
from phaze.constants import QUARANTINE_DIRNAME
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _row(row_id: str, path: Path, content: bytes) -> dict[str, object]:
    return {"id": row_id, "path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}


def test_is_under_respects_path_boundaries() -> None:
    assert is_under("/r/a.mp3", "/r/")
    assert is_under("/r", "/r")
    assert not is_under("/rx/a.mp3", "/r")


def test_locate_marks_existence_and_finds_moved_content_by_hash(tmp_path: Path) -> None:
    root = tmp_path / "root"
    present = _write(root / "rel" / "here.mp3", b"present")
    moved_to = _write(root / "sets" / "renamed.mp3", b"moved bytes")
    _write(root / "sets" / "same size.mp3", b"other bytes")  # same size as the moved file, other hash
    nfd_name = unicodedata.normalize("NFD", "Björk.mp3")
    _write(root / "rel" / nfd_name, b"nfd")
    _write(root / "rel" / "notes.xyz", b"moved bytes")  # not ingestible: the scan's walk never lists it
    _write(root / QUARANTINE_DIRNAME / "q.mp3", b"moved bytes")  # quarantined: pruned like a scan
    document = {
        "agent_id": "agent-a",
        "scan_roots": [str(root), str(tmp_path / "not-mounted")],
        "rows": [
            _row("1", present, b"present"),
            _row("2", root / "incoming" / "orig.mp3", b"moved bytes"),
            _row("3", root / "rel" / unicodedata.normalize("NFC", nfd_name), b"nfd"),
            _row("4", tmp_path / "not-mounted" / "a.mp3", b"x"),
            _row("5", root / "gone.mp3", b"nowhere at all"),
        ],
    }

    located = json.loads(json.dumps(locate_stale(document)))

    rows = {row["id"]: row for row in located["rows"]}
    assert [rows[i]["exists"] for i in "12345"] == [True, False, True, None, False]
    assert rows["2"]["found"] == [
        {
            "sha256_hash": compute_sha256(moved_to),
            "original_path": str(moved_to),
            "original_filename": "renamed.mp3",
            "current_path": str(moved_to),
            "file_type": "mp3",
            "file_size": len(b"moved bytes"),
        }
    ]
    assert rows["5"]["found"] == []
    assert "found" not in rows["1"] and "found" not in rows["4"]
    assert located["walked_roots"] == [str(root)]
    assert located["walk_errors"] == 0


def test_locate_does_not_walk_when_nothing_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    present = _write(tmp_path / "a.mp3", b"a")
    monkeypatch.setattr("phaze.agent_watcher.locate._find_by_content", lambda *_args: pytest.fail("walked with nothing gone"))

    located = locate_stale({"agent_id": "a", "scan_roots": [str(tmp_path)], "rows": [_row("1", present, b"a")]})

    assert located["walked_roots"] == []
    assert located["walk_errors"] == 0


def test_locate_counts_unreadable_paths_instead_of_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    _write(root / "a.mp3", b"same")
    real_hash = compute_sha256

    def _unreadable(path: Path) -> str:
        if path.name == "a.mp3":
            raise PermissionError(13, "Permission denied", str(path))
        return real_hash(path)

    monkeypatch.setattr("phaze.agent_watcher.locate.compute_sha256", _unreadable)

    located = locate_stale({"agent_id": "a", "scan_roots": [str(root)], "rows": [_row("1", root / "gone.mp3", b"same")]})

    assert located["rows"][0]["found"] == []
    assert located["walk_errors"] == 1


def test_locate_stale_cli_reads_stdin_and_writes_the_annotated_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "a.mp3", b"x")
    document = {"agent_id": "agent-a", "scan_roots": [str(tmp_path)], "rows": [_row("1", tmp_path / "a.mp3", b"x")]}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(document)))
    monkeypatch.setattr(sys, "stdout", out)

    with pytest.raises(SystemExit) as exited:
        wmain._entrypoint(["locate-stale"])

    assert exited.value.code == 0
    assert json.loads(out.getvalue())["rows"][0]["exists"] is True
