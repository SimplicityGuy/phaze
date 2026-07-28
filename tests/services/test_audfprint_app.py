"""Regression tests for the audfprint sidecar match parser (phaze-uciu.4).

The parser must handle BOTH real upstream report shapes. The fixture lines below are NOT
guessed -- they are reproduced from the exact format strings in dpwe/audfprint
``audfprint_match.py`` ``Matcher.file_match_to_msgs`` (verified against
https://raw.githubusercontent.com/dpwe/audfprint/master/audfprint_match.py):

  default (no -R):   msg = "Matched {qrymsg} as {ref} at {t:6.1f} s"
                     qrymsg = qry + " %.1f " % dur + "sec " + str(nhash) + " raw hashes"
  -R (time range):   msg = "Matched {range:6.1f} s starting at {start:6.1f} s in {qry}"
                           " to time {t:6.1f} s in {ref}"
  shared tail:       msg += " with {n:5d} of {m:5d} common hashes at rank {r:2d}"

``audfprint match`` runs with ``--verbose`` default 1, so these verbose lines are exactly what
the deployed sidecar receives. ``_run_query`` invokes ``match`` WITHOUT ``-R``, so the DEFAULT
shape is production reality; the previous parser only understood the ``-R`` shape and therefore
returned ``[]`` for every real query.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
import pytest

from tests.services.conftest import load_service_module


if TYPE_CHECKING:
    from types import ModuleType


# ---------------------------------------------------------------------------
# Database fixtures for the loadability probe (phaze-p3hj.2)
#
# NOTE for anyone reading a diff against the old revision: tests here used to seed the
# database with a bare `db_path.touch()`. That produces a ZERO-BYTE file -- precisely the
# artifact that took audfprint down for 11,180 files (phaze-p3hj.1) -- and the old assertions
# declared it healthy. Seeding with `write_loadable_db` instead is not cosmetic: a touched
# file is now, correctly, an outage.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _confined_media_root(audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a configured media root, since the sidecar fails CLOSED without one.

    phaze-1p5q: ``file_path`` is confined under ``AUDFPRINT_MEDIA_ROOTS`` before it can become
    argv, and an unset/empty root list rejects EVERYTHING with 400 (deliberately — an
    unconfigured sidecar must permit no path rather than guess a mount). The fixture paths
    throughout this module are notional ``/data/...`` paths that never touch the filesystem;
    confinement checks resolved prefixes, not existence, so one root covers them all.
    Confinement itself is exercised deliberately in ``TestFilePathConfinement`` below, which
    overrides this.
    """
    monkeypatch.setattr(audfprint_app, "MEDIA_ROOTS", [Path("/data")])


def argv_dbase(args: list[str]) -> str:
    """The ``--dbase`` value from a recorded audfprint argv.

    Read by NAME, not by position: the argv also carries a ``--`` end-of-options terminator
    (phaze-1p5q) and gained ``--maxtimebits`` (phaze-5i76), and positional indexing into it
    made every flag addition a test break rather than a behaviour change.
    """
    return args[args.index("--dbase") + 1]


def argv_operand(args: list[str]) -> str:
    """The file operand from a recorded audfprint argv -- always last, always after ``--``."""
    assert "--" in args, "argv must terminate options before the operand (phaze-1p5q)"
    return args[-1]


def write_loadable_db(path: Path, payload: bytes = b"landmark-hash-table") -> None:
    """Write a database the loadability probe accepts: one complete gzip member.

    The probe validates gzip framing (header, deflate stream, trailing CRC32/ISIZE) and
    deliberately does not unpickle, so the payload's content is irrelevant -- only that the
    member is whole.
    """
    with gzip.open(path, "wb") as handle:
        handle.write(payload)


def write_torn_db(path: Path) -> None:
    """Write a database truncated mid-gzip-member -- a valid header over a cut-off stream."""
    write_loadable_db(path, payload=b"landmark-hash-table" * 4096)
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])


# ---------------------------------------------------------------------------
# Faithful line reproduction (mirrors the upstream .format() calls byte-for-byte)
# ---------------------------------------------------------------------------


def _tail(n_matched: int, n_total: int, rank: int) -> str:
    return f" with {n_matched:5d} of {n_total:5d} common hashes at rank {rank:2d}"


def default_line(qry: str, dur: float, nhash: int, ref: str, t: float, n_matched: int, n_total: int, rank: int) -> str:
    """Reproduce the DEFAULT (no -R) verbose match line."""
    qrymsg = f"{qry} {dur:.1f} sec {nhash} raw hashes"
    return f"Matched {qrymsg} as {ref} at {t:6.1f} s" + _tail(n_matched, n_total, rank)


def timerange_line(rng: float, start: float, qry: str, t: float, ref: str, n_matched: int, n_total: int, rank: int) -> str:
    """Reproduce the -R/--find-time-range verbose match line."""
    head = f"Matched {rng:6.1f} s starting at {start:6.1f} s in {qry} to time {t:6.1f} s in {ref}"
    return head + _tail(n_matched, n_total, rank)


# ---------------------------------------------------------------------------
# _parse_matches: shape coverage
# ---------------------------------------------------------------------------


class TestParseMatchesDefaultShape:
    """The DEFAULT (no -R) report shape -- production reality."""

    def test_default_line_yields_reference_track_id(self, audfprint_app: ModuleType) -> None:
        line = default_line("/data/query/song.wav", 8.4, 1234, "/data/ref/track01.mp3", 12.3, 456, 789, 0)
        matches, failures = audfprint_app._parse_matches(line)
        assert failures == 0
        assert len(matches) == 1
        assert matches[0].track_id == "/data/ref/track01.mp3"

    def test_default_line_confidence_is_hash_ratio(self, audfprint_app: ModuleType) -> None:
        line = default_line("/q.wav", 8.4, 1234, "/ref/track.mp3", 12.3, 456, 789, 0)
        matches, _ = audfprint_app._parse_matches(line)
        # 456 / 789 * 100 == 57.79...
        assert matches[0].confidence == pytest.approx(57.79, abs=0.01)

    def test_default_ref_path_containing_at_token(self, audfprint_app: ModuleType) -> None:
        # A ref path that itself contains " at <n> s" must still resolve to the LAST (real) time.
        ref = "/data/ref/recorded at 3.0 s live/track.mp3"
        line = default_line("/q.wav", 8.4, 1234, ref, 12.3, 100, 200, 1)
        matches, failures = audfprint_app._parse_matches(line)
        assert failures == 0
        assert matches[0].track_id == ref

    def test_default_full_match_capped_at_100(self, audfprint_app: ModuleType) -> None:
        line = default_line("/q.wav", 8.4, 1234, "/ref/track.mp3", 12.3, 789, 789, 0)
        matches, _ = audfprint_app._parse_matches(line)
        assert matches[0].confidence == 100.0

    def test_default_line_yields_timestamp_from_offset(self, audfprint_app: ModuleType) -> None:
        """phaze-nldg: the {t:6.1f} offset into the reference track is a track-start timestamp."""
        line = default_line("/q.wav", 8.4, 1234, "/ref/track.mp3", 12.3, 456, 789, 0)
        matches, _ = audfprint_app._parse_matches(line)
        assert matches[0].timestamp == "12.3"

    def test_default_ref_path_containing_at_token_still_yields_real_timestamp(self, audfprint_app: ModuleType) -> None:
        # The ref path itself contains " at 3.0 s" -- the timestamp must resolve to the LAST
        # (real) time field, not the decoy embedded in the path.
        ref = "/data/ref/recorded at 3.0 s live/track.mp3"
        line = default_line("/q.wav", 8.4, 1234, ref, 12.3, 100, 200, 1)
        matches, failures = audfprint_app._parse_matches(line)
        assert failures == 0
        assert matches[0].timestamp == "12.3"


class TestParseMatchesTimeRangeShape:
    """The -R/--find-time-range report shape."""

    def test_timerange_line_yields_reference_track_id(self, audfprint_app: ModuleType) -> None:
        line = timerange_line(45.2, 3.1, "/data/query/song.wav", 12.3, "/data/ref/track01.mp3", 456, 789, 0)
        matches, failures = audfprint_app._parse_matches(line)
        assert failures == 0
        assert len(matches) == 1
        assert matches[0].track_id == "/data/ref/track01.mp3"

    def test_timerange_query_path_containing_in_token(self, audfprint_app: ModuleType) -> None:
        # The query path contains " in " -- the old code chased the 2nd ' in ' and mis-parsed.
        # Anchoring on " to time {t} s in " must recover the ref regardless.
        qry = "/data/query/live in concert.wav"
        line = timerange_line(45.2, 3.1, qry, 12.3, "/data/ref/track01.mp3", 456, 789, 0)
        matches, failures = audfprint_app._parse_matches(line)
        assert failures == 0
        assert matches[0].track_id == "/data/ref/track01.mp3"

    def test_timerange_line_yields_timestamp_from_offset(self, audfprint_app: ModuleType) -> None:
        """phaze-nldg: the {t:6.1f} "to time" offset is a track-start timestamp too."""
        line = timerange_line(45.2, 3.1, "/data/query/song.wav", 12.3, "/data/ref/track01.mp3", 456, 789, 0)
        matches, _ = audfprint_app._parse_matches(line)
        assert matches[0].timestamp == "12.3"


class TestParseMatchesFailureAccounting:
    """Malformed candidate lines are counted, not silently dropped."""

    def test_no_candidate_lines_is_clean_empty(self, audfprint_app: ModuleType) -> None:
        # A genuine no-match run emits no "Matched ... common hashes" line at all.
        matches, failures = audfprint_app._parse_matches("NOMATCH /data/query/song.wav 8.4 sec 1234 raw hashes\n")
        assert matches == []
        assert failures == 0

    def test_malformed_candidate_line_counts_as_failure(self, audfprint_app: ModuleType) -> None:
        # Looks like a report ("Matched" + "common hashes") but is unparseable.
        matches, failures = audfprint_app._parse_matches("Matched something totally malformed with common hashes\n")
        assert matches == []
        assert failures == 1

    def test_partial_failure_keeps_good_matches_and_counts_bad(self, audfprint_app: ModuleType) -> None:
        good = default_line("/q.wav", 8.4, 1234, "/ref/track.mp3", 12.3, 456, 789, 0)
        bad = "Matched broken line with common hashes"
        matches, failures = audfprint_app._parse_matches(good + "\n" + bad + "\n")
        assert len(matches) == 1
        assert failures == 1

    def test_multiple_default_lines_all_parsed(self, audfprint_app: ModuleType) -> None:
        lines = "\n".join(
            [
                default_line("/q.wav", 8.4, 1234, "/ref/a.mp3", 12.3, 456, 789, 0),
                default_line("/q.wav", 8.4, 1234, "/ref/b.mp3", 30.0, 100, 500, 1),
            ]
        )
        matches, failures = audfprint_app._parse_matches(lines)
        assert failures == 0
        assert {m.track_id for m in matches} == {"/ref/a.mp3", "/ref/b.mp3"}


# ---------------------------------------------------------------------------
# /query endpoint: escalation of parse failures (no silent [])
# ---------------------------------------------------------------------------


def _patch_query(monkeypatch: pytest.MonkeyPatch, app_module: ModuleType, tmp_path: Path, stdout: str, returncode: int = 0) -> None:
    """Point FPRINT_DB at a LOADABLE database and stub the subprocess call with fixed stdout."""
    db_path = tmp_path / "fprint.pklz"
    write_loadable_db(db_path)
    monkeypatch.setattr(app_module, "FPRINT_DB", str(db_path))

    def _fake_run_query(_file_path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="err")

    monkeypatch.setattr(app_module, "_run_query", _fake_run_query)


async def _post_query(app_module: ModuleType) -> tuple[int, dict]:
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
        resp = await client.post("/query", json={"file_path": "/data/query/song.wav"})
    return resp.status_code, resp.json()


class TestQueryEndpointEscalation:
    """The /query endpoint surfaces parse failures instead of silently returning []."""

    async def test_known_match_returns_non_empty(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        stdout = default_line("/data/query/song.wav", 8.4, 1234, "/data/ref/track01.mp3", 12.3, 456, 789, 0)
        _patch_query(monkeypatch, audfprint_app, tmp_path, stdout)
        status, body = await _post_query(audfprint_app)
        assert status == 200
        assert body["matches"]
        assert body["matches"][0]["track_id"] == "/data/ref/track01.mp3"

    async def test_total_parse_failure_returns_502(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Candidate report lines present, but NONE parse -> must not silently 200 with [].
        stdout = "Matched utterly broken output with common hashes\n"
        _patch_query(monkeypatch, audfprint_app, tmp_path, stdout)
        status, body = await _post_query(audfprint_app)
        assert status == 502
        assert "unparseable" in body["detail"]

    async def test_genuine_no_match_returns_200_empty(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_query(monkeypatch, audfprint_app, tmp_path, "NOMATCH /data/query/song.wav 8.4 sec 1234 raw hashes\n")
        status, body = await _post_query(audfprint_app)
        assert status == 200
        assert body["matches"] == []

    async def test_partial_parse_failure_returns_good_matches(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        good = default_line("/data/query/song.wav", 8.4, 1234, "/data/ref/track01.mp3", 12.3, 456, 789, 0)
        stdout = good + "\nMatched broken tail with common hashes\n"
        _patch_query(monkeypatch, audfprint_app, tmp_path, stdout)
        status, body = await _post_query(audfprint_app)
        assert status == 200
        assert len(body["matches"]) == 1


# ---------------------------------------------------------------------------
# /query vs /ingest serialization (phaze-orq3)
#
# The bug: the module lock (then named _ingest_lock) only excluded writer-vs-writer.
# /query spawned `audfprint match` with NO synchronization, so a match could open
# fprint.pklz mid-rewrite (upstream save_pkl is a plain in-place pickle.dump) and die
# on a torn gzip-pickle. Reads must serialize against writes on the same lock.
# ---------------------------------------------------------------------------


class TestQuerySerializesAgainstIngest:
    """``POST /query`` must hold the shared DB lock, not bypass it."""

    async def test_query_blocks_while_db_lock_is_held(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(
            audfprint_app,
            "_run_query",
            lambda _p: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            # Simulate an in-flight ingest: hold the DB lock, then fire a query at it.
            async with audfprint_app._db_lock:
                query_task = asyncio.create_task(client.post("/query", json={"file_path": "/data/query/song.wav"}))
                await asyncio.sleep(0.05)
                assert not query_task.done(), "/query ran while an ingest held the DB lock (torn-read race, phaze-orq3)"
            resp = await query_task

        assert resp.status_code == 200
        assert resp.json() == {"matches": []}

    async def test_ingest_blocks_while_db_lock_is_held(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The pre-existing writer-vs-writer guarantee must survive the lock's rename/rescope.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(
            audfprint_app,
            "_run_ingest",
            lambda _p: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            async with audfprint_app._db_lock:
                ingest_task = asyncio.create_task(client.post("/ingest", json={"file_path": "/data/real/song.mp3"}))
                await asyncio.sleep(0.05)
                assert not ingest_task.done()
            resp = await ingest_task

        assert resp.status_code == 200

    async def test_query_on_missing_db_short_circuits_without_lock(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No DB -> nothing to tear; the empty-result fast path must not deadlock on a held lock.
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client, audfprint_app._db_lock:
            resp = await asyncio.wait_for(client.post("/query", json={"file_path": "/data/query/song.wav"}), timeout=1.0)

        assert resp.status_code == 200
        assert resp.json() == {"matches": []}


# ---------------------------------------------------------------------------
# Fresh-volume bootstrap (phaze-6kw0)
#
# The bug: _ensure_database bootstrapped a missing DB by running `audfprint new` with NO
# input files. Upstream audfprint's do_cmd unconditionally divides by total ingested
# duration when printing its summary (tothashes / soundfiletotaldur) -- with zero files
# that's 0/0.0, a ZeroDivisionError, a nonzero exit, and a RuntimeError that could NEVER
# succeed. Every /ingest against a fresh (or reset) volume permanently 500'd.
#
# The fix: bootstrap the DB together with the FIRST REAL FILE via `new --dbase DB <file>`
# (audfprint's `new` creates the DB AND ingests the given file in one step, so the
# ingested duration is nonzero), then use `add` once the DB exists.
# ---------------------------------------------------------------------------


class _FakeAudfprintCli:
    """Stand-in for the real audfprint CLI's ``new``/``add`` side effects.

    Records every invocation and -- like the real CLI -- creates/updates the dbase file on both
    ``new`` and ``add``. This lets tests assert which command was chosen (the actual bug/fix
    distinction) purely from the recorded call, without shelling out to a real audfprint install.

    It writes a LOADABLE database, because the real one does: an ingest that exits 0 having left
    an unloadable artifact is a failed ingest under phaze-p3hj.2 and is never promoted.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        command, dbase, file_arg = args[2], argv_dbase(args), argv_operand(args)
        if command in ("new", "add"):
            write_loadable_db(Path(dbase), payload=f"{command}:{file_arg}".encode())
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


class TestRunIngestBootstrap:
    """``_run_ingest`` picks ``new`` (bootstrap) vs ``add`` (append) by DB presence."""

    def test_fresh_volume_uses_new_with_the_real_file_and_creates_db(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        assert not db_path.exists()
        result = audfprint_app._run_ingest("/data/real/song.mp3")

        assert result.returncode == 0
        assert db_path.exists()
        assert len(cli.calls) == 1
        command, dbase, file_arg = cli.calls[0][2], argv_dbase(cli.calls[0]), argv_operand(cli.calls[0])
        assert command == "new"
        # The engine writes the staging copy, never the live path (phaze-p3hj.2).
        assert dbase == str(audfprint_app._staging_path(db_path))
        assert file_arg == "/data/real/song.mp3"
        # ...and the staging copy is promoted, then cleaned up.
        assert not audfprint_app._staging_path(db_path).exists()

    def test_second_ingest_appends_via_add(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/real/first.mp3")
        second = audfprint_app._run_ingest("/data/real/second.mp3")

        assert second.returncode == 0
        assert [call[2] for call in cli.calls] == ["new", "add"]

    def test_no_ensure_database_function_remains(self, audfprint_app: ModuleType) -> None:
        # The old empty-file bootstrap path must be gone entirely, not just unreachable.
        assert not hasattr(audfprint_app, "_ensure_database")


class TestIngestEndpointBootstrap:
    """End-to-end ``POST /ingest`` against a fresh, empty volume."""

    async def test_first_ingest_on_fresh_volume_returns_200_and_creates_db(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/ingest", json={"file_path": "/data/real/song.mp3"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "ingested", "file_path": "/data/real/song.mp3"}
        assert db_path.exists()
        assert cli.calls[0][2] == "new"

    async def test_subsequent_ingest_appends(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            first = await client.post("/ingest", json={"file_path": "/data/real/first.mp3"})
            second = await client.post("/ingest", json={"file_path": "/data/real/second.mp3"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert [call[2] for call in cli.calls] == ["new", "add"]

    async def test_ingest_failure_is_logged_as_error(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # phaze-6kw0 also requires ingest 500s to leave server-side evidence in the sidecar's
        # own error log -- previously an ingest failure raised straight to HTTPException with
        # no log record at all.
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        def _failing_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="boom: disk full")

        monkeypatch.setattr(audfprint_app.subprocess, "run", _failing_run)

        transport = ASGITransport(app=audfprint_app.app)
        with caplog.at_level("ERROR", logger="audfprint-service"):
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                resp = await client.post("/ingest", json={"file_path": "/data/real/song.mp3"})

        assert resp.status_code == 500
        assert any("audfprint ingest FAILED" in record.message and "boom: disk full" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Server-side diagnosability of subprocess failures (phaze-cf0z)
#
# The bug: /ingest logged its stderr but /query did not -- it raised HTTPException(500,
# detail=result.stderr) with no log record. The hub's `_post_query` logs only the status
# code and never reads the body, so a query-path engine failure left NO error text on
# either side. panako had already factored the fix into one `_log_subprocess_failure`
# helper used by both endpoints; audfprint had inlined it into `ingest` only, so there
# was nothing to reuse when `query` was written.
#
# Second, independent gap in the same file: no `logging.basicConfig`, so every record
# fell through to Python's `lastResort` handler (unformatted ERROR/WARNING, INFO/DEBUG
# discarded outright) -- degrading even the ingest log the repo had already fixed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# file_path confinement + end-of-options terminator (phaze-1p5q #sec)
#
# The bug: `file_path` came straight off the unauthenticated HTTP body and was appended to
# the child argv as a BARE OPERAND. Upstream parses argv with docopt, whose usage is
# `audfprint (new|add|match|...) [options] [<file>]...`, so any token starting with `-` is
# consumed as an OPTION -- never as `<file>`. `-o/--opfile` is honoured by `setup_reporter()`
# as a plain `open(path, "w")`, so {"file_path": "--opfile=/data/fprint/fprint.pklz"} truncates
# the fingerprint database to zero bytes: exactly the artifact that burned 11,180 files in the
# 2026.7.7 outage, reachable in one request from anything on the agent compose network.
#
# The fix mirrors panako (phaze-64w1): confine to AUDFPRINT_MEDIA_ROOTS, fail CLOSED when
# unset, and terminate the option scan with `--`. NOT mirrored is panako's staged-operand
# scheme -- audfprint's decoder uses an argv list (no shell template to escape) and
# `hash_table.store` persists the operand as the fingerprint's permanent identity, so the real
# resolved path is both safe and required.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Landmark time range (phaze-5i76)
#
# The bug: the sidecar never passed --maxtimebits, so upstream's docopt default (--maxtime
# 16384 -> 14 bits) applied silently. audfprint packs each landmark into one np.uint32 as
# `(id+1) << maxtimebits | (time & (2**maxtimebits - 1))`, so every stored time is MASKED:
# at N_HOP=256 / target_sr=11025 (0.023220 s per frame) the horizon is 380.4 s -- 6m20s --
# against an archive whose primary content is multi-hour concert sets. A 3 h self-match
# measures 2.98 instead of 83.01.
#
# What is NOT fixed here, deliberately: the default stays at 14. The 32 bits are shared, so
# time bits come straight out of track-id capacity (2**(32-bits) - 1 ids). Every width that
# covers a concert set (>=18 bits) caps the corpus at 16,383 ids or fewer, against 11,180
# files already attempted and a 200K design target. There is no correct single-database
# value; the fix is sharded/per-length databases, which is a change to the STORE.
#
# What IS fixed: the width is explicit, configurable, published on /health, and a query that
# outruns the horizon is logged instead of silently returning a plausible deflated score.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bounded lock wait and an honest response ceiling (phaze-5wz9)
#
# The bug: `_db_lock` is held across the WHOLE of an ingest or match, so server-side wall time
# is `lock wait + database I/O + subprocess` -- bounded only by queue depth, while the hub sized
# its client budget against ONE subprocess. When a caller's transport gave up it did NOT stop
# the work: uvicorn does not cancel the handler on client disconnect and `asyncio.to_thread`
# cannot interrupt `subprocess.run`, so the lock stayed held for a caller that had left and the
# next caller inherited the same fate.
#
# The fix is a bounded wait with a fast 503 (classified engine-level by both hub adapters -> SAQ
# retry/backoff, never a per-file verdict) plus a PUBLISHED ceiling the hub derives its own
# budget from. The p3hj.2 database copy stays inside the lock -- moving it out would promote a
# stale copy over a concurrent ingest, and reverting to an in-place write is the outage itself --
# so its cost is measured and reported instead.
# ---------------------------------------------------------------------------


class TestBoundedLockWait:
    """A caller that cannot be served must be refused, not left to time out at the transport."""

    async def test_hopeless_lock_wait_returns_503_rather_than_hanging(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        write_loadable_db(tmp_path / "fprint.pklz")
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))
        monkeypatch.setattr(audfprint_app, "LOCK_WAIT_TIMEOUT", 0.05)

        transport = ASGITransport(app=audfprint_app.app)
        await audfprint_app._db_lock.acquire()
        try:
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                resp = await client.post("/ingest", json={"file_path": "/data/music/song.mp3"})
        finally:
            audfprint_app._db_lock.release()

        assert resp.status_code == 503
        assert "gave up waiting" in resp.json()["detail"]

    async def test_the_refused_caller_does_not_leave_the_lock_acquired(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cancelled `Lock.acquire` must not silently take the lock the next caller needs."""
        write_loadable_db(tmp_path / "fprint.pklz")
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))
        monkeypatch.setattr(audfprint_app, "LOCK_WAIT_TIMEOUT", 0.05)

        transport = ASGITransport(app=audfprint_app.app)
        await audfprint_app._db_lock.acquire()
        try:
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                await client.post("/query", json={"file_path": "/data/music/song.mp3"})
        finally:
            audfprint_app._db_lock.release()

        assert not audfprint_app._db_lock.locked()

    async def test_lock_is_released_after_a_successful_request(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app.subprocess, "run", _FakeAudfprintCli().run)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/ingest", json={"file_path": "/data/music/song.mp3"})

        assert resp.status_code == 200
        assert not audfprint_app._db_lock.locked()

    def test_response_ceiling_is_the_sum_of_all_three_terms(self, audfprint_app: ModuleType) -> None:
        """The ceiling must include the lock wait and the DB I/O, not just the subprocess.

        Deriving it from SUBPROCESS_TIMEOUT alone is exactly what made the hub's client budget
        too small for a queued caller.
        """
        assert audfprint_app.RESPONSE_CEILING_SEC == (
            audfprint_app.LOCK_WAIT_TIMEOUT + audfprint_app.DB_IO_BUDGET_SEC + audfprint_app.SUBPROCESS_TIMEOUT
        )
        assert audfprint_app.RESPONSE_CEILING_SEC > audfprint_app.SUBPROCESS_TIMEOUT

    @pytest.mark.parametrize(("env", "attr"), [("LOCK_WAIT_TIMEOUT", "LOCK_WAIT_TIMEOUT"), ("DB_IO_BUDGET_SEC", "DB_IO_BUDGET_SEC")])
    def test_ceiling_terms_are_env_configurable(self, monkeypatch: pytest.MonkeyPatch, env: str, attr: str) -> None:
        monkeypatch.setenv(env, "77")
        mod = load_service_module("audfprint", f"phaze_test_audfprint_{env.lower()}")
        assert getattr(mod, attr) == 77

    async def test_health_publishes_the_response_ceiling(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        write_loadable_db(tmp_path / "fprint.pklz")
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.get("/health")

        detail = resp.json()["detail"]
        assert f"response ceiling {audfprint_app.RESPONSE_CEILING_SEC}s" in detail
        assert "lock wait" in detail

    def test_database_io_over_budget_is_reported(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog) -> None:
        """The copy+probe term is DB-size-proportional, unbounded by any timeout, and inside the lock.

        It is deliberately not aborted -- killing a copy mid-way only turns a slow ingest into a
        failed one, and the copy itself cannot be removed without changing the store (reverting
        to an in-place write is the phaze-p3hj.2 outage). Measured and reported instead.
        """
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app, "DB_IO_BUDGET_SEC", 0)  # every real copy exceeds 0s
        monkeypatch.setattr(audfprint_app.subprocess, "run", _FakeAudfprintCli().run)

        with caplog.at_level("WARNING", logger="audfprint-service"):
            result = audfprint_app._run_ingest("/data/music/song.mp3")

        assert result.returncode == 0
        assert "database I/O took" in caplog.text
        assert "holding the global lock" in caplog.text


class TestHealthObservesDirectoryWritability:
    """phaze-25cc: writability was checked ONLY on the DB-absent branch.

    A present, loadable database short-circuited before the check, so any permission or mount
    problem under /data/fprint reported healthy right up until the next ingest 500'd. The
    named-volume ownership hazard that surfaced this (Docker seeds a volume's ownership from
    the image only when the volume is EMPTY at first mount, so the image-layer chown cannot
    reach a volume created by a pre-uid-pin image) is one way in; a read-only remount or a
    full filesystem is another.
    """

    async def test_present_database_in_an_unwritable_directory_is_unhealthy(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fprint_dir = tmp_path / "fprint"
        fprint_dir.mkdir()
        db_path = fprint_dir / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        fprint_dir.chmod(0o500)  # r-x: exactly the stale-uid volume symptom

        transport = ASGITransport(app=audfprint_app.app)
        try:
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                resp = await client.get("/health")
        finally:
            fprint_dir.chmod(0o700)

        assert resp.status_code == 503
        assert "not writable" in resp.json()["detail"]

    def test_a_read_only_database_file_is_still_healthy(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Assert the requirement is DIRECTORY writability, not `os.access(db, W_OK)`.

        Since phaze-p3hj.2 an ingest never opens the live database for writing -- it stages a
        copy in the same directory and renames it over the live path, and rename(2) needs
        write+execute on the DIRECTORY, not on the target file. Requiring file writability here
        would report a perfectly functional database unhealthy.
        """
        fprint_dir = tmp_path / "fprint"
        fprint_dir.mkdir()
        db_path = fprint_dir / "fprint.pklz"
        write_loadable_db(db_path)
        db_path.chmod(0o444)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        available, _detail = audfprint_app._database_bootstrap_status()
        assert available


class TestLandmarkTimeRange:
    """`--maxtimebits` must be explicit, derived correctly, and observable."""

    def test_horizon_and_capacity_match_the_uint32_packing(self, audfprint_app: ModuleType) -> None:
        # 16384 frames * (256 / 11025) s = 380.4 s; ids = 2**(32-14) - 1.
        assert audfprint_app.AUDFPRINT_MAXTIMEBITS == 14
        assert pytest.approx(380.4, abs=0.1) == audfprint_app.LANDMARK_TIME_HORIZON_SEC
        assert audfprint_app.MAX_TRACK_IDS == 262143

    @pytest.mark.parametrize(
        ("bits", "horizon_sec", "max_ids"),
        [(15, 760.9, 131071), (17, 3043.5, 32767), (18, 6087.0, 16383), (19, 12173.9, 8191), (20, 24347.9, 4095)],
    )
    def test_widening_buys_time_out_of_track_id_capacity(self, monkeypatch: pytest.MonkeyPatch, bits: int, horizon_sec: float, max_ids: int) -> None:
        """Pins the tradeoff table in the module comment, so a future widening is a deliberate act."""
        monkeypatch.setenv("AUDFPRINT_MAXTIMEBITS", str(bits))
        mod = load_service_module("audfprint", f"phaze_test_audfprint_maxtimebits_{bits}")
        assert pytest.approx(horizon_sec, abs=0.1) == mod.LANDMARK_TIME_HORIZON_SEC
        assert max_ids == mod.MAX_TRACK_IDS

    @pytest.mark.parametrize("bad", ["0", "32", "-1"])
    def test_out_of_range_width_fails_loudly_at_import(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        """A width outside the uint32 packing must not start a service that would corrupt silently."""
        monkeypatch.setenv("AUDFPRINT_MAXTIMEBITS", bad)
        with pytest.raises(ValueError, match="AUDFPRINT_MAXTIMEBITS"):
            load_service_module("audfprint", f"phaze_test_audfprint_maxtimebits_bad_{bad}")

    def test_bootstrap_passes_maxtimebits_explicitly(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/music/song.mp3")

        argv = cli.calls[0]
        assert argv[2] == "new"
        assert argv[argv.index("--maxtimebits") + 1] == str(audfprint_app.AUDFPRINT_MAXTIMEBITS)

    def test_append_does_not_pass_maxtimebits(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The width is PERSISTED in the pickle; upstream honours the flag on `new` only.

        Passing it on `add` would read as a live knob that silently does nothing.
        """
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/music/first.mp3")
        audfprint_app._run_ingest("/data/music/second.mp3")

        assert [call[2] for call in cli.calls] == ["new", "add"]
        assert "--maxtimebits" not in cli.calls[1]

    def test_query_does_not_pass_maxtimebits(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))

        audfprint_app._run_query("/data/music/song.mp3")

        assert "--maxtimebits" not in cli.calls[0]

    def test_query_longer_than_the_horizon_is_logged(self, audfprint_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        """The whole defect is that a deflated score looks like a weak match. Say so out loud."""
        # 3 h query against a 380.4 s horizon: upstream reports a plausible-looking low ratio.
        line = default_line("/data/music/set.mp3", 10800.0, 48310, "/data/music/set.mp3", -8734.2, 1712, 48310, 0)
        with caplog.at_level("WARNING", logger="audfprint-service"):
            matches, failures = audfprint_app._parse_matches(line)

        assert failures == 0
        assert matches[0].confidence == pytest.approx(3.54, abs=0.01)
        assert "exceeds the database's" in caplog.text
        assert "10800.0" in caplog.text

    def test_query_within_the_horizon_is_not_logged(self, audfprint_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        line = default_line("/data/music/track.mp3", 240.0, 1234, "/data/music/track.mp3", 12.3, 456, 789, 0)
        with caplog.at_level("WARNING", logger="audfprint-service"):
            audfprint_app._parse_matches(line)
        assert "landmark time horizon" not in caplog.text

    def test_query_duration_survives_a_path_containing_the_landmark_text(self, audfprint_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        """A query path that itself contains ' sec 1 raw hashes as ' must not shadow the real field."""
        qry = "/data/music/12.0 sec 1 raw hashes as decoy/set.mp3"
        line = default_line(qry, 10800.0, 48310, "/data/music/ref.mp3", -8734.2, 1712, 48310, 0)
        with caplog.at_level("WARNING", logger="audfprint-service"):
            audfprint_app._parse_matches(line)
        assert "10800.0" in caplog.text

    async def test_health_publishes_the_horizon(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert "landmark time horizon 380.4s" in detail
        assert "maxtimebits=14" in detail
        assert "262143 track ids" in detail


class TestFilePathConfinement:
    """A caller-supplied file_path must never reach argv unvalidated."""

    @staticmethod
    def _no_subprocess(monkeypatch: pytest.MonkeyPatch, app_module: ModuleType) -> None:
        """Make ANY subprocess launch a hard test failure -- a rejected path must not shell out."""

        def _explode(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"a rejected file_path reached the CLI: {args}")

        monkeypatch.setattr(app_module.subprocess, "run", _explode)

    @pytest.mark.parametrize("route", ["/ingest", "/query"])
    @pytest.mark.parametrize(
        "evil",
        [
            "--opfile=/data/fprint/fprint.pklz",  # the database-truncating primitive
            "-o/data/fprint/fprint.pklz",  # short form
            "--dbase=/tmp/x.pklz",  # redirect the store
            "--list",  # treat the operand as a file list
            "relative/path.mp3",  # not absolute
            "/etc/passwd",  # absolute but outside the media root
            "/data/../etc/passwd",  # traversal that RESOLVES outside
        ],
    )
    async def test_hostile_file_path_is_rejected_before_argv(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str, evil: str
    ) -> None:
        write_loadable_db(tmp_path / "fprint.pklz")
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))
        self._no_subprocess(monkeypatch, audfprint_app)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post(route, json={"file_path": evil})

        assert resp.status_code == 400

    def test_nul_byte_is_rejected(self, audfprint_app: ModuleType) -> None:
        with pytest.raises(audfprint_app.PathValidationError):
            audfprint_app._resolve_confined_path("/data/music/song\x00.mp3")

    @pytest.mark.parametrize("route", ["/ingest", "/query"])
    async def test_unconfigured_media_roots_fails_closed(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
    ) -> None:
        """An unconfigured sidecar must refuse everything -- never fall back to permitting everything."""
        monkeypatch.setattr(audfprint_app, "MEDIA_ROOTS", [])
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))
        self._no_subprocess(monkeypatch, audfprint_app)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post(route, json={"file_path": "/data/music/song.mp3"})

        assert resp.status_code == 400
        assert "no AUDFPRINT_MEDIA_ROOTS configured" in resp.json()["detail"]

    def test_media_roots_parses_a_comma_separated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDFPRINT_MEDIA_ROOTS", "/data/downloads , /data/staging ,")
        mod = load_service_module("audfprint", "phaze_test_audfprint_media_roots")
        assert [Path("/data/downloads"), Path("/data/staging")] == mod.MEDIA_ROOTS
        assert mod._resolve_confined_path("/data/staging/set.mp3") == Path("/data/staging/set.mp3")

    def test_media_roots_defaults_to_empty_not_to_a_guessed_mount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUDFPRINT_MEDIA_ROOTS", raising=False)
        mod = load_service_module("audfprint", "phaze_test_audfprint_media_roots_unset")
        assert mod.MEDIA_ROOTS == []

    def test_argv_terminates_options_before_the_operand(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`--` is the second, independent defense: docopt cannot reparse an operand as a flag."""
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/music/song.mp3")
        audfprint_app._run_query("/data/music/song.mp3")

        for call in cli.calls:
            assert call[-2] == "--", f"operand is not preceded by the end-of-options terminator: {call}"
            assert call[-1] == "/data/music/song.mp3"

    async def test_confined_path_is_resolved_before_it_becomes_the_stored_identity(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The operand IS the persistent track identity, so it must be the RESOLVED real path.

        `hash_table.store` keeps the operand verbatim in `self.names` and echoes it back as a
        match's ref. Unlike panako we therefore pass the real path (resolved, confined), never
        a staged name -- staging here would orphan every stored fingerprint.
        """
        media = tmp_path / "media"
        (media / "sub").mkdir(parents=True)
        target = media / "sub" / "song.mp3"
        target.write_bytes(b"audio")
        monkeypatch.setattr(audfprint_app, "MEDIA_ROOTS", [media])
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/ingest", json={"file_path": str(media / "sub" / ".." / "sub" / "song.mp3")})

        assert resp.status_code == 200
        assert argv_operand(cli.calls[0]) == str(target.resolve())


class TestSubprocessFailureLogging:
    """Both subprocess failure paths must leave the engine's stderr in the sidecar's own log."""

    async def test_query_failure_logs_stderr(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The exact previously-silent path: match exits nonzero, the caller gets a 500, and
        # before this fix the reason existed nowhere on the server.
        _patch_query(monkeypatch, audfprint_app, tmp_path, stdout="", returncode=1)
        with caplog.at_level("ERROR", logger="audfprint-service"):
            status, _ = await _post_query(audfprint_app)

        assert status == 500
        assert any("audfprint match FAILED" in record.message and "err" in record.message for record in caplog.records)

    def test_log_helper_handles_absent_stderr(self, audfprint_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="")
        with caplog.at_level("ERROR", logger="audfprint-service"):
            audfprint_app._log_subprocess_failure("ingest", "/data/real/song.mp3", result)
        assert "<no stderr>" in caplog.text

    def test_log_helper_truncates_a_stderr_flood(self, audfprint_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="x" * 10_000)
        with caplog.at_level("ERROR", logger="audfprint-service"):
            audfprint_app._log_subprocess_failure("match", "/data/real/song.mp3", result)
        assert "x" * audfprint_app.STDERR_LOG_LIMIT in caplog.text
        assert "x" * (audfprint_app.STDERR_LOG_LIMIT + 1) not in caplog.text

    def test_root_logging_is_configured(self, audfprint_app: ModuleType) -> None:
        """Without basicConfig, uvicorn leaves this logger on `lastResort`: no INFO at all.

        Asserted through the effective level rather than the handler list, because that is
        the property that actually decides whether an INFO record survives.
        """
        assert audfprint_app.logger.getEffectiveLevel() <= logging.INFO
        assert logging.getLogger().handlers, "root logger has no handler -- records fall through to lastResort"


# ---------------------------------------------------------------------------
# Subprocess timeout: duration-appropriate, env-wired, cleanly surfaced (phaze-mv1f)
#
# The bug: SUBPROCESS_TIMEOUT was a hardcoded 120 (the README documented an env var that
# was never wired), so every multi-hour concert set -- the archive's PRIMARY content --
# deterministically timed out; and subprocess.TimeoutExpired propagated uncaught, turning
# the timeout into a raw 500 traceback instead of a structured error.
# ---------------------------------------------------------------------------


class TestSubprocessTimeoutConfiguration:
    """The timeout must be sized for multi-hour sets and actually read the environment."""

    def test_default_timeout_is_sized_for_long_sets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUBPROCESS_TIMEOUT", raising=False)
        mod = load_service_module("audfprint", "phaze_test_audfprint_timeout_default")
        assert mod.SUBPROCESS_TIMEOUT == 3600

    def test_timeout_env_override_is_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The README always documented SUBPROCESS_TIMEOUT as an env var; before the fix
        # the override silently did not exist.
        monkeypatch.setenv("SUBPROCESS_TIMEOUT", "7000")
        mod = load_service_module("audfprint", "phaze_test_audfprint_timeout_override")
        assert mod.SUBPROCESS_TIMEOUT == 7000

    def test_run_commands_pass_configured_timeout(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app, "SUBPROCESS_TIMEOUT", 1234)
        seen: list[object] = []

        def _capture(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs.get("timeout"))
            if args[2] in ("new", "add"):
                write_loadable_db(Path(argv_dbase(args)))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(audfprint_app.subprocess, "run", _capture)
        audfprint_app._run_ingest("/data/real/song.mp3")
        audfprint_app._run_query("/data/real/song.mp3")
        assert seen == [1234, 1234]


class TestTimeoutExpiredHandling:
    """A timed-out engine run must surface as a structured 504, not an unhandled traceback."""

    @staticmethod
    def _raise_timeout(_file_path: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="audfprint", timeout=1234)

    async def test_ingest_timeout_returns_structured_504(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(audfprint_app, "_run_ingest", self._raise_timeout)

        transport = ASGITransport(app=audfprint_app.app)
        with caplog.at_level("ERROR", logger="audfprint-service"):
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                resp = await client.post("/ingest", json={"file_path": "/data/real/twohourset.mp3"})

        assert resp.status_code == 504
        assert "timed out after" in resp.json()["detail"]
        assert "/data/real/twohourset.mp3" in resp.json()["detail"]
        assert any("timed out" in record.message for record in caplog.records)

    async def test_query_timeout_returns_structured_504(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app, "_run_query", self._raise_timeout)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/query", json={"file_path": "/data/query/twohourset.mp3"})

        assert resp.status_code == 504
        assert "timed out after" in resp.json()["detail"]

    async def test_db_lock_released_after_ingest_timeout(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The timeout must not leak the DB lock, or every later ingest/query deadlocks.
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        calls: list[str] = []

        def _flaky(file_path: str) -> subprocess.CompletedProcess[str]:
            calls.append(file_path)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="audfprint", timeout=1)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(audfprint_app, "_run_ingest", _flaky)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            first = await client.post("/ingest", json={"file_path": "/data/real/a.mp3"})
            second = await client.post("/ingest", json={"file_path": "/data/real/b.mp3"})

        assert first.status_code == 504
        assert second.status_code == 200
        assert not audfprint_app._db_lock.locked()


# ---------------------------------------------------------------------------
# /health reflects DB unavailability (phaze-6kw0)
# ---------------------------------------------------------------------------


class TestDatabaseBootstrapStatus:
    """``_database_bootstrap_status`` is a read-only filesystem probe -- never shells out."""

    def test_existing_db_is_available(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        available, detail = audfprint_app._database_bootstrap_status()

        assert available
        assert "present" in detail

    def test_missing_db_with_writable_parent_is_available(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        available, detail = audfprint_app._database_bootstrap_status()

        assert available
        assert "bootstrap" in detail

    def test_missing_db_directory_is_unavailable(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "does-not-exist" / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        available, detail = audfprint_app._database_bootstrap_status()

        assert not available
        assert "missing" in detail

    @pytest.mark.skipif(sys.platform.startswith("win") or os.geteuid() == 0, reason="permission bits are unenforceable for root/Windows CI runners")
    def test_missing_db_unwritable_directory_is_unavailable(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        db_path = locked_dir / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        locked_dir.chmod(0o500)
        try:
            available, detail = audfprint_app._database_bootstrap_status()
        finally:
            locked_dir.chmod(0o700)

        assert not available
        assert "not writable" in detail


class TestHealthEndpoint:
    """``GET /health`` must surface DB unavailability as a non-2xx, not a hardcoded 200."""

    async def _get_health(self, audfprint_app: ModuleType) -> tuple[int, dict]:
        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.get("/health")
        return resp.status_code, resp.json()

    async def test_existing_db_reports_healthy(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        status, body = await self._get_health(audfprint_app)

        assert status == 200
        assert body["status"] == "healthy"

    async def test_fresh_volume_still_reports_healthy(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # A DB that doesn't exist YET is not the same as a DB that can never exist -- a fresh
        # volume with nothing ingested is healthy (it will bootstrap on first /ingest).
        db_path = tmp_path / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        status, body = await self._get_health(audfprint_app)

        assert status == 200
        assert body["status"] == "healthy"

    async def test_unavailable_db_directory_reports_unhealthy(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "does-not-exist" / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        status, body = await self._get_health(audfprint_app)

        assert status == 503
        assert "missing" in body["detail"]

    async def test_unavailable_db_is_logged_as_error(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_path = tmp_path / "does-not-exist" / "fprint.pklz"
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        with caplog.at_level("ERROR", logger="audfprint-service"):
            await self._get_health(audfprint_app)

        assert any("audfprint health check failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# The total audfprint outage: zero-byte fprint.pklz (phaze-p3hj.2, folding phaze-6xqg)
#
# The bug, established live in docs/spikes/phaze-p3hj.1-audfprint-total-outage-diagnosis.md:
# /data/fprint/fprint.pklz on the deployed volume was ZERO BYTES. Upstream's HashTable.save
# writes the live path in place -- `gzip.open(name, "wb")` truncates to 0 before a single byte
# of output is flushed, with no temp file and no rename -- so any kill in that window leaves
# exactly that artifact. Every later `add`/`match` loads it first and dies with
# `EOFError: Ran out of input` before it ever opens the audio: 11,180 files failed with ONE
# byte-identical error, and 0 bytes of fingerprint data were ever persisted.
#
# It was permanent because both the bootstrap decision (`command = "add" if
# Path(FPRINT_DB).exists()`) and the health check (`if db_path.exists(): return True`) accepted
# a zero-byte file as a working database. `new` -- the only path that could rebuild -- was
# unreachable, and /health answered 200 "database present" over a dead engine.
#
# The fix has three parts, and each is pinned below: write atomically (so the window cannot
# reopen), decide on LOADABILITY rather than existence (so an unusable DB is rebuilt, not
# ridden forever), and report it (ERROR logs + a Docker HEALTHCHECK).
# ---------------------------------------------------------------------------


class TestProbeDatabaseClassifiesOnDiskStates:
    """``_probe_database`` separates the four states by reading, not by ``exists()``."""

    def test_absent_database_is_absent(self, audfprint_app: ModuleType, tmp_path: Path) -> None:
        state, detail = audfprint_app._probe_database(tmp_path / "fprint.pklz")
        assert state == "absent"
        assert "bootstrap" in detail

    def test_zero_byte_database_is_empty_not_present(self, audfprint_app: ModuleType, tmp_path: Path) -> None:
        # THE production artifact: exists() is True, size is 0, every load dies.
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        assert db_path.exists(), "precondition: the outage state satisfies exists()"

        state, detail = audfprint_app._probe_database(db_path)

        assert state == "empty"
        assert "zero bytes" in detail

    def test_torn_database_is_unreadable(self, audfprint_app: ModuleType, tmp_path: Path) -> None:
        # The wider window phaze-6xqg predicted (truncated mid-stream) must be rejected too --
        # a probe that only rejected malformed pickles would have passed the zero-byte file.
        db_path = tmp_path / "fprint.pklz"
        write_torn_db(db_path)

        state, detail = audfprint_app._probe_database(db_path)

        assert state == "unreadable"
        assert "unreadable" in detail

    def test_loadable_database_is_ok(self, audfprint_app: ModuleType, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)

        state, detail = audfprint_app._probe_database(db_path)

        assert state == "ok"
        assert "loadable" in detail

    def test_probe_does_not_mutate_the_database(self, audfprint_app: ModuleType, tmp_path: Path) -> None:
        # Safe to call from /health and /query: it must never write, truncate or delete.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        before = db_path.read_bytes()

        audfprint_app._probe_database(db_path)

        assert db_path.read_bytes() == before


class TestUnusableDatabaseIsRebuiltNotRiddenForever:
    """A present-but-unloadable DB must take the ``new`` branch -- the outage must self-heal."""

    def test_zero_byte_database_bootstraps_via_new(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        result = audfprint_app._run_ingest("/data/real/song.mp3")

        assert result.returncode == 0
        assert [call[2] for call in cli.calls] == ["new"], "an unloadable DB must be REBUILT, not appended to forever"
        assert audfprint_app._probe_database(db_path)[0] == "ok"

    def test_torn_database_bootstraps_via_new(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_torn_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/real/song.mp3")

        assert [call[2] for call in cli.calls] == ["new"]

    def test_rebuild_is_logged_as_error(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Discarding a database is data loss; it is reported at ERROR, not quietly absorbed.
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app.subprocess, "run", _FakeAudfprintCli().run)

        with caplog.at_level("ERROR", logger="audfprint-service"):
            audfprint_app._run_ingest("/data/real/song.mp3")

        assert any("unusable" in record.message and record.levelname == "ERROR" for record in caplog.records)

    def test_healthy_database_still_appends_via_add(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The rebuild must be reserved for genuinely unusable databases -- a loadable one is
        # appended to, or every ingest would silently discard the whole corpus.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/real/song.mp3")

        assert [call[2] for call in cli.calls] == ["add"]


class TestIngestWritesTheDatabaseAtomically:
    """A kill during the engine's save must not be able to destroy the live database."""

    def test_engine_never_receives_the_live_database_path(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The root cause in one assertion: upstream truncates whatever --dbase points at.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        cli = _FakeAudfprintCli()
        monkeypatch.setattr(audfprint_app.subprocess, "run", cli.run)

        audfprint_app._run_ingest("/data/real/song.mp3")

        assert cli.calls[0][4] != str(db_path)
        assert Path(cli.calls[0][4]).parent == db_path.parent, "staging must share the volume, or os.replace is not atomic"

    def test_kill_during_save_leaves_the_previous_database_intact(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Reproduces the precipitating event: the engine truncates its dbase to zero bytes (what
        # `gzip.open(name, "wb")` does first) and is then killed before any output is flushed.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path, payload=b"the-corpus-we-must-not-lose")
        before = db_path.read_bytes()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        def _truncate_then_die(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            Path(args[4]).write_bytes(b"")
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(audfprint_app.subprocess, "run", _truncate_then_die)

        with pytest.raises(subprocess.TimeoutExpired):
            audfprint_app._run_ingest("/data/real/twohourset.mp3")

        assert db_path.read_bytes() == before, "the live database was destroyed by a killed ingest -- phaze-6xqg has regressed"
        assert audfprint_app._probe_database(db_path)[0] == "ok"
        assert not audfprint_app._staging_path(db_path).exists(), "scratch must not accumulate on the volume"

    def test_failed_ingest_leaves_the_previous_database_intact(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path, payload=b"the-corpus-we-must-not-lose")
        before = db_path.read_bytes()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        def _fail(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            Path(args[4]).write_bytes(b"")
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="OSError: wavfile2peaks")

        monkeypatch.setattr(audfprint_app.subprocess, "run", _fail)

        result = audfprint_app._run_ingest("/data/real/undecodable.mp3")

        assert result.returncode == 1
        assert db_path.read_bytes() == before

    def test_unusable_artifact_is_not_promoted_even_when_the_engine_exits_zero(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Promotion is gated on re-probing what was actually written; a "successful" run that
        # produced a zero-byte file would otherwise recreate the outage in one step.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path, payload=b"the-corpus-we-must-not-lose")
        before = db_path.read_bytes()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        def _lies(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            Path(args[4]).write_bytes(b"")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(audfprint_app.subprocess, "run", _lies)

        with caplog.at_level("ERROR", logger="audfprint-service"):
            result = audfprint_app._run_ingest("/data/real/song.mp3")

        assert result.returncode != 0, "an unloadable artifact is a FAILED ingest, not a success"
        assert db_path.read_bytes() == before
        assert any("refusing to promote" in record.message for record in caplog.records)

    def test_stale_staging_file_is_discarded_not_appended_to(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A hard kill leaves scratch behind; the next ingest must start from the LIVE database.
        db_path = tmp_path / "fprint.pklz"
        write_loadable_db(db_path, payload=b"live")
        live_before = db_path.read_bytes()
        staging = audfprint_app._staging_path(db_path)
        staging.write_bytes(b"")
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        seen: list[bytes] = []

        def _record_input(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(Path(args[4]).read_bytes())
            write_loadable_db(Path(args[4]))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(audfprint_app.subprocess, "run", _record_input)

        audfprint_app._run_ingest("/data/real/song.mp3")

        # The engine was handed a copy of the LIVE database, not the abandoned scratch file.
        assert seen == [live_before]

    async def test_ingest_after_the_outage_returns_200_and_leaves_a_loadable_database(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # End-to-end recovery: the exact production state in, a working engine out.
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app.subprocess, "run", _FakeAudfprintCli().run)

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/ingest", json={"file_path": "/data/real/song.mp3"})

        assert resp.status_code == 200
        assert audfprint_app._probe_database(db_path)[0] == "ok"


class TestOutageIsReported:
    """The outage state must be loud on every surface: /health, /query and the logs."""

    async def test_zero_byte_database_reports_unhealthy(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The live sidecar answered 200 {"status":"healthy","detail":"database present"} over a
        # dead engine for 10 days. That is the assertion this bead exists to invert.
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.get("/health")

        assert resp.status_code == 503
        assert "zero bytes" in resp.json()["detail"]

    async def test_torn_database_reports_unhealthy(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db_path = tmp_path / "fprint.pklz"
        write_torn_db(db_path)
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.get("/health")

        assert resp.status_code == 503

    async def test_query_on_unusable_database_is_an_outage_not_a_no_match(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # phaze-z7yw: 200 {"matches": []} is a terminal no-match verdict. An unloadable database
        # has matched nothing because it cannot run, which _post_query must see as 5xx.
        db_path = tmp_path / "fprint.pklz"
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))

        transport = ASGITransport(app=audfprint_app.app)
        with caplog.at_level("ERROR", logger="audfprint-service"):
            async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
                resp = await client.post("/query", json={"file_path": "/data/query/song.wav"})

        assert resp.status_code == 503
        assert resp.status_code >= 500, "must classify as an engine outage, not a per-file no-match"
        # phaze-cf0z: the query path drops engine stderr, so the sidecar's own log is the only
        # place this can ever be seen.
        assert any("audfprint query cannot run" in record.message for record in caplog.records)

    async def test_query_on_absent_database_is_still_a_clean_no_match(
        self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The other half of the z7yw distinction must survive: nothing ingested yet is not an
        # outage, and must not be escalated into one.
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(tmp_path / "fprint.pklz"))

        transport = ASGITransport(app=audfprint_app.app)
        async with AsyncClient(transport=transport, base_url="http://audfprint") as client:
            resp = await client.post("/query", json={"file_path": "/data/query/song.wav"})

        assert resp.status_code == 200
        assert resp.json() == {"matches": []}


class TestSidecarHealthcheckIsWired:
    """A correct /health verdict is worthless if nothing reads it (phaze-p3hj.1 §3).

    The live sidecars ran with ``State.Health=none`` -- no Docker healthcheck, and no production
    call site for ``AudfprintAdapter.health()`` either -- so the health signal was not merely
    wrong, it was ABSENT. Baking the check into the image is what makes the fix observable.
    """

    DOCKERFILE = Path(__file__).resolve().parents[2] / "services" / "audfprint" / "Dockerfile.audfprint"

    def test_image_declares_a_healthcheck_against_the_health_endpoint(self) -> None:
        text = self.DOCKERFILE.read_text()
        assert "HEALTHCHECK" in text, "the sidecar image must declare a HEALTHCHECK or /health reaches nobody -- phaze-p3hj.2"
        healthcheck = text[text.index("HEALTHCHECK") :]
        assert "/health" in healthcheck
        assert "8001" in healthcheck
