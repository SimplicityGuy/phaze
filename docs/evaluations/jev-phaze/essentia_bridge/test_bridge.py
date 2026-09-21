# mypy: ignore-errors
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from bridge import JevBridgeError, build_request, call_jev, summarize_predictions


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class TestSummarizePredictions(unittest.TestCase):
    def test_ranks_and_aggregates_frames(self):
        result = summarize_predictions(
            ["calm", "energetic", "vocal"],
            [[0.8, 0.2, 0.4], [0.6, 0.9, 0.7], [1.0, 0.1, 0.5]],
            top_k=2,
            presence_threshold=0.5,
        )

        self.assertEqual(result["frame_count"], 3)
        self.assertEqual(
            [row["label"] for row in result["top_predictions"]],
            ["calm", "vocal"],
        )
        self.assertEqual(result["top_predictions"][0]["mean_score"], 0.8)
        self.assertEqual(result["top_predictions"][0]["p90_score"], 0.96)
        self.assertAlmostEqual(result["top_predictions"][1]["frame_coverage"], 2 / 3, places=6)
        self.assertEqual(result["omitted_mean_score_sum"], 0.4)

    def test_rejects_invalid_prediction_shapes_and_values(self):
        with self.assertRaisesRegex(ValueError, "expected 2"):
            summarize_predictions(["a", "b"], [[0.2]])
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            summarize_predictions(["a"], [[1.1]])
        with self.assertRaisesRegex(ValueError, "unique"):
            summarize_predictions(["a", "a"], [[0.2, 0.3]])


class TestRequestConstruction(unittest.TestCase):
    def test_builds_one_batched_request_with_typed_questions(self):
        payload = build_request(
            {
                "goal": "Background dinner music",
                "model": {"name": "mood-model", "version": "1"},
                "labels": ["happy", "aggressive"],
                "predictions": [[0.8, 0.1], [0.7, 0.2]],
                "context": {"bpm": 112},
                "taxonomy": {
                    "dinner": "Suitable for dinner",
                    "dancefloor": "Suitable for a dance floor",
                },
            },
            model_name="jev-latest",
        )

        self.assertEqual(payload["model"], "jev-latest")
        self.assertEqual(payload["questions"]["best_bucket"]["type"], "choice")
        self.assertEqual(payload["questions"]["goal_fit"]["type"], "score")
        self.assertEqual(payload["questions"]["needs_review"]["type"], "noul")
        self.assertEqual(payload["state"]["context"]["bpm"], 112)


class TestHTTPAdapter(unittest.TestCase):
    def test_sends_bearer_token_without_persisting_it(self):
        captured = {}

        def opener(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return _Response({"model": "jev-latest", "answers": {"fit": {}}})

        payload = {"state": "evidence", "model": "jev-latest", "questions": {}}
        response = call_jev(payload, api_key="test-only", opener=opener)

        self.assertEqual(response["model"], "jev-latest")
        self.assertEqual(captured["authorization"], "Bearer test-only")
        self.assertEqual(captured["body"], payload)
        self.assertEqual(captured["timeout"], 30.0)

    def test_retries_rate_limit_then_succeeds(self):
        attempts = []
        sleeps = []

        def opener(_request, timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                error = HTTPError(
                    "https://api.typesafe.ai/v1/systemone",
                    429,
                    "rate limited",
                    {"Retry-After": "0"},
                    io.BytesIO(),
                )
                error.close()
                raise error
            return _Response({"answers": {"fit": {"type": "noul", "noul": 0.9}}})

        result = call_jev(
            {"state": "evidence", "model": "jev-latest", "questions": {}},
            api_key="test-only",
            opener=opener,
            sleeper=sleeps.append,
        )

        self.assertIn("answers", result)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.0])

    def test_requires_key_for_live_requests(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(JevBridgeError, "TYPESAFE_API_KEY"):
            call_jev({"state": {}, "model": "jev-latest", "questions": {}})


if __name__ == "__main__":
    unittest.main()
