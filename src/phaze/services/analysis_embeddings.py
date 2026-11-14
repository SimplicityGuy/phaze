"""Per-window Discogs-EffNet embeddings, read in the SAME TensorFlow session as the genre logits (phaze-l5mzm).

The genre graph (``discogs-effnet-bs64-1``) has two outputs: ``PartitionedCall:0``, the 400 genre
activations phaze has always read, and ``PartitionedCall:1``, a 1280-d ``Flatten`` that sits before the
classification head (spike phaze-webi9, finding E1). essentia's
``TensorflowPredictEffnetDiscogs`` wrapper takes ONE output name, so reading both through it costs a second
full pass (phaze-webi9 E3: 1.9x the genre cost). ``FusedEffnetDiscogs`` runs the wrapper's own front end
(mel bands, patches, fixed 64-patch batches) by hand and asks the low-level ``TensorflowPredict`` for
both outputs, so one session yields both. The class lives in :mod:`phaze.services.analysis`, which owns
every essentia import (TensorFlow must not load before that module stamps its thread env); this module
imports no essentia and holds the patch layout, the result carrier and the float16 codec.

**Equivalence, measured on the pinned wheel (essentia-tensorflow 2.1b6.dev1438, arm64 Darwin, 2026-11-13):**
for both outputs the matrices are ``np.array_equal`` to the wrapper's (max abs diff 0.0) at 2.1, 2.5, 4.0,
4.1, 7, 30, 63.9, 64.5, 180 and 200.5 s of seeded noise and on a 180 s tone mix, which covers the
one-patch case (phaze-ouz0y), a single partial batch, and partial batches just under and over 64 patches.
At 2.0 s and below the wrapper yields no patch and returns a bare ``(0,)`` array; this class returns the
same. Cost per 180 s window: 2.86 to 3.02 cpu-s, against 2.79 to 2.86 for one wrapper pass and 5.68 to
6.07 for two (three windows x three repetitions, two interleaved rounds, load average 9 to 14). So the
embedding costs nothing measurable, not a second pass. ``tests/analyze/services/pipeline/
test_analysis_embeddings_real_essentia.py`` holds the equality on the real graph; it cannot be satisfied
by a mocked essentia.

**Why phaze-webi9 could not run this route, and what made it work.** That spike fed ``TensorflowPredict`` an
unpadded ``[1, N, 128, 96]`` (or ``[N, 1, 128, 96]``) tensor and the graph's ``Pad`` op rejected the
rank. The graph's placeholder is fixed at ``[64, 128, 96]``; ``TensorflowPredict``'s ``squeeze``
(default on) drops the singleton channel axis of a ``[64, 1, 128, 96]`` tensor, which is exactly the
shape the wrapper's own ``VectorRealToTensor`` builds. Zero-padding the last batch to 64 and dropping
the padded rows is the wrapper's ``lastBatchMode="same"``. A streaming-network version of the same
pipeline also matches bit for bit but is about 3.5x slower, because the Python ``VectorInput`` streams
one sample per token; it is not used.

**Memory (D-07 / D-09).** Only standard-mode algorithms are built, so there is no streaming network to
disconnect. What a call holds transiently is the window's mel bands (11,251 x 96 float32, 4.3 MB for a
180 s window), ONE 64-patch batch (3.1 MB) and the per-patch embedding rows (0.9 MB); the patch matrix
is a strided view of the bands, never materialised. What outlives the call is the logits matrix (as the wrapper's did) and ONE pooled 1280-d
vector (5,120 B as float32). The pooled vector is the mean over patches in float32, as
``_predict_single`` pools the logits, and is narrowed to float16 only for transport and storage.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np

from phaze.telemetry import instruments as telemetry


if TYPE_CHECKING:
    from collections.abc import Iterator


log = logging.getLogger(__name__)

EmbeddingStage = Literal["inference", "encode", "assembly"]

#: Where an embedding can be dropped, in pipeline order -- the ``embedding_stage`` label's whole domain.
EMBEDDING_STAGES: Final[tuple[EmbeddingStage, ...]] = ("inference", "encode", "assembly")


# The graph's contract, from its own JSON sidecar (phaze-webi9 E1).
EMBEDDING_MODEL = "discogs-effnet-bs64-1"
INPUT_NODE = "serving_default_melspectrogram"
LOGITS_OUTPUT = "PartitionedCall:0"
EMBEDDING_OUTPUT = "PartitionedCall:1"
EMBEDDING_DIM = 1280
LOGITS_DIM = 400

# What crosses a process boundary or reaches storage: little-endian float16, fixed length. This is the
# byte layout spike phaze-cmsr0 recommends for the bytea column (2,560 B).
EMBEDDING_WIRE_DTYPE = np.dtype("<f2")
EMBEDDING_WIRE_BYTES = EMBEDDING_DIM * EMBEDDING_WIRE_DTYPE.itemsize
EMBEDDING_WIRE_FORMAT = "float16le-base64"

# TensorflowPredictEffnetDiscogs' front end, hard-coded in essentia to the model's training setup.
# Changing any of these breaks bit-equality with the wrapper; the real-essentia test catches it.
FRAME_SIZE = 512
FRAME_HOP = 256
PATCH_FRAMES = 128
PATCH_HOP = 62  # the wrapper's default patchHopSize (1.008 predictions per second)
MEL_BANDS = 96
BATCH = 64  # the graph's placeholder batch is fixed (analysis_models._FIXED_BATCH_SIZE)


class EffnetActivations(np.ndarray):
    """The ``[patches, 400]`` genre logits matrix, carrying the window's pooled embedding.

    The fused classifier stands in for the wrapper inside ``_predict_single``, which pools the logits
    and has never known about a second output. Making the logits matrix carry the pooled embedding
    keeps that contract and every test double of it unchanged: a classifier that returns a bare array
    simply has no ``embedding``, and its window gets none.
    """

    embedding: Any = None

    def __array_finalize__(self, obj: Any) -> None:
        # Slices, copies and ufunc results (the mean included) are plain logits: only the original carries it.
        self.embedding = None


def embedding_of(activations: Any) -> np.ndarray | None:
    """The pooled float32 embedding a fused call attached to ``activations``, or ``None``."""
    embedding = getattr(activations, "embedding", None)
    return embedding if isinstance(embedding, np.ndarray) else None


def patch_batches(bands: np.ndarray) -> Iterator[tuple[np.ndarray, int]]:
    """The wrapper's patch layout over ``[frames, 96]`` mel bands, as zero-padded 64-patch input batches.

    Yields ``(batch, real)``: ``batch`` is a fresh ``[64, 1, 128, 96]`` float32 tensor (the shape the
    wrapper's own ``VectorRealToTensor`` builds) and ``real`` is how many of its leading rows are real
    patches; the rest are zeros, whose outputs the caller drops (the wrapper's ``lastBatchMode="same"``).
    Patches are 128 frames at a hop of 62 and a trailing partial patch is discarded (``lastPatchMode``
    ``"discard"``), so a buffer of under 128 frames yields nothing. The patches are read from a strided
    view of ``bands`` and only one batch is ever materialised.
    """
    n_patches = count_patches(len(bands))
    if n_patches == 0:
        return
    patches = np.lib.stride_tricks.sliding_window_view(bands, PATCH_FRAMES, axis=0)[::PATCH_HOP][:n_patches].transpose(0, 2, 1)
    for start in range(0, n_patches, BATCH):
        real = min(BATCH, n_patches - start)
        batch = np.zeros((BATCH, 1, PATCH_FRAMES, MEL_BANDS), dtype=np.float32)
        batch[:real, 0] = patches[start : start + real]
        yield batch, real


def count_patches(n_frames: int) -> int:
    """How many whole 128-frame patches at hop 62 fit in ``n_frames`` mel frames."""
    return 0 if n_frames < PATCH_FRAMES else (n_frames - PATCH_FRAMES) // PATCH_HOP + 1


def with_embedding(logits: np.ndarray, embedding: np.ndarray) -> EffnetActivations:
    """``logits`` as an :class:`EffnetActivations` carrying ``embedding``."""
    out = logits.view(EffnetActivations)
    out.embedding = embedding
    return out


def encode_embedding(embedding: Any) -> str:
    """One pooled embedding as base64 of its little-endian float16 bytes (2,560 B before encoding).

    Raises ``ValueError`` on a wrong dimension or a value float16 cannot hold: a vector that does not
    round-trip is corrupt and must not be stored as if it were a measurement.
    """
    vector = np.asarray(embedding, dtype=np.float32)
    if vector.shape != (EMBEDDING_DIM,):
        msg = f"embedding must have shape ({EMBEDDING_DIM},), got {vector.shape}"
        raise ValueError(msg)
    narrowed = vector.astype(EMBEDDING_WIRE_DTYPE)
    if not np.isfinite(narrowed).all():
        msg = "embedding holds a value float16 cannot represent"
        raise ValueError(msg)
    return base64.b64encode(narrowed.tobytes()).decode("ascii")


def decode_embedding(encoded: str) -> np.ndarray:
    """The inverse of :func:`encode_embedding`: a ``(1280,)`` float16 array."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "embedding is not valid base64"
        raise ValueError(msg) from exc
    if len(raw) != EMBEDDING_WIRE_BYTES:
        msg = f"embedding must be {EMBEDDING_WIRE_BYTES} bytes, got {len(raw)}"
        raise ValueError(msg)
    return np.frombuffer(raw, dtype=EMBEDDING_WIRE_DTYPE).copy()


def embeddings_payload(encoded_by_window: dict[int, str]) -> dict[str, Any]:
    """The ``embeddings`` value of an analysis result: model identity plus one vector per coarse window.

    ``encoded_by_window`` maps a coarse window index to that window's :func:`encode_embedding` string,
    encoded (and so validated) once, per window, when the genre sweep produced it. Windows are listed in
    index order. A window with no embedding (silent, killed by a model sweep, under one patch long, or
    dropped) is absent, never a zero vector.
    """
    return {
        "model": EMBEDDING_MODEL,
        "output": EMBEDDING_OUTPUT,
        "dim": EMBEDDING_DIM,
        "format": EMBEDDING_WIRE_FORMAT,
        "windows": [{"window_index": idx, "vector": encoded_by_window[idx]} for idx in sorted(encoded_by_window)],
    }


def record_embedding_drop(stage: EmbeddingStage, what: str, *, exc_info: bool = False) -> None:
    """Log and count one embedding dropped at ``stage``, where it was dropped instead of failing the analysis.

    An embedding is secondary data: a failure on its path costs that window (or, at assembly, that file)
    its embedding and nothing else. ``inference`` is a window whose fused genre call raised, so the window
    was re-swept on the plain genre classifier and keeps its genre; ``encode`` is a pooled vector float16
    cannot hold; ``assembly`` is the file's whole ``embeddings`` key. Call it from INSIDE the ``except``
    block, so ``exc_info`` renders the live traceback and the exception is never retained.
    """
    log.warning("embedding dropped at %s: %s; the core analysis is unaffected", stage, what, exc_info=exc_info)
    telemetry.add("phaze.analysis.embeddings.dropped", 1, embedding_stage=stage)
