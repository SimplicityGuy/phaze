"""The essentia-free half of phaze-l5mzm: patch layout, the result carrier and the float16 codec.

The real-graph half (the fused classifier against the wrapper, and its memory) is
``tests/analyze/services/pipeline/test_analysis_embeddings_real_essentia.py``.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from phaze.services.analysis_embeddings import (
    BATCH,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_OUTPUT,
    EMBEDDING_WIRE_BYTES,
    EMBEDDING_WIRE_FORMAT,
    MEL_BANDS,
    PATCH_FRAMES,
    PATCH_HOP,
    EffnetActivations,
    count_patches,
    decode_embedding,
    embedding_of,
    embeddings_payload,
    encode_embedding,
    patch_batches,
    with_embedding,
)


def _vector(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(EMBEDDING_DIM).astype(np.float32)


@pytest.mark.parametrize(
    ("n_frames", "expected"),
    [(0, 0), (127, 0), (128, 1), (189, 1), (190, 2), (128 + 62 * 63, 64), (128 + 62 * 64, 65)],
)
def test_count_patches_matches_the_wrappers_layout(n_frames: int, expected: int) -> None:
    """128-frame patches at hop 62, trailing partial patch discarded (the real-graph test pins the same counts)."""
    assert count_patches(n_frames) == expected


def test_patch_batches_lays_out_patches_and_zero_pads_only_the_last_batch() -> None:
    n_patches = BATCH + 5
    n_frames = PATCH_FRAMES + PATCH_HOP * (n_patches - 1)
    bands = np.arange(n_frames * MEL_BANDS, dtype=np.float32).reshape(n_frames, MEL_BANDS)

    batches = list(patch_batches(bands))

    assert [real for _batch, real in batches] == [BATCH, 5]
    for batch, _real in batches:
        assert batch.shape == (BATCH, 1, PATCH_FRAMES, MEL_BANDS)
        assert batch.dtype == np.float32
        assert batch.flags.c_contiguous
    first, last = batches[0][0], batches[1][0]
    np.testing.assert_array_equal(first[0, 0], bands[0:PATCH_FRAMES])
    np.testing.assert_array_equal(first[1, 0], bands[PATCH_HOP : PATCH_HOP + PATCH_FRAMES])
    final_start = PATCH_HOP * (n_patches - 1)
    np.testing.assert_array_equal(last[4, 0], bands[final_start : final_start + PATCH_FRAMES])
    assert not last[5:].any(), "rows past the real patches must be zeros, whose outputs are dropped"


def test_patch_batches_yields_nothing_under_one_patch() -> None:
    assert list(patch_batches(np.zeros((PATCH_FRAMES - 1, MEL_BANDS), dtype=np.float32))) == []


def test_the_embedding_rides_on_the_logits_and_not_on_their_mean() -> None:
    """_predict_single takes the mean of the logits: the mean is a plain vector, the matrix keeps the embedding."""
    logits = np.ones((3, 400), dtype=np.float32)
    embedding = _vector()
    carried = with_embedding(logits, embedding)

    assert isinstance(carried, EffnetActivations)
    assert embedding_of(carried) is embedding
    assert embedding_of(np.mean(carried, axis=0)) is None
    assert embedding_of(carried[1:]) is None
    assert embedding_of(np.atleast_2d(carried)) is embedding, "_predict_single's atleast_2d must not drop it"
    assert embedding_of(logits) is None
    assert embedding_of(None) is None


def test_encode_is_little_endian_float16_of_fixed_length_and_round_trips() -> None:
    vector = _vector()
    encoded = encode_embedding(vector)

    raw = base64.b64decode(encoded)
    assert len(raw) == EMBEDDING_WIRE_BYTES == 2560
    np.testing.assert_array_equal(np.frombuffer(raw, dtype="<f2"), vector.astype("<f2"))
    decoded = decode_embedding(encoded)
    assert decoded.dtype == np.float16
    assert decoded.shape == (EMBEDDING_DIM,)
    np.testing.assert_array_equal(decoded, vector.astype(np.float16))


@pytest.mark.parametrize("bad", [np.zeros(EMBEDDING_DIM - 1), np.zeros((2, EMBEDDING_DIM))])
def test_encode_refuses_a_wrong_shape(bad: np.ndarray) -> None:
    with pytest.raises(ValueError, match="shape"):
        encode_embedding(bad)


@pytest.mark.parametrize("value", [np.nan, np.inf, 1e6])
def test_encode_refuses_a_value_float16_cannot_hold(value: float) -> None:
    vector = _vector()
    vector[7] = value
    with pytest.raises(ValueError, match="float16"):
        encode_embedding(vector)


def test_decode_refuses_bad_base64_and_a_wrong_length() -> None:
    with pytest.raises(ValueError, match="base64"):
        decode_embedding("not base64!")
    with pytest.raises(ValueError, match="2560 bytes"):
        decode_embedding(base64.b64encode(b"\x00" * 10).decode())


def test_payload_names_the_model_and_lists_windows_in_index_order() -> None:
    vectors = {4: _vector(4), 0: _vector(0), 2: _vector(2)}

    payload = embeddings_payload(vectors)

    assert payload["model"] == EMBEDDING_MODEL == "discogs-effnet-bs64-1"
    assert payload["output"] == EMBEDDING_OUTPUT == "PartitionedCall:1"
    assert payload["dim"] == 1280
    assert payload["format"] == EMBEDDING_WIRE_FORMAT
    assert [row["window_index"] for row in payload["windows"]] == [0, 2, 4]
    for row in payload["windows"]:
        np.testing.assert_array_equal(decode_embedding(row["vector"]), vectors[row["window_index"]].astype(np.float16))


def test_payload_of_no_windows_is_an_empty_list_not_an_absent_key() -> None:
    assert embeddings_payload({})["windows"] == []
