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

import subprocess
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
import pytest


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
