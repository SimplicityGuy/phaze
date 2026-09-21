#!/usr/bin/env python3
"""Measure captured Jev mood-probability fidelity to frozen Essentia outputs."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
import json
import math
from pathlib import Path
import statistics
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


MOODS = ("acoustic", "electronic", "aggressive", "relaxed", "happy", "sad", "party")
JEV_ARMS = ("jev_only", "hybrid")
ESSENTIA_POSITIVE_THRESHOLD = 0.5
JEV_DECISION_THRESHOLD = 0.5
CERTAINTY_FLOORS = (0.0, 0.2, 0.4, 0.6, 0.8)
PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.042


def load_json(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        value: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    else:
        value = json.loads(path.read_text())
    return value


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _average_precision(targets: Sequence[int], scores: Sequence[float]) -> float | None:
    """Return non-interpolated average precision with equal scores kept tied."""
    positives = sum(targets)
    if positives == 0:
        return None
    ranked = sorted(zip(scores, targets, strict=True), key=lambda item: item[0], reverse=True)
    seen = 0
    seen_positive = 0
    weighted_precision = 0.0
    index = 0
    while index < len(ranked):
        tied_score = ranked[index][0]
        tied_positive = 0
        tied_count = 0
        while index < len(ranked) and ranked[index][0] == tied_score:
            tied_count += 1
            tied_positive += ranked[index][1]
            index += 1
        seen += tied_count
        seen_positive += tied_positive
        weighted_precision += tied_positive / positives * (seen_positive / seen)
    return weighted_precision


def _f1(targets: Sequence[int], scores: Sequence[float], threshold: float = JEV_DECISION_THRESHOLD) -> float:
    predicted = [score >= threshold for score in scores]
    tp = sum(bool(target) and prediction for target, prediction in zip(targets, predicted, strict=True))
    fp = sum(not bool(target) and prediction for target, prediction in zip(targets, predicted, strict=True))
    fn = sum(bool(target) and not prediction for target, prediction in zip(targets, predicted, strict=True))
    return 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_sum = sum((value - left_mean) ** 2 for value in left)
    right_sum = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else None


def _dominant(values: Mapping[str, float]) -> str:
    return max(MOODS, key=lambda mood: (values[mood], -MOODS.index(mood)))


def _top_two(values: Mapping[str, float]) -> list[str]:
    return sorted(MOODS, key=lambda mood: (-values[mood], MOODS.index(mood)))[:2]


def build_predictions(
    corpus: Mapping[str, Any], results: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, dict[str, float]], dict[str, dict[str, dict[str, float]]]]:
    tracks = corpus["tracks"]
    subject_by_file = {track["file_id"]: track.get("subject_code", f"P{index:02d}") for index, track in enumerate(tracks, start=1)}
    reference = {subject_by_file[track["file_id"]]: {mood: float(track["moods"][mood]) for mood in MOODS} for track in tracks}
    predictions: dict[str, dict[str, dict[str, float]]] = {arm: {} for arm in JEV_ARMS}
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in results["records"]:
        if record["status"] != "ok":
            continue
        grouped.setdefault((record["arm"], record["file_id"]), []).append(record["answers"])
    for arm in JEV_ARMS:
        for file_id, subject in subject_by_file.items():
            repeats = grouped.get((arm, file_id), [])
            if len(repeats) != 3:
                raise ValueError(f"{subject}/{arm}: exactly three successful repeats are required")
            predictions[arm][subject] = {mood: _mean([float(row[mood]) for row in repeats]) for mood in MOODS}
    return subject_by_file, reference, predictions


def _probability_metrics(targets: Sequence[float], scores: Sequence[float]) -> dict[str, float | None]:
    squared_errors = [(score - target) ** 2 for score, target in zip(scores, targets, strict=True)]
    absolute_errors = [abs(score - target) for score, target in zip(scores, targets, strict=True)]
    mse = _mean(squared_errors)
    return {
        "brier_soft_target": mse,
        "mean_squared_error_soft_target": mse,
        "mean_absolute_error": _mean(absolute_errors),
        "root_mean_squared_error": math.sqrt(mse),
        "pearson_correlation": _pearson(targets, scores),
    }


def _operation_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    successful = [record for record in records if record["status"] == "ok"]
    latency = [float(record["latency_ms"]) for record in successful]
    input_tokens = sum(int(record["usage"]["input_tokens"]) for record in successful)
    output_tokens = sum(int(record["usage"]["output_tokens"]) for record in successful)
    return {
        "requests": len(records),
        "successful_requests": len(successful),
        "failures": len(records) - len(successful),
        "retries": sum(int(record["retries"]) for record in records),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms_p50": _percentile(latency, 0.5),
        "latency_ms_p95": _percentile(latency, 0.95),
        "public_rate_cost_usd": input_tokens / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
        "average_input_tokens_per_successful_request": input_tokens / len(successful) if successful else None,
    }


def evaluate(corpus: Mapping[str, Any], results: Mapping[str, Any]) -> dict[str, Any]:
    subject_by_file, reference, predictions = build_predictions(corpus, results)
    subjects = sorted(reference)
    track_by_subject = {subject_by_file[track["file_id"]]: track for track in corpus["tracks"]}
    per_label: dict[str, dict[str, Any]] = {arm: {} for arm in JEV_ARMS}
    for arm in JEV_ARMS:
        for mood in MOODS:
            soft_targets = [reference[subject][mood] for subject in subjects]
            binary_targets = [int(target >= ESSENTIA_POSITIVE_THRESHOLD) for target in soft_targets]
            scores = [predictions[arm][subject][mood] for subject in subjects]
            per_label[arm][mood] = {
                "essentia_positive_count": sum(binary_targets),
                "pr_auc_average_precision": _average_precision(binary_targets, scores),
                "f1_at_0_5": _f1(binary_targets, scores),
                **_probability_metrics(soft_targets, scores),
            }

    aggregate_probability: dict[str, Any] = {}
    for arm in JEV_ARMS:
        targets = [reference[subject][mood] for subject in subjects for mood in MOODS]
        scores = [predictions[arm][subject][mood] for subject in subjects for mood in MOODS]
        available_average_precision = [
            per_label[arm][mood]["pr_auc_average_precision"] for mood in MOODS if per_label[arm][mood]["pr_auc_average_precision"] is not None
        ]
        aggregate_probability[arm] = {
            **_probability_metrics(targets, scores),
            "macro_pr_auc_average_precision": _mean(available_average_precision) if available_average_precision else None,
            "macro_f1_at_0_5": _mean([per_label[arm][mood]["f1_at_0_5"] for mood in MOODS]),
        }

    dominant: dict[str, Any] = {}
    disagreements: list[dict[str, Any]] = []
    for arm in JEV_ARMS:
        exact_matches = 0
        dominant_in_top_two = 0
        top_two_overlap = []
        for subject in subjects:
            target = reference[subject]
            predicted = predictions[arm][subject]
            predicted_dominant = _dominant(predicted)
            target_top_two = _top_two(target)
            predicted_top_two = _top_two(predicted)
            exact_matches += predicted_dominant == _dominant(target)
            dominant_in_top_two += predicted_dominant in target_top_two
            top_two_overlap.append(len(set(target_top_two) & set(predicted_top_two)) / 2)
        dominant[arm] = {
            "dominant_matches": exact_matches,
            "dominant_agreement": exact_matches / len(subjects),
            "dominant_in_essentia_top_two_count": dominant_in_top_two,
            "dominant_in_essentia_top_two": dominant_in_top_two / len(subjects),
            "mean_top_two_set_overlap": _mean(top_two_overlap),
        }
    for subject in subjects:
        disagreements.append(
            {
                "subject_code": subject,
                "essentia_dominant": _dominant(reference[subject]),
                "essentia_top_two": _top_two(reference[subject]),
                **{f"{arm}_dominant": _dominant(predictions[arm][subject]) for arm in JEV_ARMS},
            }
        )

    risk_coverage: dict[str, list[dict[str, float | int | None]]] = {}
    for arm in JEV_ARMS:
        decisions = []
        for subject in subjects:
            for mood in MOODS:
                probability = predictions[arm][subject][mood]
                binary_target = reference[subject][mood] >= ESSENTIA_POSITIVE_THRESHOLD
                decisions.append((abs(probability - JEV_DECISION_THRESHOLD) * 2, (probability >= JEV_DECISION_THRESHOLD) == binary_target))
        risk_coverage[arm] = []
        for floor in CERTAINTY_FLOORS:
            kept = [correct for certainty, correct in decisions if certainty >= floor]
            risk_coverage[arm].append(
                {
                    "certainty_floor": floor,
                    "covered_decisions": len(kept),
                    "total_decisions": len(decisions),
                    "coverage": len(kept) / len(decisions),
                    "accuracy_vs_essentia_threshold": sum(kept) / len(kept) if kept else None,
                }
            )

    successful_records = [record for record in results["records"] if record["status"] == "ok"]
    repeat_stddev: dict[str, Any] = {}
    for arm in JEV_ARMS:
        per_mood: dict[str, float] = {}
        for mood in MOODS:
            deviations = []
            for file_id in subject_by_file:
                arm_records = [record for record in successful_records if record["arm"] == arm and record["file_id"] == file_id]
                deviations.append(statistics.pstdev(float(record["answers"][mood]) for record in arm_records))
            per_mood[mood] = _mean(deviations)
        repeat_stddev[arm] = {
            "mean_population_stddev": _mean(list(per_mood.values())),
            "per_label_mean_population_stddev": per_mood,
        }

    stratified: dict[str, Any] = {}
    for dimension in ("dominant_mood", "margin_band", "duration_band", "metadata_completeness"):
        if not all(dimension in track_by_subject[subject] for subject in subjects):
            continue
        categories = sorted({str(track_by_subject[subject][dimension]) for subject in subjects})
        stratified[dimension] = {}
        for category in categories:
            subset = [subject for subject in subjects if str(track_by_subject[subject][dimension]) == category]
            stratified[dimension][category] = {"subjects": len(subset)}
            for arm in JEV_ARMS:
                targets = [reference[subject][mood] for subject in subset for mood in MOODS]
                scores = [predictions[arm][subject][mood] for subject in subset for mood in MOODS]
                agreement = sum(_dominant(reference[subject]) == _dominant(predictions[arm][subject]) for subject in subset)
                stratified[dimension][category][arm] = {
                    **_probability_metrics(targets, scores),
                    "dominant_agreement": agreement / len(subset),
                    "threshold_agreement": _mean(
                        [
                            (target >= ESSENTIA_POSITIVE_THRESHOLD) == (score >= JEV_DECISION_THRESHOLD)
                            for target, score in zip(targets, scores, strict=True)
                        ]
                    ),
                }

    groups = [str(track_by_subject[subject]["leakage_group"]) for subject in subjects if "leakage_group" in track_by_subject[subject]]
    leakage_diagnostics = None
    if groups:
        group_counts = {group: groups.count(group) for group in set(groups)}
        leakage_diagnostics = {
            "distinct_artist_release_groups": len(group_counts),
            "maximum_tracks_per_group": max(group_counts.values()),
            "groups_with_two_tracks": sum(count == 2 for count in group_counts.values()),
            "single_track_groups": sum(count == 1 for count in group_counts.values()),
        }

    operations_by_arm = {arm: _operation_summary([record for record in results["records"] if record["arm"] == arm]) for arm in JEV_ARMS}
    operations_combined = _operation_summary(results["records"])
    prior_attempts = results.get("attempt_history", [])
    prior_failure_events = sum(record["status"] == "failed" for record in prior_attempts)
    average_tokens = {}
    for arm in JEV_ARMS:
        mean_tokens = operations_by_arm[arm]["average_input_tokens_per_successful_request"]
        if mean_tokens is None:
            raise ValueError(f"{arm}: no successful requests to project")
        average_tokens[arm] = float(mean_tokens)
    projections = {}
    for count in (12, 500, 5583, 93444):
        projections[str(count)] = {
            "jev_only_one_run_usd": count * average_tokens["jev_only"] / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
            "hybrid_one_run_usd": count * average_tokens["hybrid"] / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
            "two_arms_one_run_usd": count * sum(average_tokens.values()) / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
            "two_arms_three_runs_usd": count * 3 * sum(average_tokens.values()) / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
        }

    return {
        "schema_version": "phaze-jev-fidelity-evaluation-v2",
        "created_at": datetime.now(UTC).isoformat(),
        "interpretation": "Fidelity to stored Essentia mood probabilities, not independent human or semantic truth.",
        "subjects": len(subjects),
        "thresholds": {
            "essentia_positive": ESSENTIA_POSITIVE_THRESHOLD,
            "jev_decision": JEV_DECISION_THRESHOLD,
            "certainty_floors": list(CERTAINTY_FLOORS),
        },
        "per_label": per_label,
        "aggregate_probability": aggregate_probability,
        "dominant_and_top_two": dominant,
        "disagreements": disagreements,
        "accuracy_vs_coverage": risk_coverage,
        "stratified": stratified,
        "leakage_diagnostics": leakage_diagnostics,
        "repeat_variability": repeat_stddev,
        "operations": {
            "by_arm": operations_by_arm,
            "combined": operations_combined,
            "captured_summary_cost_usd": float(results["summary"]["cost_usd_public_rate"]),
            "prior_terminal_attempt_history": {
                "events": len(prior_attempts),
                "failures": prior_failure_events,
                "transport_attempts": sum(int(record["attempts"]) for record in prior_attempts),
                "internal_retries": sum(int(record["retries"]) for record in prior_attempts),
                "errors": sorted({error for record in prior_attempts for error in record["errors"]}),
            },
            "public_rate_projections": projections,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    corpus = load_json(args.corpus)
    results = load_json(args.results)
    output = evaluate(corpus, results)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
