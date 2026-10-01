"""Score SHA-addressed analysis predictions against independent annotations.

Annotations are intentionally separate from the corpus manifest. A missing label
is reported as missing evidence; it is never inferred from Essentia's output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from phaze.services.set_projection import MOOD_ORDER


_PITCH_CLASS = {
    "C": 0,
    "C#": 1,
    "Db": 1,
    "D": 2,
    "D#": 3,
    "Eb": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "Gb": 6,
    "G": 7,
    "G#": 8,
    "Ab": 8,
    "A": 9,
    "A#": 10,
    "Bb": 10,
    "B": 11,
}


def key_score(reference: str, predicted: str) -> float:
    """MIREX relation weights, independent of tonic spelling."""
    ref_note, ref_mode = reference.split()
    pred_note, pred_mode = predicted.split()
    ref, pred = _PITCH_CLASS[ref_note], _PITCH_CLASS[pred_note]
    if (ref, ref_mode) == (pred, pred_mode):
        return 1.0
    if ref_mode == pred_mode and (pred - ref) % 12 in (5, 7):
        return 0.5
    if ref_mode != pred_mode and ((ref_mode == "major" and (ref - pred) % 12 == 3) or (ref_mode == "minor" and (pred - ref) % 12 == 3)):
        return 0.3
    if ref == pred and ref_mode != pred_mode:
        return 0.2
    return 0.0


def tempo_scores(reference: float, predicted: float) -> tuple[bool, bool]:
    strict = abs(predicted / reference - 1.0) <= 0.02
    octave = any(abs(predicted / reference - ratio) <= 0.02 * ratio for ratio in (0.5, 1.0, 2.0))
    return strict, octave


def _mean_window_scores(prediction: dict[str, Any]) -> dict[str, float]:
    rows = [window.get("mood_scores") for window in prediction["windows"] if window.get("tier") == "coarse"]
    return {
        name: mean(float(row[name]) for row in rows if isinstance(row, dict) and row.get(name) is not None)
        for name in MOOD_ORDER
        if any(isinstance(row, dict) and row.get(name) is not None for row in rows)
    }


def score_one(annotation: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    """Only score fields with independently supplied labels."""
    out: dict[str, Any] = {"sha256": annotation["sha256"], "scored": [], "missing": []}
    if annotation.get("bpm") is not None:
        if prediction.get("bpm") is None:
            out["missing"].append("bpm prediction")
        else:
            strict, octave = tempo_scores(float(annotation["bpm"]), float(prediction["bpm"]))
            out["tempo"] = {
                "strict_2pct": strict,
                "octave_2pct": octave,
                "absolute_error_bpm": abs(float(prediction["bpm"]) - float(annotation["bpm"])),
            }
            out["scored"].append("bpm")
    if annotation.get("key"):
        if not prediction.get("musical_key"):
            out["missing"].append("key prediction")
        else:
            score = key_score(annotation["key"], prediction["musical_key"])
            out["key"] = {"exact": score == 1.0, "mirex_weight": score}
            out["scored"].append("key")
    if annotation.get("binary"):
        scores = _mean_window_scores(prediction)
        for name, truth in annotation["binary"].items():
            if name not in MOOD_ORDER or truth not in (0, 1):
                raise ValueError("binary labels must use MOOD_ORDER names and 0/1 values")
            if name not in scores:
                out["missing"].append(f"{name} prediction")
                continue
            predicted = scores[name]
            out.setdefault("binary", {})[name] = {
                "truth": truth,
                "score": predicted,
                "correct_at_0_5": (predicted >= 0.5) == bool(truth),
                "brier": (predicted - truth) ** 2,
            }
            out["scored"].append(name)
    if annotation.get("genre"):
        ranked = [row["name"] for row in prediction.get("style") or []]
        if not ranked:
            out["missing"].append("genre prediction")
        else:
            accepted = set(annotation["genre"])
            out["genre"] = {"top1": ranked[0] in accepted, "top5": bool(accepted.intersection(ranked[:5]))}
            out["scored"].append("genre")
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def rates(group: str, field: str) -> dict[str, float | int] | None:
        values = [float(row[group][field]) for row in rows if group in row]
        return {"n": len(values), "mean": mean(values)} if values else None

    binary: dict[str, dict[str, Any]] = {}
    for name in MOOD_ORDER:
        cells = [row["binary"][name] for row in rows if name in row.get("binary", {})]
        if not cells:
            continue
        tp = sum(cell["truth"] == 1 and cell["score"] >= 0.5 for cell in cells)
        fp = sum(cell["truth"] == 0 and cell["score"] >= 0.5 for cell in cells)
        fn = sum(cell["truth"] == 1 and cell["score"] < 0.5 for cell in cells)
        binary[name] = {
            "n": len(cells),
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "brier": mean(cell["brier"] for cell in cells),
        }
    return {
        "annotations": len(rows),
        "scored_field_count": sum(len(row["scored"]) for row in rows),
        "missing_predictions": sum(len(row["missing"]) for row in rows),
        "tempo_strict": rates("tempo", "strict_2pct"),
        "tempo_octave": rates("tempo", "octave_2pct"),
        "key_exact": rates("key", "exact"),
        "key_mirex": rates("key", "mirex_weight"),
        "genre_top1": rates("genre", "top1"),
        "genre_top5": rates("genre", "top5"),
        "binary": binary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = json.loads(args.annotations.read_text(encoding="utf-8"))
    if source.get("schema_version") != 1 or not isinstance(source.get("annotations"), list):
        parser.error("expected schema_version=1 annotation file")
    predictions: dict[str, dict[str, Any]] = {}
    for path in args.run_dir.glob("*/prediction.json"):
        sha = path.parent.name.split("-", 1)[0]
        if sha in predictions:
            parser.error("run directory has repeated predictions; score one phase/repeat at a time")
        predictions[sha] = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for annotation in source["annotations"]:
        if (
            not annotation.get("source")
            or not isinstance(annotation.get("annotators"), int)
            or annotation["annotators"] < 1
            or "ambiguity" not in annotation
        ):
            parser.error("each annotation requires source, positive annotator count, and ambiguity note")
        sha = annotation["sha256"]
        if sha not in predictions:
            rows.append({"sha256": sha, "scored": [], "missing": ["prediction"]})
        else:
            rows.append(score_one(annotation, predictions[sha]))
    summary = summarize(rows)
    report = {"summary": summary, "per_file": rows}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))  # noqa: T201
    return 0 if rows and summary["scored_field_count"] > 0 and all(not row["missing"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
