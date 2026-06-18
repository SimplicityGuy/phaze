"""SAQ task: process_file -- essentia analysis of a music file, posted via HTTP (Phase 26 D-05).

Replaces the prior ORM-bound body. Now reads the file from local disk via
payload.original_path, runs essentia in the process pool, and posts the
result via ctx["api_client"].put_analysis (PUT /api/internal/agent/analysis/{file_id}).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).

Wire-format conversion (D-26):
- ``analyze_file`` returns ``mood``/``style`` as strings (dominant label).
- ``AnalysisWritePayload`` requires ``mood``/``style`` as ``dict[str, float]``.
- We rebuild the dicts from ``analysis["features"]`` so the wire contract is
  honored end-to-end: ``mood`` averages each ``mood_*`` set's positive-class
  prediction across the 3 variants; ``style`` takes the top-10 genre
  predictions from the discogs effnet model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pebble import ProcessExpired

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_analysis import AnalysisFailurePayload, AnalysisWindowPayload, AnalysisWritePayload
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.tasks.pool import run_in_process_pool


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


# Phase 43 (T-43-09): cap the worker-side exception text before it crosses the HTTP
# boundary. The control-side `AnalysisFailurePayload.error` is the authoritative bound
# (max_length=2000); truncating here avoids shipping a huge traceback string at all.
_ERROR_DETAIL_MAX = 2000


def _agent_settings() -> AgentSettings:
    """Return the AgentSettings for this worker process (Phase 43).

    ``process_file`` is registered ONLY on the agent worker (``PHAZE_ROLE=agent``), so
    ``get_settings()`` returns an :class:`AgentSettings`. The module-level ``settings``
    singleton is ``ControlSettings``-typed and intentionally lacks the agent-only
    ``analysis_*`` fields (config.py docstring), so we MUST resolve via ``get_settings()``
    and narrow — mirroring the agent_worker startup invariant.
    """
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover - defensive; worker always agent-role
        msg = f"process_file requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)
    return cfg


# Phase 27 UAT gap-13: defer the essentia-bound import to call time. essentia-tensorflow
# is platform-gated in pyproject.toml ("sys_platform != 'linux' or platform_machine == 'x86_64'"),
# so it is intentionally absent on linux-arm64. Loading this module at SAQ worker
# startup must NOT fail when essentia is missing -- only process_file calls need it.
# scan_directory and extract_file_metadata are registered on the same agent worker and
# never touch essentia.
def _load_analyze_file() -> Any:
    # Deliberate function-scoped import -- module load must succeed on
    # linux-arm64 where essentia-tensorflow is not installed. See module
    # docstring above for the platform-marker rationale.
    from phaze.services.analysis import analyze_file  # noqa: PLC0415

    return analyze_file


_MUSIC_FILE_TYPES = frozenset({"mp3", "flac", "ogg", "m4a", "wav", "aiff", "wma", "aac", "opus"})

_MOOD_SET_NAMES = frozenset(
    {
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
    }
)


def _features_to_mood_dict(features: dict[str, Any]) -> dict[str, float] | None:
    """Average each ``mood_*`` set's positive-class predictions across variants.

    Returns the wire-format ``dict[str, float]`` mapping (e.g., ``{"happy": 0.82, "sad": 0.10}``)
    suitable for ``AnalysisWritePayload.mood``. Keys are stripped of the ``mood_`` prefix
    so downstream consumers see clean labels.
    """
    out: dict[str, float] = {}
    for set_name in _MOOD_SET_NAMES:
        set_data = features.get(set_name)
        if not isinstance(set_data, dict):
            continue
        variant_scores: list[float] = []
        for predictions in set_data.values():
            if isinstance(predictions, list) and predictions and isinstance(predictions[0], dict):
                # First class = positive class (binary classifier)
                try:
                    variant_scores.append(float(predictions[0]["prediction"]))
                except (KeyError, TypeError, ValueError):
                    continue
        if variant_scores:
            out[set_name.removeprefix("mood_")] = sum(variant_scores) / len(variant_scores)
    return out or None


def _features_to_style_dict(features: dict[str, Any]) -> dict[str, float] | None:
    """Convert ``features["genre"]["predictions"]`` to a wire-format ``dict[str, float]``.

    Returns the top genre predictions as ``{label: confidence}``. Labels have
    ``---`` replaced with ``/`` (consistent with ``derive_style``).
    """
    genre = features.get("genre")
    if not isinstance(genre, dict):
        return None
    predictions = genre.get("predictions")
    if not isinstance(predictions, list):
        return None
    out: dict[str, float] = {}
    for entry in predictions:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        confidence = entry.get("confidence")
        if label is None or confidence is None:
            continue
        try:
            out[str(label).replace("---", "/")] = float(confidence)
        except (TypeError, ValueError):
            continue
    return out or None


async def process_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run essentia analysis on a local file and post results via HTTP."""
    payload = ProcessFilePayload.model_validate(kwargs)

    # Skip non-music files (parity with prior body)
    if payload.file_type not in _MUSIC_FILE_TYPES:
        return {"file_id": str(payload.file_id), "status": "skipped", "reason": "not_music"}

    api: PhazeAgentClient = ctx["api_client"]

    # CPU-bound analysis in the killable pebble pool (D-23: original_path is in the payload).
    # The inner per-task timeout (settings.analysis_inner_timeout_sec, default 6600s) SIGKILLs a
    # runaway essentia child and reclaims its slot; the 60/30 caps bound how many windows
    # analyze_file decodes (Plan 02). Both are threaded from settings here so config drives them.
    cfg = _agent_settings()
    try:
        analysis = await run_in_process_pool(
            ctx,
            _load_analyze_file(),
            payload.original_path,
            payload.models_path,
            timeout=cfg.analysis_inner_timeout_sec,
            fine_cap=cfg.analysis_fine_cap,
            coarse_cap=cfg.analysis_coarse_cap,
        )
    except TimeoutError:
        # Inner pebble kill: the file is deterministically too long. TERMINAL -- report and
        # return NORMALLY so SAQ marks the job COMPLETE (no blind re-run of a >timeout file;
        # T-43-08). RESEARCH §Q5.
        await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="timeout"))
        return {"file_id": str(payload.file_id), "status": "analysis_failed"}
    except ProcessExpired:
        # essentia OOM/segfault crashed the child. Also deterministic -> TERMINAL the same way.
        await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="crashed"))
        return {"file_id": str(payload.file_id), "status": "analysis_failed"}
    except Exception as exc:
        # Generic / possibly-transient error. Report ONLY on the terminal attempt (so SAQ has
        # already exhausted retries), then re-raise so SAQ records the failed attempt. A
        # retryable attempt re-raises silently so the one real retry (retries=2) can run.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            await api.report_analysis_failed(
                payload.file_id,
                AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]),
            )
        raise

    features = analysis.get("features", {}) if isinstance(analysis, dict) else {}
    mood_dict = _features_to_mood_dict(features) if isinstance(features, dict) else None
    style_dict = _features_to_style_dict(features) if isinstance(features, dict) else None

    # Phase 31 ANL-01: forward the per-window time-series. ``analyze_file`` returns
    # ``windows`` as plain dicts (Plan 04), so we build AnalysisWindowPayload from each
    # dict directly -- NO ORM/database import (D-25 import boundary; tests/test_task_split.py).
    windows = [AnalysisWindowPayload(**w) for w in analysis.get("windows", [])] if isinstance(analysis, dict) else []

    # PUT result via HTTP (D-26 idempotent upsert; CR-01 partial-PUT semantics preserved by exclude_unset)
    await api.put_analysis(
        payload.file_id,
        AnalysisWritePayload(
            bpm=analysis.get("bpm"),
            musical_key=analysis.get("musical_key"),
            mood=mood_dict,
            style=style_dict,
            danceability=analysis.get("danceability"),
            energy=analysis.get("energy"),
            # Phase 43 windowed-analysis coverage (the five-field contract analyze_file emits).
            # Absent keys stay None so the partial-PUT contract preserves unset coverage.
            fine_windows_analyzed=analysis.get("fine_windows_analyzed"),
            fine_windows_total=analysis.get("fine_windows_total"),
            coarse_windows_analyzed=analysis.get("coarse_windows_analyzed"),
            coarse_windows_total=analysis.get("coarse_windows_total"),
            sampled=analysis.get("sampled"),
            windows=windows,
        ),
    )
    return {"file_id": str(payload.file_id), "status": "analyzed"}
