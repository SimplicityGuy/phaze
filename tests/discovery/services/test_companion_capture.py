"""Capture preserves bounded source text and kills timed-out/cancelled filesystem workers."""

import asyncio
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from pydantic import ValidationError
import pytest

from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureCompanionPayload, CaptureReport, CaptureTarget
from phaze.services import companion_capture_worker as worker
from phaze.tasks.companion_capture import capture_companion_source, capture_read
from phaze.tracklist_providers.domain import SourceRead


def target(path: Path, raw: bytes, **kwargs: object) -> CaptureTarget:
    return CaptureTarget(file_id=uuid.uuid4(), path=str(path), expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw), **kwargs)


@pytest.mark.parametrize(
    ("encoding", "body", "detected"),
    [
        ("utf-8-sig", "Artist: Café\r\n01. Début\n", "utf-8-sig"),
        ("utf-8", "Artist: Café\r\n", "utf-8"),
        ("utf-16", "Artist: Café\r\n", "utf-16"),
        ("utf-16-le", "Artist: Café\r\n", "utf-16-le"),
        ("cp437", "░" * 24 + "\r\nArtist: Cafe\n", "cp437"),
        ("cp1252", "Artist: Café “Live”\r\n", "cp1252"),
    ],
)
def test_encoding_and_original_formatting(tmp_path: Path, encoding: str, body: str, detected: str) -> None:
    raw = body.encode(encoding)
    path = tmp_path / "notes.nfo"
    path.write_bytes(raw)
    read = worker.read_capture(target(path, raw), CaptureBudget(), [str(tmp_path)])
    assert read.status == "found" and read.text == body and read.encoding == detected
    assert read.revision == hashlib.sha256(raw).hexdigest() and read.revision_scope == "full"
    assert read.evidence == ("revision:sha256:full",)


def test_byte_character_and_line_caps_are_honest(tmp_path: Path) -> None:
    raw = "Aé\n".encode() * 50
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    request = target(path, raw)
    bounded = worker.read_capture(request, CaptureBudget(max_bytes=6), [str(tmp_path)])
    assert bounded.status == "incomplete" and bounded.bytes_read == 6
    assert bounded.revision_scope == "bounded" and "revision:sha256:full" not in bounded.evidence
    assert bounded.text == "Aé\nA"
    for budget in (CaptureBudget(max_characters=3), CaptureBudget(max_lines=2)):
        read = worker.read_capture(request, budget, [str(tmp_path)])
        assert read.status == "incomplete" and read.truncated and read.revision_scope == "full"
        assert len(read.text or "") <= budget.max_characters and len((read.text or "").splitlines()) <= budget.max_lines


def test_missing_refused_nonregular_and_binary_are_individual_outcomes(tmp_path: Path) -> None:
    absent = worker.read_capture(target(tmp_path / "missing.txt", b""), CaptureBudget(), [str(tmp_path)])
    assert absent.status == "absent" and absent.code == "missing"
    assert worker.read_capture(target(tmp_path / "missing.txt", b""), CaptureBudget(), []).code == "refused"
    fifo = tmp_path / "fifo.txt"
    os.mkfifo(fifo)
    assert worker.read_capture(target(fifo, b""), CaptureBudget(), [str(tmp_path)]).code == "refused"
    path = tmp_path / "binary.txt"
    path.write_bytes(b"\0" * 4)
    assert worker.read_capture(target(path, b"\0" * 4), CaptureBudget(), [str(tmp_path)]).status == "unsupported"


def test_symlink_escape_and_ancestor_swap_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "notes.txt"
    secret.write_bytes(b"secret")
    (allowed / "notes.txt").symlink_to(secret)
    assert worker.read_capture(target(allowed / "notes.txt", b"secret"), CaptureBudget(), [str(allowed)]).code == "refused"
    (allowed / "swapped").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        worker._open_regular(allowed / "swapped" / "notes.txt")


def test_changed_during_read_and_revision_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"original")
    request = target(path, b"original")
    original_read = os.read

    def mutate(fd: int, count: int) -> bytes:
        block = original_read(fd, count)
        path.write_bytes(b"replaced")
        return block

    monkeypatch.setattr(worker.os, "read", mutate)
    assert worker.read_capture(request, CaptureBudget(), [str(tmp_path)]).code == "changed_during_read"
    monkeypatch.setattr(worker.os, "read", original_read)
    read = worker.read_capture(request, CaptureBudget(), [str(tmp_path)])
    assert read.status == "retry" and read.code == "revision_changed" and read.text == "replaced"


def test_unicode_twin_empty_and_wire_cap(tmp_path: Path) -> None:
    path = tmp_path / "Cafe\u0301.txt"
    path.write_bytes(b"")
    request = target(tmp_path / "Café.txt", b"")
    read = worker.read_capture(request, CaptureBudget(), [str(tmp_path)])
    assert read.status == "found" and read.text == "" and read.bytes_read == 0
    raw = b"\t" * 4000
    path.write_bytes(raw)
    assert worker.read_capture(target(path, raw), CaptureBudget(max_wire_bytes=4096), [str(tmp_path)]).code == "wire_cap"


@pytest.mark.asyncio
async def test_actual_subprocess_preserves_source_text(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"Artist: Example\r\n")
    payload = CaptureCompanionPayload(agent_id="agent-01", target=target(path, path.read_bytes()))
    assert (await capture_read(payload, [str(tmp_path)])).text == "Artist: Example\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_and_cancellation_kill_and_reap_real_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    real_spawn = asyncio.create_subprocess_exec
    children = []

    async def slow_spawn(*_args: object, **kwargs: object):
        child = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    payload = CaptureCompanionPayload(agent_id="agent-01", target=target(tmp_path / "notes.txt", b""), budget=CaptureBudget(deadline_seconds=0.1))
    task = asyncio.create_task(capture_read(payload, [str(tmp_path)]))
    if cancel:
        while not children:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).code == "deadline"
    assert len(children) == 1 and children[0].returncode is not None


@pytest.mark.asyncio
async def test_wrong_worker_identity_never_reads_or_reports(tmp_path: Path) -> None:
    api = SimpleNamespace(post_companion_capture=AsyncMock())
    payload = CaptureCompanionPayload(agent_id="wrong", target=target(tmp_path / "notes.txt", b""))
    with pytest.raises(ValueError, match="authenticated worker"):
        await capture_companion_source({"api_client": api, "agent_identity": SimpleNamespace(agent_id="owner")}, **payload.model_dump())
    api.post_companion_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_start_failure_and_oversized_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = AsyncMock(side_effect=OSError("worker unavailable"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    payload = CaptureCompanionPayload(agent_id="agent-01", target=target(tmp_path / "notes.txt", b""))
    assert (await capture_read(payload, [str(tmp_path)])).code == "worker_start_failed"
    spawn.reset_mock()
    assert (await capture_read(payload, ["x" * 32768])).code == "request_cap"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("output", "returncode", "code"), [(b"invalid json", 0, "invalid_worker_output"), (b"", 1, "worker_failed")])
async def test_invalid_worker_response_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes, returncode: int, code: str) -> None:
    child = SimpleNamespace(returncode=returncode, communicate=AsyncMock(return_value=(output, None)), wait=AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    payload = CaptureCompanionPayload(agent_id="agent-01", target=target(tmp_path / "notes.txt", b""))
    assert (await capture_read(payload, [str(tmp_path)])).code == code
    child.wait.assert_awaited_once()


@pytest.mark.parametrize("changes", [{"text": None}, {"encoding": None}, {"bytes_read": 1}, {"revision_scope": "bounded"}, {"truncated": True}])
def test_contradictory_found_report_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    request = target(tmp_path / "notes.txt", b"")
    read = {
        "status": "found",
        "code": "captured",
        "scope": str(request.file_id),
        "text": "",
        "encoding": "ascii",
        "bytes_read": 0,
        "revision": request.expected_sha256,
        "revision_scope": "full",
        "evidence": ["revision:sha256:full"],
        "retrieved_at": datetime.now(UTC),
    } | changes
    with pytest.raises(ValidationError):
        CaptureReport(target=request, read=SourceRead.model_validate(read))


@pytest.mark.parametrize(
    ("changes", "budget"),
    [
        ({"scope": "other"}, CaptureBudget()),
        ({"code": "bad\0code"}, CaptureBudget()),
        ({"bytes_read": 2}, CaptureBudget(max_bytes=1)),
        ({"text": "xx"}, CaptureBudget(max_characters=1)),
        ({"text": "x\ny\n"}, CaptureBudget(max_lines=1)),
        ({"revision_scope": "full", "revision": "a" * 64}, CaptureBudget()),
        ({"revision_scope": "full", "revision": "wrong", "evidence": ["revision:sha256:full"]}, CaptureBudget()),
        ({"revision_scope": "bounded", "revision": "a" * 64}, CaptureBudget()),
        ({"evidence": ["revision:sha256:full"]}, CaptureBudget()),
    ],
)
def test_untrusted_report_bounds(tmp_path: Path, changes: dict[str, object], budget: CaptureBudget) -> None:
    request = target(tmp_path / "notes.txt", b"")
    read = {"status": "unavailable", "code": "offline", "scope": str(request.file_id), "retrieved_at": datetime.now(UTC)} | changes
    with pytest.raises(ValidationError):
        CaptureReport(target=request, budget=budget, read=SourceRead.model_validate(read))


def test_worker_private_protocol_and_read_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "notes.txt"
    raw = b"original"
    path.write_bytes(raw)
    request = target(path, raw)
    stdin = io.BytesIO(
        json.dumps({"target": request.model_dump(mode="json"), "budget": CaptureBudget().model_dump(), "scan_roots": [str(tmp_path)]}).encode()
    )
    stdout = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(worker.sys, "stdout", stdout)
    worker.main()
    assert SourceRead.model_validate_json(stdout.getvalue()).text == "original"
    assert worker.read_capture(request.model_copy(update={"expected_size": 1}), CaptureBudget(), [str(tmp_path)]).code == "size_changed"
    monkeypatch.setattr(worker.os, "read", lambda *_args: b"")
    assert worker.read_capture(request, CaptureBudget(), [str(tmp_path)]).truncated
    monkeypatch.setattr(worker.os, "read", lambda *_args: (_ for _ in ()).throw(OSError("mount lost")))
    assert worker.read_capture(request, CaptureBudget(), [str(tmp_path)]).code == "changed_during_read"


@pytest.mark.parametrize(
    ("raw", "cap", "code"), [(b"\xffabc", 2, "capture_truncated"), (b"\xff\xfeA", 4, "invalid_encoding"), (b"a\0b", 4, "binary_content")]
)
def test_legacy_prefix_and_invalid_binary_encoding(tmp_path: Path, raw: bytes, cap: int, code: str) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    assert worker.read_capture(target(path, raw), CaptureBudget(max_bytes=cap), [str(tmp_path)]).code == code
