"""Real-filesystem regression tests for phaze.agent_watcher.poster.Poster (phaze-hk7k).

Linux-only (skipped elsewhere): ext4 -- and most Linux filesystems -- are
Unicode-normalization-SENSITIVE, so an NFD-named file (e.g. macOS-origin media
copied via SMB/rsync, where "Björk.mp3" is stored on disk as the byte
sequence for "Bjo" + "rk.mp3" with a combining diaeresis, NFD form) is only
byte-exact-reachable via its raw NFD path; ``Path(nfc_form).stat()`` ENOENTs.
macOS/APFS is normalization-INSENSITIVE for lookups, so the same assertions
would pass there even with the bug present (a false negative) -- hence the
skip, per the acceptance note on phaze-hk7k ("meaningful only on Linux CI").

These tests exercise ``Poster.post_one`` directly against files actually
created on disk (unlike ``test_observer.py``'s deterministic, host-independent
unit tests), proving the acceptance criterion end-to-end at the layer that
performs the real ``stat``/hash filesystem access: an NFD-named file created
under a watched root is posted successfully, and NFD+NFC twins are both
posted (each resolves to its own distinct filesystem entry).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any
import unicodedata

import httpx
import pytest
import respx

from phaze.agent_watcher.poster import Poster
from phaze.services.agent_client import PhazeAgentClient


if TYPE_CHECKING:
    from pathlib import Path


_TEST_TOKEN = "phaze_agent_test"  # nosec B105 -- test fixture, not a real secret

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="NFD-vs-NFC byte-exact filesystem lookup is only observable on Unicode-normalization-sensitive filesystems (Linux/ext4); macOS/APFS lookups are normalization-insensitive and would mask the regression.",
)


async def test_post_one_nfd_named_file_is_posted_successfully(tmp_path: Path) -> None:
    """phaze-hk7k acceptance: an NFD-named file is posted, not silently dropped.

    Creates a file on disk whose name is NFD-decomposed ("Björk.mp3" as "e" +
    combining acute, i.e. two code points for the "o"-umlaut sequence), then
    calls ``post_one`` with that RAW NFD path -- exactly what the corrected
    ``WatcherEventHandler`` now dispatches (it no longer NFC-normalizes
    before touch). Before the fix, the watcher would have handed
    ``Poster.post_one`` the NFC form instead, which ENOENTs against this
    on-disk NFD file and gets dropped.
    """
    nfd_name = unicodedata.normalize("NFD", "Björk.mp3")
    assert not unicodedata.is_normalized("NFC", nfd_name), "fixture precondition: NFD name"

    nfd_path = tmp_path / nfd_name
    nfd_path.write_bytes(b"\x00" * 128)

    base_url = "http://app.test:8000"
    real_client = PhazeAgentClient(base_url=base_url, token=_TEST_TOKEN, timeout=5.0)
    poster = Poster(client=real_client, agent_id="test-agent")

    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"agent_id": "test-agent", "upserted": 1, "inserted": 1, "enqueued": 0})

    with respx.mock(base_url=base_url, assert_all_called=True) as mock:
        mock.post("/api/internal/agent/files").mock(side_effect=_capture)
        await poster.post_one(str(nfd_path))

    await real_client.close()

    import json

    body = json.loads(captured["json"])
    assert len(body["files"]) == 1, "the NFD-named file must have been posted, not dropped"
    posted = body["files"][0]
    # The filesystem-facing lookup used the raw NFD path (proven by reaching
    # the POST at all); the outgoing record fields are still NFC-normalized
    # (Pitfall 3 -- DB-facing keys stay canonical).
    assert unicodedata.is_normalized("NFC", posted["original_filename"])
    assert unicodedata.is_normalized("NFC", posted["original_path"])


async def test_post_one_nfd_and_nfc_twins_both_posted(tmp_path: Path) -> None:
    """phaze-hk7k acceptance: coexisting NFD+NFC twins are BOTH posted.

    Two distinct on-disk files whose names are Unicode-equivalent but
    byte-distinct (NFD vs. NFC forms) are two separate filesystem entries on
    a normalization-sensitive filesystem. Each must resolve and post
    independently via its own raw path.
    """
    nfd_name = unicodedata.normalize("NFD", "Sigur Rós.mp3")
    nfc_name = unicodedata.normalize("NFC", "Sigur Rós.mp3")
    assert nfd_name != nfc_name, "fixture precondition: NFD and NFC forms are distinct byte sequences"

    nfd_path = tmp_path / nfd_name
    nfc_path = tmp_path / nfc_name
    nfd_path.write_bytes(b"\x01" * 64)
    nfc_path.write_bytes(b"\x02" * 64)

    base_url = "http://app.test:8000"
    real_client = PhazeAgentClient(base_url=base_url, token=_TEST_TOKEN, timeout=5.0)
    poster = Poster(client=real_client, agent_id="test-agent")

    captured: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode("utf-8"))
        return httpx.Response(200, json={"agent_id": "test-agent", "upserted": 1, "inserted": 1, "enqueued": 0})

    with respx.mock(base_url=base_url, assert_all_called=True) as mock:
        mock.post("/api/internal/agent/files").mock(side_effect=_capture)
        await poster.post_one(str(nfd_path))
        await poster.post_one(str(nfc_path))

    await real_client.close()

    assert len(captured) == 2, "both the NFD twin and the NFC twin must have been posted"
