"""Unit tests for phaze.agent_watcher.observer.WatcherEventHandler.

Five behaviors mirror 27-PATTERNS.md lines 1211-1215:

1. Extension filter: only music/video extensions reach the debouncer touch.
2. Directory events ignored (DirCreatedEvent fires no callback).
3. NFC normalization of src_path before dispatch (Pitfall 3 mitigation).
4. Thread bridge: dispatch goes through ``loop.call_soon_threadsafe`` and
   NEVER calls debouncer.touch directly on the watchdog thread (Pitfall 2).
5. Handler subscribes to BOTH ``on_created`` and ``on_modified`` (SCAN-03).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
import unicodedata
from unittest.mock import MagicMock

from watchdog.events import DirCreatedEvent, DirModifiedEvent, FileCreatedEvent, FileModifiedEvent

from phaze.agent_watcher.observer import WatcherEventHandler


if TYPE_CHECKING:
    import pytest


def test_event_handler_filters_by_extension() -> None:
    """`.txt` event ignored; `.mp3` event triggers a single dispatch via the loop."""
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_created(FileCreatedEvent(src_path="/foo/a.txt"))
    assert loop.call_soon_threadsafe.call_count == 0

    handler.on_created(FileCreatedEvent(src_path="/foo/b.mp3"))
    assert loop.call_soon_threadsafe.call_count == 1
    # First positional arg is the touch callable, second is the normalized path.
    args, _ = loop.call_soon_threadsafe.call_args
    assert args[0] is touch
    assert args[1] == "/foo/b.mp3"


def test_observer_extractable_set_is_music_and_video_only() -> None:
    """CR-01 regression: watcher's _EXTRACTABLE must be exactly {MUSIC, VIDEO}.

    The watcher's filter, scan_directory's filter, and the auto-enqueue gate in
    ``routers/agent_files.py`` MUST stay in lockstep; otherwise the operator-
    triggered ingestion population diverges from the watcher's ingestion
    population (CR-01).
    """
    from phaze.agent_watcher.observer import _EXTRACTABLE
    from phaze.constants import FileCategory

    assert frozenset({FileCategory.MUSIC, FileCategory.VIDEO}) == _EXTRACTABLE


def test_observer_drops_companion_files() -> None:
    """CR-01 regression: COMPANION extensions (.cue/.nfo/.txt/.jpg/...) drop without dispatch.

    Companion files must NOT enter the debouncer; otherwise the watcher would
    POST FileRecord rows for COMPANION siblings, which would never be auto-
    enqueued for metadata extraction. Today's filter is MUSIC+VIDEO; this test
    pins the exhaustive companion-extension set down so a future schema change
    that re-categorizes (say) ``.cue`` as MUSIC surfaces loudly.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    companion_extensions = (
        ".cue",
        ".nfo",
        ".txt",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".m3u",
        ".m3u8",
        ".pls",
        ".sfv",
        ".md5",
    )
    for ext in companion_extensions:
        handler.on_created(FileCreatedEvent(src_path=f"/foo/companion{ext}"))

    assert loop.call_soon_threadsafe.call_count == 0
    assert touch.call_count == 0


def test_event_handler_ignores_directories() -> None:
    """DirCreatedEvent (is_directory=True) is dropped without any dispatch."""
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_created(DirCreatedEvent(src_path="/foo"))

    assert loop.call_soon_threadsafe.call_count == 0
    assert touch.call_count == 0


def test_event_handler_normalizes_path() -> None:
    """NFD-form combining-accent input is NFC-normalized before dispatch (Pitfall 3)."""
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    # "é.mp3" composed as NFD ("e" + combining acute) -- two code points.
    nfd_path = unicodedata.normalize("NFD", "/é.mp3")
    assert not unicodedata.is_normalized("NFC", nfd_path), "fixture precondition: NFD input"

    handler.on_created(FileCreatedEvent(src_path=nfd_path))

    assert loop.call_soon_threadsafe.call_count == 1
    args, _ = loop.call_soon_threadsafe.call_args
    normalized_arg = args[1]
    assert unicodedata.is_normalized("NFC", normalized_arg), f"expected NFC; got {normalized_arg!r}"


def test_event_handler_uses_call_soon_threadsafe() -> None:
    """Pitfall 2: dispatch MUST go through call_soon_threadsafe, NOT direct touch().

    This is the canonical proof that the asyncio-owned debouncer dict is never
    mutated from the watchdog OS thread.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_created(FileCreatedEvent(src_path="/foo/x.mp3"))

    # call_soon_threadsafe MUST have been invoked once with the touch callable.
    assert loop.call_soon_threadsafe.call_count == 1
    # touch itself MUST NOT have been called directly on the test thread --
    # call_soon_threadsafe is a MagicMock that does NOT auto-invoke the
    # scheduled callback; this proves the bridge semantics.
    assert touch.call_count == 0


def test_event_handler_subscribes_to_created_and_modified() -> None:
    """SCAN-03 / D-01: handler reacts to BOTH FileCreatedEvent and FileModifiedEvent."""
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_created(FileCreatedEvent(src_path="/foo/a.mp3"))
    handler.on_modified(FileModifiedEvent(src_path="/foo/b.mp3"))

    assert loop.call_soon_threadsafe.call_count == 2
    paths = [call.args[1] for call in loop.call_soon_threadsafe.call_args_list]
    assert paths == ["/foo/a.mp3", "/foo/b.mp3"]


def test_event_handler_decodes_bytes_via_fs_encoding() -> None:
    """WR-03 regression: bytes src_path decoded via os.fsdecode (filesystem encoding).

    Previously the handler hardcoded ``decode("utf-8", errors="strict")``, which
    silently dropped legitimate filenames on hosts where the filesystem
    encoding is not UTF-8. ``os.fsdecode`` honors ``sys.getfilesystemencoding()``
    and uses surrogateescape, so un-decodable bytes survive rather than vanish.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    # Bytes form of "/foo/song.mp3" -- the obvious UTF-8/ASCII overlap case.
    handler.on_created(FileCreatedEvent(src_path=b"/foo/song.mp3"))

    assert loop.call_soon_threadsafe.call_count == 1
    args, _ = loop.call_soon_threadsafe.call_args
    assert args[1] == "/foo/song.mp3"


def test_event_handler_preserves_undecodable_bytes_via_surrogateescape() -> None:
    """WR-03 regression: bytes that fail strict UTF-8 decode still surface (don't get dropped).

    ``os.fsdecode`` uses surrogateescape, so a path containing a lone 0x80 byte
    (invalid UTF-8) round-trips through a surrogate-encoded string and reaches
    the debouncer. The downstream POST may still fail, but the path becomes
    diagnosable in logs instead of silently disappearing at DEBUG level.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    # 0x80 alone is invalid UTF-8 (a continuation byte with no leading byte).
    # Strict-UTF-8 decode would raise UnicodeDecodeError; os.fsdecode survives.
    handler.on_created(FileCreatedEvent(src_path=b"/foo/bad\x80name.mp3"))

    assert loop.call_soon_threadsafe.call_count == 1
    args, _ = loop.call_soon_threadsafe.call_args
    # Filename made it through; we don't assert exact spelling because
    # surrogateescape maps the byte to a surrogate codepoint (U+DC80).
    assert args[1].startswith("/foo/bad")
    assert args[1].endswith("name.mp3")


# ---------------------------------------------------------------------------
# Coverage gap fills (Codecov PR #59): observer.py:64, 68-70, 90
# ---------------------------------------------------------------------------


def test_event_handler_drops_empty_src_path() -> None:
    """Empty src_path short-circuits the filter (observer.py:64)."""
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_created(FileCreatedEvent(src_path=""))
    handler.on_modified(FileModifiedEvent(src_path=""))

    assert loop.call_soon_threadsafe.call_count == 0
    assert touch.call_count == 0


def test_event_handler_drops_path_when_fsdecode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un-decodable bytes (os.fsdecode raises) drop with a WARNING (observer.py:68-70).

    os.fsdecode with surrogateescape is intentionally lenient — to reach the
    except branch we monkeypatch it to raise. This covers the defensive log+drop
    path so an exotic future filesystem encoding (or a fsdecode regression)
    cannot silently propagate up the handler.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    def _boom(_b: bytes) -> str:
        raise UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid")

    monkeypatch.setattr(os, "fsdecode", _boom)

    handler.on_created(FileCreatedEvent(src_path=b"/foo/bad.mp3"))

    assert loop.call_soon_threadsafe.call_count == 0
    assert touch.call_count == 0


def test_event_handler_ignores_directories_in_on_modified() -> None:
    """DirModifiedEvent fires no callback (observer.py:90).

    Mirrors ``test_event_handler_ignores_directories`` but for ``on_modified``
    — without this, the directory-event guard in ``on_modified`` was
    structurally identical but uncovered.
    """
    loop = MagicMock()
    touch = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=touch)

    handler.on_modified(DirModifiedEvent(src_path="/foo"))

    assert loop.call_soon_threadsafe.call_count == 0
    assert touch.call_count == 0
