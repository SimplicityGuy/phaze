"""FastAPI wrapper for audfprint audio fingerprinting engine."""

import asyncio
import gzip
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("audfprint-service")

app = FastAPI(title="audfprint Service", version="0.1.0")

# Serialize ALL access to the pickle DB, reads included. Ingest rewrites fprint.pklz in
# place (upstream audfprint's save_pkl is a plain pickle.dump -- no temp+rename), so a
# match that opens the file mid-rewrite reads a torn gzip-pickle and dies. The former
# _ingest_lock only excluded writer-vs-writer (Research Pitfall 3); reader-vs-writer must
# be excluded too (phaze-orq3).
_db_lock = asyncio.Lock()

AUDFPRINT_SCRIPT = "/app/audfprint/audfprint.py"
FPRINT_DB = "/data/fprint/fprint.pklz"
# phaze-mv1f: the archive's primary content is multi-hour concert sets (the analyze lane
# budgets 6600s/file for the same reason), and audfprint decodes + landmarks the WHOLE
# file -- the old hardcoded 120s made every long recording deterministically
# unfingerprintable. The README always documented SUBPROCESS_TIMEOUT as an env var, but
# it was never actually wired; now it is.
SUBPROCESS_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", "3600"))
# Chunk size for the streaming loadability probe below. The probe never materializes the
# database in memory -- it decompresses and discards, so cost is CPU, not RSS.
_PROBE_CHUNK_BYTES = 1 << 20

# The four states the on-disk database can be in. They are distinguishable by exception at
# read time (phaze-p3hj.1 §6): absent -> FileNotFoundError, zero-byte -> `EOFError: Ran out
# of input`, torn -> `EOFError: Compressed file ended before the end-of-stream marker was
# reached` / gzip.BadGzipFile, loadable -> clean read to EOF.
DatabaseState = Literal["ok", "absent", "empty", "unreadable"]


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
    detail: str = ""


def _staging_path(db_path: Path) -> Path:
    """The same-directory scratch path an ingest writes before it is promoted.

    Same directory => same filesystem => ``os.replace`` is an atomic rename rather than a
    copy. A FIXED name (not ``mkstemp``) so a hard kill cannot leak an unbounded pile of
    half-written ~100s-of-MB databases onto the volume: the next ingest reuses and truncates
    this one path. Safe because every write is already serialized by ``_db_lock`` and the
    sidecar runs a single uvicorn worker (see the lock's own rationale above).
    """
    return db_path.with_name(f".{db_path.name}.tmp")


def _probe_database(db_path: Path) -> tuple[DatabaseState, str]:
    """Classify the on-disk database by actually READING it -- never by ``exists()``.

    This is the phaze-p3hj.2 fix for the total outage diagnosed in phaze-p3hj.1: a zero-byte
    ``fprint.pklz`` satisfies ``Path.exists()``, so an existence-keyed predicate reports a
    healthy database, always picks ``add`` over ``new``, and every ``add``/``match`` then dies
    in upstream's ``pickle.load`` with ``EOFError: Ran out of input``. 11,180 files were burned
    that way. Existence is not loadability, and only loadability is worth reporting.

    The probe streams the whole gzip member and discards it, which validates the header, the
    deflate stream, and the trailing CRC32/ISIZE -- so it rejects a torn database as well as an
    empty one (the diagnosis' §5 correction: what was actually on disk was ZERO bytes, a
    narrower window than "truncated", and a probe that only rejects malformed pickles would
    have passed it). It deliberately does NOT unpickle: unpickling requires upstream's
    ``hash_table`` module in THIS process and executes the payload, and gzip integrity already
    separates every state the writer can produce.

    Read-only and side-effect-free, so it is safe on the ``/health`` and ``/query`` paths.
    """
    try:
        size = db_path.stat().st_size
    except FileNotFoundError:
        return "absent", "database absent, will bootstrap on first ingest"
    if size == 0:
        return "empty", f"database at {db_path} is zero bytes: an interrupted write left no data to load"
    try:
        with gzip.open(db_path, "rb") as handle:
            while handle.read(_PROBE_CHUNK_BYTES):
                pass
    except (OSError, EOFError) as exc:
        # gzip.BadGzipFile and zlib.error surface as OSError; a truncated member raises
        # EOFError. Classified as unusable and reported as such -- NOT swallowed.
        return "unreadable", f"database at {db_path} is unreadable ({size} bytes): {type(exc).__name__}: {exc}"
    return "ok", f"database present and loadable ({size} bytes)"


def _promote_database(staging: Path, db_path: Path) -> None:
    """Publish a freshly written database over the live path atomically.

    ``os.replace`` is a rename: readers see either the whole old database or the whole new
    one, never a zero-length or half-written file. That closes the window upstream's
    ``HashTable.save`` opens -- a plain ``gzip.open(name, "wb")`` on the LIVE path, which
    truncates it to zero bytes before a single byte of output is flushed (phaze-6xqg, folded
    into this bead). A kill anywhere in that window used to leave the database permanently
    unloadable; now it can only destroy the scratch copy.

    Both fsyncs matter: the file's, so the rename cannot be made durable ahead of the data it
    points at; the directory's, so the rename itself survives a power loss.
    """
    fd = os.open(staging, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    staging.replace(db_path)  # Path.replace IS os.replace: one atomic rename(2), same volume.
    dir_fd = os.open(db_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _database_bootstrap_status() -> tuple[bool, str]:
    """Report whether the fingerprint DB is loadable or creatable, without mutating anything.

    Deliberately no audfprint subprocess invocation: running audfprint's ``new`` here with no
    input file would reintroduce the exact ZeroDivisionError bootstrap bug this function exists
    to detect (phaze-6kw0). A missing DB is healthy as long as its directory exists and is
    writable -- the first real ``POST /ingest`` bootstraps it via ``_run_ingest``. A DB that is
    present but NOT loadable is unhealthy: that is the outage state, and reporting it healthy
    is what made the outage permanent and invisible (phaze-p3hj.1 §3).
    """
    db_path = Path(FPRINT_DB)
    state, detail = _probe_database(db_path)
    if state != "absent":
        return state == "ok", detail
    parent = db_path.parent
    if not parent.is_dir():
        return False, f"database directory missing: {parent}"
    if not os.access(parent, os.W_OK):
        return False, f"database directory not writable: {parent}"
    return True, detail


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run audfprint synchronously (called via to_thread), writing the database atomically.

    When the database isn't LOADABLE yet, bootstrap it together with THIS (real) file via
    ``new`` -- audfprint's ``new`` subcommand creates the database AND ingests the given
    file in one step, so the ingested duration is nonzero and upstream's unconditional
    summary division (``tothashes / soundfiletotaldur``) never divides by zero. Once the
    database exists, subsequent calls use ``add`` to append. This replaces the old
    empty-file ``new`` bootstrap (formerly ``_ensure_database``), which could never
    succeed -- upstream ``audfprint`` unconditionally divides by total ingested duration
    when printing its summary, and an empty ingest run means dividing by zero (phaze-6kw0).

    phaze-p3hj.2 changes two things about that, both from the phaze-p3hj.1 diagnosis:

    1. **The bootstrap predicate is loadability, not existence.** A present-but-unloadable
       database used to pin the choice at ``add`` forever, making ``new`` -- the only path
       that could rebuild -- unreachable, so the outage could never self-heal. An unusable
       database is now rebuilt, loudly (ERROR, with the byte size and the exact read error).
    2. **audfprint never writes the live path.** It writes the same-directory staging copy
       and we ``os.replace`` that over the live database only after re-probing it. Upstream's
       in-place ``gzip.open(..., "wb")`` therefore truncates the SCRATCH file, not the one
       every subsequent ``add``/``match`` has to load. The engine exiting 0 while leaving an
       unloadable artifact is a failed ingest (nonzero return, 500) -- it is not promoted, and
       the previous database survives untouched.
    """
    db_path = Path(FPRINT_DB)
    staging = _staging_path(db_path)
    state, detail = _probe_database(db_path)
    command = "add" if state == "ok" else "new"
    if state in ("empty", "unreadable"):
        logger.error("audfprint database is unusable and will be rebuilt from this file (previous fingerprints are lost): %s", detail)

    # A leftover staging file means a previous run was killed outright; it is scratch by
    # construction, so drop it rather than appending to a half-written database.
    staging.unlink(missing_ok=True)
    try:
        if command == "add":
            shutil.copyfile(db_path, staging)
        result = subprocess.run(
            ["python", AUDFPRINT_SCRIPT, command, "--dbase", str(staging), file_path],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            return result
        written_state, written_detail = _probe_database(staging)
        if written_state != "ok":
            logger.error("audfprint exited 0 but wrote an unusable database; refusing to promote it: %s", written_detail)
            failure = f"audfprint reported success but produced an unusable database, so the previous one was kept: {written_detail}"
            return subprocess.CompletedProcess(args=result.args, returncode=1, stdout=result.stdout, stderr=f"{result.stderr}\n{failure}")
        _promote_database(staging, db_path)
        return result
    finally:
        # Covers the nonzero-exit, unusable-artifact and TimeoutExpired paths alike: a run
        # that did not earn promotion leaves no scratch behind and no mark on the live DB.
        staging.unlink(missing_ok=True)


def _run_query(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run audfprint match command synchronously (called via to_thread)."""
    return subprocess.run(
        ["python", AUDFPRINT_SCRIPT, "match", "--dbase", FPRINT_DB, file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


# Upstream audfprint (dpwe/audfprint) emits a verbose match line in ONE of two shapes,
# selected by the -R/--find-time-range flag. Both end with the identical tail
# " with {N} of {M} common hashes at rank {r}". Source: audfprint_match.py
# Matcher.file_match_to_msgs (https://github.com/dpwe/audfprint) -- the exact format strings:
#
#   default (no -R): "Matched {qrymsg} as {ref} at {t:6.1f} s"
#                    where qrymsg = "{qry} {dur:.1f} sec {nhash} raw hashes"
#     e.g. "Matched /q.wav 8.4 sec 1234 raw hashes as /ref/track.mp3 at   12.3 s
#           with   456 of   789 common hashes at rank  0"
#
#   -R (find-time-range): "Matched {range:6.1f} s starting at {start:6.1f} s in {qry}
#                          to time {t:6.1f} s in {ref}"
#     e.g. "Matched   45.2 s starting at    3.1 s in /q.wav to time   12.3 s in /ref/track.mp3
#           with   456 of   789 common hashes at rank  0"
#
# `audfprint match` runs with --verbose default 1, so these verbose lines are EXACTLY what
# the deployed sidecar receives. The previous parser only understood the -R shape (it keyed off
# " in " / " to time "), but _run_query invokes match WITHOUT -R -- so every real line raised
# and was swallowed, yielding [] for every query and silently degrading the system to
# panako-only (all confidences capped at 70). See phaze-uciu.4.

# Trailing hash-count tail, shared by both shapes. Anchored to end-of-line.
_TAIL_RE = re.compile(r"\s+with\s+(\d+)\s+of\s+(\d+)\s+common hashes\s+at rank\s+-?\d+\s*$")
# default shape: ref sits between the fixed " raw hashes as " terminator of the query message
# and the trailing float-typed " at {t} s". Non-greedy ref + the anchored " at {float} s$" tail
# means a ref path that itself contains " at <n> s" still resolves to the LAST (real) time field.
# phaze-nldg: `time` (upstream's `{t:6.1f}`) is the match's offset INTO the reference track --
# i.e. exactly the track-start timestamp a tracklist wants -- so capture it, not just consume it.
_DEFAULT_REF_RE = re.compile(r"raw hashes as (?P<ref>.+?)\s+at\s+(?P<time>-?\d+(?:\.\d+)?)\s+s$")
# -R shape: ref is everything after the fixed landmark " to time {t} s in ". Anchoring on
# " to time {float} s in " (instead of counting " in " occurrences) is robust to a query path
# that itself contains " in " -- the second ' in ' the old code chased is not positionally stable.
_TIMERANGE_REF_RE = re.compile(r"to time\s+(?P<time>-?\d+(?:\.\d+)?)\s+s in (?P<ref>.+)$")


def _parse_matches(stdout: str) -> tuple[list[QueryMatch], int]:
    """Parse audfprint match output into structured results.

    Handles BOTH upstream report shapes (default and -R/--find-time-range); see the module-level
    format documentation above. Returns ``(matches, parse_failures)`` where ``parse_failures`` is
    the count of lines that LOOK like a match report (they carry the "Matched" + "common hashes"
    markers) but could not be parsed. The caller uses that count to escalate a total parse failure
    to a non-2xx response instead of silently returning ``[]`` (phaze-uciu.4).
    """
    matches: list[QueryMatch] = []
    parse_failures = 0
    for line in stdout.strip().splitlines():
        # Only "Matched ... common hashes" lines are match reports. A genuine no-match run emits
        # no such line, so a query with zero candidates is a real empty result, not a failure.
        if "Matched" not in line or "common hashes" not in line:
            continue

        tail = _TAIL_RE.search(line)
        if tail is None:
            parse_failures += 1
            logger.warning("Failed to parse audfprint match line: %s", line)
            continue
        # Strip the shared tail, then match the ref against the remaining head so each ref
        # regex's end-anchor ($) lands on the true end of the "... {ref}" / "... {t} s" segment.
        head = line[: tail.start()]
        ref_match = _DEFAULT_REF_RE.search(head) or _TIMERANGE_REF_RE.search(head)
        if ref_match is None:
            parse_failures += 1
            logger.warning("Failed to parse audfprint match line: %s", line)
            continue

        track_id = ref_match.group("ref").strip()
        matched_hashes = int(tail.group(1))
        total_hashes = int(tail.group(2))
        confidence = (matched_hashes / total_hashes * 100.0) if total_hashes > 0 else 0.0
        confidence = min(100.0, max(0.0, confidence))
        # phaze-nldg: emit the parsed offset as a plain seconds string (e.g. "12.3") -- well
        # under the tracklist_tracks.timestamp varchar(20) cap (phaze-btlu) and already one of
        # the formats `cue_generator.parse_timestamp_string` accepts verbatim (its third,
        # raw-float branch).
        timestamp = str(round(float(ref_match.group("time")), 1))
        matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2), timestamp=timestamp))

    return matches, parse_failures


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint.

    Reflects real database availability instead of a hardcoded "healthy" -- a missing-but-
    creatable DB (fresh volume, nothing ingested yet) is healthy; a DB whose directory is
    missing or unwritable is not, and neither is one that is present but cannot be loaded.
    Callers (``AudfprintAdapter.health()``) only look at the HTTP status code, so an unhealthy
    DB is surfaced as a non-2xx response.

    phaze-p3hj.1 §3 found that a correct verdict here was not enough on its own: nothing read
    this endpoint at all. There was no Docker healthcheck on the sidecar
    (``State.Health=none``) and no production call site for ``AudfprintAdapter.health()``, so
    the signal was absent rather than merely wrong. ``Dockerfile.audfprint`` now declares a
    ``HEALTHCHECK`` against this endpoint, which is what makes the verdict observable
    (``docker ps`` reports the sidecar unhealthy) without waiting on the deeper application-
    side wiring tracked separately.
    """
    available, detail = _database_bootstrap_status()
    if not available:
        logger.error("audfprint health check failed: %s", detail)
        raise HTTPException(status_code=503, detail=detail)
    return HealthResponse(status="healthy", engine="audfprint", detail=detail)


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the audfprint fingerprint database."""
    async with _db_lock:
        try:
            result = await asyncio.to_thread(_run_ingest, request.file_path)
        except subprocess.TimeoutExpired:
            # subprocess.run kills the child on timeout, then raises. Left uncaught this
            # was a raw 500 traceback instead of a structured error (phaze-mv1f).
            detail = f"audfprint ingest timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
            logger.error(detail)
            raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        logger.error("audfprint ingest failed for %s: %s", request.file_path, result.stderr)
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the audfprint database for matches.

    The absent/unusable split is the phaze-z7yw distinction applied to the database itself: a
    database that does not exist yet holds nothing to match against, so an empty result is a
    genuine no-match; a database that exists but cannot be loaded is an OUTAGE, and answering
    "no matches" to it would launder the outage into a terminal per-file verdict. The 5xx is
    what ``_post_query`` turns into ``EngineQueryError``. Probed OUTSIDE the lock (the probe
    only reads) so the absent fast path still cannot deadlock behind an in-flight ingest.
    """
    state, detail = _probe_database(Path(FPRINT_DB))
    if state == "absent":
        return QueryResponse(matches=[])
    if state != "ok":
        # Without this the match subprocess dies on the same unloadable file and the reason is
        # dropped on the floor -- _post_query never reads the body (phaze-cf0z). Log it here.
        logger.error("audfprint query cannot run: %s", detail)
        raise HTTPException(status_code=503, detail=detail)
    async with _db_lock:
        try:
            result = await asyncio.to_thread(_run_query, request.file_path)
        except subprocess.TimeoutExpired:
            detail = f"audfprint query timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
            logger.error(detail)
            raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    matches, parse_failures = _parse_matches(result.stdout)
    if parse_failures and not matches:
        # audfprint reported matches (candidate lines were present) but the parser understood
        # NONE of them. Returning [] here would silently degrade to a one-engine result capped at
        # 70 and hide the breakage -- exactly the phaze-uciu.4 failure mode. Surface a non-2xx so
        # the orchestrator (and operators) see the parse failure instead of a false "no match".
        logger.error("audfprint match parse failure: %d candidate line(s), 0 parsed", parse_failures)
        detail = f"audfprint match output unparseable: {parse_failures} candidate line(s) matched the report shape but none parsed"
        raise HTTPException(status_code=502, detail=detail)
    if parse_failures:
        # Some lines parsed, some did not: return what we have but do NOT let the partial failure
        # pass unseen -- an aggregate error log makes the degradation observable.
        logger.error("audfprint match partial parse failure: %d of %d candidate line(s) unparsed", parse_failures, parse_failures + len(matches))
    return QueryResponse(matches=matches)
