#!/usr/bin/env python3
# mypy: ignore-errors
"""Turn labeled Essentia predictions into a compact Jev judgment request.

This proof of concept intentionally runs after Essentia inference. It does not
send audio or embedding tensors to TypeSafe and does not add network calls to
Essentia's streaming graph.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504, 529}


class JevBridgeError(RuntimeError):
    """Raised when the Jev request cannot be built or completed."""


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = percentile * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _validated_frames(labels: Sequence[str], predictions: Sequence[Sequence[float]]) -> list[list[float]]:
    if not labels:
        raise ValueError("labels must not be empty")
    if len(set(labels)) != len(labels):
        raise ValueError("labels must be unique")
    if not predictions:
        raise ValueError("predictions must contain at least one frame")

    frames: list[list[float]] = []
    for frame_index, frame in enumerate(predictions):
        if len(frame) != len(labels):
            raise ValueError(f"prediction frame {frame_index} has {len(frame)} values; expected {len(labels)}")
        converted = []
        for label, value in zip(labels, frame, strict=True):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"prediction for {label!r} must be numeric")
            score = float(value)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError(f"prediction for {label!r} must be finite and between 0 and 1")
            converted.append(score)
        frames.append(converted)
    return frames


def summarize_predictions(
    labels: Sequence[str],
    predictions: Sequence[Sequence[float]],
    *,
    top_k: int = 8,
    presence_threshold: float = 0.5,
) -> dict[str, Any]:
    """Aggregate per-frame classifier outputs into compact labeled evidence.

    The scores remain Essentia/model evidence. They are not presented as Jev
    probabilities and are not combined with Jev confidence.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if not 0.0 <= presence_threshold <= 1.0:
        raise ValueError("presence_threshold must be between 0 and 1")

    frames = _validated_frames(labels, predictions)
    rows = []
    for label_index, label in enumerate(labels):
        values = [frame[label_index] for frame in frames]
        rows.append(
            {
                "label": label,
                "mean_score": round(sum(values) / len(values), 6),
                "max_score": round(max(values), 6),
                "p90_score": round(_percentile(values, 0.9), 6),
                "frame_coverage": round(
                    sum(value >= presence_threshold for value in values) / len(values),
                    6,
                ),
            }
        )

    ranked = sorted(rows, key=lambda row: (-row["mean_score"], row["label"]))
    emitted = ranked[: min(top_k, len(ranked))]
    return {
        "frame_count": len(frames),
        "label_count": len(labels),
        "top_k": len(emitted),
        "presence_threshold": presence_threshold,
        "top_predictions": emitted,
        "omitted_mean_score_sum": round(sum(row["mean_score"] for row in ranked[len(emitted) :]), 6),
    }


def build_state(
    *,
    goal: str,
    model: Mapping[str, Any],
    evidence: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not goal.strip():
        raise ValueError("goal must not be empty")
    if not model.get("name"):
        raise ValueError("model.name is required")
    return {
        "goal": goal,
        "audio_evidence": {
            "source": "Essentia classifier output",
            "model": dict(model),
            "summary": dict(evidence),
            "interpretation": (
                "Scores are acoustic-model outputs in [0, 1]. They are evidence, not TypeSafe probabilities and not direct observations of the audio."
            ),
        },
        "context": dict(context or {}),
    }


def build_questions(taxonomy: Mapping[str, Any]) -> dict[str, Any]:
    if len(taxonomy) < 2:
        raise ValueError("taxonomy must define at least two choices")
    return {
        "best_bucket": {
            "type": "choice",
            "instructions": (
                "Which catalog bucket best fits `goal`, using `audio_evidence` and `context`? Treat the classifier scores as fallible evidence."
            ),
            "criteria": dict(taxonomy),
        },
        "goal_fit": {
            "type": "score",
            "instructions": ("How well does this track fit `goal`, using `audio_evidence` and `context`?"),
            "criteria": [
                "Contradicts the goal.",
                "Weak fit with important conflicts.",
                "Mixed or uncertain fit.",
                "Good fit with minor reservations.",
                "Excellent fit with strong supporting evidence.",
            ],
        },
        "needs_review": {
            "type": "noul",
            "instructions": ("Is the available evidence too ambiguous, sparse, or conflicting to use the selected bucket without review?"),
            "criteria": {
                "true": "The evidence is insufficient or materially conflicting.",
                "false": "The evidence is coherent enough for this low-stakes categorization.",
            },
        },
    }


def build_request(source: Mapping[str, Any], *, model_name: str) -> dict[str, Any]:
    required = {"goal", "model", "labels", "predictions", "taxonomy"}
    missing = sorted(required - set(source))
    if missing:
        raise ValueError(f"input is missing required fields: {', '.join(missing)}")

    evidence = summarize_predictions(
        source["labels"],
        source["predictions"],
        top_k=int(source.get("top_k", 8)),
        presence_threshold=float(source.get("presence_threshold", 0.5)),
    )
    state = build_state(
        goal=str(source["goal"]),
        model=source["model"],
        evidence=evidence,
        context=source.get("context"),
    )
    return {
        "state": state,
        "model": model_name,
        "questions": build_questions(source["taxonomy"]),
    }


def call_jev(
    payload: Mapping[str, Any],
    *,
    api_key: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 30.0,
    max_attempts: int = 3,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Call Jev without adding a mandatory SDK dependency to Essentia."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevBridgeError("TYPESAFE_API_KEY is required for a live request")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if not endpoint.startswith("https://"):
        raise ValueError("TypeSafe endpoint must use HTTPS")

    request = Request(  # noqa: S310 -- HTTPS is validated above
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    for attempt in range(max_attempts):
        try:
            with opener(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise JevBridgeError("TypeSafe response did not contain an answers object")
            return result
        except HTTPError as error:
            if error.code not in RETRYABLE_STATUS_CODES or attempt + 1 == max_attempts:
                raise JevBridgeError(f"TypeSafe request failed with HTTP {error.code}") from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            delay = float(retry_after) if retry_after else 2**attempt
            sleeper(min(delay, 20.0))
        except URLError as error:
            if attempt + 1 == max_attempts:
                raise JevBridgeError("TypeSafe request could not reach the API") from error
            sleeper(min(2**attempt, 20.0))

    raise AssertionError("retry loop exited unexpectedly")


def _write_json(value: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output:
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")  # noqa: T201 -- CLI output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON file containing labeled predictions")
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render the Jev request without making a network call",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="TypeSafe model alias")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="TypeSafe API endpoint")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    source = json.loads(args.input.read_text(encoding="utf-8"))
    payload = build_request(source, model_name=args.model)
    result = payload if args.dry_run else call_jev(payload, endpoint=args.endpoint, timeout=args.timeout)
    _write_json(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
