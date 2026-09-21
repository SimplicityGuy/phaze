from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


HERE = Path(__file__).parent
CORPUS = HERE / "phaze-eswzd.1-corpus.json"
RESULTS = HERE / "jev-runs.json"


def _module() -> ModuleType:
    script = HERE / "evaluate_jev_pilot.py"
    spec = importlib.util.spec_from_file_location("evaluate_jev_pilot", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evaluator() -> ModuleType:
    return _module()


def test_live_results_form_complete_three_repeat_arms(evaluator: ModuleType) -> None:
    corpus = json.loads(CORPUS.read_text())
    results = json.loads(RESULTS.read_text())
    subjects, reference, predictions = evaluator.build_predictions(corpus, results)
    assert len(subjects) == 12
    assert len(reference) == 12
    assert set(predictions) == {"jev_only", "hybrid"}
    assert all(len(rows) == 12 for rows in predictions.values())


def test_metrics_run_from_frozen_evidence_without_human_ratings(evaluator: ModuleType) -> None:
    corpus = json.loads(CORPUS.read_text())
    results = json.loads(RESULTS.read_text())
    output = evaluator.evaluate(corpus, results)
    assert output["schema_version"] == "phaze-jev-fidelity-evaluation-v2"
    assert output["thresholds"]["essentia_positive"] == 0.5
    assert set(output["per_label"]) == {"jev_only", "hybrid"}
    assert output["per_label"]["jev_only"]["acoustic"]["essentia_positive_count"] == 3
    assert output["operations"]["combined"]["successful_requests"] == 72
    assert output["operations"]["combined"]["failures"] == 0
    assert output["operations"]["captured_summary_cost_usd"] == pytest.approx(0.003356262)
    assert output["operations"]["combined"]["public_rate_cost_usd"] == pytest.approx(0.003356262)


def test_cli_requires_only_corpus_results_and_output(evaluator: ModuleType, tmp_path: Path) -> None:
    output_path = tmp_path / "evaluation.json"
    assert evaluator.main([str(CORPUS), str(RESULTS), str(output_path)]) == 0
    output = json.loads(output_path.read_text())
    assert output["interpretation"].startswith("Fidelity to stored Essentia")
    assert output["dominant_and_top_two"]["jev_only"]["dominant_matches"] == 5
    assert output["dominant_and_top_two"]["hybrid"]["dominant_matches"] == 8


def test_average_precision_keeps_equal_scores_tied(evaluator: ModuleType) -> None:
    assert evaluator._average_precision([1, 0], [0.5, 0.5]) == pytest.approx(0.5)


def test_500_track_fidelity_and_strata_from_frozen_manifest(evaluator: ModuleType) -> None:
    corpus = evaluator.load_json(HERE / "phaze-eswzd.1-benchmark.json.gz")
    records = []
    for track in corpus["tracks"]:
        for arm in ("jev_only", "hybrid"):
            for repeat in (1, 2, 3):
                records.append(
                    {
                        "file_id": track["file_id"],
                        "arm": arm,
                        "repeat": repeat,
                        "status": "ok",
                        "answers": track["moods"],
                        "latency_ms": 100.0,
                        "retries": 0,
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    }
                )
    results = {"records": records, "summary": {"cost_usd_public_rate": 0.000126}}
    output = evaluator.evaluate(corpus, results)
    assert output["subjects"] == 500
    assert output["operations"]["combined"]["successful_requests"] == 3000
    assert output["leakage_diagnostics"] == {
        "distinct_artist_release_groups": 437,
        "maximum_tracks_per_group": 2,
        "groups_with_two_tracks": 63,
        "single_track_groups": 374,
    }
    assert output["stratified"]["margin_band"]["ambiguous"]["subjects"] == 199
    assert output["stratified"]["metadata_completeness"]["incomplete"]["subjects"] == 100
    assert output["dominant_and_top_two"]["jev_only"]["dominant_matches"] == 500
    assert output["aggregate_probability"]["hybrid"]["mean_absolute_error"] == pytest.approx(0.0)
