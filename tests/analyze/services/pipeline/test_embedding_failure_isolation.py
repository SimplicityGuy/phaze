"""An embedding failure degrades to "no embedding for this window or file", never to a failed analysis (phaze-l5mzm).

The per-window EffNet embedding is secondary data, so it follows the rule the grids follow
(docs/design/0026-subwindow-mood-and-beat-grid.md section 10): a failure on its path drops the embedding, logs it and
counts it on ``phaze.analysis.embeddings.dropped``, and leaves the core analysis exactly as complete as it would have been
without the option. Each failure mode and the test that discharges it:

* the fused genre call raises on a window -> ``test_a_fused_call_that_raises_costs_the_window_its_embedding_only``: the
  window is re-swept on the plain classifier after the fused graph is released, so its genre is unchanged and one graph
  is resident at a time (``test_the_retry_builds_its_graph_only_after_the_fused_one_is_released``);
* a pooled vector float16 cannot hold -> ``test_a_vector_float16_cannot_hold_drops_that_windows_embedding_only``;
* building the file's ``embeddings`` key raises -> ``test_an_assembly_error_omits_the_embeddings_and_keeps_the_result``;
* the option off runs the exact current path -> ``test_with_the_option_off_the_genre_sweep_never_builds_the_fused_classifier``
  and ``test_with_the_option_off_the_result_has_no_embeddings_key``;
* end to end through ``analyze_file`` -> ``test_an_embedding_failure_leaves_the_core_analysis_complete``.

The classifiers here are stand-ins at the ``_construct_classifier`` boundary, so the real ``_predict_single`` (and its
``atleast_2d`` / ``with_patches`` carriers) runs. That the fused classifier reproduces the wrapper is the real-graph
suite's job (``test_analysis_embeddings_real_essentia.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis_embeddings import EMBEDDING_DIM, decode_embedding, with_embedding
from phaze.services.analysis_models import GENRE_MODEL
from phaze.services.analysis_windows import CoarseWindow, FineWindow


if TYPE_CHECKING:
    from phaze.services.analysis_models import ModelConfig


_DROPPED = "phaze.analysis.embeddings.dropped"
# Exactly representable levels: each window's buffer is constant at its level, so a stand-in can tell windows apart.
_LEVELS = {0: 0.25, 1: 0.5, 2: 0.75}


@pytest.fixture
def drops(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, dict[str, Any]]]:
    """Every ``phaze.analysis.embeddings.dropped`` increment, as ``(value, attributes)``."""
    seen: list[tuple[float, dict[str, Any]]] = []
    real_add = analysis_mod.telemetry.add

    def spy(name: str, value: float = 1, **attributes: Any) -> None:
        if name == _DROPPED:
            seen.append((value, attributes))
        real_add(name, value, **attributes)

    monkeypatch.setattr(analysis_mod.telemetry, "add", spy)
    return seen


class _Graphs:
    """Stand-in classifiers, built through the real ``_get_classifier`` cache and released through the real release.

    ``fused_raises`` / ``plain_raises`` / ``fused_inf`` / ``fused_empty`` name window levels. ``built`` records each
    construction as ``(model name, fused?)`` and ``resident`` how many graphs were cached at that moment; ``plain_genre_seen``
    is the levels the plain genre classifier was called on.
    """

    def __init__(
        self,
        *,
        fused_raises: frozenset[float] = frozenset(),
        plain_raises: frozenset[float] = frozenset(),
        fused_inf: frozenset[float] = frozenset(),
        fused_empty: frozenset[float] = frozenset(),
    ) -> None:
        self.fused_raises, self.plain_raises, self.fused_inf, self.fused_empty = fused_raises, plain_raises, fused_inf, fused_empty
        self.built: list[tuple[str, bool]] = []
        self.resident: list[int] = []
        self.plain_genre_seen: list[float] = []

    def construct(self, model: ModelConfig, _graph_path: str, _batch_size: int) -> Any:
        self.built.append((model.name, model.fused_embedding))
        self.resident.append(len(analysis_mod._classifier_cache))
        genre = model.classifier_type == "effnet_discogs"

        def plain(audio: Any) -> Any:
            level = float(audio[0])
            if genre:
                self.plain_genre_seen.append(level)
                if level in self.plain_raises:
                    raise RuntimeError("plain genre call broke")
                if level in self.fused_empty:
                    return np.zeros(0, dtype=np.float32)  # what the wrapper returns under one patch
            return np.full((3, 400 if genre else 2), level, dtype=np.float32)

        def fused(audio: Any) -> Any:
            level = float(audio[0])
            if level in self.fused_raises:
                raise RuntimeError("fused call broke")
            if level in self.fused_empty:
                return np.zeros(0, dtype=np.float32)
            vector = np.full(EMBEDDING_DIM, level, dtype=np.float32)
            if level in self.fused_inf:
                vector[7] = np.inf
            return with_embedding(np.full((3, 400), level, dtype=np.float32), vector)

        return fused if model.fused_embedding else plain


def _install(monkeypatch: pytest.MonkeyPatch, graphs: _Graphs) -> None:
    monkeypatch.setattr(analysis_mod, "_construct_classifier", graphs.construct)
    monkeypatch.setattr(
        analysis_mod, "_get_labels", lambda filename, _d: [f"G{i}" for i in range(400)] if "discogs" in filename else ["positive", "negative"]
    )
    analysis_mod._classifier_cache.clear()


def _buffers() -> list[tuple[int, Any]]:
    return [(idx, np.full(16000, level, dtype=np.float32)) for idx, level in _LEVELS.items()]


def _sweep(
    monkeypatch: pytest.MonkeyPatch, graphs: _Graphs, *, embeddings: bool
) -> tuple[dict[int, dict[str, Any]], set[int], MagicMock, dict[int, str] | None]:
    _install(monkeypatch, graphs)
    on_failure = MagicMock()
    out: dict[int, str] | None = {} if embeddings else None
    features, failed = analysis_mod._run_model_sets_over_windows(_buffers(), "/m", on_failure, embeddings_out=out)
    assert analysis_mod._classifier_cache == {}, "every graph the sweep built is released"
    return features, failed, on_failure, out


@pytest.fixture
def baseline(monkeypatch: pytest.MonkeyPatch) -> dict[int, dict[str, Any]]:
    """The features of a healthy sweep with the option OFF: what every run with it on must still produce."""
    features, failed, _on_failure, _out = _sweep(monkeypatch, _Graphs(), embeddings=False)
    assert failed == set()
    return features


def _decoded(out: dict[int, str] | None) -> dict[int, float]:
    assert out is not None
    vectors = {idx: decode_embedding(encoded) for idx, encoded in out.items()}
    assert all(v.shape == (EMBEDDING_DIM,) and v.dtype == np.float16 for v in vectors.values())
    return {idx: float(v[0]) for idx, v in vectors.items()}


# --- option off ------------------------------------------------------------------------------------------------------


def test_with_the_option_off_the_genre_sweep_never_builds_the_fused_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    graphs = _Graphs()
    _features, _failed, _on_failure, out = _sweep(monkeypatch, graphs, embeddings=False)
    assert out is None
    assert not any(fused for _name, fused in graphs.built)
    assert graphs.built[-1] == (GENRE_MODEL.name, False)


def test_with_the_option_on_every_window_gets_its_embedding_and_the_same_genre(
    monkeypatch: pytest.MonkeyPatch, baseline: dict[int, dict[str, Any]], drops: list[tuple[float, dict[str, Any]]]
) -> None:
    graphs = _Graphs()
    features, failed, on_failure, out = _sweep(monkeypatch, graphs, embeddings=True)
    assert features == baseline
    assert failed == set()
    on_failure.assert_not_called()
    assert _decoded(out) == _LEVELS
    assert graphs.built[-1] == (GENRE_MODEL.name, True)
    assert [fused for _name, fused in graphs.built].count(True) == 1, "the fused classifier replaces the plain genre sweep, not adds to it"
    assert drops == []


# --- inference -------------------------------------------------------------------------------------------------------


def test_a_fused_call_that_raises_costs_the_window_its_embedding_only(
    monkeypatch: pytest.MonkeyPatch, baseline: dict[int, dict[str, Any]], drops: list[tuple[float, dict[str, Any]]], caplog: pytest.LogCaptureFixture
) -> None:
    graphs = _Graphs(fused_raises=frozenset({_LEVELS[1]}))
    with caplog.at_level("WARNING"):
        features, failed, on_failure, out = _sweep(monkeypatch, graphs, embeddings=True)
    assert failed == set(), "an embedding failure must not kill the window"
    on_failure.assert_not_called()
    assert features == baseline, "the window's genre comes from the plain classifier, exactly as without the option"
    assert _decoded(out) == {0: _LEVELS[0], 2: _LEVELS[2]}
    assert graphs.plain_genre_seen == [_LEVELS[1]], "only the failed window is re-swept"
    assert drops == [(1, {"embedding_stage": "inference"})]
    assert any("embedding dropped at inference" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def test_the_retry_builds_its_graph_only_after_the_fused_one_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    graphs = _Graphs(fused_raises=frozenset({_LEVELS[1]}))
    _sweep(monkeypatch, graphs, embeddings=True)
    assert graphs.built[-2:] == [(GENRE_MODEL.name, True), (GENRE_MODEL.name, False)]
    assert set(graphs.resident) == {0}, "a graph was built while another was still resident"


def test_a_window_the_plain_classifier_also_fails_is_killed_as_before(
    monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]]
) -> None:
    graphs = _Graphs(fused_raises=frozenset({_LEVELS[1]}), plain_raises=frozenset({_LEVELS[1]}))
    _features, failed, on_failure, out = _sweep(monkeypatch, graphs, embeddings=True)
    assert failed == {1}
    on_failure.assert_called_once_with(1)
    assert _decoded(out) == {0: _LEVELS[0], 2: _LEVELS[2]}
    assert drops == [(1, {"embedding_stage": "inference"})]


def test_a_window_killed_by_an_earlier_model_is_not_re_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    graphs = _Graphs(fused_raises=frozenset({_LEVELS[1]}))
    real_construct = graphs.construct

    def construct(model: ModelConfig, graph_path: str, batch_size: int) -> Any:
        classifier = real_construct(model, graph_path, batch_size)
        if model.classifier_type == "effnet_discogs":
            return classifier

        def first_model_kills_window_1(audio: Any) -> Any:
            if float(audio[0]) == _LEVELS[1]:
                raise RuntimeError("characteristic model broke")
            return classifier(audio)

        return first_model_kills_window_1

    _install(monkeypatch, graphs)
    monkeypatch.setattr(analysis_mod, "_construct_classifier", construct)
    on_failure = MagicMock()
    out: dict[int, str] = {}
    _features, failed = analysis_mod._run_model_sets_over_windows(_buffers(), "/m", on_failure, embeddings_out=out)
    assert failed == {1}
    on_failure.assert_called_once_with(1)
    assert graphs.plain_genre_seen == []
    assert set(out) == {0, 2}


# --- encode ----------------------------------------------------------------------------------------------------------


def test_a_vector_float16_cannot_hold_drops_that_windows_embedding_only(
    monkeypatch: pytest.MonkeyPatch, baseline: dict[int, dict[str, Any]], drops: list[tuple[float, dict[str, Any]]]
) -> None:
    graphs = _Graphs(fused_inf=frozenset({_LEVELS[1]}))
    features, failed, on_failure, out = _sweep(monkeypatch, graphs, embeddings=True)
    assert failed == set()
    on_failure.assert_not_called()
    assert features == baseline
    assert _decoded(out) == {0: _LEVELS[0], 2: _LEVELS[2]}
    assert graphs.plain_genre_seen == [], "a bad vector costs the embedding, not a second genre pass"
    assert drops == [(1, {"embedding_stage": "encode"})]


def test_a_window_under_one_patch_has_no_embedding_and_is_not_a_drop(
    monkeypatch: pytest.MonkeyPatch, baseline: dict[int, dict[str, Any]], drops: list[tuple[float, dict[str, Any]]]
) -> None:
    # Window 1 yields no patch on both routes, as a buffer of 2.0 s or less does on the real graph.
    plain_features, _failed, _on_failure, _out = _sweep(monkeypatch, _Graphs(fused_empty=frozenset({_LEVELS[1]})), embeddings=False)
    features, failed, _on_failure, out = _sweep(monkeypatch, _Graphs(fused_empty=frozenset({_LEVELS[1]})), embeddings=True)
    assert failed == set()
    assert features == plain_features
    assert features[1]["genre"] == {"predictions": []}
    assert _decoded(out) == {0: _LEVELS[0], 2: _LEVELS[2]}
    assert drops == []


# --- assembly --------------------------------------------------------------------------------------------------------


def _windows(*, embeddings: dict[int, str]) -> tuple[list[FineWindow], list[CoarseWindow]]:
    fine = [FineWindow(0, 0.0, 30.0, 120.0, "A minor", 1.0)]
    coarse = [
        CoarseWindow(idx, idx * 180.0, (idx + 1) * 180.0, "happy", "house", 0.5, {"danceability": {}}, embedding=embeddings.get(idx))
        for idx in (2, 0, 1)
    ]
    return fine, coarse


def _encoded(level: float) -> str:
    from phaze.services.analysis_embeddings import encode_embedding

    return encode_embedding(np.full(EMBEDDING_DIM, level, dtype=np.float32))


def test_assembly_lists_each_windows_embedding_in_index_order() -> None:
    fine, coarse = _windows(embeddings={0: _encoded(0.25), 2: _encoded(0.75)})
    result = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 3, include_embeddings=True)
    assert [row["window_index"] for row in result["embeddings"]["windows"]] == [0, 2]
    assert result["embeddings"]["windows"][1]["vector"] == _encoded(0.75)
    assert all("embedding" not in w for w in result["windows"]), "the vector never rides on a window payload"


def test_with_the_option_off_the_result_has_no_embeddings_key() -> None:
    fine, coarse = _windows(embeddings={0: _encoded(0.25)})
    plain_fine, plain_coarse = _windows(embeddings={})
    assert analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 3) == analysis_mod._assemble_analysis_result(
        "f", plain_fine, 1, plain_coarse, 3
    )
    assert "embeddings" not in analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 3)


def test_an_assembly_error_omits_the_embeddings_and_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]]
) -> None:
    fine, coarse = _windows(embeddings={0: _encoded(0.25)})
    plain = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 3)

    def boom(*_args: Any) -> Any:
        raise ValueError("assembly broke")

    monkeypatch.setattr(analysis_mod, "embeddings_payload", boom)
    result = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 3, include_embeddings=True)
    assert result == plain, "absent, not null: the rest of the result is exactly what the option-off run returns"
    assert drops == [(1, {"embedding_stage": "assembly"})]


def test_the_derived_window_carries_the_embedding_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    features, failed, _on_failure, out = _sweep(monkeypatch, _Graphs(), embeddings=True)
    assert out is not None
    window = analysis_mod._derive_coarse_window((0, 0.0, 1.0), features, failed, MagicMock(), embedding=out[0])
    assert window is not None
    assert window.embedding == out[0]
    assert "embedding" not in window.as_payload_dict()


# --- acceptance: end to end ------------------------------------------------------------------------------------------


def _stub_audio(monkeypatch: pytest.MonkeyPatch, graphs: _Graphs) -> None:
    def decode(_path: str, rate: int, spans: list[tuple[int, float, float]], _on_skip: object, **_kwargs: object) -> dict[int, object]:
        # Coarse windows carry their own level so a stand-in can fail one; fine windows are a flat 0.1.
        level = (lambda idx: _LEVELS[idx]) if rate == analysis_mod._COARSE_SAMPLE_RATE else (lambda _idx: 0.1)
        return {idx: np.full(round((end - start) * rate), level(idx), dtype=np.float32) for idx, start, end in spans}

    monkeypatch.setattr(analysis_mod, "_probe_duration_sec", lambda _path: 450.0)  # coarse windows: 180 + 180 + 90
    monkeypatch.setattr(analysis_mod, "_decode_windows", decode)
    monkeypatch.setattr(analysis_mod.es, "RhythmExtractor2013", lambda **_kw: MagicMock(return_value=(120.0, np.array([0.5]), 3.0, None, None)))
    monkeypatch.setattr(analysis_mod.es, "KeyExtractor", lambda **_kw: MagicMock(return_value=("A", "minor", 0.9)))
    _install(monkeypatch, graphs)


@pytest.mark.parametrize("failure", ["inference", "encode", "assembly"])
def test_an_embedding_failure_leaves_the_core_analysis_complete(
    monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]], failure: str
) -> None:
    _stub_audio(monkeypatch, _Graphs())
    plain = analysis_mod.analyze_file("unused.wav", "/m")
    healthy = analysis_mod.analyze_file("unused.wav", "/m", include_embeddings=True)
    assert [row["window_index"] for row in healthy["embeddings"]["windows"]] == [0, 1, 2]  # the healthy run has vectors to lose
    assert {k: v for k, v in healthy.items() if k != "embeddings"} == plain

    _stub_audio(
        monkeypatch,
        _Graphs(
            fused_raises=frozenset({_LEVELS[1]}) if failure == "inference" else frozenset(),
            fused_inf=frozenset({_LEVELS[1]}) if failure == "encode" else frozenset(),
        ),
    )
    if failure == "assembly":

        def boom(*_args: Any) -> Any:
            raise ValueError("assembly broke")

        monkeypatch.setattr(analysis_mod, "embeddings_payload", boom)
    result = analysis_mod.analyze_file("unused.wav", "/m", include_embeddings=True)

    assert {k: v for k, v in result.items() if k != "embeddings"} == plain, "windows, bpm, key, mood, style, genre: all unchanged"
    if failure == "assembly":
        assert "embeddings" not in result
    else:
        assert [row["window_index"] for row in result["embeddings"]["windows"]] == [0, 2]
    assert drops == [(1, {"embedding_stage": failure})]
