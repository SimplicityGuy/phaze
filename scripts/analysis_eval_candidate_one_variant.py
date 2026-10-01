"""Experimental speed candidate: one MusiCNN-MSD classifier per result family.

This is an Essentia-based ablation, not a clean-room implementation. It retains
the fine-tier tempo/key methods, all 11 coarse characteristic families, the
Discogs400 genre model, and every natural window. The only removed work is the
MusiCNN-MTT and VGGish variants for each characteristic. Its outputs require
the normal quality review; presence of every field is not proof of accuracy.

Run inside the pinned Phaze image through analysis_eval.py's neutral protocol.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
import sys
import traceback
from typing import IO, Any


def _protocol_channel() -> IO[str]:
    """Keep Essentia/TensorFlow's native stdout banners out of JSONL output."""
    fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(fd, "w", buffering=1)


def _emit(channel: IO[str], payload: dict[str, Any]) -> None:
    channel.write(json.dumps(payload, allow_nan=False) + "\n")
    channel.flush()


def run(audio: str, models_dir: str, channel: IO[str]) -> int:
    from phaze.services import analysis  # noqa: PLC0415 -- import after stdout diversion
    from scripts.analysis_eval import normalize_current  # noqa: PLC0415 -- import after stdout diversion

    original_sets = analysis.MODEL_SETS
    if len(original_sets) != 11 or any(len(group.models) != 3 for group in original_sets):
        raise RuntimeError("unexpected production model registry; candidate selection needs review")
    selected = tuple(replace(group, models=(next(model for model in group.models if model.variant == "musicnn_msd"),)) for group in original_sets)
    analysis.MODEL_SETS = selected

    def progress(fine_done: int, fine_total: int, coarse_done: int, coarse_total: int) -> None:
        _emit(channel, {"type": "progress", "analyzed": fine_done, "total": fine_total, "coarse_analyzed": coarse_done, "coarse_total": coarse_total})

    def heartbeat(stage: str, done: int, total: int) -> None:
        _emit(channel, {"type": "heartbeat", "stage": stage, "done": done, "total": total})

    duration = analysis._probe_duration_sec(audio)
    result = analysis.analyze_file(audio, models_dir, progress_cb=progress, heartbeat_cb=heartbeat)
    neutral, problems = normalize_current(result, duration)
    if problems:
        raise RuntimeError("candidate semantic adapter failed: " + "; ".join(problems))
    _emit(channel, {"type": "result", "result": neutral})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio")
    parser.add_argument("--models-dir", required=True)
    args = parser.parse_args()
    with _protocol_channel() as channel:
        try:
            return run(args.audio, args.models_dir, channel)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _emit(channel, {"type": "error", "message": f"{type(exc).__name__}: {exc}"[:2000]})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
