"""Tests for phaze.tasks.s3_upload (Phase 53 KSTAGE-02 agent upload leg).

``upload_file_s3`` runs on the file-server agent: it PUTs each part of the media
file to its presigned URL over httpx, collects the per-part ETag (D-04), and
reports the ordered list via the control-plane ``/uploaded`` callback. It holds
NO S3 SDK or bucket credentials -- the byte transfer is httpx-only.

respx intercepts the presigned part PUTs; the api_client is a recording fake so
the report_upload_complete handoff is asserted directly.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path):  # type: ignore[no-untyped-def]
    """Minimal AgentSettings env so get_settings() returns AgentSettings; clears the lru_cache."""
    from phaze.config import get_settings

    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeApiClient:
    """Records report_upload_complete / report_upload_failed calls."""

    def __init__(self) -> None:
        self.complete_calls: list[tuple[uuid.UUID, list]] = []
        self.failed_calls: list[tuple[uuid.UUID, str | None]] = []

    async def report_upload_complete(self, file_id, parts):  # type: ignore[no-untyped-def]
        self.complete_calls.append((file_id, parts))

    async def report_upload_failed(self, file_id, detail=None):  # type: ignore[no-untyped-def]
        self.failed_calls.append((file_id, detail))


def _write_file(tmp_path, data: bytes):  # type: ignore[no-untyped-def]
    p = tmp_path / "track.mp3"
    p.write_bytes(data)
    return p


@respx.mock
async def test_upload_puts_each_part_and_reports_etags(agent_env, tmp_path):  # type: ignore[no-untyped-def]
    """Each chunk is PUT to its presigned URL; ETags collected and reported in order."""
    from phaze.tasks.s3_upload import upload_file_s3

    src = _write_file(tmp_path, b"A" * 6 + b"B" * 4)  # 10 bytes -> 2 parts at size 6
    url1 = "https://s3.test/bucket/key?partNumber=1"
    url2 = "https://s3.test/bucket/key?partNumber=2"
    r1 = respx.put(url1).mock(return_value=httpx.Response(200, headers={"ETag": '"etag-1"'}))
    r2 = respx.put(url2).mock(return_value=httpx.Response(200, headers={"ETag": '"etag-2"'}))

    api = _FakeApiClient()
    file_id = uuid.uuid4()
    result = await upload_file_s3(
        {"api_client": api},
        file_id=str(file_id),
        original_path=str(src),
        part_urls=[url1, url2],
        part_size_bytes=6,
        agent_id="fileserver-1",
    )

    assert r1.called and r2.called
    assert r1.calls.last.request.content == b"A" * 6
    assert r2.calls.last.request.content == b"B" * 4
    assert result == {"file_id": str(file_id), "status": "uploaded"}

    assert len(api.complete_calls) == 1
    sent_file_id, sent_parts = api.complete_calls[0]
    assert sent_file_id == file_id
    assert [(p.part_number, p.etag) for p in sent_parts] == [(1, "etag-1"), (2, "etag-2")]


@respx.mock
async def test_non_2xx_part_raises_runtimeerror_no_callback(agent_env, tmp_path):  # type: ignore[no-untyped-def]
    """A non-2xx part PUT raises RuntimeError (SAQ retry) and never reports completion."""
    from phaze.tasks.s3_upload import upload_file_s3

    src = _write_file(tmp_path, b"X" * 5)
    url1 = "https://s3.test/bucket/key?partNumber=1"
    respx.put(url1).mock(return_value=httpx.Response(500))

    api = _FakeApiClient()
    with pytest.raises(RuntimeError):
        await upload_file_s3(
            {"api_client": api},
            file_id=str(uuid.uuid4()),
            original_path=str(src),
            part_urls=[url1],
            part_size_bytes=64,
            agent_id="fileserver-1",
        )
    assert api.complete_calls == []


@respx.mock
async def test_cancellation_is_reraised_not_swallowed(agent_env, tmp_path):  # type: ignore[no-untyped-def]
    """An asyncio.CancelledError during a part PUT is re-raised after reaping (no swallow, no callback)."""
    from phaze.tasks.s3_upload import upload_file_s3

    src = _write_file(tmp_path, b"Y" * 5)
    url1 = "https://s3.test/bucket/key?partNumber=1"

    def _raise_cancel(_request):  # type: ignore[no-untyped-def]
        # asyncio.CancelledError is a BaseException (not Exception) on 3.14, so respx rejects it as a
        # bare side_effect type -- raise it from a callable side_effect instead.
        raise asyncio.CancelledError

    respx.put(url1).mock(side_effect=_raise_cancel)

    api = _FakeApiClient()
    with pytest.raises(asyncio.CancelledError):
        await upload_file_s3(
            {"api_client": api},
            file_id=str(uuid.uuid4()),
            original_path=str(src),
            part_urls=[url1],
            part_size_bytes=64,
            agent_id="fileserver-1",
        )
    assert api.complete_calls == []


async def test_missing_original_path_is_terminal(agent_env, tmp_path):  # type: ignore[no-untyped-def]
    """A missing/unreadable original_path is a TERMINAL RuntimeError (no local fallback, no callback)."""
    from phaze.tasks.s3_upload import upload_file_s3

    api = _FakeApiClient()
    with pytest.raises(RuntimeError):
        await upload_file_s3(
            {"api_client": api},
            file_id=str(uuid.uuid4()),
            original_path=str(tmp_path / "does-not-exist.mp3"),
            part_urls=["https://s3.test/bucket/key?partNumber=1"],
            part_size_bytes=64,
            agent_id="fileserver-1",
        )
    assert api.complete_calls == []
