"""Tests for `phaze.scripts.download_models` (Phase 29 D-21 / 260608-i21 / 260608-u8g).

Covers the local-validation + repair behaviour:
- `_download_one` streams to `<dest>.part` and atomically renames on success
- `_download_one` fails fast on 4xx and retries transient/5xx/truncated reads
- `_with_retries` is the single bounded-retry implementation behind the repair GET
- `MANIFEST` is built programmatically from the model tuples and covers exactly 68
  files with the authoritative byte sizes
- `_ensure_present_local` compares the on-disk byte size against the baked-in
  manifest size: keeps a correct-size file (zero network), re-downloads a
  missing or wrong-size one
- `download_to` walks both CLASSIFIER_MODELS and GENRE_MODELS; a fully valid set
  issues ZERO HTTP requests, and only a missing/mismatched file is GET-repaired

Uses `respx` (already a dev dep — see `pyproject.toml`) to intercept GET.
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
    MANIFEST,
    _download_one,
    _ensure_present_local,
    download_to,
)


if TYPE_CHECKING:
    from collections.abc import Iterator
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
# _download_one (repair path; the size-based skip lives in _ensure_present_local)
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
# MANIFEST (programmatically derived from the model tuples; 260608-u8g)        #
# --------------------------------------------------------------------------- #


def test_manifest_covers_exactly_68_files() -> None:
    """MANIFEST has exactly (classifier + genre) * 2 == 68 entries with the spot sizes."""
    assert len(MANIFEST) == len(CLASSIFIER_MODELS) * 2 + len(GENRE_MODELS) * 2 == 68

    # .pb spot sizes: common musicnn, the lone msd-1 outlier, vggish, discogs effnet.
    assert MANIFEST["mood_acoustic-musicnn-msd-2.pb"] == 3239548
    assert MANIFEST["voice_instrumental-musicnn-msd-1.pb"] == 3239625
    assert MANIFEST["danceability-vggish-audioset-1.pb"] == 288629030
    assert MANIFEST["discogs-effnet-bs64-1.pb"] == 18366619

    # .json spot sizes.
    assert MANIFEST["discogs-effnet-bs64-1.json"] == 14990
    assert MANIFEST["mood_acoustic-musicnn-msd-2.json"] == 3078

    # Every entry is a .pb or .json keyed by a known stem.
    assert all(name.endswith((".pb", ".json")) for name in MANIFEST)


# --------------------------------------------------------------------------- #
# _ensure_present_local (local-validation decision; 260608-u8g)               #
# --------------------------------------------------------------------------- #


@respx.mock
def test_ensure_present_local_keeps_correct_size_file_zero_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct-size file -> pure os.stat, ZERO httpx.stream call."""
    url = "https://example.test/valid.pb"
    dest = tmp_path / "valid.pb"
    payload = b"already-valid-bytes"
    dest.write_bytes(payload)

    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("a correct-size file must NOT trigger any network call")

    monkeypatch.setattr(download_models.httpx, "stream", _boom)
    monkeypatch.setattr(download_models.httpx, "head", _boom)

    _ensure_present_local(url, dest, len(payload))

    assert dest.read_bytes() == payload, "a correct-size file must be left untouched"


@respx.mock
def test_ensure_present_local_downloads_missing_file(tmp_path: Path) -> None:
    """Missing file -> exactly one GET that writes the payload."""
    url = "https://example.test/missing-but-sized.pb"
    dest = tmp_path / "missing-but-sized.pb"
    payload = b"fresh-download"

    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=payload))

    _ensure_present_local(url, dest, len(payload))

    assert dest.read_bytes() == payload
    assert get_route.call_count == 1


@respx.mock
def test_ensure_present_local_redownloads_wrong_size_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Present-but-wrong-size file is re-downloaded to the manifest size."""
    url = "https://example.test/wrong-size.pb"
    dest = tmp_path / "wrong-size.pb"
    full_payload = b"the-complete-weight-payload" * 8
    dest.write_bytes(b"short")  # 5 bytes, != expected
    _patch_sleep(monkeypatch)

    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=full_payload))

    _ensure_present_local(url, dest, len(full_payload))

    assert dest.read_bytes() == full_payload, "wrong-size file must be replaced by the full payload"
    assert dest.stat().st_size == len(full_payload)
    assert not dest.with_suffix(dest.suffix + ".part").exists(), "no .part may remain"
    assert get_route.call_count == 1, "a wrong-size file must be re-fetched exactly once"


@respx.mock
def test_ensure_present_local_repair_wrong_size_raises_and_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-10ij: a repair download whose size violates the manifest must FAIL, not be blessed.

    The server responds with a self-consistent body (its own Content-Length matches
    the bytes streamed, so ``_download_one`` promotes it) whose size disagrees with
    the pinned manifest — the upstream-re-publish / proxy-error-page scenario. The
    repair path must re-validate against ``expected_size``, delete the mismatched
    file, and raise a per-file RuntimeError naming both sizes.
    """
    url = "https://example.test/republished.pb"
    dest = tmp_path / "republished.pb"
    dest.write_bytes(b"stale")  # wrong size -> triggers the repair path
    _patch_sleep(monkeypatch)
    wrong_payload = b"republished-weights-with-a-new-size" * 4
    expected_size = len(wrong_payload) + 12345  # manifest disagrees with what the server now serves

    get_route = respx.get(url).mock(return_value=httpx.Response(200, content=wrong_payload))

    with pytest.raises(RuntimeError, match=r"republished\.pb") as excinfo:
        _ensure_present_local(url, dest, expected_size)

    assert str(len(wrong_payload)) in str(excinfo.value), "the on-disk size must be named"
    assert str(expected_size) in str(excinfo.value), "the manifest size must be named"
    assert get_route.call_count == 1, "a manifest violation is not retryable; the route is hit once"
    assert not dest.exists(), "the manifest-violating file must be deleted, never left for essentia to load"


@respx.mock
def test_ensure_present_local_repair_missing_file_wrong_size_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-10ij: the missing-file repair path enforces the same manifest re-validation."""
    url = "https://example.test/fresh-wrong.pb"
    dest = tmp_path / "fresh-wrong.pb"
    _patch_sleep(monkeypatch)
    wrong_payload = b"error page pretending to be a weight file"

    respx.get(url).mock(return_value=httpx.Response(200, content=wrong_payload))

    with pytest.raises(RuntimeError, match=r"fresh-wrong\.pb"):
        _ensure_present_local(url, dest, len(wrong_payload) + 1)

    assert not dest.exists(), "a manifest-violating fresh download must not be left in place"


@respx.mock
def test_ensure_present_local_repair_chunked_wrong_size_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-10ij: a chunked response (no Content-Length) is still caught by the manifest check.

    ``_download_one`` validates nothing on a Content-Length-absent response, so the
    post-repair manifest comparison is the ONLY integrity gate on this path.
    """
    url = "https://example.test/chunked.pb"
    dest = tmp_path / "chunked.pb"
    _patch_sleep(monkeypatch)

    def _chunks() -> Iterator[bytes]:
        yield b"partial-chunked-body"

    # Iterator content -> Transfer-Encoding: chunked, no Content-Length header.
    respx.get(url).mock(return_value=httpx.Response(200, content=_chunks()))

    with pytest.raises(RuntimeError, match=r"chunked\.pb"):
        _ensure_present_local(url, dest, 999_999)

    assert not dest.exists()


# --------------------------------------------------------------------------- #
# download_to (orchestration over both model families)                        #
# --------------------------------------------------------------------------- #


def _expected_filenames() -> set[str]:
    """All 68 expected on-disk filenames across both model families."""
    names: set[str] = set()
    for model_path in CLASSIFIER_MODELS:
        stem = model_path.rsplit("/", 1)[-1]
        names.add(f"{stem}.pb")
        names.add(f"{stem}.json")
    for model in GENRE_MODELS:
        names.add(f"{model}.pb")
        names.add(f"{model}.json")
    return names


@respx.mock
def test_download_to_valid_set_issues_zero_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully valid, size-matched set issues ZERO HTTP requests (no HEAD, no GET)."""
    # Patch MANIFEST so every expected file is "correct" at 1 byte.
    patched = dict.fromkeys(_expected_filenames(), 1)
    monkeypatch.setattr(download_models, "MANIFEST", patched)

    # Pre-seed every expected file with a 1-byte sentinel.
    for name in patched:
        (tmp_path / name).write_bytes(b"X")

    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("a size-valid set must issue ZERO network requests")

    monkeypatch.setattr(download_models.httpx, "head", _boom)
    monkeypatch.setattr(download_models.httpx, "stream", _boom)

    download_to(tmp_path)

    # Sentinels still in place: nothing was overwritten.
    for name in patched:
        assert (tmp_path / name).read_bytes() == b"X"


@respx.mock
def test_download_to_repairs_only_the_missing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the one missing file is GET-repaired; all others stay untouched."""
    patched = dict.fromkeys(_expected_filenames(), 1)
    monkeypatch.setattr(download_models, "MANIFEST", patched)

    # Seed all-but-one expected file; pick the genre .pb as the missing one.
    missing_name = f"{GENRE_MODELS[0]}.pb"
    for name in patched:
        if name != missing_name:
            (tmp_path / name).write_bytes(b"X")

    missing_url = f"{_GENRE_BASE}/{GENRE_MODELS[0]}.pb"
    get_route = respx.get(missing_url).mock(return_value=httpx.Response(200, content=b"P"))

    download_to(tmp_path)

    assert get_route.call_count == 1, "exactly one GET for the single missing file"
    assert (tmp_path / missing_name).exists(), "the missing file must now exist"
    assert (tmp_path / missing_name).read_bytes() == b"P"
