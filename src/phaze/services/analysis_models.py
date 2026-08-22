"""Analysis model registry and TensorFlow inference batch-size policy.

Extracted VERBATIM from ``services/analysis.py`` (phaze-bk9el.15) -- declarations and
one pure env-parsing function, with no essentia import and no dependency on the analysis
pipeline. Nothing here touches D-07 (chunk-bounded exhaustive analysis), D-08
(progress-based liveness) or D-09 (the streaming network must be DISCONNECTED, not
dropped); those surfaces stay whole in ``analysis.py``.

``services/analysis.py`` re-exports every name below, so ``phaze.services.analysis`` stays
the import site for dependents and tests and its callers keep resolving them through THAT
module's globals -- which is what keeps ``monkeypatch.setattr(analysis, ...)`` reaching the
pipeline's own call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type definitions for model registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for a single ML model file."""

    name: str  # e.g., "mood_acoustic"
    variant: str  # e.g., "musicnn_msd", "musicnn_mtt", "vggish"
    filename: str  # e.g., "mood_acoustic-musicnn-msd-2" (no extension)
    classifier_type: str  # "musicnn", "vggish", "effnet_discogs"


@dataclass(frozen=True)
class ModelSetConfig:
    """A set of model variants for one characteristic."""

    name: str
    models: tuple[ModelConfig, ...]


# ---------------------------------------------------------------------------
# Model registry: 11 characteristic model sets (33 models) per D-02
# ---------------------------------------------------------------------------


def _make_standard_set(name: str, filename_prefix: str) -> ModelSetConfig:
    """Create a model set with the standard 3 variants (musicnn_msd-2, musicnn_mtt-2, vggish-1)."""
    return ModelSetConfig(
        name=name,
        models=(
            ModelConfig(name=name, variant="musicnn_msd", filename=f"{filename_prefix}-musicnn-msd-2", classifier_type="musicnn"),
            ModelConfig(name=name, variant="musicnn_mtt", filename=f"{filename_prefix}-musicnn-mtt-2", classifier_type="musicnn"),
            ModelConfig(name=name, variant="vggish", filename=f"{filename_prefix}-vggish-audioset-1", classifier_type="vggish"),
        ),
    )


MODEL_SETS: tuple[ModelSetConfig, ...] = (
    _make_standard_set("mood_acoustic", "mood_acoustic"),
    _make_standard_set("mood_electronic", "mood_electronic"),
    _make_standard_set("mood_aggressive", "mood_aggressive"),
    _make_standard_set("mood_relaxed", "mood_relaxed"),
    _make_standard_set("mood_happy", "mood_happy"),
    _make_standard_set("mood_sad", "mood_sad"),
    _make_standard_set("mood_party", "mood_party"),
    _make_standard_set("danceability", "danceability"),
    _make_standard_set("gender", "gender"),
    _make_standard_set("tonality", "tonal_atonal"),
    # voice_instrumental uses musicnn-msd-1 (not -2), per prototype
    ModelSetConfig(
        name="voice_instrumental",
        models=(
            ModelConfig(name="voice_instrumental", variant="musicnn_msd", filename="voice_instrumental-musicnn-msd-1", classifier_type="musicnn"),
            ModelConfig(name="voice_instrumental", variant="musicnn_mtt", filename="voice_instrumental-musicnn-mtt-2", classifier_type="musicnn"),
            ModelConfig(name="voice_instrumental", variant="vggish", filename="voice_instrumental-vggish-audioset-1", classifier_type="vggish"),
        ),
    ),
)

GENRE_MODEL = ModelConfig(
    name="discogs_genre",
    variant="effnet",
    filename="discogs-effnet-bs64-1",
    classifier_type="effnet_discogs",
)


# ---------------------------------------------------------------------------
# TensorFlow inference batch size (phaze-0582)
# ---------------------------------------------------------------------------

# `TensorflowPredict*` batches patches before feeding the graph, and its `batchSize`
# DEFAULTS to 64. phaze passed no override until phaze-0582, so every inference stood up
# a `[64, patch, bands]` input and -- far more expensively -- 64x the intermediate
# activations of a musicnn / VGGish / EfficientNet forward pass. Spike phaze-mqq5 measured
# that default as the dominant remaining term in the analysis peak.
#
# 32 is the KNEE of the curve, not an arbitrary pick. Re-measured for phaze-0582 end to end
# through the real `analyze_file` on the burst node, host-side `VmHWM`, synthetic audio,
# node otherwise idle -- one arm per process, the two arms differing ONLY in this file:
#
#   60 min, fine cap saturated (60 fine + 20 coarse):  2.4445 -> 1.6206 GiB (-33.71%),
#                                                      3039.97 -> 3052.09 s (+0.40%)
#   10 min (20 fine + 4 coarse):                       2.1743 -> 1.4111 GiB (-35.10%),
#                                                      344.27 -> 345.72 s  (+0.42%)
#
# Below 32 the memory curve is FLAT -- phaze-mqq5 measured batch 16 and batch 8 at ~-34%
# each -- because what is left is the graph plus the allocator floor, not the batch; and
# batch 1 buys only ~8 more points of peak for +55.7% WALL CLOCK, which is the wrong trade
# on a node measured CPU-bound. So going lower costs time and returns nothing.
#
# This is NOT a byte-identical change and must not be described as one. Regrouping patches
# into different batches changes float32 summation order, so the last ulp moves. Measured
# over the whole serialized result (3702 leaves, 1922 numeric) on the 60-minute file: max
# |delta| 1.79e-7 (~1.5 float32 ulp at 1.0), 0/714 top-1 flips, every categorical field
# (bpm/key/mood/style) and every window count identical, `danceability` moving 2.98e-9 in
# the float64. The bar this change is held to is that tolerance (aggregated |delta| <= 1e-3,
# zero top-1 flips), NOT bit-exactness -- an equivalence test written against a sha256 will
# fail for the right reason and be deleted for the wrong one.
#
# The batch value is also the ONLY thing that moved: the same patched code run with
# `PHAZE_ANALYSIS_TF_BATCH_SIZE=64` reproduces the pre-change output **byte-identically**
# (max |delta| exactly 0.0 across all 918 leaves), so the whole delta above is attributable
# to the batch and none of it to the plumbing.
_DEFAULT_TF_BATCH_SIZE = 32

# Deployment override. Read from the ENVIRONMENT at classifier construction, deliberately
# not plumbed through the per-job windowing kwargs (`fine_window_sec` / `coarse_window_sec` /
# `fine_min_sec`): those are per-FILE knobs the enqueue path varies per request, while this is
# a per-HOST sizing knob.
# phaze-rvcn will make thread/concurrency sizing host-derived; when it does, the derivation
# belongs in `_resolve_tf_batch_size` -- this one function is the seam.
_TF_BATCH_SIZE_ENV = "PHAZE_ANALYSIS_TF_BATCH_SIZE"

# `discogs-effnet-bs64-1`'s input Placeholder is `[64, 128, 96]` -- a batch of 64 baked into
# the graph, which is literally what the `bs64` in the filename means. The batch lever
# CANNOT move it from the caller: any other value is a configuration error, not a data
# point, and it is also why `lastBatchMode` exists on that algorithm at all. It therefore
# keeps its own arena and the measured saving is delivered by the other 33 graphs.
_FIXED_BATCH_SIZE: dict[str, int] = {GENRE_MODEL.filename: 64}


def _resolve_tf_batch_size(model: ModelConfig) -> int:
    """Resolve the `TensorflowPredict*` ``batchSize`` for one model.

    Models whose graph fixes the batch in the Placeholder (:data:`_FIXED_BATCH_SIZE`)
    always get that value and ignore the override entirely. Everything else takes
    ``PHAZE_ANALYSIS_TF_BATCH_SIZE`` when it parses as a positive int, else
    :data:`_DEFAULT_TF_BATCH_SIZE`. A malformed or non-positive override is logged and
    ignored rather than raised: a typo in a deployment env var must not turn every
    analysis into a hard failure.
    """
    fixed = _FIXED_BATCH_SIZE.get(model.filename)
    if fixed is not None:
        return fixed

    raw = os.environ.get(_TF_BATCH_SIZE_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_TF_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a positive int; using %d", _TF_BATCH_SIZE_ENV, raw, _DEFAULT_TF_BATCH_SIZE)
        return _DEFAULT_TF_BATCH_SIZE
    if value < 1:
        log.warning("%s=%r is not a positive int; using %d", _TF_BATCH_SIZE_ENV, raw, _DEFAULT_TF_BATCH_SIZE)
        return _DEFAULT_TF_BATCH_SIZE
    return value
