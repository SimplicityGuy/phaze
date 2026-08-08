"""Shared fixtures for the analyze-service tests.

Holds one thing: the seam that keeps the mocked-essentia tests exercising the path
production actually takes after phaze-5lop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

import phaze.services.analysis as analysis_mod


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@pytest.fixture(autouse=True)
def streaming_decode_under_mocked_essentia() -> Iterator[None]:
    """Route the phaze-5lop streaming fan-out through whatever ``analysis.es`` is mocked with.

    phaze-5lop replaced the per-window ``es.EasyLoader`` decode with ONE
    ``essentia.streaming`` fan-out network per tier. That network is real essentia and needs
    a real audio file, so under a mocked ``analysis.es`` (nothing exists at
    ``/fake/audio.mp3``) it can only raise, and every mocked test would silently drop into
    :func:`~phaze.services.analysis._decode_windows`'s per-window fallback -- leaving the
    suite testing the fallback everywhere and the shipped decode nowhere.

    So when -- and only when -- ``analysis.es`` is a ``MagicMock`` at the moment the decode
    runs, the streaming pass is stubbed with one that calls that same mock ``EasyLoader``
    once per window and isolates per-window failures the way the real pass does (a window
    the pass produced no audio for is simply absent from the returned mapping). Every mocked
    test therefore drives the streaming code path -- one call per tier, buffers keyed by
    window index -- while the buffers, the ``EasyLoader`` call arity and every assertion on
    them stay exactly what they were before phaze-5lop.

    The ``MagicMock`` check is made at CALL time, not fixture-setup time, precisely because
    ``@patch`` decorators are applied after fixtures are set up -- at setup ``analysis.es``
    is still the real module even in a test that mocks it.

    A test running against real essentia and a real audio fixture (e.g.
    ``test_real_decode_short_no_overflow``) is left completely alone: it gets the real
    fan-out, which is the point of it. The fallback keeps its own dedicated coverage in
    ``test_analysis_streaming_decode.py``.
    """
    real_streaming_decode = analysis_mod._decode_windows_streaming

    def _stub(file_path: str, sample_rate: int, windows: Sequence[tuple[int, float, float]]) -> dict[int, Any]:
        if not isinstance(analysis_mod.es, MagicMock):
            return real_streaming_decode(file_path, sample_rate, windows)  # real essentia: real fan-out
        decoded: dict[int, Any] = {}
        for idx, start, end in windows:
            try:
                decoded[idx] = analysis_mod.es.EasyLoader(filename=file_path, sampleRate=sample_rate, startTime=start, endTime=end)()
            except Exception:  # noqa: S112 -- stands in for "the pass produced no audio for this window"
                continue
        return decoded

    with patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_stub):
        yield
