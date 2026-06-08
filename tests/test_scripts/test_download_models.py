"""Tests for `phaze.scripts.download_models` (Phase 29 D-21 / 260608-i21 / 260608-jbg).

Covers the download + size-validation behaviour:
- `_download_one` streams to `<dest>.part` and atomically renames on success
- `_download_one` fails fast on 4xx and retries transient/5xx/truncated reads
- `_with_retries` is the single bounded-retry implementation shared by HEAD + GET
- `_head_content_length` / `_try_head_size` read the server Content-Length, bound
  the HEAD with the same timeout/retry machinery, and degrade gracefully
- `_ensure_present` validates on-disk byte size against the HEAD Content-Length:
  keeps a valid file (no GET), re-downloads a truncated one, and falls back when
  the size is unobtainable
- `download_to` walks both CLASSIFIER_MODELS and GENRE_MODELS, HEAD-validating and
  only GETting missing/mismatched files under the documented Essentia URL bases

Uses `respx` (already a dev dep — see `pyproject.toml`) to intercept HEAD + GET.
`download_models.time.sleep` is monkeypatched to a no-op counter so there is ZERO
real network I/O and ZERO real sleep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from phaze.scripts import download_models
from phaze.scripts.download_models import (
    CLASSIFIER_MODELS,
    GENRE_MODELS,
    _download_one,
    _ensure_present,
    _head_content_length,
    _try_head_size,
    download_to,
)


if TYPE_CHECKING:
    from pathlib import Path


_CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
_GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``download_models.time.sleep`` with a no-op counter and return it.

    Keeps retries observable (one append per backoff) while ensuring tests never
    incur a real delay or real network I/O.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(download_models.time, "sleep", lambda delay: sleeps.append(delay))
    return sleeps


# --------------------------------------------------------------------------- #
# _download_one (always-fetch; the size-based skip lives in _ensure_present)  #
# --------------------------------------------------------------------------- #


@respx.mock
def test_download_one_streams_atomically(tmp_path: Path) -> None:
    """Success path: byte stream is written to `<dest>.part`, then renamed."""
    url = "https://example.test/model.pb"
    dest = tmp_path / "subdir" / "model.pb"  # parent dir doesn't exist yet
    payload = b"model-bytes" * 1024  # > 1 chunk worth

    respx.get(url).mock(return_value=httpx.Response(200, content=payload))

    _download_one(url, dest)

    assert dest.exists(), "destination must exist after successful download"
    assert dest.read_bytes() == payload
    # The atomic `.part` rename means no temp file is left behind on success.
    assert not (tmp_path / "subdir" / "model.pb.part").exists()


@respx.mock
def test_download_one_4xx_raises_and_no_dest_written(tmp_path: Path) -> None:
    """Failure path: a 4xx response raises HTTPStatusError and `dest` is not created.

    The atomic `.part` rename means a failed download MUST leave `dest` absent;
    `phaze.tasks._shared.model_bootstrap.ensure_models_present` relies on
    `glob("*.pb")` skipping `.part` files to decide whether to retry.
    """
    url = "https://example.test/missing.pb"
    dest = tmp_path / "missing.pb"
    route = respx.get(url).mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        _download_one(url, dest)

    assert not dest.exists(), "failed download must NOT leave dest in place"
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "no .part may linger after a fail-fast 4xx"
    # Fail-fast: a 4xx is NOT in the retry tuple, so the route is hit exactly once
    # even though a retry loop exists (260608-i21).
    assert route.call_count == 1, "a 4xx must fail fast without retrying"


@respx.mock
def test_download_one_retries_transient_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (c): two transient transport errors then a 200 → file lands, no .part, sleeps twice."""
    url = "https://example.test/transient.pb"
    dest = tmp_path / "transient.pb"
    payload = b"model-bytes" * 512
    sleeps = _patch_sleep(monkeypatch)

    respx.get(url).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.ReadError("boom"),
            httpx.Response(200, content=payload),
        ]
    )

    _download_one(url, dest)

    assert dest.exists(), "file must download successfully on a later attempt"
    assert dest.read_bytes() == payload
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "no .part may remain after success"
    assert len(sleeps) == 2, "backoff must sleep once per failed attempt before success"


@respx.mock
def test_download_one_retries_5xx_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 then a 200 → 5xx is RETRIED (not failed fast); succeeds, sleeps once."""
    url = "https://example.test/server-error.pb"
    dest = tmp_path / "server-error.pb"
    payload = b"genre-weights" * 256
    sleeps = _patch_sleep(monkeypatch)

    respx.get(url).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, content=payload),
        ]
    )

    _download_one(url, dest)

    assert dest.exists(), "a 5xx must be retried, not fail fast"
    assert dest.read_bytes() == payload
    assert len(sleeps) == 1, "exactly one backoff between the 503 and the successful 200"


@respx.mock
def test_download_one_raises_after_exhausting_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient error on every attempt → RuntimeError naming the file after _MAX_ATTEMPTS calls."""
    url = "https://example.test/unreachable.pb"
    dest = tmp_path / "unreachable.pb"
    _patch_sleep(monkeypatch)

    route = respx.get(url).mock(side_effect=httpx.ConnectTimeout("unreachable"))

    with pytest.raises(RuntimeError, match=r"unreachable\.pb") as excinfo:
        _download_one(url, dest)

    assert f"{download_models._MAX_ATTEMPTS} attempts" in str(excinfo.value)
    assert route.call_count == download_models._MAX_ATTEMPTS, "must try exactly _MAX_ATTEMPTS times"
    assert not dest.exists(), "an exhausted download must NOT promote a dest file"
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "no .part may linger after exhaustion"


@respx.mock
def test_download_one_failed_attempt_leaves_no_truncated_dest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomicity: after exhausting retries, neither dest nor its .part exist."""
    url = "https://example.test/atomic.pb"
    dest = tmp_path / "atomic.pb"
    _patch_sleep(monkeypatch)

    respx.get(url).mock(side_effect=httpx.ReadError("connection reset mid-stream"))

    with pytest.raises(RuntimeError):
        _download_one(url, dest)

    assert not dest.exists(), "no truncated dest may be promoted"
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "the .part scratch file must be cleaned up"


@respx.mock
def test_download_one_retries_truncated_read_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short read vs Content-Length is treated as retryable; a full later read wins."""
    url = "https://example.test/truncated.pb"
    dest = tmp_path / "truncated.pb"
    payload = b"complete-weights" * 64
    sleeps = _patch_sleep(monkeypatch)

    respx.get(url).mock(
        side_effect=[
            # Declares more bytes than it delivers -> truncated transfer -> retry.
            httpx.Response(200, headers={"Content-Length": str(len(payload) + 1000)}, content=b"short"),
            httpx.Response(200, content=payload),
        ]
    )

    _download_one(url, dest)

    assert dest.exists(), "a truncated transfer must be retried, then succeed"
    assert dest.read_bytes() == payload
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert len(sleeps) == 1, "exactly one backoff between the truncated read and the full one"


# --------------------------------------------------------------------------- #
# _head_content_length / _try_head_size                                       #
# --------------------------------------------------------------------------- #


@respx.mock
def test_head_content_length_returns_size(tmp_path: Path) -> None:
    """A HEAD 200 with Content-Length yields that integer size."""
    url = "https://example.test/sized.pb"
    respx.head(url).mock(return_value=httpx.Response(200, headers={"Content-Length": "4096"}))

    assert _head_content_length(url) == 4096


@respx.mock
def test_head_content_length_absent_header_returns_none(tmp_path: Path) -> None:
    """A HEAD 200 with no Content-Length yields None (unobtainable size)."""
    url = "https://example.test/no-length.pb"
    respx.head(url).mock(return_value=httpx.Response(200))

    assert _head_content_length(url) is None


@respx.mock
def test_head_timeout_retries_bounded_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (a): a HEAD that times out on every attempt retries a BOUNDED number of times.

    The HEAD route call_count MUST equal _MAX_ATTEMPTS — never an unbounded block —
    mirroring the GET bound proven by test_download_one_raises_after_exhausting_attempts.
    """
    url = "https://example.test/hang.pb"
    sleeps = _patch_sleep(monkeypatch)

    route = respx.head(url).mock(side_effect=httpx.ConnectTimeout("stalled TLS"))

    with pytest.raises(RuntimeError, match=r"HEAD .*hang\.pb") as excinfo:
        _head_content_length(url)

    assert f"{download_models._MAX_ATTEMPTS} attempts" in str(excinfo.value)
    assert route.call_count == download_models._MAX_ATTEMPTS, "HEAD retries must be bounded by _MAX_ATTEMPTS"
    assert len(sleeps) == download_models._MAX_ATTEMPTS - 1, "one backoff between each pair of attempts"


@respx.mock
def test_head_5xx_retried_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 HEAD then a 200 with Content-Length → retried, returns the size."""
    url = "https://example.test/head-5xx.pb"
    sleeps = _patch_sleep(monkeypatch)

    respx.head(url).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, headers={"Content-Length": "1234"}),
        ]
    )

    assert _head_content_length(url) == 1234
    assert len(sleeps) == 1


@respx.mock
def test_head_4xx_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx HEAD propagates HTTPStatusError without retrying (fail fast)."""
    url = "https://example.test/head-404.pb"
    _patch_sleep(monkeypatch)

    route = respx.head(url).mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        _head_content_length(url)

    assert route.call_count == 1, "a 4xx HEAD must fail fast without retrying"


@respx.mock
def test_try_head_size_degrades_to_none_on_exhausted_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HEAD that exhausts retries degrades to None (never crashes the caller)."""
    url = "https://example.test/degrade.pb"
    dest = tmp_path / "degrade.pb"
    _patch_sleep(monkeypatch)

    respx.head(url).mock(side_effect=httpx.ConnectTimeout("stalled"))

    assert _try_head_size(url, dest) is None


@respx.mock
def test_try_head_size_degrades_to_none_on_4xx(tmp_path: Path) -> None:
    """A 4xx HEAD degrades to None via _try_head_size rather than propagating."""
    url = "https://example.test/degrade-404.pb"
    dest = tmp_path / "degrade-404.pb"
    respx.head(url).mock(return_value=httpx.Response(404))

    assert _try_head_size(url, dest) is None


# --------------------------------------------------------------------------- #
# _ensure_present (validate-or-download decision)                             #
# --------------------------------------------------------------------------- #


@respx.mock
def test_ensure_present_keeps_valid_file_no_get(tmp_path: Path) -> None:
    """Valid-keep: on-disk size == HEAD Content-Length → keep, issue NO GET."""
    url = "https://example.test/valid.pb"
    dest = tmp_path / "valid.pb"
    payload = b"already-valid-bytes"
    dest.write_bytes(payload)

    respx.head(url).mock(return_value=httpx.Response(200, headers={"Content-Length": str(len(payload))}))
    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=b"REPLACED"))

    _ensure_present(url, dest)

    assert dest.read_bytes() == payload, "a valid file must be left untouched"
    assert get_route.call_count == 0, "a size-valid file must NOT trigger a GET"


@respx.mock
def test_ensure_present_redownloads_truncated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (b): a present-but-truncated file is removed and re-downloaded to full size."""
    url = "https://example.test/truncated-ondisk.pb"
    dest = tmp_path / "truncated-ondisk.pb"
    full_payload = b"the-complete-weight-payload" * 8
    dest.write_bytes(b"short")  # 5 bytes, != full length
    _patch_sleep(monkeypatch)

    respx.head(url).mock(return_value=httpx.Response(200, headers={"Content-Length": str(len(full_payload))}))
    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=full_payload))

    _ensure_present(url, dest)

    assert dest.read_bytes() == full_payload, "truncated file must be replaced by the full payload"
    assert dest.stat().st_size == len(full_payload)
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "no .part may remain"
    assert get_route.call_count == 1, "a truncated file must be re-fetched exactly once"


@respx.mock
def test_ensure_present_downloads_missing_file(tmp_path: Path) -> None:
    """Known size + file missing → download."""
    url = "https://example.test/missing-but-sized.pb"
    dest = tmp_path / "missing-but-sized.pb"
    payload = b"fresh-download"

    respx.head(url).mock(return_value=httpx.Response(200, headers={"Content-Length": str(len(payload))}))
    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=payload))

    _ensure_present(url, dest)

    assert dest.read_bytes() == payload
    assert get_route.call_count == 1


@respx.mock
def test_ensure_present_unobtainable_size_keeps_present_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unobtainable size + file present → keep it, WARN, issue NO GET."""
    import logging

    url = "https://example.test/no-length-present.pb"
    dest = tmp_path / "no-length-present.pb"
    payload = b"cannot-validate-this"
    dest.write_bytes(payload)

    respx.head(url).mock(return_value=httpx.Response(200))  # no Content-Length
    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=b"NOPE"))

    with caplog.at_level(logging.WARNING, logger="phaze.scripts.download_models"):
        _ensure_present(url, dest)

    assert dest.read_bytes() == payload, "an unvalidatable present file must be kept"
    assert get_route.call_count == 0, "no GET when keeping an unvalidatable present file"
    assert any("keeping existing file" in rec.getMessage() for rec in caplog.records)


@respx.mock
def test_ensure_present_unobtainable_size_downloads_missing_file(tmp_path: Path) -> None:
    """Unobtainable size + file absent → fall through to GET, which downloads."""
    url = "https://example.test/no-length-missing.pb"
    dest = tmp_path / "no-length-missing.pb"
    payload = b"downloaded-anyway"

    respx.head(url).mock(return_value=httpx.Response(200))  # no Content-Length
    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=payload))

    _ensure_present(url, dest)

    assert dest.read_bytes() == payload
    assert get_route.call_count == 1


# --------------------------------------------------------------------------- #
# download_to (orchestration over both model families)                        #
# --------------------------------------------------------------------------- #


@respx.mock
def test_download_to_fetches_classifier_and_genre_urls(tmp_path: Path) -> None:
    """`download_to` walks both model families: HEAD then GET each missing .pb + .json.

    This is the contract that `phaze.tasks._shared.model_bootstrap` depends on
    when it triggers a bulk download into an empty `/models` directory.
    """
    # Each 1-byte payload; HEAD advertises the matching Content-Length so the
    # missing files fall through to a GET.
    for model_path in CLASSIFIER_MODELS:
        respx.head(f"{_CLASSIFIER_BASE}/{model_path}.pb").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.head(f"{_CLASSIFIER_BASE}/{model_path}.json").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.get(f"{_CLASSIFIER_BASE}/{model_path}.pb").mock(return_value=httpx.Response(200, content=b"P"))
        respx.get(f"{_CLASSIFIER_BASE}/{model_path}.json").mock(return_value=httpx.Response(200, content=b"J"))
    for model in GENRE_MODELS:
        respx.head(f"{_GENRE_BASE}/{model}.pb").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.head(f"{_GENRE_BASE}/{model}.json").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.get(f"{_GENRE_BASE}/{model}.pb").mock(return_value=httpx.Response(200, content=b"P"))
        respx.get(f"{_GENRE_BASE}/{model}.json").mock(return_value=httpx.Response(200, content=b"J"))

    download_to(tmp_path)

    # Expect 2 files per model across both families. CLASSIFIER_MODELS uses
    # the trailing path segment as the filename (matches the prod helper's
    # `rsplit("/", 1)[-1]` logic).
    expected_classifier_basenames = {p.rsplit("/", 1)[-1] for p in CLASSIFIER_MODELS}
    for basename in expected_classifier_basenames:
        assert (tmp_path / f"{basename}.pb").exists(), f"missing {basename}.pb"
        assert (tmp_path / f"{basename}.json").exists(), f"missing {basename}.json"
    for model in GENRE_MODELS:
        assert (tmp_path / f"{model}.pb").exists(), f"missing genre {model}.pb"
        assert (tmp_path / f"{model}.json").exists(), f"missing genre {model}.json"


@respx.mock
def test_download_to_valid_set_issues_only_heads_no_get(tmp_path: Path) -> None:
    """Case (d): a fully valid, size-matched set issues only HEADs and ZERO GETs."""
    # Pre-seed every expected file with a 1-byte sentinel; HEAD advertises size 1.
    expected_classifier_basenames = {p.rsplit("/", 1)[-1] for p in CLASSIFIER_MODELS}
    for basename in expected_classifier_basenames:
        (tmp_path / f"{basename}.pb").write_bytes(b"X")
        (tmp_path / f"{basename}.json").write_bytes(b"X")
    for model in GENRE_MODELS:
        (tmp_path / f"{model}.pb").write_bytes(b"X")
        (tmp_path / f"{model}.json").write_bytes(b"X")

    get_routes = []
    for model_path in CLASSIFIER_MODELS:
        respx.head(f"{_CLASSIFIER_BASE}/{model_path}.pb").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.head(f"{_CLASSIFIER_BASE}/{model_path}.json").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        get_routes.append(respx.get(f"{_CLASSIFIER_BASE}/{model_path}.pb").mock(return_value=httpx.Response(200, content=b"NOPE")))
        get_routes.append(respx.get(f"{_CLASSIFIER_BASE}/{model_path}.json").mock(return_value=httpx.Response(200, content=b"NOPE")))
    for model in GENRE_MODELS:
        respx.head(f"{_GENRE_BASE}/{model}.pb").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        respx.head(f"{_GENRE_BASE}/{model}.json").mock(return_value=httpx.Response(200, headers={"Content-Length": "1"}))
        get_routes.append(respx.get(f"{_GENRE_BASE}/{model}.pb").mock(return_value=httpx.Response(200, content=b"NOPE")))
        get_routes.append(respx.get(f"{_GENRE_BASE}/{model}.json").mock(return_value=httpx.Response(200, content=b"NOPE")))

    download_to(tmp_path)

    assert all(route.call_count == 0 for route in get_routes), "a size-valid set must issue ZERO GETs"
    # Sentinels still in place: nothing was overwritten.
    for basename in expected_classifier_basenames:
        assert (tmp_path / f"{basename}.pb").read_bytes() == b"X"
