"""Audio analysis service: model registry, essentia analysis, mood/style derivation."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import gc
import json
import logging
import os
from pathlib import Path
import platform
import resource
from statistics import mean, median
import subprocess  # nosec B404  # ffprobe duration probe (D-10); fixed argv, no shell
from typing import TYPE_CHECKING, Any

import numpy as np

from phaze.services import analysis_decoder
from phaze.services.analysis_sizing import apply_thread_env


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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
import essentia.streaming as ess  # noqa: E402


log = logging.getLogger(__name__)


# Bound on the ffprobe stderr excerpt carried in an AnalysisProbeError message. The error text
# is stored (truncated again by the callers' own _ERROR_DETAIL_MAX) and read by an operator --
# enough for ffprobe's actual complaint, never an unbounded string.
_PROBE_STDERR_MAX = 500

# A streaming chunk decode is one blocking ``essentia.run`` call, so Python cannot
# report progress from the decoding thread until it returns.  A small watchdog thread
# emits truthful liveness while that call is still running.  Sixty seconds is 30x
# inside D-08's 1 800 s silence bound, leaving ample scheduling slack without making
# heartbeat traffic meaningful at archive scale.
_DECODE_HEARTBEAT_INTERVAL_SEC = 60.0


class AnalysisProbeError(RuntimeError):
    """``ffprobe`` could not read a usable duration for the file (phaze-l832u, D-10).

    Raised by :func:`_probe_duration_sec` when ffprobe cannot be run, exits nonzero, emits
    unparseable output, or reports no positive duration for any audio stream or for the
    container. FATAL by design and NOT caught anywhere below :func:`analyze_file`: this probe is
    the reason a mis-suffixed or undecodable download fails loudly with a stored
    ``error_message`` instead of recording a NULL-everything "success". Distinct from
    :class:`AnalysisDecodeError` (headers readable, every WINDOW undecodable) so the stored
    failure text says which of the two happened without re-running anything.
    """


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

# The streaming decode's sink-key namespace and its chunk-gate margin live with the decoder
# that uses them, in `analysis_decoder`. They were duplicated here during the extraction; two
# copies of a load-bearing constant can silently diverge, so this module has none.


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


def _resolve_malloc_trim() -> Callable[[int], int] | None:
    """Resolve glibc's ``malloc_trim`` once, or ``None`` where it does not exist.

    Deliberately narrow: ``malloc_trim`` is a glibc extension. macOS and musl do not have it,
    and a ``CDLL(None)`` lookup there raises ``AttributeError`` -- so this returns ``None`` and
    :func:`_malloc_trim` becomes a no-op rather than a per-call exception. Resolution is
    memoised in :data:`_MALLOC_TRIM` because the lookup is the expensive part, not the call.

    Gated on ``platform.libc_ver`` rather than ``sys.platform`` because the C library, not the
    kernel, is what owns this symbol -- musl Linux would pass a ``sys.platform`` check and then
    fail the lookup. (It also keeps the body reachable for a type checker running on macOS,
    which narrows ``sys.platform`` to a literal and would call everything below it dead.)
    """
    return analysis_decoder._resolve_malloc_trim(platform_module=platform, ctypes_module=ctypes)


_MALLOC_TRIM: Callable[[int], int] | None = _resolve_malloc_trim()


def _malloc_trim() -> None:
    """Hand the decode pass's freed transient back to the OS (phaze-rc1q rec. 4).

    ``free()`` returns a block to glibc's arena, not to the kernel, and glibc only trims
    the top of the main arena on its own. The streaming fan-out (:func:`_decode_windows`)
    frees a transient roughly twice the size of a tier's PCM the instant its ``Pool`` is
    dropped; TensorFlow's own arena allocations in the model sweep that follows do NOT
    reuse those retained pages, so without this call the sweep's 2.5 GiB stacks *on top of*
    the decode transient instead of sitting under it.

    **This is a BACKSTOP, and the measurement says so.** phaze-rc1q 6b priced it at
    ``-0.403 GiB`` for ``+0.13%`` wall -- but every variant that spike measured kept the
    ``Pool`` alive through the sweep, so most of what its trim reclaimed was ``Pool`` slack
    that :func:`_decode_windows_streaming` now never holds. Ablated one knob at a time on THIS
    implementation (phaze-5lop, vox, deployed image, 60-min file at saturated caps, 1.3999 GiB
    baseline): dropping the ``Pool`` keys is worth **-0.3495 GiB**, and this trim on top of it
    only **-0.0106 GiB**. Kept anyway because it costs nothing measurable (-0.2%, inside
    run-to-run spread) and because the same ablation shows it worth **3x more** (-0.0299 GiB)
    when a large freed transient IS retained -- which is exactly the regression it is here to
    absorb. phaze-7i0k 4 measured the same call worth 0.0% against the pre-phaze-5lop workload
    and was right: that workload freed no large transient at all.

    Never raises -- a failed trim is a missed optimisation, not an analysis failure.
    """
    analysis_decoder._malloc_trim(_MALLOC_TRIM, log)


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
    """Run a single model prediction and return mean activations.

    ``classifier(audio_16k)`` is documented (and normally observed) to return a 2D
    ``[patches, classes]`` array, averaged over ``axis=0`` into the per-class mean
    activation vector callers expect. But on a short enough buffer -- the trailing
    coarse/fine window of a short file is the reproducing case (phaze-ouz0y) -- essentia
    squeezes the patch dimension away and returns a bare 1D ``[classes]`` vector instead.
    ``np.mean`` on a 1D array with ``axis=0`` has no batch axis left to reduce over, so it
    collapses the WHOLE vector to a single 0-d ``numpy.float64`` scalar. That scalar then
    reaches ``zip(labels, predictions)`` in ``_run_model_sets_over_windows``, and ``zip``
    raises exactly the production crash: ``TypeError: 'numpy.float64' object is not
    iterable``.

    ``np.atleast_2d`` normalizes the single-patch case by prepending a length-1 batch axis
    (``[classes]`` -> ``[1, classes]``) before the mean, so the reduction always has a real
    axis to collapse and always returns the ``[classes]`` vector callers need -- a no-op for
    the normal ``[patches, classes]`` shape, since ``atleast_2d`` never touches an
    already-2D array.
    """
    classifier = _get_classifier(model, models_dir)
    activations = classifier(audio_16k)
    return np.mean(np.atleast_2d(activations), axis=0)


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
# Observability signals -- one seam for progress + liveness (phaze-mp0op)
# ---------------------------------------------------------------------------
#
# phaze-w55w1's heartbeat feature added exactly ONE signal (liveness) and had to thread an
# optional callback param through six functions, each with its own hand-written
# ``if cb is not None:`` guard, to do it -- on top of the ``progress_cb`` signal already
# paying the same tax on a narrower path since Phase 57.1. AnalysisSignals collapses both
# into one object with two TOTAL methods: an unset channel is a no-op call, never a ``None``
# a caller has to test, so the guards disappear from every call site instead of multiplying
# with every new signal.
#
# Carries NO I/O and no transport -- it emits an ``(int, int)`` progress count or a
# ``(str, int, int)`` stage beat exactly as the two raw callbacks did. Throttling and the
# HTTP/pickle boundary stay downstream in the lane bridge (``analysis_child.py`` /
# ``services/analysis_exec.py``), so this seam stays importable by the essentia child.
@dataclass(frozen=True)
class AnalysisSignals:
    """Progress + liveness callbacks for one ``analyze_file`` run, collapsed into one seam."""

    progress_cb: Callable[[int, int], None] | None = None
    heartbeat_cb: Callable[[str, int, int], None] | None = None

    def progress(self, analyzed: int, total: int) -> None:
        """Fire the UI progress channel (fine tier only). A no-op when unset."""
        if self.progress_cb is not None:
            self.progress_cb(analyzed, total)

    def beat(self, stage: str, done: int, total: int) -> None:
        """Fire the liveness channel. A no-op when unset."""
        if self.heartbeat_cb is not None:
            self.heartbeat_cb(stage, done, total)


NO_SIGNALS = AnalysisSignals()


def _noop() -> None:
    """Total default for ``_decode_windows``'s ``on_beat`` / ``_run_model_sets_over_windows``'s ``on_model_done``."""


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

# ---------------------------------------------------------------------------
# D-07 DECISION RECORD -- exhaustive analysis, bounded by CHUNK not by CAP (phaze-w55w1)
# ---------------------------------------------------------------------------
#
# WHAT CHANGED. Phase 43 bounded per-file cost with a window CAP (`analysis_fine_cap` = 60 /
# `analysis_coarse_cap` = 30) and an even stride (`_stride_to_cap`) across the whole file, so a
# file past 30 min (fine) / 90 min (coarse) was SAMPLED, not analyzed. The operator reviewed that
# trade on 2026-08-11 and removed it (ADR-0007 section 7): every file now gets EVERY natural
# window in both tiers, and `sampled` / `_stride_to_cap` / the per-job cap overrides are gone.
#
# WHY THE CAPS COULD NOT SIMPLY BE DELETED. Both passes held ALL of a tier's kept windows' PCM
# concurrently -- deliberately, because the coarse inference is MODEL-major (phaze-15sw: one TF
# graph built, swept across every buffer, released before the next; the alternative held ~4 GiB of
# co-resident graphs). The cap was what made "all kept windows" a CONSTANT. Delete the cap and
# that retention becomes O(duration): ADR-0007 section 3 extrapolates ~7.3 GiB of fine PCM and
# ~2.8 GiB of coarse PCM on a 12-hour set, against a measured whole-process floor of 1.7383 GiB
# and the ADR-0005 pod memory limit sized from it. That is a cgroup OOMKill, not an analysis.
#
# THE REPLACEMENT. The tiers now process their windows in bounded CHUNKS: decode a chunk, analyze
# it, release it, move on. Peak PCM residency is a function of the CHUNK SIZE and never of the
# file's duration -- the same invariant the caps provided ("retention stays bounded by the CAP and
# never by duration"), re-derived from a knob that no longer discards audio.
#
# ^ TRUE OF THE PCM, AND IT WAS NOT TRUE OF THE PROCESS UNTIL D-09. As shipped, this paragraph
# was read as a statement about peak RSS and phaze-b2qs9 measured that it was not one: the chunk
# GATE this design needs leaked its whole streaming network per chunk, so peak RSS was linear in
# duration and breached the 4Gi pod limit at four hours. The live-PCM arithmetic below is correct
# and always was; what it never covered is the network carrying the PCM. See D-09.
#
# THE CHUNK SIZES ARE THE OLD CAPS, ON PURPOSE. 60 fine / 30 coarse reproduce EXACTLY the
# per-tier residency the pre-removal code was measured at, so the whole ADR-0005 / phaze-esut /
# phaze-7i0k / phaze-0582 memory corpus stays valid unchanged rather than needing re-derivation:
#
#   FINE   60 windows x 30 s x 44 100 Hz x 4 B (float32)  ~= 317 MB peak PCM per chunk
#   COARSE 30 windows x 180 s x 16 000 Hz x 4 B (float32) ~= 345 MB peak PCM per chunk
#
# The two never stack: the fine tier's chunk is released and `malloc_trim`ed before the coarse
# tier runs, exactly as before. The model-major invariant is PRESERVED and is per-chunk, not
# per-file: within a chunk exactly one `TensorflowPredict*` graph is ever resident. The price the
# chunking DOES pay is that each of the 34 graphs is now constructed once per COARSE CHUNK
# instead of once per file (34 x ceil(coarse_windows / 30) constructions), and each chunk needs
# its own decode pass because `MonoLoader` cannot seek -- see `_decode_windows_streaming`'s
# `stop_at_sec` gate for how much of that second cost is bought back.
#
# MEASURED SINCE. This was written as "NOT MEASURED HERE ... established by construction", and
# that gap is exactly where the defect lived: phaze-b2qs9 did the owed measurement on vox and
# found peak RSS linear in duration, and phaze-u1n7j then found the retention and closed it
# (D-09). The per-window arithmetic above is still arithmetic, not a peak-RSS measurement -- but
# the SHAPE it claims is now enforced by a test that runs the real streaming network rather than
# argued from construction (`test_repeated_gated_chunk_decodes_do_not_grow_peak_rss`).
_FINE_CHUNK_WINDOWS = 60
_COARSE_CHUNK_WINDOWS = 30


# ---------------------------------------------------------------------------
# D-09 DECISION RECORD -- a streaming network must be DISCONNECTED, not just dropped (phaze-u1n7j)
# ---------------------------------------------------------------------------
#
# THE BUG D-07 SHIPPED. D-07 below claims the chunked passes make peak memory "a function of
# the CHUNK SIZE and never of the total duration". phaze-b2qs9 measured that on real hardware
# and real audio and it is FALSE of the process: whole-process peak RSS is LINEAR in duration,
# `0.7634 + 0.3108 x n_fine_chunks` GiB (R^2 0.99959), reaching 4.1854 GiB at four hours and
# 10.2768 GiB at twelve -- against a deployed `memory_limit` of 4Gi. Every file past ~3 hours
# was an OOMKilled pod. D-07's live-PCM arithmetic is correct and simply does not describe the
# process, because it accounts for the audio and not for the NETWORK that carries it.
#
# THE MECHANISM. `essentia.streaming` connections are C++-side. `>>` calls into
# `_essentia.connect` / `_essentia.poolConnect`, which allocate a fixed-size ring buffer per
# connected source (essentia's `BufferUsage::forLargeAudioStream`) and, for a Pool sink, an
# entire `PoolStorage` algorithm that Python never holds a reference to at all -- `poolConnect`
# returns it and the caller drops it on the floor. Dropping the Python proxies (`branches
# .clear()`, `del loader, pool, gate`) frees the PROXIES and leaves all of that behind, because
# essentia's Python bindings expose no network object and therefore no destructor that walks it.
# `malloc_trim` cannot help: the pages are still live-referenced, not merely un-returned to the
# OS. `gc.collect()` alone does not help either -- measured, it reclaims 210-231 objects per call
# and leaves the curve unchanged -- but it turns out to be REQUIRED as the second half of the
# fix, for the separate reason below.
#
# MEASURED, not inferred (macOS/arm64, essentia-tensorflow 2.1b6.dev1438, one process, RSS read
# from `ps` / `ru_maxrss` after `gc.collect()`, repeated calls with the SAME file and window set,
# so any growth is retention and not the audio):
#
#   leak per `_decode_windows_streaming` call = ~5 MiB x n_windows. It is per BRANCH, not per
#   sample: 10 / 20 / 40 windows leak 50.2 / 100.4 / 200.9 MB, while 20 windows of 15 s, 30 s
#   and 60 s all leak the same 100.4 MB. Per-branch retention held at 5.03-5.34 MiB at 44.1 kHz
#   and 4.22-5.16 MiB at 16 kHz across every shape tried.
#
# AND IT IS THE CHUNK GATE THAT DOES IT -- the single most useful fact here, because it is what
# makes the bug phaze-w55w1's and not phaze-5lop's. Holding file, window set and span fixed and
# varying ONLY `stop_at_sec`: gated leaks 5.14 MiB/branch, ungated leaks 0.02. The gate is the
# `Trimmer` hung STRAIGHT off the loader with no `Scale` interposer (see
# `_decode_windows_streaming`'s docstring for why it must be), and a network that is merely
# BUILT with one and never run already leaks the full amount, so the retention is created at
# CONNECT time and not by the decode. A bare `MonoLoader`, a loader plus gate with no fan-out,
# and unconnected `Trimmer`/`Scale` algorithms are all FLAT across rounds. That also explains
# phaze-b2qs9 §2b's control cleanly: the pre-w55w1 capped code has no gate, and its peak moved
# +1.3% across a 4x duration span.
#
# Two corollaries worth keeping, because both mislead:
#   - The per-branch constant predicts BOTH of phaze-b2qs9's slopes from one number: 60 fine
#     branches x ~5 MiB = ~0.29 GiB per fine chunk (measured +0.3108) and 30 coarse branches
#     x ~5 MiB = ~0.15 GiB per coarse chunk (measured +0.13 +/- 0.02).
#   - The near-match to the ~317 MB of live fine PCM is a COINCIDENCE. The leak does not scale
#     with window length at all, and reading it as "the PCM is retained" sends the next
#     investigation at the buffers instead of at the edges.
#
# THE FIX, AND IT HAS TWO HALVES -- EITHER ALONE LEAVES THE CURVE LINEAR.
#
#   1. DISCONNECT every edge before dropping the algorithms. `_StreamConnector.disconnect` wraps
#      `_essentia.disconnect` / `poolDisconnect`, the only exposed calls that release the C++
#      side, so `_disconnect_network` walks each algorithm's `connections` map and severs it.
#   2. `gc.collect()` after the frame is gone. Every streaming algorithm is born into a
#      reference CYCLE: `StreamingAlgo.__init__` builds a `_StreamConnector(self, self, name)`
#      and stores it as a KEY of `self.connections`, so the algorithm reaches itself through its
#      own connection map. Refcounting therefore never destroys the proxy, and the C++
#      algorithm -- which owns buffers of its own -- dies only when the proxy does. The collect
#      lives in `_decode_windows`, not in the `finally` below, because inside that `finally` the
#      frame's own locals (`source` still holds the gate's connector) keep the network reachable
#      and the collect would be a no-op.
#
# Measured on the same probe, 20 windows x 8 calls of the SHIPPED function: before, RSS
# 650 -> 1181 MB still climbing +106 MB/call; after, high-water reaches 654.7 MB on call 1 and
# does not move again through call 8. Decoded output is byte-identical (asserted in
# `tests/analyze/services/pipeline/test_analysis_streaming_decode.py`).
#
# WHY IT RUNS IN A `finally` AND SWALLOWS. A half-built network -- the build raised partway
# through the branch loop -- still holds edges, and that is exactly the path where leaking is
# least affordable because `_decode_windows` is about to RETRY the same chunk ungated. The
# teardown therefore runs on every exit and never raises: a disconnect that fails must not
# replace the caller's exception with its own, so it is logged and the walk continues.
#
# VERIFIED ON VOX, 2026-08-13 -- the numbers above are macOS, and this paragraph is the reason
# they are not the whole story. `docs/spikes/phaze-u1n7j-vox-fix-verification.md` has the full
# record; the load-bearing results, all on the deployed image against the SAME real corpus files
# phaze-b2qs9 measured, on the same node, peak from `wait4()` `ru_maxrss`:
#
#   band          fine chunks   before (GiB)   after (GiB)   wall delta
#   1:00                    2        2.1107        1.4985       -0.22%
#   4:00                    8        4.1854        1.6500       -0.52%
#   12:04                  25       10.2768        1.6725       -0.83%
#
# Three things in that table are worth more than the headline. **The 12-hour file needed 2.57x
# the deployed 4Gi limit and now sits at 41.8% of it.** **Wall clock did not move** -- this fix
# recreates nothing per chunk, it severs edges the teardown already meant to release, and the
# isolated chunk loop measured 60.0-60.7 s per gated chunk on both arms. And the same file
# analyzed before and after returns a **byte-identical** 128 118-byte result payload, every
# window and every feature, so the `gc.collect()` through the reference cycle perturbs nothing.
#
# READ THE RESIDUE CORRECTLY, because the obvious reading is wrong. 1:00 -> 12:04 is +11.6%, but
# that is not a slope: `_COARSE_CHUNK_WINDOWS = 30` and the 1-hour file is the only one whose
# coarse chunk is PART FULL (20 windows), so it sits one bounded step below every longer file.
# Between the two bands that both have full coarse chunks the spread is **+1.4% for 3.1x the
# fine chunks** -- a residual 0.0013 GiB/chunk against the defect's 0.3108, i.e. 99.6% of the
# slope gone, and what is left is capped by the chunk size rather than by the file's length.
#
# AND IT IS NOT GLIBC. Arena fragmentation was the standing candidate a macOS-only diagnosis
# could not rule out. Under Debian 13 / glibc 2.41 the shipped chunk loop retains 5.42 MiB per
# window branch before this fix (macOS: 5.14) and 0.00 after, and `scripts/
# essentia_gated_network_leak.py` -- which imports NO phaze at all -- shows the same 5.28 MiB
# per branch and goes flat under `--teardown disconnect`. Two allocators, one constant. Its
# `--no-gate` arm is flat on the broken teardown, which is what pins the retention to the GATE.
def _disconnect_network(algos: Sequence[Any]) -> None:
    """Sever every connection held by ``algos`` so essentia releases the C++ side (D-09).

    Dropping a streaming algorithm's Python proxy does NOT free its connection buffers or the
    ``PoolStorage`` a Pool sink creates; only an explicit disconnect does. Without this, each
    chunk decode leaks ~5 MB per window branch and whole-process peak RSS grows linearly with
    the file's duration until the pod is OOMKilled (phaze-b2qs9 measured it; phaze-u1n7j found
    it here).

    Tolerant by construction: it accepts a partially built network (``None`` entries, algorithms
    with no ``connections`` map, edges already severed) because its most important caller is the
    failure path. It never raises.
    """
    analysis_decoder._disconnect_network(algos, log)


def _release_decode_network() -> None:
    """Complete a chunk decode's teardown: collect the network's proxies, then trim (D-09).

    The second half of the phaze-u1n7j fix, and it must run OUTSIDE
    :func:`_decode_windows_streaming` -- that function's own ``finally`` still has ``source``
    bound to the gate's connector, so the network is reachable there and a collect would do
    nothing. By the time this runs the frame is gone and the only thing keeping each streaming
    algorithm alive is the reference cycle it was born with (``self.connections`` is keyed by a
    connector holding ``self``), which is exactly what the cyclic collector is for.

    The ``malloc_trim`` that follows is unchanged and still the phaze-rc1q backstop; it is
    ordered after the collect deliberately, so it trims pages the collect has just freed rather
    than pages it is about to.
    """
    analysis_decoder._release_decode_network(collect=gc.collect, trim=_malloc_trim)


def _chunked(windows: list[tuple[int, float, float]], size: int) -> list[list[tuple[int, float, float]]]:
    """Split a tier's window list into consecutive chunks of at most ``size`` entries.

    Window order and original indices are preserved (chunk boundaries are the ONLY thing
    introduced), so a chunked run analyzes the identical window set an unchunked one would.
    An empty input yields no chunks, so a zero-window tier does no decode work at all.
    """
    return analysis_decoder._chunked(windows, size)


def _probe_duration_sec(file_path: str) -> float:
    """Return total audio duration in seconds WITHOUT materializing PCM, via ``ffprobe``.

    D-10 DECISION RECORD -- ffprobe, not ``es.MetadataReader`` (phaze-l832u)
    ----------------------------------------------------------------------
    This probe used to read ``es.MetadataReader`` output index 8. MetadataReader is TagLib,
    and **TagLib cannot read a duration out of Matroska**: it returns ``0``. That was harmless
    while every file reaching :func:`analyze_file` was the archive's own mp3/m4a/ogg, and fatal
    the moment phaze-3ea41 put an unconditional pre-analysis remux to ``.mka`` in front of it --
    ``total_sec == 0`` makes :func:`_iter_windows` yield nothing, both ``*_total`` counts land on
    0, and the zero-window guard fails the file after "analyzing" it in ~4 s. Measured inside the
    deployed 2026.8.3 agent image, on one file, three ways::

        original .mp3   MetadataReader duration = 90
        remuxed  .mka   MetadataReader duration = 0
        remuxed  .mka   ffprobe        duration = 90.044000

    Only the metadata probe was ever broken -- essentia DECODES the ``.mka`` correctly (the same
    measurement got 2/2 windows out of it). So the fix is the probe, not the container: ffprobe
    reads a duration out of every container the pipeline can produce, including the ``.mka``
    extraction intermediate a VIDEO container still legitimately goes through (which is why
    merely gating the remux would have left phaze-3ea41's actual feature broken in exactly the
    same way -- see the epic's design note).

    ffprobe is the sanctioned tool here (never the abandoned ``ffmpeg-python``) and is already
    how ``services/video_audio.py::probe_container_streams`` talks to the same binary; this is the
    established invocation shape, not a third style. Like MetadataReader it reads container and
    stream headers only -- it never decodes PCM.

    The longest AUDIO STREAM's own ``duration`` is preferred, with the container-level
    ``format.duration`` as the fallback -- see :func:`_duration_from_ffprobe_payload` for why
    that order and not the other. A failure here stays FATAL and propagates as
    :class:`AnalysisProbeError` -- this function is the reason a mis-suffixed or undecodable
    download fails loudly instead of recording a NULL-everything success.

    Deliberately UNBOUNDED (no ``timeout=``), matching the MetadataReader call it replaces and
    ``video_audio.py``'s own ffprobe: a header read is not a wall clock to bound (D-01 /
    phaze-1b39), and a genuinely wedged probe is already covered by the analysis child's
    progress-based stall watchdog (D-08) in the lane above.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:stream=duration",
        "-select_streams",
        "a",
        file_path,
    ]
    try:
        # Fixed argv, never a shell, no interpolation (services/analysis_sizing.py convention).
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603  # nosec B603
    except OSError as exc:
        msg = f"ffprobe could not be run to probe the duration of {file_path!r}: {exc}"
        raise AnalysisProbeError(msg) from exc
    if proc.returncode != 0:
        msg = f"ffprobe failed (exit {proc.returncode}) probing the duration of {file_path!r}: {proc.stderr.strip()[:_PROBE_STDERR_MAX]}"
        raise AnalysisProbeError(msg)
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        msg = f"ffprobe produced non-JSON output probing the duration of {file_path!r}"
        raise AnalysisProbeError(msg) from exc
    duration = _duration_from_ffprobe_payload(payload)
    if duration is None:
        msg = f"ffprobe reported no readable audio duration for {file_path!r}"
        raise AnalysisProbeError(msg)
    return duration


def _duration_from_ffprobe_payload(payload: Any) -> float | None:
    """Pull a duration (seconds) out of ffprobe's JSON, or ``None`` when it carries none.

    The caller's argv selects AUDIO streams only, so ``streams[].duration`` is the length of
    the material :func:`analyze_file` will actually window -- preferred over the container-level
    ``format.duration``, which on a container carrying anything besides that audio (a video
    track running past the end of the audio, a trailing chapter/attachment) describes the
    CONTAINER's span rather than the audio's, and would hand ``_iter_windows`` windows that lie
    past the last sample. ``format.duration`` is the fallback because Matroska -- the very
    container the extraction step produces -- routinely records its duration at container level
    and reports ``N/A`` per stream, which is precisely the case that must not come back 0.

    ffprobe writes the literal ``"N/A"`` for an unknown duration: any entry that fails to parse,
    or is not strictly positive, is skipped rather than allowed to become a silent 0.0 (the
    exact shape this whole function exists to stop returning). ``None`` means every candidate
    was unusable, which the caller turns into a hard failure.
    """

    def _longest(values: list[Any]) -> float | None:
        best: float | None = None
        for raw in values:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and (best is None or value > best):
                best = value
        return best

    if not isinstance(payload, dict):
        return None
    streams = payload.get("streams")
    stream_durations = [s.get("duration") for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []
    from_streams = _longest(stream_durations)
    if from_streams is not None:
        return from_streams
    fmt = payload.get("format")
    return _longest([fmt.get("duration")]) if isinstance(fmt, dict) else None


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


def _decode_windows_streaming(
    file_path: str,
    sample_rate: int,
    windows: Sequence[tuple[int, float, float]],
    *,
    stop_at_sec: float | None = None,
) -> dict[int, Any]:
    """Decode EVERY window of one chunk in a SINGLE streaming pass (phaze-5lop).

    Returns ``{window_index: float32 buffer}``, omitting any window the pass produced no
    audio for. Raises if the network itself cannot be built or run -- :func:`_decode_windows`
    owns that fallback.

    **Why this exists.** ``es.EasyLoader`` does not seek: standard ``EasyLoader`` wraps the
    *streaming* ``EasyLoader`` composite, so ``Trimmer``'s "tell my parent to stop" optimisation
    (``trimmer.cpp``, a line upstream itself marks ``FIXME``) cannot cross the ``MonoLoader``
    composite boundary and the file is decoded and resampled from byte 0 for **every** window.
    Per-file decode was therefore ``O(n_windows x total_duration)`` -- 6.15 hours of
    single-threaded libsamplerate for a 12-hour file at production caps (phaze-esut 8,
    remeasured phaze-rc1q 4a). This function decodes once per tier and fans the ONE resampled
    stream out to a ``Trimmer`` per window, which is 3.5x / 10.9x / ~15x / ~18x faster on
    decode at 10 / 60 / 180 / 720 minutes for byte-identical buffers (phaze-rc1q 4, 5).

    **Three upstream traps this shape closes** -- all read out of ``MTG/essentia@master``,
    none of them stylistic:

    1. ``MonoLoader`` per TIER, not one shared loader fanned to both. ``monoloader.cpp`` builds
       ``AudioLoader -> MonoMixer -> Resample`` unconditionally with no ratio-1.0 short circuit,
       so hanging a second ``Resample(44100->16000)`` off a 44.1 kHz loader would run the coarse
       tier through TWO libsamplerate passes and produce a different signal than
       ``EasyLoader(sampleRate=16000)`` does. One loader per tier is bit-for-bit each
       ``EasyLoader``'s own internal chain -- which is also why this takes no ``inputSampleRate``:
       ``MonoLoader`` reads the native rate off ``AudioLoader`` at configure time, exactly as
       ``EasyLoader`` does. (phaze-rc1q 3a)
    1. ``Scale(factor=1.0)`` is interposed per branch and is LOAD-BEARING, not decorative.
       A streaming ``Trimmer`` calls ``_input.source()->parent()->shouldStop(true)`` when it
       reaches ``endTime``; hung straight off the shared loader, the earliest-ending window
       would shut the shared decode down and truncate every other window. The interposer gives
       each ``Trimmer`` a private parent to stop. It is ``EasyLoader``'s own composition
       (``db2amp(-6+6) == 1.0``, same default clipping), so it is free of semantic risk and
       costs 13% of the fine tier / 0.6% of a full ``analyze_file``. (phaze-rc1q 3b, 7c)
    1. The buffer is extracted and its ``Pool`` key **removed immediately**, so the ``Pool``'s
       copy dies with the loop iteration instead of staying live through the model sweep.
       ``pool[key]`` copies (verified: two reads do not alias, and an extracted array survives
       ``pool.remove``), so leaving the keys in place holds every window's PCM TWICE. **Measured
       by ablating this one line on the deployed image, 60-min file at saturated caps: peak
       1.7383 -> 2.0878 GiB, +0.3495, for 1.1% less wall.** It is the single largest term in the
       change's memory cost -- larger than the ``malloc_trim`` below by 33x -- and without it a
       decode this fast would have cost `phaze-3j67` a pod sizing tier. (phaze-rc1q rec. 3)

    Not applicable here, recorded so it is not rediscovered: the standard/streaming
    normalization divergence upstream documents is ``TensorflowPredictFSDSINet``-only, and
    phaze runs none of it -- musicnn / vggish / effnet normalize in NEITHER mode. The one
    mode-coupled behaviour that does touch phaze's set is ``EffnetDiscogs`` batch padding, and
    it cannot bind because the models stay in standard mode; only the DECODE moves.
    (phaze-rc1q 3c, 3d)

    **``stop_at_sec`` -- the chunk gate (phaze-w55w1).** Exhaustive analysis processes a tier in
    bounded chunks (D-07), and each chunk needs its own decode because ``MonoLoader`` cannot
    seek. Left ungated that would decode the WHOLE file once per chunk. The gate turns trap 2
    above from a hazard into the mechanism: a ``Trimmer`` hung STRAIGHT off the loader (no
    ``Scale`` interposer) is exactly what stops the shared decode, so one interposed at the head
    of the fan-out with ``endTime`` just past the chunk's last window ends the decode there.
    ``duration x (K + 1) / 2`` is the AUDIO VOLUME a gated run decodes across ``K`` chunks,
    against ``K x duration`` ungated -- it is not a wall-clock model. phaze-b2qs9 §4 measured the
    gate directly: it IS taken on the deployed essentia (zero fallbacks, full window sets, every
    non-final chunk of every tier of every file) and it IS worth something -- **18.5-36.0%** of a
    non-final chunk's decode in a controlled A/B (§4c, §4e) -- but per-chunk decode time is NOT
    proportional to the chunk boundary. Across every full run it instead FALLS monotonically with
    chunk index, and the ungated final chunk -- which decodes the whole file -- is consistently
    the CHEAPEST chunk of all (§4b, §4d); a fine chunk admitting 11x the audio of chunk 0 cost
    only 1.16x the wall clock. Treat the formula as the volume it is, not the saving it was
    written to claim. It is still an OPTIMISATION only: ``startTime`` stays 0 so every window
    keeps absolute file time, the margin (``analysis_decoder._CHUNK_GATE_MARGIN_SEC``) guarantees the last
    window of the chunk is complete, and the final chunk is passed ``stop_at_sec=None`` because
    it runs to EOF anyway. If the gated network cannot be built or run, :func:`_decode_windows`
    retries UNGATED before falling back to the per-window loader, so a gate that misbehaves on
    some future essentia costs wall clock and never correctness.
    """
    runtime = analysis_decoder.StreamingRuntime(
        pool=essentia.Pool,
        mono_loader=ess.MonoLoader,
        scale=ess.Scale,
        trimmer=ess.Trimmer,
        run=essentia.run,
        disconnect_network=_disconnect_network,
    )
    return analysis_decoder._decode_windows_streaming(
        file_path,
        sample_rate,
        windows,
        runtime=runtime,
        stop_at_sec=stop_at_sec,
    )


def _decode_windows(
    file_path: str,
    sample_rate: int,
    windows: Sequence[tuple[int, float, float]],
    on_skip: Callable[[int, float, float, bool], None],
    *,
    stop_at_sec: float | None = None,
    on_beat: Callable[[], None] = _noop,
) -> dict[int, Any]:
    """Decode one chunk's windows, streaming fan-out first, per-window ``EasyLoader`` as fallback.

    Returns ``{window_index: buffer}`` for the windows that decoded. ``on_skip(idx, start, end,
    exc_info)`` fires once per window that did not, with ``exc_info`` False when there is no
    live exception to render (a window the streaming pass simply produced no audio for).

    The fallback is not defensive padding: it is what preserves phaze-zibn's contract. The
    per-window loop isolates a bad window from the rest of the file, and a single shared network
    cannot -- one raise takes the whole tier. So a network-level failure drops back to the
    decode this replaced, which then fails (or skips) exactly the windows it always did, and
    ``analyze_file``'s all-windows-failed floor still sees the same evidence.

    The ``malloc_trim`` is here rather than at the call sites because THIS is where the fan-out's
    transient dies: the ``Pool`` doubling slack is freed the moment the loop above finishes, and
    glibc keeps those pages unless asked. (phaze-rc1q rec. 4 -- measured -0.403 GiB for +0.13% wall.)

    ``stop_at_sec`` is the chunk gate (phaze-w55w1). It is a pure wall-clock optimisation, so it
    gets its OWN retry rung: a gated pass that fails is retried UNGATED before the per-window
    fallback engages. Without that rung a gate broken by some future essentia would demote every
    chunk of every file to the ``O(n_windows x duration)`` decode phaze-5lop removed -- a far
    worse outcome than simply decoding each chunk from byte 0.

    **``on_beat`` fires once per window of the FALLBACK loop, and it is not optional dressing.**
    That loop is the longest silent stretch anywhere in the pipeline: each ``EasyLoader`` call
    re-decodes the file from byte 0 (it cannot seek -- the whole reason phaze-5lop exists), so
    on a long file a single window takes minutes and a chunk takes hours, with nothing emitted
    in between. Under the stall watchdog (D-08) that silence is indistinguishable from a hang,
    so an UNBEATEN fallback would be reliably killed and the file terminally marked
    ANALYSIS_FAILED -- the degradation path destroying exactly the work it exists to salvage.
    The beat says "still decoding", which is true and is all the watchdog needs.

    The streaming rungs run under a watchdog because ``essentia.run`` is one blocking C++
    call.  The watchdog beats every 60 seconds until that call returns, decoupling D-08's
    silence threshold from file duration and source sample rate.
    """

    runtime = analysis_decoder.DecodeRuntime(
        streaming_decode=_decode_windows_streaming,
        easy_loader=es.EasyLoader,
        release_decode_network=_release_decode_network,
        logger=log,
        heartbeat_interval_sec=_DECODE_HEARTBEAT_INTERVAL_SEC,
    )
    return analysis_decoder._decode_windows(
        file_path,
        sample_rate,
        windows,
        on_skip,
        runtime=runtime,
        stop_at_sec=stop_at_sec,
        on_beat=on_beat,
        watchdog_enabled=on_beat is not _noop,
    )


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
    on_model_done: Callable[[], None] = _noop,
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Run all 11 characteristic model sets + the genre model over EVERY coarse buffer of a chunk.

    **Model-major** (phaze-15sw): models are the outer loop, windows the inner one, so
    exactly one ``TensorflowPredict*`` graph is resident at any instant instead of 34.
    The price is holding the chunk's decoded buffers concurrently
    (:data:`_COARSE_CHUNK_WINDOWS` x 180 s x 16 kHz x 4 B ~= 345 MB) instead of 4.09 GiB of
    idle graphs. See the ``_classifier_cache`` comment for the measurement that motivated
    the reprice.

    Since phaze-w55w1 the CALLER passes one chunk at a time rather than the whole file, so
    each graph is constructed once per chunk instead of once per file. That is the cost the
    D-07 chunking pays to keep PCM residency independent of duration; the co-residency
    invariant this function exists to hold -- ONE graph at a time -- is unchanged, and it is
    the invariant that was worth 4 GiB.

    ``on_failure(window_index)`` fires once, in-handler, for each window an inference
    fails on; that window is excluded from every later model. ``on_model_done()`` fires
    after each of the 34 sweeps completes -- the liveness heartbeat for a chunk that can
    otherwise spend many minutes inside C++ without emitting a window completion.

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
            on_model_done()

    for key, (genre_predictions, genre_labels) in _sweep_one_model(GENRE_MODEL, buffers, models_dir, failed, on_failure).items():
        genre_pairs = list(zip(genre_labels, genre_predictions, strict=False))
        genre_pairs.sort(key=lambda pair: float(pair[1]), reverse=True)
        features[key]["genre"] = {
            "predictions": [{"label": label, "confidence": float(conf)} for label, conf in genre_pairs[:10]],
        }
    on_model_done()

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


def _chunk_stop_sec(chunks: list[list[tuple[int, float, float]]], position: int) -> float | None:
    """``endTime`` for a chunk's decode gate, or ``None`` for the last chunk.

    The last chunk runs to EOF, so gating it buys nothing and could only risk clipping the
    file's final window; every earlier chunk stops just past its own last window's end.
    """
    if position >= len(chunks) - 1:
        return None
    return chunks[position][-1][2]


def _analyze_fine_windows(
    file_path: str,
    total_sec: float,
    win_sec: int,
    min_sec: int,
    *,
    signals: AnalysisSignals = NO_SIGNALS,
) -> tuple[list[FineWindow], int]:
    """FINE pass: BPM + key for EVERY ``win_sec`` window, in bounded 44.1 kHz chunks.

    Returns ``(windows, total)`` where ``total`` is the natural window count. Since
    phaze-w55w1 every natural window is analyzed -- there is no stride and no cap -- so
    ``len(windows)`` equals ``total`` except for per-window failures, which are skipped.

    **Chunking (D-07).** The tier is processed :data:`_FINE_CHUNK_WINDOWS` windows at a time:
    decode the chunk, extract BPM/key from it, drop it, trim, move on. Peak PCM residency is
    therefore ``chunk x 30 s x 44 100 Hz x 4 B ~= 317 MB`` -- the SAME figure the capped
    implementation was measured at, and independent of the file's duration. Each chunk needs
    its own streaming decode (``MonoLoader`` cannot seek); the non-final chunks pass a
    ``stop_at_sec`` gate so their decode ends at the chunk boundary instead of at EOF.

    Phase 57.1 (PROG-01): ``signals.progress`` fires a START signal ``signals.progress(0,
    len(natural))`` BEFORE the loop and then ``signals.progress(len(fine_windows),
    len(natural))`` after every successful append. The denominator is the natural count --
    IDENTICAL to the ``fine_windows_total`` the completion PUT reports, so the in-flight bar
    and final coverage agree (denominator invariant). This seam emits only an ``(int, int)``
    count and does NO I/O; throttling and transport live DOWNSTREAM in the lane bridge, never
    here (keeps the compute seam HTTP/pickle-free).

    phaze-w55w1 adds ``signals.beat(stage, done, total)`` alongside it: the LIVENESS channel.
    It fires on window completions AND on chunk-decode boundaries, because the supervising
    layer now kills only on absence of progress (never on elapsed time) and a chunk decode is
    the longest stretch of this pass that completes no windows. ``signals.progress`` cannot
    serve that role: it is fine-tier-only by design (WORK-04) and says nothing during decode.

    ``RhythmExtractor2013`` and ``KeyExtractor`` are constructed ONCE per file and reused
    across every window and every chunk (phaze-ap8y), not rebuilt per window: neither takes a
    per-window parameter, and construction was 7.55 s of the 31.50 s fine tier on a 60-window
    file (measured, phaze-i93a §6a). ``reset()`` between windows was verified NOT required —
    0/60 output mismatches with and without it, across the full ``(window_index, bpm, key,
    confidence)`` tuple — so it is deliberately not called here.

    Decode failures within a chunk are discovered, and logged, before that chunk's extraction
    rather than interleaved with it (phaze-5lop): only the ORDER of the warnings moves, never
    which windows are dropped.
    """
    natural = _iter_windows(total_sec, win_sec, min_sec, drop_short_trailing=True)
    total = len(natural)
    signals.progress(0, total)  # START: analyzed=0, total=natural

    def _skip(idx: int, start: float, end: float, exc_info: bool = True) -> None:
        log.warning("fine window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=exc_info)

    rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
    key_extractor = es.KeyExtractor(profileType="edma")
    fine_windows: list[FineWindow] = []
    chunks = _chunked(natural, _FINE_CHUNK_WINDOWS)
    for position, chunk in enumerate(chunks):
        signals.beat("fine_decode", len(fine_windows), total)
        decoded = _decode_windows(
            file_path,
            _FINE_SAMPLE_RATE,
            chunk,
            _skip,
            stop_at_sec=_chunk_stop_sec(chunks, position),
            # Keeps the per-window EasyLoader fallback audible to the stall watchdog; see
            # _decode_windows' docstring for why an unbeaten fallback is always killed.
            on_beat=lambda: signals.beat("fine_decode", len(fine_windows), total),
        )
        for idx, start, end in chunk:
            buf = decoded.pop(idx, None)  # pop, not [], so each window's PCM dies as it is consumed
            if buf is None:
                continue  # no audio for this window; already reported by the decode above
            try:
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
                _skip(idx, start, end)
                continue
            finally:
                del buf
            signals.progress(len(fine_windows), total)  # bump (throttle lives downstream, not here)
            signals.beat("fine", len(fine_windows), total)
        decoded.clear()  # this chunk's PCM is consumed; nothing crosses the chunk boundary
        _malloc_trim()
    return fine_windows, total


def _make_span_reporter(
    spans: list[tuple[int, float, float]],
    on_skip: Callable[[int, float, float, bool], None],
) -> Callable[[int], None]:
    """Adapt ``on_skip(idx, start, end, exc_info)`` to the ``on_failure(idx)`` sweep contract.

    A factory, not an inline lambda: the coarse pass builds one of these per CHUNK, and a
    closure written inside that loop would capture the loop's binding rather than this
    chunk's, so a late-firing report would render another chunk's window geometry.
    """
    geometry = {idx: (start, end) for idx, start, end in spans}

    def _report(window_index: int) -> None:
        start, end = geometry[window_index]
        on_skip(window_index, start, end, True)

    return _report


def _analyze_coarse_windows(
    file_path: str,
    total_sec: float,
    win_sec: int,
    models_dir: str,
    *,
    signals: AnalysisSignals = NO_SIGNALS,
) -> tuple[list[CoarseWindow], int]:
    """COARSE pass: mood/style/danceability for EVERY ``win_sec`` window (no length floor).

    Returns ``(windows, total)`` mirroring :func:`_analyze_fine_windows`. Every natural
    window is analyzed since phaze-w55w1 -- no stride, no cap.

    **Chunking (D-07).** The tier is processed :data:`_COARSE_CHUNK_WINDOWS` windows at a
    time, and each chunk runs the same three phases the whole tier used to:

    1. **decode** the chunk's windows up front -- ``chunk x 180 s x 16 kHz x 4 B ~= 345 MB``
       held concurrently, deliberately, in exchange for not holding 34 co-resident TF graphs
       (~4 GiB, phaze-15sw). This is the SAME residency the capped implementation had; what
       changed is that it is now bounded by the chunk rather than by the cap, so it no longer
       grows with duration;
    1. **infer** model-major across the chunk, one resident graph at a time;
    1. **derive + assemble** in window order, then release the chunk.

    The model-major sweep is why the coarse chunk cannot be 1: it needs every buffer of its
    unit in hand before the first graph is built. Chunking therefore costs 34 graph
    constructions per chunk instead of per file -- the deliberate price for a peak that does
    not scale with duration. It does NOT weaken the phaze-15sw invariant: exactly one
    ``TensorflowPredict*`` graph is resident at any instant, within a chunk and across chunk
    boundaries alike (``_sweep_one_model``'s ``finally`` releases before the next is built).

    Per-window failure isolation is preserved across all three phases: a decode failure drops
    that window before inference, an inference failure kills only that window (later models
    skip it), and derivation failure drops it at assembly. Same warning, same ``exc_info``;
    only the ORDER of the log lines changes, since a window's inference failure is discovered
    during the sweep rather than in window order.

    ``signals.beat(stage, done, total)`` is the liveness channel (phaze-w55w1). The coarse
    tier is the part of a long analysis that goes longest without completing a window, so it
    beats at chunk-decode start, after EVERY one of the 34 model sweeps, and at chunk
    assembly -- enough that a live multi-hour analysis is never mistaken for a hang.
    """
    natural = _iter_windows(total_sec, win_sec, 0, drop_short_trailing=False)
    total = len(natural)

    def _skip(idx: int, start: float, end: float, exc_info: bool = True) -> None:
        log.warning("coarse window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=exc_info)

    coarse_windows: list[CoarseWindow] = []

    chunks = _chunked(natural, _COARSE_CHUNK_WINDOWS)
    for position, chunk in enumerate(chunks):
        # (1) decode
        signals.beat("coarse_decode", len(coarse_windows), total)
        decoded = _decode_windows(
            file_path,
            _COARSE_SAMPLE_RATE,
            chunk,
            _skip,
            stop_at_sec=_chunk_stop_sec(chunks, position),
            on_beat=lambda: signals.beat("coarse_decode", len(coarse_windows), total),
        )
        spans: list[tuple[int, float, float]] = [(idx, start, end) for idx, start, end in chunk if idx in decoded]
        buffers: list[tuple[int, Any]] = [(idx, decoded.pop(idx)) for idx, _start, _end in spans]

        # (2) infer, model-major. The failure reporter is built by a factory rather than
        # closed over the loop variable, so each chunk's callback holds ITS OWN geometry map
        # instead of whatever the last iteration left behind (ruff B023).
        features_by_window, failed = _run_model_sets_over_windows(
            buffers, models_dir, _make_span_reporter(spans, _skip), lambda: signals.beat("coarse_model", len(coarse_windows), total)
        )
        buffers.clear()  # the peak is behind us; do not carry ~345 MB of PCM through assembly
        _malloc_trim()

        # (3) derive + assemble, in window order
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
        signals.beat("coarse", len(coarse_windows), total)
    return coarse_windows, total


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
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Analyze a single audio file via essentia as a two-tier time-series.

    The main synchronous function called from ``run_in_process_pool``. Instead of
    decoding the whole file into one buffer (the latent OOM) and feeding long
    audio to ``RhythmExtractor2013`` (the ``OnsetDetectionGlobal`` overflow), it
    analyzes the file as a set of short windows (Plan 31-01 locked strategy) so no
    essentia algorithm ever sees more than one window. Since phaze-5lop those windows
    come off ONE streaming decode pass per tier (:func:`_decode_windows`) instead of one
    non-seeking ``EasyLoader`` call per window: same windows, byte-identical PCM, but
    the file is decoded and resampled twice per analysis rather than 80-90 times.

    Two passes:
      * FINE (44.1 kHz): ``RhythmExtractor2013`` + ``KeyExtractor`` per
        ``fine_window_sec`` window; trailing windows shorter than
        ``fine_min_sec`` are dropped (except window 0).
      * COARSE (16 kHz): the 34 TF model sets per ``coarse_window_sec`` window;
        every window with audio is analyzed (no minimum-length floor).

    **Coverage is EXHAUSTIVE (phaze-w55w1 / ADR-0007 §7).** Every natural window of both
    tiers is analyzed, on files of every length; there is no cap, no even stride, and no
    "sampled" result. What bounds per-file memory instead is CHUNKING: each tier is decoded
    and analyzed :data:`_FINE_CHUNK_WINDOWS` / :data:`_COARSE_CHUNK_WINDOWS` windows at a
    time, so peak PCM residency is ~317 MB (fine) / ~345 MB (coarse) regardless of duration
    — the same per-tier figures the capped implementation was measured at. See the D-07
    decision record above :data:`_FINE_CHUNK_WINDOWS` for why those numbers, and what the
    chunking costs.

    Each pass holds only its own current chunk and nothing of the other's, deliberately.
    COARSE (phaze-15sw) holds a whole chunk's 16 kHz windows at once because its inference is
    model-major: one ``TensorflowPredict*`` graph is built, run across every window of the
    chunk, and released before the next is built. That trades ~345 MB of PCM for the ~4 GiB
    of co-resident TF graphs the window-major loop used to hold (phaze-esut / phaze-7i0k).
    FINE holds its 44.1 kHz chunk the same way because a single decode pass per chunk is what
    makes the tier fast (phaze-5lop) — and that transient is released, and its pages returned
    to the OS, BEFORE the coarse pass runs, so the two never stack. **The passes run in
    sequence, never fanned off one shared decode: sharing is only 5.3% faster and holds
    +0.364 GiB, because the tiers' resamplers differ and so the expensive stage cannot be
    shared at all (phaze-rc1q §8).**

    Per-window failures are logged and skipped — one bad window never fails the
    file. The one floor (phaze-zibn): if EVERY window fails in BOTH passes while
    the natural window count was non-zero, :class:`AnalysisDecodeError` is raised
    instead of returning an empty success — total decode failure is a file-level
    failure, not a completed analysis. Window sizes default to the
    ``AgentSettings`` defaults (30/180/15) and may be overridden by the agent
    worker.

    Returns a dict with the representative aggregates
    (``bpm``/``musical_key``/``mood``/``style``/``danceability``/``features``)
    PLUS ``windows``: a flat list of fine + coarse window dicts, each ready for
    ``AnalysisWindowPayload(**w)`` — PLUS four progress counts
    (``fine_windows_analyzed``/``fine_windows_total``/``coarse_windows_analyzed``/
    ``coarse_windows_total``). ``*_total`` is the natural window count and
    ``*_analyzed`` the count actually analyzed, so the two are EQUAL on a healthy file
    and differ only by per-window skips. They no longer express a coverage gap — there
    is none to express — they are the progress denominators the in-flight bar and the
    completion PUT share.

    Phase 57.1 (PROG-01): an optional sync ``progress_cb(analyzed, total)`` is threaded
    into the FINE per-window loop (``_analyze_fine_windows``) — a START signal then a
    per-window bump up to ``(len(fine_windows), fine_windows_total)``. The callback emits
    only an ``(int, int)`` count; ``analyze_file`` itself does NO I/O and imports no HTTP
    client (the Phase 101 exec'd-child JSON-protocol boundary, ``phaze.analysis_child`` /
    ``services.analysis_exec``, plus the ``tests/shared/core/test_task_split.py`` essentia import
    boundary, stay intact). Transport + throttle are the LANE's job. Fine-only is sufficient
    for the in-flight bar (WORK-04); the COARSE pass is intentionally not on that channel.

    phaze-w55w1 adds ``heartbeat_cb(stage, done, total)``: the same shape, but the LIVENESS
    channel rather than the UI one. Both tiers beat on it, and so do the stages between
    window completions (chunk decode, each coarse model sweep), because the supervising layer
    now decides "stuck" from ABSENCE OF PROGRESS rather than from elapsed wall clock — see
    ``services/analysis_exec.py``. It carries no I/O either; the child turns it into a
    protocol line and the parent turns that into a watchdog reset.
    """
    _suppress_essentia_logging()

    # One AnalysisSignals shared by both tiers (phaze-mp0op) -- analyze_file's own signature
    # keeps the two raw callbacks unchanged (analysis_child.py passes both by keyword), and
    # builds the seam once here rather than threading progress_cb/heartbeat_cb separately.
    signals = AnalysisSignals(progress_cb, heartbeat_cb)
    total_sec = _probe_duration_sec(file_path)

    fine_windows, fine_total = _analyze_fine_windows(file_path, total_sec, fine_window_sec, fine_min_sec, signals=signals)
    coarse_windows, coarse_total = _analyze_coarse_windows(file_path, total_sec, coarse_window_sec, models_dir, signals=signals)

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
    }
