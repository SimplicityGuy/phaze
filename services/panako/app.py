"""FastAPI wrapper for Panako audio fingerprinting engine."""

import asyncio
from collections.abc import Iterator
import contextlib
import hashlib
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("panako-service")

app = FastAPI(title="Panako Service", version="0.1.0")

PANAKO_JAR = "/app/panako.jar"
# phaze-mv1f: the archive's primary content is multi-hour concert sets (the analyze lane
# budgets 6600s/file for the same reason), and Panako store/query decodes the WHOLE file
# through the JVM -- the old hardcoded 120s made every long recording deterministically
# unfingerprintable. The README always documented SUBPROCESS_TIMEOUT as an env var, but
# it was never actually wired; now it is.
SUBPROCESS_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", "3600"))

# Panako stores fingerprints in LMDB via lmdbjava, which reaches into java.nio.Buffer
# by reflection. Since JDK 16 the module system denies that by default, so EVERY store
# and query dies with:
#   InaccessibleObjectException: Unable to make field long java.nio.Buffer.address
#   accessible: module java.base does not "opens java.nio" to unnamed module
# The runtime stage ships a JRE 21, so this flag is mandatory -- without it the service
# 500s on every request even with a perfectly good jar. Upstream's own build.gradle
# sets the identical flag for its test JVM ("needed for lmdb to work correctly").
JAVA_BASE_CMD = ["java", "--add-opens=java.base/java.nio=ALL-UNNAMED", "-jar", PANAKO_JAR]
# The health probe shells out to the JVM; keep it far below the ingest timeout so a
# wedged engine surfaces as unhealthy quickly instead of hanging the healthcheck.
HEALTH_TIMEOUT = 30
# Truncate captured stderr in logs -- a stack-trace flood per failed file would bury
# the signal, but the head of the trace is what identifies the failure mode.
STDERR_LOG_LIMIT = 2000

# phaze-64w1 #sec: this sidecar is unauthenticated (uvicorn --host 0.0.0.0) and
# `file_path` arrives verbatim from an agent-registered FileRecord. Panako's OWN decode
# path (TarsosDSP's PipeDecoder, reached from every store/query) substitutes the operand
# we hand the CLI into a `/bin/bash -c "ffmpeg ... -i \"%resource%\" ..."` template with
# only bare double-quoting -- a `"`, backtick, or `$(...)` in the path breaks out of that
# quoting into arbitrary shell execution INSIDE the container. Passing our own argv as a
# list (no shell on our side) does not help: the shell that matters is Panako's, one hop
# further down. The fix is to never let a caller-controlled byte reach that substitution
# at all -- see `_resolve_confined_path` / `_staged_query_operand` / `_ingest_identity_path`
# below.
#
# `PANAKO_MEDIA_ROOTS` mirrors the comma-separated `PHAZE_AGENT_SCAN_ROOTS` shape (a
# deployment can mount more than one read-only media path into this container). There is
# DELIBERATELY no built-in default: an unset or empty value yields an EMPTY list, and
# `_resolve_confined_path` fails CLOSED on an empty list -- every file_path is rejected --
# rather than falling back to a guessed path that may not match the actual mount(s). The
# deployment must set this explicitly next to the container's real `volumes:` entries.
_raw_media_roots = os.environ.get("PANAKO_MEDIA_ROOTS", "")
MEDIA_ROOTS: list[Path] = [Path(root.strip()) for root in _raw_media_roots.split(",") if root.strip()]
# Ephemeral, per-request staging area for the QUERY-side operand only (see
# `_staged_query_operand`) -- deleted again once the request finishes. Defaults under the
# system tempdir so no extra volume/mount is required. Do NOT use this for ingest identity
# (see `INGEST_IDENTITY_DIR` below): Panako persists whatever operand `store` was given as
# that fingerprint's permanent identity, so an ingest-side staged name must outlive the
# request that created it, not be deleted with it.
STAGING_DIR = Path(os.environ.get("PANAKO_STAGING_DIR", str(Path(tempfile.gettempdir()) / "panako-stage")))
# phaze-64w1 (third bounce): a per-request uuid4 name for the INGEST side broke fingerprint
# identity -- Panako's `store` persists the exact operand it's given, and echoes it back
# verbatim as a `query` match's "match path" (the field `_parse_matches` reads as
# `track_id`). Deleting that operand after the request (as `_staged_query_operand` does)
# orphans every stored fingerprint the moment the ingest request finishes: the next query
# that matches it returns a temp name that no longer exists anywhere. `_ingest_identity_path`
# instead stages under THIS directory with a name deterministic in the resolved real path
# (sha256), created idempotently and NEVER deleted -- Panako's LMDB store (`panako_data`,
# `/data/fprint`) outlives the request, so the identity that maps back to it must too.
# Defaults alongside that same persistent volume (`$HOME`, which the Dockerfile pins to
# `/data/fprint`) rather than the ephemeral system tempdir `STAGING_DIR` uses, so identities
# survive a container restart the same way the LMDB store they describe does.
INGEST_IDENTITY_DIR = Path(os.environ.get("PANAKO_INGEST_IDENTITY_DIR", str(Path(os.environ.get("HOME", "/data/fprint")) / "panako-identities")))
# A resolved suffix is only ever used cosmetically (so ffprobe/ffmpeg see a plausible
# extension); anything not matching this is dropped rather than carried into the staged
# name, so it can never itself be a vector.
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class PathValidationError(ValueError):
    """A caller-supplied file_path failed validation. Reported as HTTP 400, never shelled out."""


def _resolve_confined_path(file_path: str) -> Path:
    """Resolve ``file_path`` and confine it under one of :data:`MEDIA_ROOTS`.

    Rejects a relative path, an embedded NUL byte, and anything that resolves (after
    ``..`` traversal / symlink resolution) outside every configured media root -- including
    when ``MEDIA_ROOTS`` is empty, which rejects EVERYTHING (fail closed: an unconfigured
    sidecar permits no path, rather than silently permitting every path). Existence is
    deliberately NOT required here -- the pipeline renames/moves files out from under
    async fingerprint jobs, and Panako's own "could not read" handling already covers a
    missing file; this function's only job is confinement against a malicious/mistaken
    path, not the separate silent-failure defect tracked elsewhere.
    """
    if not file_path or "\x00" in file_path:
        raise PathValidationError("file_path must be a non-empty path with no NUL bytes")
    candidate = Path(file_path)
    if not candidate.is_absolute():
        raise PathValidationError("file_path must be an absolute path")
    if not MEDIA_ROOTS:
        raise PathValidationError("no PANAKO_MEDIA_ROOTS configured -- refusing every file_path (fail closed)")
    resolved = candidate.resolve()
    for root in MEDIA_ROOTS:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            return resolved
    roots_display = ", ".join(str(root) for root in MEDIA_ROOTS)
    raise PathValidationError(f"file_path must resolve under one of: {roots_display}")


def _safe_suffix(resolved: Path) -> str:
    """The resolved path's suffix if it looks like a plausible extension, else "".

    Cosmetic only (so ffprobe/ffmpeg see a plausible extension); anything not matching
    this is dropped rather than carried into a staged name, so it can never itself be a
    vector.
    """
    suffix = resolved.suffix
    return suffix if _SAFE_SUFFIX_RE.match(suffix) else ""


@contextlib.contextmanager
def _staged_query_operand(file_path: str) -> Iterator[str]:
    """Validate ``file_path`` and stage it under a generated, collision-proof safe name.

    QUERY-side only -- see the `INGEST_IDENTITY_DIR` module comment for why ingest needs a
    different (persistent, deterministic) scheme instead of this one. A query's input file
    is decoded and fingerprinted transiently for comparison; Panako does not persist this
    operand anywhere a later request could look it up, so an ephemeral uuid4 name -- NO
    caller-controlled byte -- symlinked to the confined, resolved target is safe here: it's
    deleted again once this request is done.
    """
    resolved = _resolve_confined_path(file_path)
    suffix = _safe_suffix(resolved)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged = STAGING_DIR / f"pnk-{uuid.uuid4().hex}{suffix}"
    staged.symlink_to(resolved)
    try:
        yield str(staged)
    finally:
        staged.unlink(missing_ok=True)


def _ingest_identity_path(file_path: str) -> Path:
    """Resolve + confine ``file_path``, then return its deterministic, persistent staged identity.

    Panako's `store` persists EXACTLY the operand it's given as that fingerprint's identity,
    and returns it verbatim in every future query match against it (the "match path" field
    `_parse_matches` reads as `track_id`) -- so the operand can be neither the caller's raw
    path (the injection this whole module defends against) NOR an ephemeral per-request name
    (every stored fingerprint would orphan the moment the request that created it finished --
    phaze-64w1's third bounce, caught by the docker-validate smoke self-match assertion).

    Deterministic + persistent solves both at once: the identity symlink's name is
    ``sha256(str(resolved_real_path))`` -- content-independent and stable, so re-ingesting the
    same archive location reuses the same identity instead of fragmenting it -- and, unlike
    `_staged_query_operand`, it is NEVER deleted: Panako's LMDB store outlives the request, so
    the identity that maps back to it must too. `_destage_track_id` is this function's inverse.
    """
    resolved = _resolve_confined_path(file_path)
    suffix = _safe_suffix(resolved)
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()
    INGEST_IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    identity = INGEST_IDENTITY_DIR / f"pnk-{digest}{suffix}"
    if not (identity.is_symlink() and identity.resolve() == resolved):
        identity.unlink(missing_ok=True)
        identity.symlink_to(resolved)
    return identity


def _destage_track_id(track_id: str) -> str:
    """Translate a Panako-returned match identity back to the real archive path.

    Every stored fingerprint's identity is one of our own `_ingest_identity_path` symlinks
    (never the caller's raw path -- that's the injection defense), so a query match's "match
    path" is always a staged identity, not the real path directly. Resolve it back so callers
    (dedup, tracklist matching) see the archive path, not an internal staging artifact.
    Anything that isn't one of our identity symlinks -- Panako's "null" sentinel is already
    filtered out before this runs, but defend anyway against unrecognized data -- is returned
    unchanged: a destage miss degrades to an unresolved id, it never raises into a 500.
    """
    candidate = Path(track_id)
    try:
        if candidate.parent.resolve() == INGEST_IDENTITY_DIR.resolve() and candidate.is_symlink():
            return str(candidate.resolve())
    except OSError:
        pass
    return track_id


class IngestRequest(BaseModel):
    """Request body for the ingest endpoint."""

    file_path: str


class IngestResponse(BaseModel):
    """Response body for the ingest endpoint."""

    status: str
    file_path: str


class QueryMatch(BaseModel):
    """A single fingerprint match result."""

    track_id: str
    confidence: float
    timestamp: str | None = None


class QueryResponse(BaseModel):
    """Response body for the query endpoint."""

    matches: list[QueryMatch]


class HealthResponse(BaseModel):
    """Response body for the health endpoint."""

    status: str
    engine: str
    detail: str | None = None


def _log_subprocess_failure(operation: str, file_path: str, result: subprocess.CompletedProcess[str]) -> None:
    """Log a failed Panako subprocess server-side, including its stderr.

    During the 2026.7.7 outage every /ingest returned 500 for 40 minutes and left
    ZERO tracebacks in `docker logs phaze-panako` -- only uvicorn access lines. The
    stderr that would have identified the cause in seconds ("Unable to access jarfile
    /app/panako.jar") was returned to the caller and then dropped on the floor.
    """
    stderr = (result.stderr or "").strip()
    logger.error(
        "Panako %s FAILED for %s (exit %d): %s",
        operation,
        file_path,
        result.returncode,
        stderr[:STDERR_LOG_LIMIT] or "<no stderr>",
    )


def _probe_jar() -> str | None:
    """Verify the Panako jar exists and the CLI actually runs.

    Returns None when healthy, or a human-readable reason string when not.
    """
    if not Path(PANAKO_JAR).exists():
        return f"Panako jar missing at {PANAKO_JAR}"
    if Path(PANAKO_JAR).stat().st_size == 0:
        return f"Panako jar at {PANAKO_JAR} is empty (0 bytes)"
    try:
        result = subprocess.run(
            JAVA_BASE_CMD,
            capture_output=True,
            text=True,
            timeout=HEALTH_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return "java runtime not found on PATH"
    except subprocess.TimeoutExpired:
        return f"Panako CLI did not respond within {HEALTH_TIMEOUT}s"
    # Panako's bare-invocation help text goes to stdout and exits non-zero on some
    # builds, so the exit code alone is not a reliable signal. What IS reliable is
    # that a working jar produces Panako's own output; a missing/corrupt one produces
    # a JVM loader error such as "Unable to access jarfile" or "Invalid or corrupt".
    combined = f"{result.stdout}\n{result.stderr}"
    if "Unable to access jarfile" in combined or "Invalid or corrupt jarfile" in combined:
        return f"Panako jar is unreadable or corrupt: {combined.strip()[:STDERR_LOG_LIMIT]}"
    if "panako" not in combined.lower():
        return f"Panako CLI produced unrecognized output: {combined.strip()[:STDERR_LOG_LIMIT] or '<no output>'}"
    return None


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako store command synchronously (called via to_thread)."""
    return subprocess.run(
        [*JAVA_BASE_CMD, "store", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def _run_query(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako query command synchronously (called via to_thread)."""
    return subprocess.run(
        [*JAVA_BASE_CMD, "query", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


# A Panako query record has FIXED arity 13 (see _parse_matches); the trailing 7 fields
# (match id .. seconds-with-match) are engine-generated numerics/percentages that can
# never contain ';', so they can always be taken from the RIGHT end of the split.
_RECORD_FIELDS = 13
_TRAILING_FIELDS = 7


def _is_float(fragment: str) -> bool:
    try:
        float(fragment)
    except ValueError:
        return False
    return True


def _match_path(middle: list[str]) -> str | None:
    """Recover the match-path field from the variable-arity middle of a record.

    ``middle`` holds the raw ';'-split fragments of "query path ; query start ;
    query stop ; match path". Either path may itself contain ';', so locate the first
    adjacent pair of purely-numeric fragments (query start/stop) with at least one
    fragment on each side, and re-join everything after that pair -- restoring any ';'
    the split consumed inside the match path. Returns None when no such pair exists
    (structurally unparseable row).
    """
    for i in range(1, len(middle) - 2):
        if _is_float(middle[i]) and _is_float(middle[i + 1]):
            return ";".join(middle[i + 2 :]).strip()
    return None


def _parse_matches(stdout: str) -> list[QueryMatch]:
    """Parse Panako query output into structured results.

    Panako query output is semicolon-separated with FIXED arity 13:
      0 index; 1 total; 2 query path; 3 query start; 4 query stop; 5 match path;
      6 match id; 7 match start; 8 match stop; 9 score; 10 time factor;
      11 freq factor; 12 seconds-with-match percentage

    We use the match path as track_id and the seconds-with-match percentage as
    confidence normalized to 0-100.

    The two path fields embed raw file paths verbatim with NO quoting/escaping, and a
    messy personal archive legitimately contains ';' in filenames ("Sven; Vath -
    Cocoon.mp3" -- the exact corpus this tool exists to clean up). A blind positional
    ``line.split(';')`` shifts every field after an embedded ';', fabricating a phantom
    match (track_id = a path fragment, confidence read from the wrong column) or
    silently dropping a real one (phaze-9pmn). Parse from the record's fixed-arity ends
    instead: leading index/total from the LEFT, the 7 never-semicolon numeric fields
    from the RIGHT, and recover the match path from the variable middle.
    """
    matches: list[QueryMatch] = []
    for line in stdout.strip().splitlines():
        # Skip header lines or empty lines
        if not line.strip() or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < _RECORD_FIELDS:
            continue
        try:
            # Field 0 is index -- skip if it's a header (non-numeric)
            int(parts[0])
        except ValueError:
            continue
        tail = [p.strip() for p in parts[-_TRAILING_FIELDS:]]
        track_id = _match_path(parts[2:-_TRAILING_FIELDS])
        if track_id is None or not _is_float(tail[1]) or not _is_float(tail[3]) or not _is_float(tail[6]):
            logger.warning("Failed to parse match line: %s", line)
            continue
        # Panako emits a SENTINEL ROW for "no match found" rather than emitting
        # nothing: match path and match id are the literal string "null" and the
        # score/start/stop are -1. Without this guard that row is parsed as a real
        # hit, and the service returns a phantom match {track_id: "null",
        # confidence: 0.0} -- feeding a bogus duplicate into the dedup pipeline.
        if track_id.lower() == "null" or tail[0].lower() == "null":
            continue
        match_score = float(tail[3])  # match score
        if match_score < 0:
            continue
        match_percentage = float(tail[6])  # seconds-with-match percentage
        confidence = min(100.0, max(0.0, match_percentage))
        # phaze-nldg: tail[1] ("match start") is the offset INTO the matched reference track
        # where this match begins -- exactly the track-start timestamp a tracklist wants.
        # Emitted as a plain seconds string, well under the tracklist_tracks.timestamp
        # varchar(20) cap (phaze-btlu) and one of the formats
        # `cue_generator.parse_timestamp_string` accepts verbatim (its raw-float branch).
        timestamp = str(round(float(tail[1]), 1))
        matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2), timestamp=timestamp))
    return matches


@app.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Health check endpoint.

    This MUST actually exercise the jar. The 2026.7.7 panako image shipped with no
    /app/panako.jar at all, and because this endpoint used to return a hardcoded
    {"status": "healthy"}, every healthcheck and dashboard reported a healthy engine
    through a total 100%-failure outage. A health check that cannot observe the
    engine's core dependency is worse than no health check at all.
    """
    detail = await asyncio.to_thread(_probe_jar)
    if detail is not None:
        logger.error("Panako health check FAILED: %s", detail)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unhealthy", engine="panako", detail=detail)
    return HealthResponse(status="healthy", engine="panako")


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the Panako fingerprint database."""
    try:
        identity = _ingest_identity_path(request.file_path)
        result = await asyncio.to_thread(_run_ingest, str(identity))
    except PathValidationError as exc:
        logger.warning("Panako ingest rejected file_path %r: %s", request.file_path, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child on timeout, then raises. Left uncaught this
        # was a raw 500 traceback instead of a structured error (phaze-mv1f).
        detail = f"Panako ingest timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
        logger.error(detail)
        raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        _log_subprocess_failure("ingest", request.file_path, result)
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the Panako database for matches."""
    try:
        with _staged_query_operand(request.file_path) as safe_path:
            result = await asyncio.to_thread(_run_query, safe_path)
    except PathValidationError as exc:
        logger.warning("Panako query rejected file_path %r: %s", request.file_path, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except subprocess.TimeoutExpired:
        detail = f"Panako query timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
        logger.error(detail)
        raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        _log_subprocess_failure("query", request.file_path, result)
        raise HTTPException(status_code=500, detail=result.stderr)
    matches = _parse_matches(result.stdout)
    # phaze-64w1 (third bounce): a match's track_id is Panako's stored identity for
    # whatever it matched -- one of our own `_ingest_identity_path` staging symlinks, per
    # the injection defense above -- not the real archive path. Resolve it back before it
    # ever reaches a caller, or every match is keyed to an internal staging artifact.
    for match in matches:
        match.track_id = _destage_track_id(match.track_id)
    return QueryResponse(matches=matches)
