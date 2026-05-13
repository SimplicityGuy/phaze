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

import unicodedata
from unittest.mock import MagicMock

from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileModifiedEvent

from phaze.agent_watcher.observer import WatcherEventHandler


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
