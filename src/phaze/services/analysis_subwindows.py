"""Pool a mood graph's per-patch output into fixed sub-windows (phaze-x3gak).

The storage contract is ``docs/design/0026-subwindow-mood-and-beat-grid.md``: sub-windows are
:data:`~phaze.services.analysis_windows.SUBWINDOW_SEC` long, laid from each COARSE window's own start
(:func:`~phaze.services.analysis_windows.iter_subwindows`); every patch belongs to the one sub-window
holding its CENTRE, half-open ``[start, end)``; a slice holding no centre is ``None``, never an
invented vector.

Because each patch is counted exactly once, a coarse window's mean is recoverable from its parts:
for one variant, the patch-count-weighted mean of the sub-window means IS ``np.mean(matrix, axis=0)``,
the value ``_predict_single`` has always returned. That recovery is the discriminating test.

Same derivation as the coarse vector: the POSITIVE class is chosen by LABEL (never by position),
variants of one set are averaged, and the result is positional in ``MOOD_ORDER``. No essentia here.

Memory (D-07 / D-09): a matrix is ``[patches, classes]`` -- 119 x 2 floats for a 180 s window -- and is
pooled the moment its model sweep returns it. What outlives the sweep is a handful of floats per
slice, and the matrix itself is released with the prediction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from phaze.services.analysis_windows import SUBWINDOW_SEC, iter_subwindows
from phaze.services.set_projection import MOOD_ORDER


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# (patch length, patch hop) in seconds, MEASURED on the deployed graphs (docs/design/0026-subwindow-mood-and-beat-grid.md section 1):
# musicnn 187 frames / hop 93 at 256 samples per frame; VGGish 96 / 93 at 160 samples per frame; 16 kHz.
PATCH_GEOMETRY: dict[str, tuple[float, float]] = {
    "musicnn": (187 * 256 / 16000, 93 * 256 / 16000),
    "vggish": (96 * 160 / 16000, 93 * 160 / 16000),
}


class PatchMean(np.ndarray):
    """A per-class mean vector that remembers the ``[patches, classes]`` matrix it was taken from.

    ``_predict_single`` has always returned the mean, and its callers, and every test double of it,
    treat the result as a plain vector. Making the vector carry its matrix keeps that contract
    byte-for-byte while giving the sweep the patch rows without a second inference. A stub that
    returns a bare array simply has no ``patches``, and its window gets no sub-window mood.
    """

    patches: Any = None

    def __array_finalize__(self, obj: Any) -> None:
        # Slices, copies and ufunc results are plain vectors: only the original keeps the matrix.
        self.patches = None


def with_patches(mean: Any, patches: Any) -> PatchMean:
    """``mean`` as a :class:`PatchMean` carrying ``patches``."""
    out = np.asarray(mean).view(PatchMean)
    out.patches = patches
    return out


def positive_index(labels: Sequence[str]) -> int:
    """Column of the positive class, chosen by LABEL.

    The same rule as ``analysis_derive._positive_class_prediction``: the first label that does not
    carry the ``non_``/``not_`` negation prefix, falling back to the first column.
    """
    for i, label in enumerate(labels):
        if not str(label).startswith(("non_", "not_")):
            return i
    return 0


def patch_centres(n_patches: int, classifier_type: str) -> list[float]:
    """Centre of each patch, in seconds from the start of the buffer it was computed on."""
    length, hop = PATCH_GEOMETRY[classifier_type]
    return [k * hop + length / 2 for k in range(n_patches)]


def pool_positive_class(
    patches: Any,
    labels: Sequence[str],
    classifier_type: str,
    n_slices: int,
    sub_sec: int = SUBWINDOW_SEC,
) -> tuple[list[float], list[int]]:
    """``(sums, counts)`` of the positive-class patch values, per sub-window slice.

    A patch goes to ``floor(centre / sub_sec)``, clipped to the last slice so a centre on the buffer's
    final sample cannot index past it. ``np.atleast_2d`` normalizes the single-patch case exactly as
    ``_predict_single`` does.
    """
    matrix = np.atleast_2d(np.asarray(patches))
    column = matrix[:, positive_index(labels)].astype(np.float64)
    sums = [0.0] * n_slices
    counts = [0] * n_slices
    for centre, value in zip(patch_centres(len(column), classifier_type), column, strict=True):
        slot = min(int(centre // sub_sec), n_slices - 1)
        sums[slot] += float(value)
        counts[slot] += 1
    return sums, counts


def n_slices_for(start_sec: float, end_sec: float, sub_sec: int = SUBWINDOW_SEC) -> int:
    """Number of sub-window slices of the coarse window ``[start_sec, end_sec)``."""
    return len(iter_subwindows(start_sec, end_sec, sub_sec))


def combine_variants(
    pooled: Mapping[str, Sequence[tuple[Sequence[float], Sequence[int]]]],
    n_slices: int,
) -> tuple[tuple[float, ...] | None, ...]:
    """One mood vector per slice, positional in ``MOOD_ORDER``, from per-set per-variant pools.

    Per set, per slice: each variant's mean over its own patches, then the mean across the variants
    that have any (``_features_to_mood_dict``'s variant average). A slice where any set has no
    contributing patch is ``None``; so is every slice when a set is absent altogether -- a gap, never
    a vector with a hole in it.
    """
    if any(name not in pooled or not pooled[name] for name in MOOD_ORDER):
        return (None,) * n_slices
    out: list[tuple[float, ...] | None] = []
    for slot in range(n_slices):
        vector: list[float] = []
        for name in MOOD_ORDER:
            means = [sums[slot] / counts[slot] for sums, counts in pooled[name] if counts[slot] > 0]
            if not means:
                break
            vector.append(sum(means) / len(means))
        else:
            out.append(tuple(vector))
            continue
        out.append(None)
    return tuple(out)
