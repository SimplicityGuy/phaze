"""Tests for `phaze.scripts.download_models` (Phase 29 D-21).

Covers the previously-untested branches of `_download_one` and `download_to`:
- `_download_one` skips when `dest` already exists (idempotent fast-path)
- `_download_one` streams to `<dest>.part` and atomically renames on success
- `_download_one` raises (and leaves the `.part` behind for the bootstrap
  caller to reject — see `phaze.tasks._shared.model_bootstrap`)
- `download_to` walks both CLASSIFIER_MODELS and GENRE_MODELS, requesting
  `.pb` + `.json` per model under the documented Essentia URL bases

Uses `respx` (already a dev dep — see `pyproject.toml`) to intercept
`httpx.stream`. No real network I/O is performed.
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
    download_to,
)


if TYPE_CHECKING:
    from pathlib import Path


_CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
_GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"


@respx.mock
def test_download_one_skips_when_dest_exists(tmp_path: Path) -> None:
    """Idempotent fast-path: an existing file returns immediately without HTTP I/O."""
    dest = tmp_path / "already-here.pb"
    dest.write_bytes(b"pre-existing")
    # No respx route registered — any HTTP call would 1) fail at network or
    # 2) trip respx's strict "unhandled request" mode. Either way the test
    # would fail if `_download_one` made a request.

    _download_one("https://example.invalid/should-not-be-fetched.pb", dest)

    assert dest.read_bytes() == b"pre-existing", "existing file must be left untouched"


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
    # Fail-fast: a 4xx is NOT in the retry tuple, so the route is hit exactly once
    # even though a retry loop now exists (260608-i21).
    assert route.call_count == 1, "a 4xx must fail fast without retrying"


@respx.mock
def test_download_to_fetches_classifier_and_genre_urls(tmp_path: Path) -> None:
    """`download_to` walks both model families and requests .pb + .json per model.

    This is the contract that `phaze.tasks._shared.model_bootstrap` depends on
    when it triggers a bulk download into an empty `/models` directory.
    """
    # Mock every classifier .pb + .json with a 1-byte payload.
    for model_path in CLASSIFIER_MODELS:
        respx.get(f"{_CLASSIFIER_BASE}/{model_path}.pb").mock(return_value=httpx.Response(200, content=b"P"))
        respx.get(f"{_CLASSIFIER_BASE}/{model_path}.json").mock(return_value=httpx.Response(200, content=b"J"))
    # Same for genre.
    for model in GENRE_MODELS:
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
def test_download_to_is_idempotent_on_already_populated_dir(tmp_path: Path) -> None:
    """Re-running `download_to` against a full models dir is a no-op (no HTTP)."""
    # Pre-seed every expected file with a sentinel byte so `_download_one`
    # takes the existence-skip branch for all of them.
    expected_classifier_basenames = {p.rsplit("/", 1)[-1] for p in CLASSIFIER_MODELS}
    for basename in expected_classifier_basenames:
        (tmp_path / f"{basename}.pb").write_bytes(b"X")
        (tmp_path / f"{basename}.json").write_bytes(b"X")
    for model in GENRE_MODELS:
        (tmp_path / f"{model}.pb").write_bytes(b"X")
        (tmp_path / f"{model}.json").write_bytes(b"X")

    # No respx routes registered — if `_download_one` reached the network for
    # any file the call would raise (respx is in strict mode by default).
    download_to(tmp_path)

    # Sentinels still in place: nothing was overwritten.
    for basename in expected_classifier_basenames:
        assert (tmp_path / f"{basename}.pb").read_bytes() == b"X"


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``download_models.time.sleep`` with a no-op counter and return it.

    Keeps retries observable (one append per backoff) while ensuring tests never
    incur a real delay or real network I/O.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(download_models.time, "sleep", lambda delay: sleeps.append(delay))
    return sleeps


@respx.mock
def test_download_one_retries_transient_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two transient transport errors then a 200 → file lands, no .part, sleeps twice."""
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
