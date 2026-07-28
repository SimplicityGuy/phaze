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


# phaze-cf0z: uvicorn configures only the `uvicorn*` loggers, never the root logger, so
# without this call every record emitted below falls through to Python's `lastResort`
# handler: WARNING/ERROR reach stderr unformatted and untimestamped, and every INFO/DEBUG
# record is discarded outright. That silently degraded even the ingest-path error log this
# service already had. panako has configured logging since it was written; audfprint never
# did, which is half of why the 2026.7.7 outage left nothing usable in `docker logs`.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

# ---------------------------------------------------------------------------
# Landmark time range (phaze-5i76)
#
# audfprint packs each stored landmark into ONE np.uint32 as
# `(track_id + 1) << maxtimebits | (frame_time & (2**maxtimebits - 1))`
# (upstream hash_table.py `HashTable.store`). Two consequences, both hard:
#
#   * every stored time is MASKED to `maxtimebits`, so a reference longer than
#     `2**maxtimebits` frames aliases modulo that horizon; and
#   * the 32 bits are SHARED, so time bits are bought directly out of track-id capacity:
#     `max_track_ids = 2**(32 - maxtimebits) - 1`.
#
# The sidecar never passed `--maxtimebits`, so upstream's docopt default (`--maxtime 16384`
# -> 14 bits) applied silently. At `N_HOP = 256` / `target_sr = 11025` (audfprint_analyze) one
# frame is 256/11025 = 0.023220 s, so the horizon is 16384 * 0.023220 = 380.4 s -- SIX MINUTES
# TWENTY SECONDS. The archive's primary content is multi-hour concert sets. A 3 h set matching
# ITSELF measures 2.98 confidence instead of 83.01 (~28x collapse), because the true alignment
# is split across one delta bin per 380 s block and only the winning bin is counted.
#
# The full tradeoff, exactly (uint32 packing, 0.023220 s/frame):
#
#     bits   time horizon        max track ids
#     ----   -----------------   -------------
#       14     380.4 s (6m20s)         262,143   <- default, and the deployed value
#       15     760.9 s (12m41s)        131,071
#       16    1521.7 s (25m22s)         65,535
#       17    3043.5 s (50m43s)         32,767
#       18    6087.0 s (1h41m)          16,383
#       19   12173.9 s (3h23m)           8,191
#       20   24347.9 s (6h46m)           4,095
#
# THE DEFAULT IS DELIBERATELY LEFT AT 14, and that is the whole finding: at this project's
# scale there is no value that is simply correct. 11,180 files were already attempted against
# this database and the design target is 200K -- so every width that covers a concert set
# (>=18) caps the corpus one to two orders of magnitude BELOW its current size. Widening in
# place would trade a confidence bug for a hard ingest ceiling (upstream has no capacity
# guard; the uint32 assignment simply raises OverflowError once ids run out). The real fix is
# to sharded or per-length databases, which is a change to the STORE and is deliberately out
# of scope here.
#
# What this bead does change is that the value is now EXPLICIT, CONFIGURABLE, PUBLISHED, and
# no longer silent: the horizon and the id ceiling are reported by /health and logged at
# bootstrap, and a query longer than the horizon is logged as the confidence-deflating event
# it is instead of returning a plausible-looking low score. An operator who shards can raise
# `AUDFPRINT_MAXTIMEBITS` per shard without patching code.
#
# NOTE: the width is PERSISTED. `new` bakes it into fprint.pklz and `add`/`match` read it back
# from the pickle (upstream audfprint.py:441 applies `--maxtimebits` on `new`/`newmerge` ONLY),
# so changing this value only takes effect on a rebuild -- and rebuilding invalidates every
# fingerprint stored under the old width. It is passed on the `new` invocation only, because
# on `add`/`match` upstream ignores it.
AUDFPRINT_MAXTIMEBITS = int(os.environ.get("AUDFPRINT_MAXTIMEBITS", "14"))
if not 1 <= AUDFPRINT_MAXTIMEBITS <= 31:
    _msg = f"AUDFPRINT_MAXTIMEBITS must be between 1 and 31 (uint32 packing); got {AUDFPRINT_MAXTIMEBITS}"
    raise ValueError(_msg)
# audfprint_analyze: N_HOP = 256 frames at target_sr = 11025 Hz.
_FRAME_SECONDS = 256 / 11025
LANDMARK_TIME_HORIZON_SEC = (1 << AUDFPRINT_MAXTIMEBITS) * _FRAME_SECONDS
MAX_TRACK_IDS = (1 << (32 - AUDFPRINT_MAXTIMEBITS)) - 1
# Truncate captured stderr in logs -- a stack-trace flood per failed file would bury the
# signal, but the head of the trace is what identifies the failure mode. Mirrors panako.
STDERR_LOG_LIMIT = 2000

# phaze-1p5q #sec: this sidecar is unauthenticated (uvicorn --host 0.0.0.0, no `ports:` but
# reachable by every container on the agent compose network) and `file_path` arrives verbatim
# from an agent-registered FileRecord. Upstream audfprint parses its argv with DOCOPT, whose
# usage is `audfprint (new|add|match|...) [options] [<file>]...` -- so ANY argv element
# starting with `-` is consumed as an OPTION, never as `<file>`. The one that bites is
# `-o/--opfile`, honoured by `setup_reporter()` as a plain `open(opfile, "w")`: a request body
# of `{"file_path": "--opfile=/data/fprint/fprint.pklz"}` truncates the fingerprint database
# to zero bytes -- the exact artifact that burned 11,180 files in the 2026.7.7 outage
# (phaze-p3hj.1), reachable here in one unauthenticated request.
#
# Two independent defenses, because either alone is a single point of failure:
#   1. CONFINEMENT (`_resolve_confined_path`) -- the value must be an absolute path that
#      resolves under a configured media root. An option-shaped string is not absolute, so it
#      is rejected before any argv is built. This is the load-bearing one.
#   2. The `--` END-OF-OPTIONS TERMINATOR in both argv lists. Verified against the docopt
#      pinned in the image: `parse_argv` returns every remaining token as an Argument once it
#      sees `--` (docopt.py:441), unconditionally. Belt to the confinement's braces.
#
# Unlike panako (phaze-64w1) there is deliberately NO staged-symlink operand here. panako
# needed one because its OWN decode path substitutes the operand into a `/bin/bash -c` ffmpeg
# template, so a quote or `$(...)` in the path is shell execution one hop down. audfprint's
# decoder shells out with an argv LIST (`audio_read.py:204 subprocess.Popen([...])`, no
# shell), so there is no second-hop template to defend and a real path is safe to pass. It is
# also the RIGHT thing to pass: `hash_table.store` persists the operand verbatim in
# `self.names` as that fingerprint's permanent identity and echoes it back as the match ref
# (`track_id`), so staging the operand under a generated name would orphan every stored
# fingerprint -- precisely panako's third-bounce regression, which it had to solve with a
# persistent deterministic identity scheme. audfprint needs none of that machinery.
#
# `AUDFPRINT_MEDIA_ROOTS` mirrors panako's `PANAKO_MEDIA_ROOTS` (comma-separated,
# container-side paths). There is DELIBERATELY no built-in default: an unset or empty value
# yields an EMPTY list and `_resolve_confined_path` fails CLOSED -- every file_path is
# rejected -- rather than falling back to a guessed mount that may not be the real one.
_raw_media_roots = os.environ.get("AUDFPRINT_MEDIA_ROOTS", "")
MEDIA_ROOTS: list[Path] = [Path(root.strip()) for root in _raw_media_roots.split(",") if root.strip()]


class PathValidationError(ValueError):
    """A caller-supplied file_path failed validation. Reported as HTTP 400, never shelled out."""


def _resolve_confined_path(file_path: str) -> Path:
    """Resolve ``file_path`` and confine it under one of :data:`MEDIA_ROOTS`.

    Rejects a relative path (which is what every option-shaped string is), an embedded NUL
    byte, and anything that resolves -- after ``..`` traversal and symlink resolution --
    outside every configured media root, INCLUDING when ``MEDIA_ROOTS`` is empty, which
    rejects everything. Existence is deliberately NOT required: the pipeline renames and
    moves files out from under async fingerprint jobs, and audfprint's own "could not read"
    handling already covers a missing file. This function's only job is confinement against a
    malicious or mistaken path.

    Byte-identical in shape to panako's ``_resolve_confined_path`` (phaze-64w1) on purpose --
    the root cause of both defects is one unhardened rule ("a string from an HTTP body must
    never reach an argv operand unchecked") applied point-wise instead of to every sidecar.
    """
    if not file_path or "\x00" in file_path:
        raise PathValidationError("file_path must be a non-empty path with no NUL bytes")
    candidate = Path(file_path)
    if not candidate.is_absolute():
        raise PathValidationError("file_path must be an absolute path")
    if not MEDIA_ROOTS:
        raise PathValidationError("no AUDFPRINT_MEDIA_ROOTS configured -- refusing every file_path (fail closed)")
    resolved = candidate.resolve()
    for root in MEDIA_ROOTS:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            return resolved
    roots_display = ", ".join(str(root) for root in MEDIA_ROOTS)
    raise PathValidationError(f"file_path must resolve under one of: {roots_display}")


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


def _log_subprocess_failure(operation: str, file_path: str, result: subprocess.CompletedProcess[str]) -> None:
    """Log a failed audfprint subprocess server-side, including its stderr (phaze-cf0z).

    A SHARED helper rather than a per-site ``logger.error`` on purpose. The ingest path
    already logged its stderr; ``query`` did not, and raised ``HTTPException(500,
    detail=result.stderr)`` with no server-side record at all. The failure context was
    handed to the caller and then dropped: ``_post_query`` in the hub logs only the status
    code and never reads the body, so a query-path engine failure (the unpickle traceback of
    a torn ``fprint.pklz``, an EACCES on the media mount, a missing dependency) vanished from
    BOTH sides. That is the exact shape panako documents from the 2026.7.7 outage -- "every
    /ingest returned 500 for 40 minutes and left ZERO tracebacks in docker logs" -- which is
    why panako factored it into one helper used by both endpoints. There was nothing here to
    reuse, so ``query`` was written without it; now there is.
    """
    stderr = (result.stderr or "").strip()
    logger.error(
        "audfprint %s FAILED for %s (exit %d): %s",
        operation,
        file_path,
        result.returncode,
        stderr[:STDERR_LOG_LIMIT] or "<no stderr>",
    )


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
        argv = ["python", AUDFPRINT_SCRIPT, command, "--dbase", str(staging)]
        if command == "new":
            # phaze-5i76: bake the landmark time range in EXPLICITLY at bootstrap instead of
            # inheriting upstream's silent 14-bit docopt default. Only `new` honours the flag
            # (upstream applies it in the `new`/`newmerge` branch; `add`/`match` read the
            # width back out of the pickle), so this is the single point of control for the
            # whole database's lifetime.
            argv += ["--maxtimebits", str(AUDFPRINT_MAXTIMEBITS)]
            logger.info(
                "bootstrapping audfprint database with maxtimebits=%d: landmark times wrap at %.1f s, capacity %d track ids",
                AUDFPRINT_MAXTIMEBITS,
                LANDMARK_TIME_HORIZON_SEC,
                MAX_TRACK_IDS,
            )
        result = subprocess.run(
            # `--` terminates docopt's option scan: everything after it is an operand, so a
            # path can never be reinterpreted as a flag (phaze-1p5q).
            [*argv, "--", file_path],
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
    """Run audfprint match command synchronously (called via to_thread).

    ``file_path`` is the already-resolved, already-confined path from
    ``_resolve_confined_path`` -- never the caller's raw string (phaze-1p5q).
    """
    return subprocess.run(
        # `--`: see the ingest argv above. The match path is equally reachable (via
        # ScanLiveSetPayload.original_path) and had the identical bare-operand shape.
        ["python", AUDFPRINT_SCRIPT, "match", "--dbase", FPRINT_DB, "--", file_path],
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
# phaze-5i76: the default shape's query message carries the decoded QUERY duration
# ("{qry} {dur:.1f} sec {nhash} raw hashes"). Greedy `.+` on the query path so the fixed
# " sec {int} raw hashes as " landmark resolves to the LAST (real) occurrence even when the
# path itself contains that text -- the same reasoning as the ref regexes above. Only the
# default shape reports it; the -R shape does not, so wrap reporting is default-shape only.
_QUERY_DURATION_RE = re.compile(r"^Matched .+ (?P<dur>\d+(?:\.\d+)?) sec \d+ raw hashes as ")


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
    longest_query_sec = 0.0
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

        duration = _QUERY_DURATION_RE.match(line)
        if duration is not None:
            longest_query_sec = max(longest_query_sec, float(duration.group("dur")))

    if longest_query_sec > LANDMARK_TIME_HORIZON_SEC:
        # phaze-5i76: the query outruns the database's landmark time horizon, so the true
        # alignment is split across one delta bin per horizon-length block and only the
        # winning bin is counted. The confidence below is deflated by roughly that block
        # count and the reported offset is not a real position in the reference. Say so:
        # the defining property of this defect is that both numbers look PLAUSIBLE, so
        # nothing downstream can tell a deflated score from a weak match.
        blocks = longest_query_sec / LANDMARK_TIME_HORIZON_SEC
        logger.warning(
            "audfprint query of %.1f s exceeds the database's %.1f s landmark time horizon (maxtimebits=%d): "
            "confidences below are deflated by roughly %.1fx and match offsets are not real reference positions",
            longest_query_sec,
            LANDMARK_TIME_HORIZON_SEC,
            AUDFPRINT_MAXTIMEBITS,
            blocks,
        )

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
    # phaze-5i76: publish the landmark time range alongside the database verdict. It is a
    # PERSISTED property of this database that silently truncates every reference longer than
    # the horizon, and there was previously no way to observe it short of unpickling the file.
    detail = (
        f"{detail}; landmark time horizon {LANDMARK_TIME_HORIZON_SEC:.1f}s (maxtimebits={AUDFPRINT_MAXTIMEBITS}, capacity {MAX_TRACK_IDS} track ids)"
    )
    return HealthResponse(status="healthy", engine="audfprint", detail=detail)


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the audfprint fingerprint database."""
    # Validate BEFORE taking the lock: a rejected path must not queue behind an in-flight
    # ingest, and a 400 must never be delayed by a 3600s subprocess (phaze-1p5q, phaze-5wz9).
    try:
        resolved = _resolve_confined_path(request.file_path)
    except PathValidationError as exc:
        logger.warning("audfprint ingest rejected file_path %r: %s", request.file_path, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    async with _db_lock:
        try:
            result = await asyncio.to_thread(_run_ingest, str(resolved))
        except subprocess.TimeoutExpired:
            # subprocess.run kills the child on timeout, then raises. Left uncaught this
            # was a raw 500 traceback instead of a structured error (phaze-mv1f).
            detail = f"audfprint ingest timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
            logger.error(detail)
            raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        _log_subprocess_failure("ingest", request.file_path, result)
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
    try:
        resolved = _resolve_confined_path(request.file_path)
    except PathValidationError as exc:
        logger.warning("audfprint query rejected file_path %r: %s", request.file_path, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None
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
            result = await asyncio.to_thread(_run_query, str(resolved))
        except subprocess.TimeoutExpired:
            detail = f"audfprint query timed out after {SUBPROCESS_TIMEOUT}s for {request.file_path}"
            logger.error(detail)
            raise HTTPException(status_code=504, detail=detail) from None
    if result.returncode != 0:
        _log_subprocess_failure("match", request.file_path, result)
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
