"""Audio analysis service: model registry, essentia analysis, mood/style derivation."""

from __future__ import annotations

from dataclasses import dataclass, field
import gc
import json
import logging
import os
from pathlib import Path
import platform
import resource
from statistics import mean, median
from typing import TYPE_CHECKING, Any

import numpy as np

from phaze.services.analysis_sizing import apply_thread_env


if TYPE_CHECKING:
    from collections.abc import Callable


# Suppress TF C++ logging before any essentia/TF import
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

# phaze-rvcn: derive TF_NUM_INTRAOP_THREADS / TF_NUM_INTEROP_THREADS / OMP_NUM_THREADS from
# the host's SCHEDULABLE PHYSICAL core count -- here, because TF reads all three when it
# builds its thread pools and a value stamped after the first session is read by nothing.
#
# Left unset, TF sizes its intra-op pool from the machine's core count and gives each worker
# thread its own allocation arena, which makes the per-process analyze peak a function of the
# HOST rather than of the workload: every figure in docs/k8s-burst.md would rise silently on
# a bigger box, reintroducing the node-scoped OOM ADR-0005 exists to prevent. The cap is the
# mechanism that decouples the two -- see services/analysis_sizing.py for the policy, the
# measurements behind the constants, and the env overrides (an operator-set value wins).
#
# Bound to a name rather than called bare so the derivation stays inspectable after import (and
# so this pre-import stamp reads as an assignment, like the TF_CPP_MIN_LOG_LEVEL line above).
ANALYSIS_SIZING = apply_thread_env()

# E402 is deliberate and load-bearing on BOTH lines: essentia pulls TensorFlow in at import, and
# TF reads TF_CPP_MIN_LOG_LEVEL and its thread-pool env at that point -- so these imports MUST come
# after the two stamps above. (ruff waives E402 after a bare `os.environ` mutation but not after the
# `apply_thread_env` call, which is why only these two lines carry the marker.)
import essentia  # noqa: E402
import essentia.standard as es  # noqa: E402


log = logging.getLogger(__name__)


class AnalysisDecodeError(RuntimeError):
    """EVERY analysis window failed to decode -- the file's audio stream is unusable (phaze-zibn).

    Raised by :func:`analyze_file` when the natural window count was non-zero (the duration
    probe read a positive length) but the per-window ``EasyLoader`` decode failed on every fine
    AND every coarse window -- e.g. a truncated download whose valid ID3 header reports a long
    duration over no decodable frames. Without this floor the per-window failure isolation
    returned a success dict with all-``None`` aggregates and zero analyzed windows, the
    completion PUT stamped ``analysis_completed_at``, and the undecodable file was permanently
    recorded as successfully analyzed. Raising instead routes the file to the callers' existing
    terminal failure handling (``report_analysis_failed`` / ``ANALYSIS_FAILED``), consistent
    with the timeout/crash paths.

    NOTE (phaze-by30): "the duration probe succeeded" does NOT imply "the natural window count
    is non-zero" -- a duration probe can itself read 0 seconds (a container whose readable
    header nonetheless yields zero-length audio properties, or a genuinely sub-1-second file
    truncating to 0). That produces ``fine_total == coarse_total == 0``, which this class's own
    guard deliberately does NOT raise on (0/0 is exempted so a probe-level non-decode isn't
    double-reported here) -- callers that need a floor on THAT case check the coverage fields
    directly instead of relying on this exception (see ``tasks/functions.py::process_file``).
    """


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
# Module-level caches for lazy loading in ProcessPoolExecutor workers
# ---------------------------------------------------------------------------

# _classifier_cache holds the ONE graph currently being swept across the coarse windows.
#
# It used to hold all 34 for the process's lifetime. That was a deliberate, documented
# time optimization -- "inference-only; no per-window graph reload" -- priced against
# wall-clock and never against a memory bound. Spike phaze-esut measured the residency it
# bought: +4.090 GiB on macOS / +3.995 GiB on Linux (phaze-7i0k) with ZERO inference
# performed, ~50% of the Linux peak, while `_run_model_sets` used the graphs strictly one
# at a time. The memory bound became the binding constraint (node-scoped OOM kills), so
# phaze-15sw repriced the trade rather than fixing a bug: `_run_model_sets_over_windows`
# iterates MODEL-major, so each graph is constructed exactly once per file (the load cost
# the cache existed to avoid is unchanged), swept across every coarse window, then released
# by `_release_classifier` before the next is built.
_classifier_cache: dict[str, Any] = {}
_labels_cache: dict[str, list[str]] = {}
_essentia_logging_suppressed = False


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
# not plumbed through the per-job windowing kwargs (`fine_cap`/`coarse_cap`): those are
# per-FILE knobs the enqueue path varies per request, while this is a per-HOST sizing knob.
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


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _suppress_essentia_logging() -> None:
    """Suppress essentia info/warning logging (safe to call multiple times)."""
    global _essentia_logging_suppressed
    if not _essentia_logging_suppressed:
        essentia.log.infoActive = False
        essentia.log.warningActive = False
        _essentia_logging_suppressed = True


def _get_classifier(model: ModelConfig, models_dir: str) -> Any:
    """Get or create the cached classifier instance for the given model.

    Construction now passes an explicit ``batchSize`` (phaze-0582) instead of inheriting
    essentia's default of 64 -- see :func:`_resolve_tf_batch_size` and
    :data:`_DEFAULT_TF_BATCH_SIZE` for the measurement behind the value, and
    :data:`_FIXED_BATCH_SIZE` for the one model that cannot take it.

    What changed before that (phaze-15sw) is the LIFETIME of what this caches. Under
    model-major iteration the caller sweeps one model across every window before calling
    :func:`_release_classifier`, so this still constructs each graph exactly once per file
    -- the cache hit rate on the hot path is identical -- but the cache holds one graph
    instead of 34.
    """
    if model.filename in _classifier_cache:
        return _classifier_cache[model.filename]

    graph_path = str(Path(models_dir) / (model.filename + ".pb"))
    batch_size = _resolve_tf_batch_size(model)

    if model.classifier_type == "musicnn":
        classifier = es.TensorflowPredictMusiCNN(graphFilename=graph_path, batchSize=batch_size)
    elif model.classifier_type == "vggish":
        classifier = es.TensorflowPredictVGGish(graphFilename=graph_path, batchSize=batch_size)
    elif model.classifier_type == "effnet_discogs":
        # batchSize stays 64 here (via _FIXED_BATCH_SIZE) because the graph's Placeholder
        # is fixed at 64, and `lastBatchMode` stays unpassed at its default "same" --
        # zero-pad the final batch, then erase exactly the padded predictions -- which is
        # the behaviour phaze-rc1q 3d flagged as batch-coupled.
        #
        # phaze-0582 checked that interaction on the wheel rather than assuming it:
        # `lastBatchMode` is a parameter of THIS algorithm ONLY. TensorflowPredictMusiCNN
        # and TensorflowPredictVGGish -- the 33 graphs whose batch actually moves -- do not
        # expose it at all, so the padding branch is unreachable for them. And the returned
        # patch count is invariant to batchSize across 64/32/16/8/1 (musicnn 402, vggish
        # 645 on the same buffer), so nothing is padded in or dropped. The one algorithm
        # the padding DOES apply to is the one this change does not touch.
        classifier = es.TensorflowPredictEffnetDiscogs(graphFilename=graph_path, batchSize=batch_size)
    else:
        msg = f"Unknown classifier type: {model.classifier_type}"
        raise ValueError(msg)

    _classifier_cache[model.filename] = classifier
    return classifier


def _get_labels(model_filename: str, models_dir: str) -> list[str]:
    """Get or load cached labels for the given model file."""
    if model_filename in _labels_cache:
        return _labels_cache[model_filename]

    json_path = Path(models_dir) / (model_filename + ".json")
    with json_path.open() as f:
        metadata = json.load(f)

    labels = [label.replace("---", "/") for label in metadata["classes"]]
    _labels_cache[model_filename] = labels
    return labels


def _predict_single(audio_16k: Any, model: ModelConfig, models_dir: str) -> Any:
    """Run a single model prediction and return mean activations."""
    classifier = _get_classifier(model, models_dir)
    activations = classifier(audio_16k)
    return np.mean(activations, axis=0)


def _release_classifier(model_filename: str) -> None:
    """Evict one graph from ``_classifier_cache`` and hand its native memory back.

    The `TensorflowPredict*` wrapper owns a C++ TF session; dropping the last Python
    reference runs its destructor, which is what actually returns the graph's arena.
    **Measured (phaze-15sw, on the Linux burst node, deployed image + model set):** the
    refcount drop alone already returns it -- popping a vggish graph moves instantaneous
    RSS 0.500 -> 0.219 GiB *before* any collection, and building then releasing all 34 in
    turn leaves RSS at 0.263 GiB against 3.751 GiB when they are held co-resident. So the
    eviction frees memory; it does not merely drop a name.

    ``gc.collect()`` is therefore insurance, not the mechanism: it costs ~0.2 s across the
    34 releases of a file and closes the case where something in a reference cycle keeps
    the wrapper alive past the pop. The one such path this module could create -- retaining
    a caught exception, whose traceback pins ``_predict_single``'s frame and with it the
    classifier -- is closed at the source in :func:`_sweep_one_model`, which reports a
    window failure inside the handler and retains only the window index.

    No-op when the model is not cached, so it is safe in a ``finally``.
    """
    if _classifier_cache.pop(model_filename, None) is None:
        return
    gc.collect()


def _peak_rss_gib() -> float | None:
    """This process's peak (high-water) RSS in GiB so far, or ``None`` if unreadable.

    phaze-7qfd -- makes the memory floor spikes `phaze-esut`/`phaze-7i0k` measured by hand a
    routine observable instead of something reconstructed from OOM forensics after the fact.

    **The unit differs by platform and was verified, not assumed** (both directly against a
    live process on each platform, and against `phaze-7i0k`'s independent cross-check, which
    found `ru_maxrss` and `/proc/self/status:VmHWM` agree to the byte on Linux):

    * **Linux** -- read `/proc/self/status:VmHWM`, which `proc(5)` documents in kB (kibibytes)
      *always*, regardless of `getrusage`'s platform-dependent unit. This is the TRUE
      high-water mark -- the kernel's own peak-RSS accounting -- not a resident-at-this-instant
      sample, so it is unaffected by exactly where in the job this function is called.
    * **Darwin** (dev/test only; never the production job pod) -- `getrusage(RUSAGE_SELF)
      .ru_maxrss` is in **bytes**. Reusing the Linux divisor here would under-report by 1024x.
    * Anywhere else, return ``None`` rather than guess a unit that was never verified.

    Dispatches on ``platform.system()`` rather than ``sys.platform`` -- the latter is
    special-cased by mypy for cross-platform-typeshed conditionals (``--python-platform``
    defaults to the host running the type checker), which would mark whichever branch
    that host isn't as permanently unreachable instead of a real runtime dispatch.
    """
    system = platform.system()
    if system == "Linux":
        try:
            with Path("/proc/self/status").open() as status_file:
                for line in status_file:
                    if line.startswith("VmHWM:"):
                        vm_hwm_kib = int(line.split()[1])  # proc(5): always kB, never bytes
                        return vm_hwm_kib / (1024 * 1024)
        except OSError:
            pass
        # /proc unreadable (e.g. a sandboxed or non-Linux-proc container): ru_maxrss is KiB
        # on Linux, unlike Darwin's bytes -- see the docstring's platform note.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    if system == "Darwin":
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3)
    return None


def _log_job_peak_rss() -> None:
    """Log this job's peak RSS once, at INFO, after analysis completes.

    **What this measures, post-`phaze-15sw`.** Model-major coarse inference means only ONE
    `TensorflowPredict*` graph is ever resident at a time (see the `_classifier_cache` module
    comment) -- there is no longer a single "all models loaded" instant to log after, the way
    there was under the old window-major loop. `VmHWM`/`ru_maxrss` is a HIGH-WATER MARK, not a
    point-in-time sample, so calling this once at the end of :func:`analyze_file` still reports
    the job's true peak regardless of which stage produced it (in practice, the coarse pass's
    first model sweep -- see `phaze-7i0k` section 2d). That peak is this job's contribution to
    the design-peak figure `docs/k8s-burst.md` sizes cluster memory from.
    """
    peak_gib = _peak_rss_gib()
    if peak_gib is None:
        return
    log.info("analyze job peak RSS (high-water mark): %.3f GiB", peak_gib)


# ---------------------------------------------------------------------------
# Mood / style derivation
# ---------------------------------------------------------------------------

_MOOD_SET_NAMES = frozenset(
    {
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
    }
)


def _positive_class_prediction(predictions: list[dict[str, Any]]) -> float:
    """Return the POSITIVE-class probability from a binary classifier's prediction list.

    essentia's binary-classifier metadata orders classes ALPHABETICALLY, not
    positive-first, so ``predictions[0]`` is the positive class for only SOME model
    sets. e.g. ``mood_relaxed`` = ``['non_relaxed', 'relaxed']`` and ``mood_sad`` /
    ``mood_party`` put the NEGATIVE class first — indexing ``[0]`` there scored the
    mood with P(non_relaxed) and systematically inverted relaxed/sad/party.

    Select the positive class by LABEL: it is the entry whose label does NOT start
    with a negation prefix (``non_`` / ``not_``). Falls back to the first entry when
    no label qualifies (defensive — preserves behavior for unexpected label shapes).
    Callers guard ``if predictions`` so the list is non-empty here.
    """
    positive: dict[str, Any] | None = None
    for entry in predictions:
        if not str(entry.get("label", "")).startswith(("non_", "not_")):
            positive = entry
            break
    if positive is None:
        positive = predictions[0]
    return float(positive["prediction"])


def derive_mood(features: dict[str, Any]) -> str:
    """Derive dominant mood from feature predictions.

    For each mood model set, average the positive-class prediction (selected by
    label, not list position) across the 3 variants. Return the mood name (without
    'mood_' prefix) with the highest averaged confidence.
    """
    best_mood = ""
    best_score = -1.0

    for set_name in _MOOD_SET_NAMES:
        if set_name not in features:
            continue

        variant_scores: list[float] = []
        for _variant_name, predictions in features[set_name].items():
            if predictions:
                variant_scores.append(_positive_class_prediction(predictions))

        if variant_scores:
            avg_score = sum(variant_scores) / len(variant_scores)
            if avg_score > best_score:
                best_score = avg_score
                best_mood = set_name

    # Strip "mood_" prefix
    return best_mood.removeprefix("mood_")


def derive_style(genre_features: dict[str, Any]) -> str:
    """Derive top style/genre from genre model predictions.

    Returns the label of the highest-confidence genre prediction.
    Defensively replaces '---' with '/' in labels.
    """
    predictions = genre_features.get("predictions", [])
    if not predictions:
        return "unknown"

    top = max(predictions, key=lambda p: p["confidence"])
    return str(top["label"]).replace("---", "/")


def derive_danceability(features: dict[str, Any]) -> float | None:
    """Derive a scalar danceability from the danceability model set.

    Averages the positive-class ('danceable') prediction across the 3 variants,
    selected by label (robust to class order) rather than list position. Returns
    None if the danceability set is absent/empty.
    """
    set_data = features.get("danceability")
    if not set_data:
        return None

    scores: list[float] = []
    for _variant_name, predictions in set_data.items():
        if predictions:
            scores.append(_positive_class_prediction(predictions))

    return sum(scores) / len(scores) if scores else None


# ---------------------------------------------------------------------------
# Windowed time-series: per-window value containers + aggregate reductions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FineWindow:
    """A single fine-tier (BPM/key) analysis window."""

    window_index: int
    start_sec: float
    end_sec: float
    bpm: float | None
    musical_key: str | None
    confidence: float = 0.0

    def as_payload_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict ready for AnalysisWindowPayload(**w)."""
        return {
            "tier": "fine",
            "window_index": self.window_index,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "bpm": self.bpm,
            "musical_key": self.musical_key,
        }


@dataclass(frozen=True)
class CoarseWindow:
    """A single coarse-tier (mood/style/danceability) analysis window."""

    window_index: int
    start_sec: float
    end_sec: float
    mood: str | None
    style: str | None
    danceability: float | None
    features: dict[str, Any] = field(default_factory=dict)

    def as_payload_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict ready for AnalysisWindowPayload(**w)."""
        return {
            "tier": "coarse",
            "window_index": self.window_index,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "mood": self.mood,
            "style": self.style,
            "danceability": self.danceability,
            "features": self.features,
        }


def aggregate_bpm(fine: list[FineWindow]) -> float | None:
    """Representative BPM = median of fine-window BPMs (rounded to 0.1).

    Excludes windows with ``confidence == 0.0`` (unreliable BPM on short/silent
    audio per RESEARCH Pitfall 2) and windows with no BPM. Returns None if empty.
    """
    vals = [w.bpm for w in fine if w.bpm is not None and w.confidence != 0.0]
    return round(median(vals), 1) if vals else None


def _max_by_duration(weights: dict[str, float]) -> str | None:
    """Return the key with the greatest accumulated duration (stable on ties)."""
    if not weights:
        return None
    # max() is stable: on a tie it returns the first-inserted key.
    return max(weights, key=lambda k: weights[k])


def aggregate_key(fine: list[FineWindow]) -> str | None:
    """Representative key = duration-weighted modal key across fine windows."""
    weights: dict[str, float] = {}
    for w in fine:
        if w.musical_key:
            weights[w.musical_key] = weights.get(w.musical_key, 0.0) + (w.end_sec - w.start_sec)
    return _max_by_duration(weights)


def aggregate_dominant(coarse: list[CoarseWindow], attr: str) -> str | None:
    """Time-weighted dominant label (mood/style) across coarse windows."""
    weights: dict[str, float] = {}
    for w in coarse:
        label = getattr(w, attr)
        if label:
            weights[label] = weights.get(label, 0.0) + (w.end_sec - w.start_sec)
    return _max_by_duration(weights)


def aggregate_danceability(coarse: list[CoarseWindow]) -> float | None:
    """Representative danceability = mean across coarse windows; None if empty."""
    vals = [w.danceability for w in coarse if w.danceability is not None]
    return mean(vals) if vals else None


# ---------------------------------------------------------------------------
# Main analysis function (synchronous, for ProcessPoolExecutor)
# ---------------------------------------------------------------------------

# Sample rates for the two analysis passes (locked by Plan 31-01 spike).
_FINE_SAMPLE_RATE = 44100
_COARSE_SAMPLE_RATE = 16000

# AgentSettings defaults (config.py). analyze_file accepts overrides so the
# agent worker can pass settings.analysis_* values; defaults mirror the config.
_DEFAULT_FINE_WINDOW_SEC = 30
_DEFAULT_COARSE_WINDOW_SEC = 180
_DEFAULT_FINE_MIN_SEC = 15

# Per-file cost caps (Phase 43): bound essentia work to a constant regardless of
# duration. A file whose natural window count exceeds the cap is strided evenly
# across the WHOLE file via ``_stride_to_cap`` rather than analyzed window-by-window.
_DEFAULT_FINE_CAP = 60
_DEFAULT_COARSE_CAP = 30


def _probe_duration_sec(file_path: str) -> float:
    """Return total audio duration in seconds WITHOUT materializing PCM.

    Uses ``es.MetadataReader`` (reads container/header metadata; it does NOT
    decode the full signal, unlike ``MonoLoader``). Output index 8 is the
    duration in seconds. A failure here is fatal (the file is unreadable) and
    propagates to the caller rather than being treated as a per-window skip.
    """
    metadata = es.MetadataReader(filename=file_path, filterMetadata=True)()
    return float(metadata[8])


def _iter_windows(total_sec: float, win_sec: int, min_sec: int, *, drop_short_trailing: bool) -> list[tuple[int, float, float]]:
    """Yield ``(index, start_sec, end_sec)`` for fixed-duration windows over a file.

    When ``drop_short_trailing`` is True (FINE tier), a trailing window shorter
    than ``min_sec`` is dropped — EXCEPT window 0, so very short tracks still
    produce one window. When False (COARSE tier) every window with audio is
    emitted (no minimum-length floor; RESEARCH Open Q3 RESOLVED).
    """
    windows: list[tuple[int, float, float]] = []
    start = 0.0
    idx = 0
    while start < total_sec:
        end = min(start + win_sec, total_sec)
        if drop_short_trailing and (end - start) < min_sec and idx > 0:
            break
        windows.append((idx, start, end))
        start = end
        idx += 1
    return windows


def _stride_to_cap(windows: list[tuple[int, float, float]], cap: int) -> tuple[list[tuple[int, float, float]], bool]:
    """Even-stride ``windows`` down to ``<=cap`` entries, preserving original idx.

    Bounds per-file analysis cost to a constant regardless of duration: when a
    file's natural window count exceeds ``cap`` we sample evenly across the WHOLE
    file (first and last window always kept) instead of truncating to first-N.

    Returns ``(kept, sampled)``:
      * ``cap <= 0`` or ``len(windows) <= cap`` → ``(windows, False)`` unchanged.
      * otherwise → ``(kept, True)`` where ``kept`` retains each original tuple's
        idx (NO renumbering), is sorted ascending by idx, and never exceeds
        ``cap`` (rounding collisions dedup via a set, yielding ``<= cap``).

    Math: endpoint-inclusive even stride ``round(i * (n - 1) / (cap - 1))`` for
    ``i in 0..cap-1`` spans positions ``0 .. n-1`` so the first and last windows
    are always included (RESEARCH §Q2).
    """
    n = len(windows)
    if cap <= 0 or n <= cap:
        return windows, False
    if cap == 1:  # defense-in-depth: cap is validated ge=2 in config, but a direct call must not divide by zero
        return windows[:1], True
    picks = {round(i * (n - 1) / (cap - 1)) for i in range(cap)}  # set dedups rounding collisions
    kept = [windows[p] for p in sorted(picks)]
    return kept, True


def _sweep_one_model(
    model: ModelConfig,
    buffers: list[tuple[int, Any]],
    models_dir: str,
    failed: set[int],
    on_failure: Callable[[int], None],
) -> dict[int, tuple[Any, list[str]]]:
    """Construct ONE model's graph, run it across every still-live buffer, release it.

    The model-major primitive (phaze-15sw). ``buffers`` is ``(window_index, audio_16k)``
    in window order; ``failed`` is the shared per-window kill list, read to skip windows an
    earlier model already failed on and added to when this model fails on one.
    Returns ``{window_index: (mean_activations, labels)}`` for the windows that succeeded.

    The prediction call order per window is ``_predict_single`` then ``_get_labels``,
    identical to the window-major loop this replaced, so a raising ``_get_labels`` fails
    exactly the same windows it failed before (all of them, one at a time).

    ``on_failure(window_index)`` is invoked from INSIDE the ``except`` block, and only the
    index is retained afterwards. That is deliberate and memory-load-bearing: keeping the
    caught exception would keep its traceback, whose ``_predict_single`` frame holds a
    reference to ``classifier`` -- pinning for the rest of the file the very graph the
    ``finally`` below is about to evict, and re-creating in the failure path exactly the
    co-residency this restructure exists to remove. Reporting in-handler also keeps
    ``log.warning(..., exc_info=True)`` rendering the same traceback it always did.

    The ``finally`` is the other load-bearing line: it is what bounds residency to one
    graph even when the consumer raises partway through a sweep.
    """
    out: dict[int, tuple[Any, list[str]]] = {}
    try:
        for key, buf in buffers:
            if key in failed:
                continue  # a previous model already killed this window; the window-major loop had abandoned it too
            try:
                predictions = _predict_single(buf, model, models_dir)
                labels = _get_labels(model.filename, models_dir)
            except Exception:  # per-window failure isolation, hoisted from _analyze_coarse_windows
                failed.add(key)
                on_failure(key)  # report NOW; see the docstring on why the exception is not retained
                continue
            out[key] = (predictions, labels)
    finally:
        _release_classifier(model.filename)
    return out


def _run_model_sets_over_windows(
    buffers: list[tuple[int, Any]],
    models_dir: str,
    on_failure: Callable[[int], None],
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Run all 11 characteristic model sets + the genre model over EVERY coarse buffer.

    **Model-major** (phaze-15sw): models are the outer loop, windows the inner one, so
    exactly one ``TensorflowPredict*`` graph is resident at any instant instead of 34.
    Each model is still constructed exactly once per file -- no per-window graph reload,
    so the wall-clock optimization ``_classifier_cache`` existed to provide is fully
    preserved -- and the price is holding the <=``coarse_cap`` decoded buffers
    concurrently (30 x 180 s x 16 kHz x 4 B ~= 345 MB) instead of 4.09 GiB of idle graphs.
    See the ``_classifier_cache`` comment for the measurement that motivated the reprice.

    ``on_failure(window_index)`` fires once, in-handler, for each window an inference
    fails on; that window is excluded from every later model.

    Returns ``({window_index: features}, {failed window_index})``. Feature-dict key
    insertion order is pinned to ``MODEL_SETS`` order (pre-seeded) with ``"genre"`` last,
    matching the window-major build byte for byte -- the dicts are JSON-serialized
    downstream, where insertion order is output.
    """
    features: dict[int, dict[str, Any]] = {key: {model_set.name: {} for model_set in MODEL_SETS} for key, _ in buffers}
    failed: set[int] = set()

    for model_set in MODEL_SETS:
        for model in model_set.models:
            for key, (predictions, labels) in _sweep_one_model(model, buffers, models_dir, failed, on_failure).items():
                features[key][model_set.name][model.variant] = [
                    {"label": label, "prediction": float(pred)} for label, pred in zip(labels, predictions, strict=False)
                ]

    for key, (genre_predictions, genre_labels) in _sweep_one_model(GENRE_MODEL, buffers, models_dir, failed, on_failure).items():
        genre_pairs = list(zip(genre_labels, genre_predictions, strict=False))
        genre_pairs.sort(key=lambda pair: float(pair[1]), reverse=True)
        features[key]["genre"] = {
            "predictions": [{"label": label, "confidence": float(conf)} for label, conf in genre_pairs[:10]],
        }

    return features, failed


def _run_model_sets(audio_16k: Any, models_dir: str) -> dict[str, Any]:
    """Run all 11 characteristic model sets + the genre model on ONE buffer.

    The single-buffer entry point, retained for callers/harnesses that hold exactly one
    window; it is a thin wrapper over :func:`_run_model_sets_over_windows` so there is
    one inference path, not two. Failure semantics are the pre-phaze-15sw ones: the
    exception propagates rather than being isolated, because with one window there is
    nothing to isolate it from. The bare ``raise`` re-raises the exception currently being
    handled by :func:`_sweep_one_model` -- so the caller still sees the original error and
    traceback, without that exception ever being stored.
    """

    def _propagate(_window_index: int) -> None:
        raise  # intentional bare re-raise: only ever called from inside _sweep_one_model's except block

    features, _failed = _run_model_sets_over_windows([(0, audio_16k)], models_dir, _propagate)
    return features[0]


def _analyze_fine_windows(
    file_path: str,
    total_sec: float,
    win_sec: int,
    min_sec: int,
    cap: int,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[list[FineWindow], int, bool]:
    """FINE pass: BPM + key per ``win_sec`` window via segmented EasyLoader decode.

    Returns ``(windows, total, sampled)`` where ``total`` is the natural window
    count BEFORE striding and ``sampled`` is True when the cap forced an even
    stride. ``len(windows)`` (analyzed) counts successful appends; per-window
    failures are skipped, so it may be below the post-stride target.

    Phase 57.1 (PROG-01): when ``progress_cb`` is provided it fires a START signal
    ``progress_cb(0, len(natural))`` BEFORE the loop and then ``progress_cb(len(fine_windows),
    len(natural))`` after every successful append. The denominator is ``len(natural)`` —
    the pre-stride natural count — IDENTICAL to the ``fine_windows_total`` the completion
    PUT reports, so the in-flight bar and final coverage agree (denominator invariant).
    This seam emits only an ``(int, int)`` count and does NO I/O; throttling and transport
    live DOWNSTREAM in the lane bridge, never here (keeps the compute seam HTTP/pickle-free).
    ``progress_cb=None`` (the default) leaves behavior byte-identical to before.

    ``RhythmExtractor2013`` and ``KeyExtractor`` are constructed ONCE per file and reused
    across every window (phaze-ap8y), not rebuilt per window: neither takes a per-window
    parameter, and construction was 7.55 s of the 31.50 s fine tier on a 60-window file
    (measured, phaze-i93a §6a). ``reset()`` between windows was verified NOT required —
    0/60 output mismatches with and without it, across the full ``(window_index, bpm, key,
    confidence)`` tuple — so it is deliberately not called here.
    """
    natural = _iter_windows(total_sec, win_sec, min_sec, drop_short_trailing=True)
    kept, sampled = _stride_to_cap(natural, cap)
    if progress_cb is not None:
        progress_cb(0, len(natural))  # START: analyzed=0, total=natural pre-stride
    rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
    key_extractor = es.KeyExtractor(profileType="edma")
    fine_windows: list[FineWindow] = []
    for idx, start, end in kept:
        try:
            buf = es.EasyLoader(filename=file_path, sampleRate=_FINE_SAMPLE_RATE, startTime=start, endTime=end)()
            bpm, _beats, confidence, _, _beats_intervals = rhythm_extractor(buf)
            key, scale, _strength = key_extractor(buf)
            fine_windows.append(
                FineWindow(
                    window_index=idx,
                    start_sec=start,
                    end_sec=end,
                    bpm=round(float(bpm), 1),
                    musical_key=f"{key} {scale}",
                    confidence=float(confidence),
                )
            )
        except Exception:  # per-window failure isolation: skip, never fail the file
            log.warning("fine window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=True)
            continue
        if progress_cb is not None:
            progress_cb(len(fine_windows), len(natural))  # bump (throttle lives downstream, not here)
    return fine_windows, len(natural), sampled


def _analyze_coarse_windows(file_path: str, total_sec: float, win_sec: int, models_dir: str, cap: int) -> tuple[list[CoarseWindow], int, bool]:
    """COARSE pass: mood/style/danceability per ``win_sec`` window (no length floor).

    Returns ``(windows, total, sampled)`` mirroring ``_analyze_fine_windows``:
    ``total`` is the natural pre-stride count and ``sampled`` is True when the
    cap forced an even stride.

    Three phases since phaze-15sw, because the inference is MODEL-major (see
    :func:`_run_model_sets_over_windows`) and a model-major sweep needs every buffer in
    hand before the first graph is built:

    1. **decode** every kept window up front -- <=``cap`` buffers held concurrently
       (30 x 180 s x 16 kHz x 4 B ~= 345 MB), deliberately, in exchange for not holding
       34 co-resident TF graphs (~4 GiB);
    1. **infer** model-major across all of them, one resident graph at a time;
    1. **derive + assemble** in window order.

    Per-window failure isolation is preserved across all three: a decode failure drops
    that window before inference, an inference failure kills only that window (later
    models skip it), and derivation failure drops it at assembly. Same warning, same
    ``exc_info``; only the ORDER of the log lines changes, since a window's inference
    failure is now discovered during the sweep rather than in window order.
    """
    natural = _iter_windows(total_sec, win_sec, 0, drop_short_trailing=False)
    kept, sampled = _stride_to_cap(natural, cap)

    def _skip(idx: int, start: float, end: float) -> None:
        log.warning("coarse window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=True)

    # (1) decode
    spans: list[tuple[int, float, float]] = []
    buffers: list[tuple[int, Any]] = []
    for idx, start, end in kept:
        try:
            buf = es.EasyLoader(filename=file_path, sampleRate=_COARSE_SAMPLE_RATE, startTime=start, endTime=end)()
        except Exception:  # per-window failure isolation: skip, never fail the file
            _skip(idx, start, end)
            continue
        spans.append((idx, start, end))
        buffers.append((idx, buf))

    # (2) infer, model-major
    geometry = {idx: (start, end) for idx, start, end in spans}
    features_by_window, failed = _run_model_sets_over_windows(buffers, models_dir, lambda idx: _skip(idx, *geometry[idx]))
    buffers.clear()  # the peak is behind us; do not carry ~345 MB of PCM through assembly

    # (3) derive + assemble, in window order
    coarse_windows: list[CoarseWindow] = []
    for idx, start, end in spans:
        if idx in failed:
            continue  # already reported, in-handler, by the model sweep that failed it
        try:
            features = features_by_window[idx]
            coarse_windows.append(
                CoarseWindow(
                    window_index=idx,
                    start_sec=start,
                    end_sec=end,
                    mood=derive_mood(features),
                    style=derive_style(features["genre"]),
                    danceability=derive_danceability(features),
                    features=features,
                )
            )
        except Exception:  # per-window failure isolation: skip, never fail the file
            _skip(idx, start, end)
            continue
    return coarse_windows, len(natural), sampled


def _representative_features(coarse: list[CoarseWindow]) -> dict[str, Any]:
    """Pick a representative full-features dict for the aggregate ``analysis`` row.

    Returns the longest-duration coarse window's features (ties → first). Keeps
    the existing ``features`` JSONB structure (all model sets + genre) populated
    for downstream consumers; empty dict when there are no coarse windows.
    """
    if not coarse:
        return {}
    longest = max(coarse, key=lambda w: w.end_sec - w.start_sec)
    return longest.features


def analyze_file(
    file_path: str,
    models_dir: str,
    *,
    fine_window_sec: int = _DEFAULT_FINE_WINDOW_SEC,
    coarse_window_sec: int = _DEFAULT_COARSE_WINDOW_SEC,
    fine_min_sec: int = _DEFAULT_FINE_MIN_SEC,
    fine_cap: int = _DEFAULT_FINE_CAP,
    coarse_cap: int = _DEFAULT_COARSE_CAP,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Analyze a single audio file via essentia as a two-tier time-series.

    The main synchronous function called from ``run_in_process_pool``. Instead of
    decoding the whole file into one buffer (the latent OOM) and feeding long
    audio to ``RhythmExtractor2013`` (the ``OnsetDetectionGlobal`` overflow), it
    decodes one short window at a time via segmented ``EasyLoader`` (Plan 31-01
    locked strategy) so no essentia algorithm ever sees more than one window.

    Two passes:
      * FINE (44.1 kHz): ``RhythmExtractor2013`` + ``KeyExtractor`` per
        ``fine_window_sec`` window; trailing windows shorter than
        ``fine_min_sec`` are dropped (except window 0).
      * COARSE (16 kHz): the 34 TF model sets per ``coarse_window_sec`` window;
        every window with audio is analyzed (no minimum-length floor).

    The two passes bound memory differently, and deliberately so. FINE holds one
    44.1 kHz window at a time. COARSE (phaze-15sw) holds ALL its ``coarse_cap``
    16 kHz windows at once -- ~345 MB at the default 30 -- because its inference is
    model-major: one ``TensorflowPredict*`` graph is built, run across every window,
    and released before the next is built. That trades ~345 MB of PCM for the ~4 GiB
    of co-resident TF graphs the window-major loop used to hold (phaze-esut /
    phaze-7i0k), at the same 34 model constructions per file. Both passes stay
    bounded by the CAPS, not by duration.

    Per-window failures are logged and skipped — one bad window never fails the
    file. The one floor (phaze-zibn): if EVERY window fails in BOTH passes while
    the natural window count was non-zero, :class:`AnalysisDecodeError` is raised
    instead of returning an empty success — total decode failure is a file-level
    failure, not a completed analysis. Window sizes default to the
    ``AgentSettings`` defaults (30/180/15) and may be overridden by the agent
    worker.

    To keep per-file cost constant regardless of duration, each pass is bounded
    by a cap (``fine_cap``/``coarse_cap``, defaults 60/30): a file whose natural
    window count exceeds the cap is strided EVENLY across the whole file instead
    of analyzed window-by-window (root cause of the 4h-timeout: cost was
    O(duration)). Under the cap, behavior is unchanged (every window analyzed).

    Returns a dict with the representative aggregates
    (``bpm``/``musical_key``/``mood``/``style``/``danceability``/``features``)
    PLUS ``windows``: a flat list of fine + coarse window dicts, each ready for
    ``AnalysisWindowPayload(**w)`` — PLUS a five-field coverage contract
    (``fine_windows_analyzed``/``fine_windows_total``/``coarse_windows_analyzed``/
    ``coarse_windows_total``/``sampled``) so a sampled file can be re-deepened
    later (Phase 44). ``*_total`` is the natural pre-stride window count;
    ``*_analyzed`` is the count actually analyzed (post-stride, minus per-window
    skips); ``sampled`` is True when either pass was strided.

    Phase 57.1 (PROG-01): an optional sync ``progress_cb(analyzed, total)`` is threaded
    into the FINE per-window loop (``_analyze_fine_windows``) — a START signal then a
    per-window bump up to ``(len(fine_windows), fine_windows_total)``. The callback emits
    only an ``(int, int)`` count; ``analyze_file`` itself does NO I/O and imports no HTTP
    client (the Phase 101 exec'd-child JSON-protocol boundary, ``phaze.analysis_child`` /
    ``services.analysis_exec``, plus the ``tests/shared/core/test_task_split.py`` essentia import
    boundary, stay intact). Transport + throttle are the LANE's job. Fine-only is sufficient
    for the in-flight bar (WORK-04); the COARSE pass is intentionally not instrumented.
    """
    _suppress_essentia_logging()

    total_sec = _probe_duration_sec(file_path)

    fine_windows, fine_total, fine_sampled = _analyze_fine_windows(
        file_path, total_sec, fine_window_sec, fine_min_sec, fine_cap, progress_cb=progress_cb
    )
    coarse_windows, coarse_total, coarse_sampled = _analyze_coarse_windows(file_path, total_sec, coarse_window_sec, models_dir, coarse_cap)

    # phaze-zibn: per-window failure isolation must not mask TOTAL decode failure. If the
    # file naturally had windows to analyze but every single one failed in BOTH passes, the
    # audio stream is undecodable (e.g. a valid header over a truncated/corrupt payload) --
    # raise so the callers route it to their terminal failure handling instead of recording
    # an empty all-None result as a completed analysis. A partial failure (>=1 window
    # analyzed in either pass) remains a genuine partial success.
    if not fine_windows and not coarse_windows and (fine_total > 0 or coarse_total > 0):
        msg = f"all analysis windows failed to decode ({fine_total} fine + {coarse_total} coarse natural windows, 0 analyzed): {file_path}"
        raise AnalysisDecodeError(msg)

    windows: list[dict[str, Any]] = [w.as_payload_dict() for w in fine_windows]
    windows.extend(w.as_payload_dict() for w in coarse_windows)

    # phaze-7qfd: log the job's peak RSS once the memory-dominant work is done, so the
    # floor is a routine observable instead of something reconstructed from OOM forensics.
    # See _log_job_peak_rss's docstring for what this does and does not measure post-15sw.
    _log_job_peak_rss()

    return {
        "bpm": aggregate_bpm(fine_windows),
        "musical_key": aggregate_key(fine_windows),
        "mood": aggregate_dominant(coarse_windows, "mood"),
        "style": aggregate_dominant(coarse_windows, "style"),
        "danceability": aggregate_danceability(coarse_windows),
        "features": _representative_features(coarse_windows),
        "windows": windows,
        "fine_windows_analyzed": len(fine_windows),
        "fine_windows_total": fine_total,
        "coarse_windows_analyzed": len(coarse_windows),
        "coarse_windows_total": coarse_total,
        "sampled": fine_sampled or coarse_sampled,
    }
