"""Regression tests for the panako sidecar (phaze-vp07).

The 2026.7.7 panako image shipped with NO ``/app/panako.jar``: the runtime stage copied
``build/libs/Panako-*-all.jar`` (capital P) while upstream's ``settings.gradle`` declares
``rootProject.name = 'panako'`` (lowercase), so shadowJar actually emits
``panako-2.1-all.jar``. BuildKit resolves a zero-match COPY glob to an EMPTY layer instead
of failing, so the image built green and every ``POST /ingest`` 500'd in production.

Two observability failures turned that into a 40-minute silent outage:
  * ``/health`` returned a hardcoded ``{"status": "healthy"}`` without touching the jar,
    so every healthcheck and dashboard reported a healthy engine.
  * Non-2xx ingest failures were never logged server-side -- the subprocess stderr went
    to the caller and nowhere else, leaving zero tracebacks in ``docker logs``.

The fixture lines below are NOT guessed -- they were captured from a real
``java -jar /app/panako.jar query`` run inside the rebuilt image (Panako 2.1, Olaf
strategy), including the ``null``-sentinel "no match" row.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient


if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    import pytest


# ---------------------------------------------------------------------------
# Real captured Panako CLI output (Panako 2.1 / Olaf strategy)
# ---------------------------------------------------------------------------

HEADER = (
    "Index; Total ; Query path;Query start (s);Query stop (s); Match path;Match id; "
    "Match start (s); Match stop (s); Match score; Time factor (%); Frequency factor(%); Seconds with match (%)"
)

# A genuine self-match, captured verbatim from the rebuilt image.
REAL_MATCH_ROW = "1 ; 1 ; /audio/smoke.wav ; 1.376 ; 24.432 ; /audio/smoke.wav ; 19515506 ; 1.376 ; 24.432 ; 36 ; 1.000 % ; 1.000 %; 0.75"

# Panako's "no match" SENTINEL row -- it emits this instead of emitting nothing.
NO_MATCH_ROW = "1 ; 1 ; /audio/reference.wav ; 0.000 ; 0.000 ; null ; null ; -1.000 ; -1.000 ; -1 ; -1.000 % ; -1.000 %; 0.00"


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, app_module: ModuleType, attr: str, *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> None:
    """Stub one of the module's subprocess wrappers with a fixed CompletedProcess."""

    def _fake(_file_path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(app_module, attr, _fake)


async def _post(app_module: ModuleType, route: str) -> tuple[int, dict]:
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://panako") as client:
        resp = await client.post(route, json={"file_path": "/audio/smoke.wav"})
    return resp.status_code, resp.json()


async def _get_health(app_module: ModuleType) -> tuple[int, dict]:
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://panako") as client:
        resp = await client.get("/health")
    return resp.status_code, resp.json()


class TestMatchParsing:
    """``_parse_matches`` must distinguish a real hit from Panako's null sentinel."""

    def test_real_match_parses(self, panako_app: ModuleType) -> None:
        matches = panako_app._parse_matches(f"{HEADER}\n{REAL_MATCH_ROW}\n")
        assert len(matches) == 1
        assert matches[0].track_id == "/audio/smoke.wav"

    def test_null_sentinel_is_not_a_match(self, panako_app: ModuleType) -> None:
        """The no-match sentinel must NOT become a phantom {track_id: "null"} result.

        Before the fix this returned one match with track_id "null" and confidence 0.0,
        injecting a bogus duplicate into the downstream dedup pipeline.
        """
        assert panako_app._parse_matches(f"{HEADER}\n{NO_MATCH_ROW}\n") == []

    def test_header_only_is_empty(self, panako_app: ModuleType) -> None:
        assert panako_app._parse_matches(f"{HEADER}\n") == []

    def test_sentinel_mixed_with_real_match_keeps_only_real(self, panako_app: ModuleType) -> None:
        matches = panako_app._parse_matches(f"{HEADER}\n{NO_MATCH_ROW}\n{REAL_MATCH_ROW}\n")
        assert [m.track_id for m in matches] == ["/audio/smoke.wav"]

    def test_blank_and_semicolonless_lines_skipped(self, panako_app: ModuleType) -> None:
        assert panako_app._parse_matches("\n   \nMatches 19515506 (id) Filtered hits: 36\n") == []

    def test_short_row_skipped(self, panako_app: ModuleType) -> None:
        assert panako_app._parse_matches("1 ; 1 ; /audio/a.wav ; 0.0\n") == []

    def test_negative_score_row_skipped(self, panako_app: ModuleType) -> None:
        """A negative match score is Panako's other 'nothing found' signal."""
        row = "1 ; 1 ; /audio/a.wav ; 0.0 ; 0.0 ; /audio/b.wav ; 123 ; 0.0 ; 0.0 ; -1 ; 1.0 % ; 1.0 %; 0.00"
        assert panako_app._parse_matches(row) == []

    def test_unparseable_numeric_field_is_logged_and_skipped(self, panako_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        row = "1 ; 1 ; /audio/a.wav ; 0.0 ; 0.0 ; /audio/b.wav ; 123 ; 0.0 ; 0.0 ; NOTANUMBER ; 1.0 % ; 1.0 %; 0.00"
        with caplog.at_level("WARNING"):
            assert panako_app._parse_matches(row) == []
        assert "Failed to parse match line" in caplog.text

    def test_probe_flags_unrecognized_cli_output(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        jar = tmp_path / "panako.jar"
        jar.write_bytes(b"x")
        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(jar))
        monkeypatch.setattr(
            panako_app.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )
        assert "unrecognized output" in (panako_app._probe_jar() or "")


class TestSemicolonPathParsing:
    """File paths containing ';' must not shift fields (phaze-9pmn).

    Panako embeds the raw query/match paths verbatim in its ';'-separated record with no
    quoting or escaping. The old blind positional ``split(';')`` turned a matched file
    like "Sven; Vath - Cocoon.mp3" into a phantom match with a truncated track_id and a
    confidence read from the wrong column -- or silently dropped the row entirely.
    """

    def test_match_path_with_semicolon_survives_intact(self, panako_app: ModuleType) -> None:
        row = "1 ; 1 ; /audio/query.wav ; 1.376 ; 24.432 ; /data/music/Sven; Vath - Cocoon.mp3 ; 19515506 ; 1.376 ; 24.432 ; 36 ; 1.000 % ; 1.000 %; 0.75"
        matches = panako_app._parse_matches(row)
        assert len(matches) == 1
        assert matches[0].track_id == "/data/music/Sven; Vath - Cocoon.mp3"
        assert matches[0].confidence == 0.75

    def test_query_path_with_semicolon_does_not_shift_match_fields(self, panako_app: ModuleType) -> None:
        row = "1 ; 1 ; /audio/Artist; Live at Coachella.mp3 ; 1.376 ; 24.432 ; /audio/ref.wav ; 19515506 ; 1.376 ; 24.432 ; 36 ; 1.000 % ; 1.000 %; 0.75"
        matches = panako_app._parse_matches(row)
        assert len(matches) == 1
        assert matches[0].track_id == "/audio/ref.wav"
        assert matches[0].confidence == 0.75

    def test_semicolons_in_both_paths(self, panako_app: ModuleType) -> None:
        row = (
            "1 ; 1 ; /audio/Artist; Live at Coachella.mp3 ; 1.376 ; 24.432 ; "
            "/data/music/Sven; Vath - Cocoon.mp3 ; 19515506 ; 1.376 ; 24.432 ; 36 ; 1.000 % ; 1.000 %; 0.75"
        )
        matches = panako_app._parse_matches(row)
        assert [m.track_id for m in matches] == ["/data/music/Sven; Vath - Cocoon.mp3"]

    def test_multiple_semicolons_in_match_path(self, panako_app: ModuleType) -> None:
        row = "1 ; 1 ; /audio/q.wav ; 0.0 ; 10.0 ; /m/a; b; c.mp3 ; 42 ; 0.0 ; 10.0 ; 36 ; 1.000 % ; 1.000 %; 0.50"
        matches = panako_app._parse_matches(row)
        assert [m.track_id for m in matches] == ["/m/a; b; c.mp3"]

    def test_semicolon_path_with_negative_score_still_skipped(self, panako_app: ModuleType) -> None:
        row = "1 ; 1 ; /audio/q.wav ; 0.0 ; 10.0 ; /m/a; b.mp3 ; 42 ; 0.0 ; 10.0 ; -1 ; 1.000 % ; 1.000 %; 0.00"
        assert panako_app._parse_matches(row) == []

    def test_structurally_unrecoverable_row_is_logged_and_skipped(self, panako_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        # No adjacent numeric pair bracketing the paths -> the record structure is gone;
        # warn and skip rather than fabricate a match from arbitrary fragments.
        row = "1 ; 1 ; /audio/q.wav ; notafloat ; alsonot ; /m/a.mp3 ; 42 ; 0.0 ; 10.0 ; 36 ; 1.000 % ; 1.000 %; 0.75"
        with caplog.at_level("WARNING"):
            assert panako_app._parse_matches(row) == []
        assert "Failed to parse match line" in caplog.text


class TestSubprocessWrappers:
    """The wrappers must invoke the CLI with the lmdb module flag and the right verb."""

    def test_run_ingest_builds_store_command(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        panako_app._run_ingest("/audio/x.wav")
        assert seen["cmd"] == [*panako_app.JAVA_BASE_CMD, "store", "/audio/x.wav"]

    def test_run_query_builds_query_command(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        panako_app._run_query("/audio/x.wav")
        assert seen["cmd"] == [*panako_app.JAVA_BASE_CMD, "query", "/audio/x.wav"]

    async def test_query_success_returns_parsed_matches(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, panako_app, "_run_query", stdout=f"{HEADER}\n{REAL_MATCH_ROW}\n")
        status, body = await _post(panako_app, "/query")
        assert status == 200
        assert body["matches"][0]["track_id"] == "/audio/smoke.wav"


class TestHealthExercisesTheJar:
    """/health must observe the engine, not assert wellness unconditionally."""

    async def test_healthy_when_jar_runs(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(panako_app, "_probe_jar", lambda: None)
        status, body = await _get_health(panako_app)
        assert status == 200
        assert body["status"] == "healthy"

    async def test_unhealthy_when_jar_missing(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact production scenario: no jar in the image -> must NOT report healthy."""
        monkeypatch.setattr(panako_app, "_probe_jar", lambda: "Panako jar missing at /app/panako.jar")
        status, body = await _get_health(panako_app)
        assert status == 503
        assert body["status"] == "unhealthy"
        assert "missing" in body["detail"]

    def test_probe_reports_missing_jar(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(panako_app, "PANAKO_JAR", "/nonexistent/panako.jar")
        assert "missing" in (panako_app._probe_jar() or "")

    def test_probe_reports_empty_jar(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        empty = tmp_path / "panako.jar"
        empty.write_bytes(b"")
        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(empty))
        assert "empty" in (panako_app._probe_jar() or "")

    def test_probe_detects_unreadable_jarfile(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A present-but-unrunnable jar (the JVM loader error) is not healthy."""
        jar = tmp_path / "panako.jar"
        jar.write_bytes(b"not actually a jar")
        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(jar))
        monkeypatch.setattr(
            panako_app.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Error: Unable to access jarfile /app/panako.jar"),
        )
        assert "unreadable or corrupt" in (panako_app._probe_jar() or "")

    def test_probe_detects_missing_java(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        jar = tmp_path / "panako.jar"
        jar.write_bytes(b"x")

        def _boom(*_a: object, **_k: object) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(jar))
        monkeypatch.setattr(panako_app.subprocess, "run", _boom)
        assert "java runtime not found" in (panako_app._probe_jar() or "")

    def test_probe_detects_timeout(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        jar = tmp_path / "panako.jar"
        jar.write_bytes(b"x")

        def _slow(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="java", timeout=30)

        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(jar))
        monkeypatch.setattr(panako_app.subprocess, "run", _slow)
        assert "did not respond" in (panako_app._probe_jar() or "")

    def test_probe_healthy_on_real_panako_output(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        jar = tmp_path / "panako.jar"
        jar.write_bytes(b"x")
        monkeypatch.setattr(panako_app, "PANAKO_JAR", str(jar))
        monkeypatch.setattr(
            panako_app.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Panako - Acoustic Fingerprinting\nusage: panako store", stderr=""
            ),
        )
        assert panako_app._probe_jar() is None


class TestFailuresAreLoggedServerSide:
    """40 minutes of 500s left ZERO tracebacks in docker logs. Never again."""

    async def test_ingest_failure_logs_stderr(
        self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_run(monkeypatch, panako_app, "_run_ingest", stderr="Error: Unable to access jarfile /app/panako.jar", returncode=1)
        with caplog.at_level("ERROR"):
            status, _ = await _post(panako_app, "/ingest")
        assert status == 500
        assert "Unable to access jarfile" in caplog.text
        assert "ingest FAILED" in caplog.text

    async def test_query_failure_logs_stderr(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        _patch_run(monkeypatch, panako_app, "_run_query", stderr="boom", returncode=1)
        with caplog.at_level("ERROR"):
            status, _ = await _post(panako_app, "/query")
        assert status == 500
        assert "query FAILED" in caplog.text

    async def test_successful_ingest_returns_200(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, panako_app, "_run_ingest", stdout="1; 1; smoke.wav; 00:00:25; 433.00 ms; 56.70")
        status, body = await _post(panako_app, "/ingest")
        assert status == 200
        assert body["status"] == "ingested"

    def test_log_helper_handles_absent_stderr(self, panako_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="")
        with caplog.at_level("ERROR"):
            panako_app._log_subprocess_failure("ingest", "/audio/x.wav", result)
        assert "<no stderr>" in caplog.text


class TestLmdbModuleFlag:
    """lmdbjava needs java.nio opened or EVERY store/query dies on JDK 16+."""

    def test_add_opens_flag_is_present(self, panako_app: ModuleType) -> None:
        assert "--add-opens=java.base/java.nio=ALL-UNNAMED" in panako_app.JAVA_BASE_CMD
