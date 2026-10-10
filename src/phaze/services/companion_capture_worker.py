"""Killable agent-side bounded capture worker. No ORM, controller or network imports.

The parent sends one bounded JSON request over stdin. All filesystem resolution, reads,
hashing and decoding occur here, so timeout/cancellation can terminate a stalled mount read.
"""

import codecs
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from phaze.constants import is_quarantined
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureReport, CaptureTarget
from phaze.services.companion_features import detect_encoding
from phaze.services.containment import resolve_contained_twin
from phaze.tracklist_providers.domain import OutcomeStatus, SourceRead


def _open_regular(path: Path) -> int:
    """Pin every directory with nofollow; never reopen through mutable ancestor symlinks."""
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("not_regular")
        return fd
    finally:
        os.close(directory)


def _stamp(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _failure(target: CaptureTarget, code: str, status: str = "unavailable") -> SourceRead:
    return SourceRead(status=OutcomeStatus(status), code=code, scope=str(target.file_id), evidence=(code,), retrieved_at=datetime.now(UTC))


def read_capture(target: CaptureTarget, budget: CaptureBudget, scan_roots: list[str]) -> SourceRead:
    """Read a single pinned regular file once. Capped input never scans the remainder for a digest."""
    if is_quarantined(target.path):
        return _failure(target, "quarantined")
    try:
        path, _root = resolve_contained_twin(target.path, scan_roots)
        if is_quarantined(str(path)):
            return _failure(target, "quarantined")
        fd = _open_regular(path)
    except ValueError:
        return _failure(target, "refused")
    except FileNotFoundError:
        return _failure(target, "missing", "absent")
    except OSError:
        return _failure(target, "unreadable")
    try:
        before = os.fstat(fd)
        if before.st_size != target.expected_size:
            return _failure(target, "size_changed", "retry")
        raw = bytearray()
        while len(raw) < min(before.st_size, budget.max_bytes):
            block = os.read(fd, min(65536, min(before.st_size, budget.max_bytes) - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        current_fd = _open_regular(path)
        try:
            current = os.fstat(current_fd)
        finally:
            os.close(current_fd)
        if _stamp(before) != _stamp(after) or _stamp(before) != _stamp(current):
            return _failure(target, "changed_during_read", "retry")
    except (OSError, ValueError):
        return _failure(target, "changed_during_read", "retry")
    finally:
        os.close(fd)

    complete_bytes = len(raw) == before.st_size
    digest = hashlib.sha256(raw).hexdigest()
    encoding = detect_encoding(bytes(raw))
    if not complete_bytes and encoding in {"cp437", "cp1252"}:
        try:
            codecs.getincrementaldecoder("utf-8")(errors="strict").decode(bytes(raw), final=False)
        except UnicodeDecodeError:
            pass
        else:
            encoding = "utf-8"
    if encoding == "all-nul":
        return _failure(target, "binary_content", "unsupported")
    try:
        # final=False permits a multibyte sequence cut at the byte cap without replacement.
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        text = decoder.decode(bytes(raw), final=complete_bytes)
    except UnicodeDecodeError:
        return _failure(target, "invalid_encoding", "unsupported")
    if "\0" in text:
        return _failure(target, "binary_content", "unsupported")
    truncated = not complete_bytes
    if len(text) > budget.max_characters:
        text = text[: budget.max_characters]
        truncated = True
    lines = text.splitlines(keepends=True)
    if len(lines) > budget.max_lines:
        text = "".join(lines[: budget.max_lines])
        truncated = True
    evidence = ("revision:sha256:full" if complete_bytes else "revision:sha256:bounded",)
    status = "incomplete" if truncated else "found"
    code = "capture_truncated" if truncated else "captured"
    if complete_bytes and digest != target.expected_sha256:
        status, code = "retry", "revision_changed"
    read = SourceRead(
        status=OutcomeStatus(status),
        code=code,
        scope=str(target.file_id),
        text=text,
        encoding=encoding,
        revision=digest,
        revision_scope="full" if complete_bytes else "bounded",
        truncated=truncated,
        bytes_read=len(raw),
        evidence=evidence,
        retrieved_at=datetime.now(UTC),
    )
    try:
        CaptureReport(target=target, budget=budget, read=read)
    except ValueError:
        return _failure(target, "wire_cap", "incomplete")
    return read


def main() -> None:
    """Private subprocess protocol: finite input and output, never arbitrary commands."""
    request: dict[str, Any] = json.loads(sys.stdin.buffer.read(32769))
    target = CaptureTarget.model_validate(request["target"])
    budget = CaptureBudget.model_validate(request["budget"])
    read = read_capture(target, budget, request["scan_roots"])
    sys.stdout.write(read.model_dump_json())


if __name__ == "__main__":
    main()
