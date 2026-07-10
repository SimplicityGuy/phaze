"""Fingerprint service layer: Protocol, adapters, orchestrator, progress tracking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx
import structlog


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    """Result of ingesting a file into a fingerprint engine."""

    status: str
    error: str | None = None


@dataclass
class QueryMatch:
    """A single match from a fingerprint engine query."""

    track_id: str
    confidence: float
    timestamp: str | None = None


@dataclass
class CombinedMatch:
    """A combined match across multiple fingerprint engines."""

    track_id: str
    confidence: float
    engines: dict[str, float] = field(default_factory=dict)
    timestamp: str | None = None
    resolved_artist: str | None = None
    resolved_title: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FingerprintEngine(Protocol):
    """Common interface for fingerprint engine adapters (D-13)."""

    @property
    def name(self) -> str: ...

    @property
    def weight(self) -> float: ...

    async def ingest(self, file_path: str) -> IngestResult: ...

    async def query(self, file_path: str) -> list[QueryMatch]: ...

    async def health(self) -> bool: ...


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class AudfprintAdapter:
    """HTTP client adapter for the audfprint container (D-06)."""

    def __init__(self, base_url: str = "http://audfprint:8001", weight: float = 0.6) -> None:
        self.base_url = base_url
        self._weight = weight
        self._client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    @property
    def name(self) -> str:
        return "audfprint"

    @property
    def weight(self) -> float:
        return self._weight

    async def ingest(self, file_path: str) -> IngestResult:
        """POST /ingest with file_path, return IngestResult."""
        try:
            resp = await self._client.post("/ingest", json={"file_path": file_path})
            if resp.status_code == 200:
                return IngestResult(status="success")
            return IngestResult(status="failed", error=f"HTTP {resp.status_code}: {resp.text}")
        except Exception as exc:
            return IngestResult(status="failed", error=str(exc))

    async def query(self, file_path: str) -> list[QueryMatch]:
        """POST /query with file_path, return list of QueryMatch."""
        try:
            resp = await self._client.post("/query", json={"file_path": file_path})
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [QueryMatch(track_id=m["track_id"], confidence=m["confidence"], timestamp=m.get("timestamp")) for m in data.get("matches", [])]
        except Exception:
            logger.exception("audfprint query failed")
            return []

    async def health(self) -> bool:
        """GET /health, return True if 200."""
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()


class PanakoAdapter:
    """HTTP client adapter for the Panako container (D-06)."""

    def __init__(self, base_url: str = "http://panako:8002", weight: float = 0.4) -> None:
        self.base_url = base_url
        self._weight = weight
        self._client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    @property
    def name(self) -> str:
        return "panako"

    @property
    def weight(self) -> float:
        return self._weight

    async def ingest(self, file_path: str) -> IngestResult:
        """POST /ingest with file_path, return IngestResult."""
        try:
            resp = await self._client.post("/ingest", json={"file_path": file_path})
            if resp.status_code == 200:
                return IngestResult(status="success")
            return IngestResult(status="failed", error=f"HTTP {resp.status_code}: {resp.text}")
        except Exception as exc:
            return IngestResult(status="failed", error=str(exc))

    async def query(self, file_path: str) -> list[QueryMatch]:
        """POST /query with file_path, return list of QueryMatch."""
        try:
            resp = await self._client.post("/query", json={"file_path": file_path})
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [QueryMatch(track_id=m["track_id"], confidence=m["confidence"], timestamp=m.get("timestamp")) for m in data.get("matches", [])]
        except Exception:
            logger.exception("panako query failed")
            return []

    async def health(self) -> bool:
        """GET /health, return True if 200."""
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class FingerprintOrchestrator:
    """Combines results from multiple fingerprint engines with weighted scoring (D-11, D-12)."""

    def __init__(self, engines: list[FingerprintEngine]) -> None:
        self.engines = engines
        self.engines_by_name: dict[str, FingerprintEngine] = {e.name: e for e in engines}

    async def ingest_all(self, file_path: str) -> dict[str, IngestResult]:
        """Call each engine's ingest. Return per-engine results; never re-raise."""
        results: dict[str, IngestResult] = {}
        for engine in self.engines:
            try:
                results[engine.name] = await engine.ingest(file_path)
            except Exception as exc:
                logger.warning("Engine %s ingest failed: %s", engine.name, exc)
                results[engine.name] = IngestResult(status="failed", error=str(exc))
        return results

    async def combined_query(self, file_path: str) -> list[CombinedMatch]:
        """Query all engines and combine scores (D-11, D-12).

        If both engines match same track: weighted average.
        If only one engine matches: cap at 70.0 (D-12).
        Sort by confidence descending.
        """
        # Collect matches by track_id from each engine
        matches_by_track: dict[str, dict[str, float]] = defaultdict(dict)

        for engine in self.engines:
            try:
                engine_matches = await engine.query(file_path)
            except Exception:
                logger.exception("Engine %s query failed", engine.name)
                continue
            for match in engine_matches:
                matches_by_track[match.track_id][engine.name] = match.confidence

        # Calculate combined scores
        combined: list[CombinedMatch] = []

        for track_id, engine_scores in matches_by_track.items():
            if len(engine_scores) == len(self.engines):
                # Both engines matched: weighted average (weights sum to 1.0)
                confidence = sum(self.engines_by_name[name].weight * score for name, score in engine_scores.items())
            else:
                # Single-engine match: cap at 70.0 (D-12)
                raw_score = next(iter(engine_scores.values()))
                confidence = min(70.0, raw_score)

            combined.append(CombinedMatch(track_id=track_id, confidence=confidence, engines=dict(engine_scores)))

        # Sort by confidence descending
        combined.sort(key=lambda m: m.confidence, reverse=True)
        return combined

    async def health_all(self) -> dict[str, bool]:
        """Check health of all engines."""
        results: dict[str, bool] = {}
        for engine in self.engines:
            results[engine.name] = await engine.health()
        return results


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------


async def get_fingerprint_progress(session: AsyncSession) -> dict[str, int]:
    """Return total/completed/failed fingerprint counts, DERIVED from output tables + the dedup marker.

    All three keys share ONE denominator (D-10/D-17): files whose ``file_type`` is in
    :data:`~phaze.services.pipeline.MUSIC_VIDEO_TYPES` AND that are NOT dedup-resolved
    (``~dedup_resolved_clause()``). Because every key rides that denominator, ``completed`` and
    ``failed`` are strict subsets of ``total`` and the progress bar can never exceed 100%:

    - ``total``: count of music/video files not dedup-resolved. Derived from ``file_type`` + the dedup
      marker only -- NO ``FileRecord.state`` read (READ-04 / D-10).
    - ``completed``: of those, files whose fingerprint stage is DONE -- ``done_clause(Stage.FINGERPRINT)``:
      any engine row with ``status IN ('success','completed')`` (rides the ``ix_fprint_success`` partial
      index). This is now a FILE count. Previously it read ``state == FINGERPRINTED``, whose sole writer
      is ``retry_analysis_failed`` -- so it counted ~nothing; the number VISIBLY JUMPS. That is the fix,
      not a regression (D-11).
    - ``failed``: of those, files whose fingerprint stage is FAILED -- ``failed_clause(Stage.FINGERPRINT)``:
      no engine succeeded AND at least one engine failed (DERIV-05 aggregation). This is now a FILE count.
      Previously it was a ``fingerprint_results`` ROW count, which double-counted a two-engine failure and
      misclassified a one-success/one-failure file as failed; the number VISIBLY DROPS. That is the fix,
      not a regression (D-11).

    The 3-key ``{total, completed, failed}`` contract is preserved (D-09) so ``docs/api.md`` and the
    ``justfile`` curl recipe keep working. There is no per-engine breakdown -- ``done_clause(FINGERPRINT)``
    already IS the per-engine coverage predicate; a GROUP BY engine is Phase 87 (D-12).

    DB imports are intentionally function-local: this service module is loaded by the agent worker, which
    is forbidden from importing ``phaze.database`` / ``phaze.models`` / ``phaze.services.pipeline`` /
    ``phaze.services.stage_status`` at module scope. Only the controller invokes this function, so lazy
    imports keep the agent-worker import boundary intact (D-00e / Pitfall 5).
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from phaze.enums.stage import Stage  # noqa: PLC0415
    from phaze.models.file import FileRecord  # noqa: PLC0415
    from phaze.services.pipeline import MUSIC_VIDEO_TYPES  # noqa: PLC0415
    from phaze.services.stage_status import dedup_resolved_clause, done_clause, failed_clause  # noqa: PLC0415

    # Shared denominator (D-10/D-17): music/video files that are NOT dedup-resolved. Every key rides this
    # tuple, so completed ⊆ total and failed ⊆ total.
    denom = (FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), ~dedup_resolved_clause())

    total = (await session.execute(select(func.count(FileRecord.id)).where(*denom))).scalar_one()
    completed = (await session.execute(select(func.count(FileRecord.id)).where(*denom, done_clause(Stage.FINGERPRINT)))).scalar_one()
    failed = (await session.execute(select(func.count(FileRecord.id)).where(*denom, failed_clause(Stage.FINGERPRINT)))).scalar_one()

    return {"total": total, "completed": completed, "failed": failed}
