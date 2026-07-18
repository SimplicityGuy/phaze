"""Watchdog -> asyncio bridge for the always-on watcher (Phase 27 D-01, Pitfall 2/3).

``WatcherEventHandler`` is the sole sanctioned bridge between the watchdog
Observer's OS thread and the asyncio-owned :class:`Debouncer`. It:

1. Subscribes to ``FileCreatedEvent`` + ``FileModifiedEvent`` only (D-01).
   Other watchdog event types (move, delete, *DirEvent) are ignored.
2. Filters by ``EXTENSION_MAP`` -- only ``FileCategory.MUSIC`` and
   ``FileCategory.VIDEO`` paths enter the debouncer (SCAN-03).
3. Dispatches the RAW OS path (whatever Unicode normalization form the
   filesystem handed watchdog) through, unchanged. It is later used
   verbatim as the filesystem handle for ``stat``/hashing in
   :class:`phaze.agent_watcher.poster.Poster`, mirroring
   ``phaze.tasks.scan.scan_directory`` (which stats the raw ``os.walk``
   path and normalizes only the outgoing DB record fields). Normalizing
   to NFC here -- as a prior revision did -- makes the dispatched string
   diverge from the on-disk byte sequence on Unicode-normalization-
   sensitive filesystems (ext4/most Linux filesystems): an NFD-named
   file (e.g. macOS-origin media synced via SMB/rsync) then permanently
   ENOENTs in ``Poster.post_one`` and is silently dropped (Pitfall 3
   correction). ``Poster`` still NFC-normalizes the *record* fields
   (``original_path``, ``original_filename``, ``current_path``) before
   they hit the wire / DB -- that normalization is unaffected.
4. Dispatches the touch through the asyncio loop's thread-safe scheduler --
   the ONLY sanctioned cross-thread bridge (Pitfall 2). NEVER call
   ``debouncer.touch`` directly: the underlying ``dict[str, _PendingEntry]``
   is asyncio-owned and any cross-thread mutation is a data race on
   CPython 3.13.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from watchdog.events import FileSystemEventHandler

from phaze.constants import EXTENSION_MAP, FileCategory


if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from watchdog.events import DirCreatedEvent, DirModifiedEvent, FileCreatedEvent, FileModifiedEvent


logger = structlog.get_logger(__name__)


_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})
"""Extension categories the watcher posts to the controller; everything else is dropped."""


class WatcherEventHandler(FileSystemEventHandler):
    """Watchdog event handler that bridges to the asyncio Debouncer."""

    def __init__(self, loop: asyncio.AbstractEventLoop, debouncer_touch: Callable[[str], None]) -> None:
        super().__init__()
        self._loop = loop
        self._debouncer_touch = debouncer_touch

    def _filter_and_dispatch(self, src_path: bytes | str) -> None:
        # watchdog types ``src_path`` as ``bytes | str`` (some platforms emit
        # bytes for non-UTF-8 filesystem names). Decode via ``os.fsdecode`` so
        # the system's filesystem encoding (``sys.getfilesystemencoding()``) is
        # honored -- on hosts whose LANG is not UTF-8 (legacy ext4, older NFS,
        # filesystems with pre-UTF-8 Latin-1 filenames) a hardcoded UTF-8 strict
        # decode silently dropped legitimate music files. ``os.fsdecode`` uses
        # surrogateescape by default, so un-decodable bytes survive into logs
        # rather than vanishing. (WR-03)
        if not src_path:
            return
        if isinstance(src_path, bytes):
            try:
                path_str = os.fsdecode(src_path)
            except (UnicodeDecodeError, ValueError):
                logger.warning("watcher: dropping path; cannot decode via fs encoding; len=%d", len(src_path))
                return
        else:
            path_str = src_path
        ext = "." + Path(path_str).suffix.lower().lstrip(".")
        if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
            return
        # NOTE: `path_str` is dispatched RAW -- do not NFC-normalize it here.
        # It becomes the filesystem handle Poster.post_one stats/hashes; see
        # the module docstring point 3 for why normalizing it broke NFD-named
        # ingestion on Linux.
        # Pitfall 2: NEVER call ``self._debouncer_touch(path_str)`` directly --
        # this method runs on the watchdog OS thread; the debouncer's backing
        # dict is asyncio-owned. The asyncio thread-safe scheduler call below
        # is the canonical cross-thread primitive.
        self._loop.call_soon_threadsafe(self._debouncer_touch, path_str)

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        if event.is_directory:
            return
        self._filter_and_dispatch(event.src_path)

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        if event.is_directory:
            return
        self._filter_and_dispatch(event.src_path)
