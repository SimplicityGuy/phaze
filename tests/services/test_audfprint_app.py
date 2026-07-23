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


def _patch_query(monkeypatch: pytest.MonkeyPatch, app_module: ModuleType, stdout: str, returncode: int = 0) -> None:
    """Force FPRINT_DB to 'exist' and stub the subprocess call with fixed stdout."""

    class _AlwaysExists:
        def exists(self) -> bool:
            return True

    monkeypatch.setattr(app_module, "Path", lambda *_a, **_k: _AlwaysExists())

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

    async def test_known_match_returns_non_empty(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = default_line("/data/query/song.wav", 8.4, 1234, "/data/ref/track01.mp3", 12.3, 456, 789, 0)
        _patch_query(monkeypatch, audfprint_app, stdout)
        status, body = await _post_query(audfprint_app)
        assert status == 200
        assert body["matches"]
        assert body["matches"][0]["track_id"] == "/data/ref/track01.mp3"

    async def test_total_parse_failure_returns_502(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        # Candidate report lines present, but NONE parse -> must not silently 200 with [].
        stdout = "Matched utterly broken output with common hashes\n"
        _patch_query(monkeypatch, audfprint_app, stdout)
        status, body = await _post_query(audfprint_app)
        assert status == 502
        assert "unparseable" in body["detail"]

    async def test_genuine_no_match_returns_200_empty(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_query(monkeypatch, audfprint_app, "NOMATCH /data/query/song.wav 8.4 sec 1234 raw hashes\n")
        status, body = await _post_query(audfprint_app)
        assert status == 200
        assert body["matches"] == []

    async def test_partial_parse_failure_returns_good_matches(self, audfprint_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        good = default_line("/data/query/song.wav", 8.4, 1234, "/data/ref/track01.mp3", 12.3, 456, 789, 0)
        stdout = good + "\nMatched broken tail with common hashes\n"
        _patch_query(monkeypatch, audfprint_app, stdout)
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
        db_path.touch()
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
        db_path.touch()
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

    Records every invocation and -- like the real CLI -- creates/updates the dbase file
    on both ``new`` and ``add``. This lets tests assert which command was chosen (the
    actual bug/fix distinction) purely from the recorded call, without shelling out to a
    real audfprint install.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        command, dbase = args[2], args[4]
        if command in ("new", "add"):
            Path(dbase).touch()
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
        command, dbase, file_arg = cli.calls[0][2], cli.calls[0][4], cli.calls[0][5]
        assert command == "new"
        assert dbase == str(db_path)
        assert file_arg == "/data/real/song.mp3"

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
        assert any("audfprint ingest failed" in record.message and "boom: disk full" in record.message for record in caplog.records)


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
        db_path.touch()
        monkeypatch.setattr(audfprint_app, "FPRINT_DB", str(db_path))
        monkeypatch.setattr(audfprint_app, "SUBPROCESS_TIMEOUT", 1234)
        seen: list[object] = []

        def _capture(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs.get("timeout"))
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
        db_path.touch()
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
        db_path.touch()
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
        db_path.touch()
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
