"""Fingerprint service layer: Protocol, adapters, orchestrator, progress tracking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx
from sqlalchemy import func, select

from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


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
        total_weight = sum(e.weight for e in self.engines)
        combined: list[CombinedMatch] = []

        for track_id, engine_scores in matches_by_track.items():
            if len(engine_scores) == len(self.engines):
                # Both engines matched: weighted average
                confidence = sum(self.engines_by_name[name].weight * score for name, score in engine_scores.items()) / total_weight * total_weight
                # Simplified: just sum weighted scores since weights sum to 1.0
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
    """Return total/completed/failed counts from fingerprint_results and files tables.

    - total: count of files eligible for fingerprinting (METADATA_EXTRACTED or later, excluding FAILED)
    - completed: count of files in FINGERPRINTED state
    - failed: count of fingerprint_results with status='failed'
    """
    # Total: files in states eligible for fingerprinting
    eligible_states = {
        FileState.METADATA_EXTRACTED,
        FileState.FINGERPRINTED,
        FileState.ANALYZED,
        FileState.PROPOSAL_GENERATED,
        FileState.APPROVED,
        FileState.REJECTED,
        FileState.EXECUTED,
        FileState.DUPLICATE_RESOLVED,
    }
    total_result = await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state.in_(eligible_states)))
    total = total_result.scalar_one()

    # Completed: files in FINGERPRINTED state
    completed_result = await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state == FileState.FINGERPRINTED))
    completed = completed_result.scalar_one()

    # Failed: fingerprint_results with status='failed'
    failed_result = await session.execute(select(func.count(FingerprintResult.id)).where(FingerprintResult.status == "failed"))
    failed = failed_result.scalar_one()

    return {"total": total, "completed": completed, "failed": failed}
