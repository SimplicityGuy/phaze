"""Describe agreement between one offline run and a frozen homelab DB reference.

These are incumbent-agreement measures, not independent accuracy scores.
Candidate windows contribute in proportion to overlap with each reference window.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import mean
from typing import Any, cast

from phaze.services.set_projection import MOOD_ORDER
from scripts.analysis_eval_score import key_score


def _prediction(run_dir: Path, sha: str) -> dict[str, Any]:
    paths = list(run_dir.glob(f"{sha}-*/prediction.json"))
    if len(paths) != 1:
        raise ValueError(f"expected exactly one prediction for {sha[:12]}")
    return cast("dict[str, Any]", json.loads(paths[0].read_text()))


def _matching_windows(reference: dict[str, Any], windows: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    result = []
    for window in windows:
        if window.get("tier") != reference["tier"]:
            continue
        overlap = min(reference["end_sec"], window["end_sec"]) - max(reference["start_sec"], window["start_sec"])
        if overlap > 0:
            result.append((overlap, window))
    return result


def _weighted_numeric(matched: list[tuple[float, dict[str, Any]]], field: str, *, nested: str | None = None) -> float | None:
    values = [(overlap, (window.get(nested) or {}).get(field) if nested else window.get(field)) for overlap, window in matched]
    present = [(weight, float(value)) for weight, value in values if value is not None]
    return sum(weight * value for weight, value in present) / sum(weight for weight, _ in present) if present else None


def _weighted_category(matched: list[tuple[float, dict[str, Any]]], field: str) -> str | None:
    weights: dict[str, float] = defaultdict(float)
    for overlap, window in matched:
        if window.get(field):
            weights[str(window[field])] += overlap
    return max(weights.items(), key=lambda pair: pair[1])[0] if weights else None


def compare(reference: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    analysis = reference["analysis"]
    measures: dict[str, list[float]] = defaultdict(list)
    if analysis["bpm"] is not None and prediction.get("bpm") is not None:
        measures["file_bpm_relative_error"].append(abs(prediction["bpm"] / analysis["bpm"] - 1))
    if analysis["musical_key"] and prediction.get("musical_key"):
        measures["file_key_exact"].append(float(key_score(analysis["musical_key"], prediction["musical_key"]) == 1))
        measures["file_key_mirex"].append(key_score(analysis["musical_key"], prediction["musical_key"]))
    old_styles = [row["name"] for row in (analysis["style"] or [])]
    new_styles = [row["name"] for row in (prediction.get("style") or [])]
    if old_styles and new_styles:
        measures["file_style_top1_match"].append(float(old_styles[0] == new_styles[0]))
        measures["file_style_top5_overlap"].append(float(bool(set(old_styles[:5]) & set(new_styles[:5]))))

    windows = prediction.get("windows") or []
    missing = {"fine": 0, "coarse": 0}
    aligned = {"fine": 0, "coarse": 0}
    for old in reference["windows"]:
        tier = old["tier"]
        matched = _matching_windows(old, windows)
        if not matched:
            missing[tier] += 1
            continue
        aligned[tier] += 1
        scalar_fields = ("bpm",) if tier == "fine" else ("energy",)
        category_fields = ("musical_key",) if tier == "fine" else ("mood", "style")
        for field in scalar_fields:
            value = _weighted_numeric(matched, field)
            if old.get(field) is not None and value is not None:
                measures[f"{tier}_{field}_absolute_error"].append(abs(float(old[field]) - value))
        for field in category_fields:
            category = _weighted_category(matched, field)
            if old.get(field) and category:
                measures[f"{tier}_{field}_match"].append(float(old[field] == category))
        if tier == "coarse":
            old_scores = old.get("mood_scores") or {}
            for field in MOOD_ORDER:
                value = _weighted_numeric(matched, field, nested="mood_scores")
                if old_scores.get(field) is not None and value is not None:
                    measures[f"coarse_{field}_absolute_error"].append(abs(float(old_scores[field]) - value))
    return {
        "sha256": reference["sha256"],
        "source_genre_tag_present": bool(reference.get("source_genre_tag")),
        "reference_windows": {tier: sum(w["tier"] == tier for w in reference["windows"]) for tier in ("fine", "coarse")},
        "prediction_windows": {tier: sum(w["tier"] == tier for w in windows) for tier in ("fine", "coarse")},
        "aligned_windows": aligned,
        "missing_windows": missing,
        "measures": {key: {"n": len(values), "mean": mean(values)} for key, values in sorted(measures.items())},
        "_raw_measures": measures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.reference.read_text())
    if source.get("schema_version") != 1 or not isinstance(source.get("items"), list):
        parser.error("expected schema_version=1 reference export")
    rows = [compare(item, _prediction(args.run_dir, item["sha256"])) for item in source["items"]]
    raw: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for key, values in row.pop("_raw_measures").items():
            raw[key].extend(values)
    report = {
        "reference_sha256": __import__("hashlib").sha256(args.reference.read_bytes()).hexdigest(),
        "run_dir": args.run_dir.name,
        "summary": {
            "files": len(rows),
            "source_genre_tags": sum(row["source_genre_tag_present"] for row in rows),
            "missing_windows": {tier: sum(row["missing_windows"][tier] for row in rows) for tier in ("fine", "coarse")},
            "measures": {key: {"n": len(values), "mean": mean(values)} for key, values in sorted(raw.items())},
        },
        "per_file": rows,
    }
    if args.output.exists():
        parser.error("output already exists")
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
