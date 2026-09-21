#!/usr/bin/env python3
"""Run the frozen two-arm Phaze mood pilot against TypeSafe Jev.

This is an evaluation utility, not product code. It reads the TypeSafe API key
only from ``TYPESAFE_API_KEY`` and never includes it in output or log messages.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
SCHEMA_VERSION = "phaze-jev-mood-pilot-v1"
PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.042
MOODS = ("acoustic", "electronic", "aggressive", "relaxed", "happy", "sad", "party")
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})

QUESTION_DEFINITIONS: dict[str, dict[str, Any]] = {
    mood: {
        "type": "noul",
        "instructions": (
            f"Based only on the supplied state, is this track likely to sound {mood}? "
            "Treat metadata and measurements as fallible evidence. Do not assume that "
            "a missing signal means no."
        ),
        "criteria": {
            "true": f"The track is likely to have a perceptible {mood} character.",
            "false": f"The track is unlikely to have a perceptible {mood} character.",
        },
    }
    for mood in MOODS
}


@dataclass(frozen=True)
class ApiResult:
    response: dict[str, Any] | None
    latency_ms: float
    attempts: int
    errors: tuple[str, ...]


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return number


def validate_corpus(corpus: Mapping[str, Any]) -> list[dict[str, Any]]:
    if tuple(corpus.get("mood_order", ())) != MOODS:
        raise ValueError("corpus mood_order must match the frozen seven-label order")
    tracks = corpus.get("tracks")
    if not isinstance(tracks, list) or len(tracks) != 12:
        raise ValueError("corpus must contain exactly 12 tracks")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("every track must be an object")
        file_id = str(track.get("file_id", ""))
        if not file_id or file_id in seen:
            raise ValueError("track file_id values must be present and unique")
        seen.add(file_id)
        moods = track.get("moods")
        if not isinstance(moods, dict) or set(moods) != set(MOODS):
            raise ValueError(f"{file_id}: moods must contain the frozen seven-label set")
        for mood in MOODS:
            _finite_number(moods[mood], field=f"{file_id}.moods.{mood}")
        validated.append(dict(track))
    return validated


def build_state(track: Mapping[str, Any], arm: str) -> dict[str, Any]:
    if arm not in {"jev_only", "hybrid"}:
        raise ValueError(f"unknown arm: {arm}")
    state: dict[str, Any] = {
        "evidence_contract": {
            "subject": "one audio track",
            "important_limit": "Jev has not heard the audio; it must infer only from this state.",
            "arm": arm,
        },
        "catalog_metadata": {
            "artist": track.get("artist"),
            "title": track.get("title"),
            "album": track.get("album"),
        },
        "non_classifier_measurements": {
            "duration_seconds": track.get("duration_seconds"),
            "bpm": track.get("bpm"),
            "musical_key": track.get("musical_key"),
        },
    }
    if arm == "hybrid":
        state["essentia_classifier_evidence"] = {
            "meaning": (
                "Per-label positive-class probabilities averaged across the file's stored "
                "coarse-window projections. They are fallible evidence, not ground truth."
            ),
            "mood_probabilities": {mood: float(track["moods"][mood]) for mood in MOODS},
        }
    return state


def build_payload(track: Mapping[str, Any], arm: str) -> dict[str, Any]:
    return {
        "state": build_state(track, arm),
        "model": MODEL,
        "questions": QUESTION_DEFINITIONS,
    }


def _error_label(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return f"network {type(error.reason).__name__}"
    return type(error).__name__


def call_jev(
    payload: Mapping[str, Any],
    *,
    api_key: str,
    timeout: float = 45.0,
    max_attempts: int = 3,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> ApiResult:
    request = Request(
        ENDPOINT,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    errors: list[str] = []
    started = time.perf_counter()
    for attempt in range(1, max_attempts + 1):
        try:
            with opener(request, timeout=timeout) as response:
                body = json.loads(response.read().decode())
            if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
                raise ValueError("response is missing an answers object")
            return ApiResult(body, (time.perf_counter() - started) * 1000, attempt, tuple(errors))
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            errors.append(_error_label(error))
            retryable = not isinstance(error, HTTPError) or error.code in RETRYABLE_STATUS
            if attempt == max_attempts or not retryable:
                break
            sleeper(min(2 ** (attempt - 1), 8))
    return ApiResult(None, (time.perf_counter() - started) * 1000, max_attempts, tuple(errors))


def _answer_values(response: Mapping[str, Any]) -> dict[str, float]:
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("response answers must be an object")
    values: dict[str, float] = {}
    for mood in MOODS:
        answer = answers.get(mood)
        if not isinstance(answer, dict):
            raise ValueError(f"response is missing {mood} answer")
        values[mood] = _finite_number(answer.get("noul"), field=f"answers.{mood}.noul")
    return values


def _usage(response: Mapping[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


def run_pilot(
    corpus: Mapping[str, Any],
    *,
    api_key: str,
    repeats: int = 3,
    caller: Callable[..., ApiResult] = call_jev,
    progress: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    if corpus.get("source_data_redacted") and caller is call_jev:
        raise ValueError("redacted study evidence is for offline scoring, not live Jev requests")
    tracks = validate_corpus(corpus)
    records: list[dict[str, Any]] = []
    for track in tracks:
        for arm in ("jev_only", "hybrid"):
            payload = build_payload(track, arm)
            for repeat in range(1, repeats + 1):
                result = caller(payload, api_key=api_key)
                record: dict[str, Any] = {
                    "file_id": track["file_id"],
                    "arm": arm,
                    "repeat": repeat,
                    "latency_ms": round(result.latency_ms, 3),
                    "attempts": result.attempts,
                    "retries": result.attempts - 1,
                    "errors": list(result.errors),
                }
                if result.response is None:
                    record["status"] = "failed"
                else:
                    record.update(
                        {
                            "status": "ok",
                            "resolved_model": result.response.get("model"),
                            "answers": _answer_values(result.response),
                            "usage": _usage(result.response),
                        }
                    )
                records.append(record)
                if progress:
                    progress(f"{len(records):02d}/72 {track['file_id']} {arm} repeat={repeat} {record['status']}")

    total_input_tokens = sum(record.get("usage", {}).get("input_tokens", 0) for record in records)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "request_contract": {
            "endpoint": ENDPOINT,
            "requested_model": MODEL,
            "questions": QUESTION_DEFINITIONS,
            "repeats": repeats,
            "arms": {
                "jev_only": "metadata plus BPM, key, and duration; no classifier evidence",
                "hybrid": "the same state plus the seven stored Essentia mood probabilities",
            },
        },
        "price_basis": {
            "usd_per_million_input_tokens": PRICE_USD_PER_MILLION_INPUT_TOKENS,
            "output_tokens": "free at the public price retrieved 2026-09-20",
        },
        "summary": {
            "subjects": len(tracks),
            "requests": len(records),
            "successful_requests": sum(record["status"] == "ok" for record in records),
            "failed_requests": sum(record["status"] == "failed" for record in records),
            "input_tokens": total_input_tokens,
            "output_tokens": sum(record.get("usage", {}).get("output_tokens", 0) for record in records),
            "cost_usd_public_rate": total_input_tokens / 1_000_000 * PRICE_USD_PER_MILLION_INPUT_TOKENS,
        },
        "records": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        parser.error("TYPESAFE_API_KEY must be set in the process environment")
    corpus = json.loads(args.corpus.read_text())
    result = run_pilot(corpus, api_key=api_key, repeats=args.repeats)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"wrote {result['summary']['requests']} request records to {args.output}")  # noqa: T201 -- evaluation CLI progress
    return 0 if result["summary"]["failed_requests"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
