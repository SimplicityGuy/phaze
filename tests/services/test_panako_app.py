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
import pytest

from tests.services.conftest import load_service_module


if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


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


async def _post(app_module: ModuleType, route: str, file_path: str = "/audio/smoke.wav") -> tuple[int, dict]:
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://panako") as client:
        resp = await client.post(route, json={"file_path": file_path})
    return resp.status_code, resp.json()


@pytest.fixture
def panako_media(panako_app: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Confine the loaded module's MEDIA_ROOTS/STAGING_DIR/INGEST_IDENTITY_DIR to an isolated tree.

    phaze-64w1: the query path (``_staged_query_operand``, ephemeral) and the ingest path
    (``_ingest_identity_path``, persistent) each validate + confine ``file_path`` under one of
    ``MEDIA_ROOTS`` and stage it under their own directory before it ever reaches the CLI, so
    any test that exercises the HTTP layer (rather than calling ``_run_ingest``/``_run_query``
    directly) needs a real, MEDIA_ROOTS-confined location to point requests at, plus isolated
    staging directories so tests never share state. Returns the (single-entry) media root
    directory; callers create files under it.
    """
    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setattr(panako_app, "MEDIA_ROOTS", [media_root])
    monkeypatch.setattr(panako_app, "STAGING_DIR", tmp_path / "stage")
    monkeypatch.setattr(panako_app, "INGEST_IDENTITY_DIR", tmp_path / "identities")
    return media_root


@pytest.fixture
def smoke_file(panako_media: Path) -> Path:
    """A real file confined under ``panako_media``, standing in for the old literal path."""
    smoke = panako_media / "smoke.wav"
    smoke.write_bytes(b"RIFF")
    return smoke


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

    def test_real_match_yields_timestamp_from_match_start(self, panako_app: ModuleType) -> None:
        """phaze-nldg: "Match start (s)" (field 7 / tail[1]) is the reference-track offset --
        exactly the track-start timestamp a tracklist wants -- so it must be carried through.
        """
        matches = panako_app._parse_matches(f"{HEADER}\n{REAL_MATCH_ROW}\n")
        assert matches[0].timestamp == "1.4"

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

    def test_unparseable_match_start_field_is_logged_and_skipped(self, panako_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        """phaze-nldg: "Match start (s)" (tail[1]) is now load-bearing for the timestamp --
        an unparseable value there must skip the row the same way an unparseable score does.
        """
        row = "1 ; 1 ; /audio/a.wav ; 0.0 ; 0.0 ; /audio/b.wav ; 123 ; NOTANUMBER ; 0.0 ; 36 ; 1.0 % ; 1.0 %; 0.00"
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

    async def test_query_success_returns_parsed_matches(self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, panako_app, "_run_query", stdout=f"{HEADER}\n{REAL_MATCH_ROW}\n")
        status, body = await _post(panako_app, "/query", file_path=str(smoke_file))
        assert status == 200
        assert body["matches"][0]["track_id"] == "/audio/smoke.wav"


class TestPathValidationAndStaging:
    """phaze-64w1: file_path must never reach the Panako CLI verbatim.

    Panako's own decode path (TarsosDSP's PipeDecoder, reached from every store/query)
    substitutes the operand we hand the CLI into a `/bin/bash -c "... -i \\"%resource%\\" ..."`
    template with only bare double-quoting -- a `"`, a backtick, or `$(...)` in the path
    breaks out of that quoting into arbitrary shell execution. `_ingest_identity_path`
    (ingest) and `_staged_query_operand` (query) both confine the caller's path under one of
    MEDIA_ROOTS and stage the resolved file under a generated safe name, so the CLI never
    sees a caller-controlled byte -- whether from an attacker or from a legitimate messy
    filename like the ones exercised in TestSemicolonPathParsing above.
    """

    async def test_shell_metacharacter_filename_is_staged_safely(
        self, panako_app: ModuleType, panako_media: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `$(...)`-shaped filename must reach the CLI as a safe staged name, never verbatim."""
        evil = panako_media / "$(touch pwned).wav"
        evil.write_bytes(b"RIFF")
        seen: dict[str, object] = {}

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        status, body = await _post(panako_app, "/ingest", file_path=str(evil))
        assert status == 200
        operand = seen["cmd"][-1]
        assert "$(" not in operand
        assert "touch" not in operand
        assert operand.startswith(str(panako_app.INGEST_IDENTITY_DIR))
        # The response still echoes the caller's ORIGINAL path -- never the internal staged name.
        assert body["file_path"] == str(evil)

    async def test_double_quote_filename_is_staged_safely(self, panako_app: ModuleType, panako_media: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A literal `"` used to close Panako's `-i "%resource%"` quoting early; it must never reach it."""
        evil = panako_media / 'Artist - "Live" Version.wav'
        evil.write_bytes(b"RIFF")
        seen: dict[str, object] = {}

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        status, _ = await _post(panako_app, "/query", file_path=str(evil))
        assert status == 200
        operand = seen["cmd"][-1]
        assert '"' not in operand

    async def test_staged_operand_is_a_real_readable_handle_on_the_file(
        self, panako_app: ModuleType, panako_media: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The staged operand must actually decode to the caller's file, not just have a safe name."""
        evil = panako_media / "$(id).wav"
        evil.write_bytes(b"payload-bytes")

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            operand = panako_app.Path(cmd[-1])
            assert operand.is_symlink()
            assert operand.resolve() == evil.resolve()
            assert operand.read_bytes() == b"payload-bytes"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        status, _ = await _post(panako_app, "/ingest", file_path=str(evil))
        assert status == 200

    async def test_ingest_identity_symlink_persists_after_the_request(
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """phaze-64w1 third bounce: unlike the query-side operand, an ingest identity must NOT
        be cleaned up -- Panako's `store` persists it as the fingerprint's permanent identity,
        so deleting it after the request orphans every match against it forever.
        """
        _patch_run(monkeypatch, panako_app, "_run_ingest")
        status, _ = await _post(panako_app, "/ingest", file_path=str(smoke_file))
        assert status == 200
        staged = list(panako_app.INGEST_IDENTITY_DIR.iterdir())
        assert len(staged) == 1
        assert staged[0].is_symlink()
        assert staged[0].resolve() == smoke_file.resolve()

    async def test_query_staged_symlink_is_cleaned_up_after_the_request(
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The query-side operand IS ephemeral -- Panako never persists it anywhere a later
        request could look it up, so it's safe (and correct) to delete once the request ends.
        """
        _patch_run(monkeypatch, panako_app, "_run_query")
        status, _ = await _post(panako_app, "/query", file_path=str(smoke_file))
        assert status == 200
        assert list(panako_app.STAGING_DIR.iterdir()) == []

    async def test_relative_path_rejected(self, panako_app: ModuleType) -> None:
        status, body = await _post(panako_app, "/ingest", file_path="relative/path.wav")
        assert status == 400
        assert "absolute" in body["detail"]

    async def test_nul_byte_in_path_rejected(self, panako_app: ModuleType, panako_media: Path) -> None:
        status, body = await _post(panako_app, "/ingest", file_path=str(panako_media / "evil\x00.wav"))
        assert status == 400
        assert "NUL" in body["detail"]

    async def test_path_traversal_outside_media_root_rejected(self, panako_app: ModuleType, panako_media: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.wav"
        outside.write_bytes(b"RIFF")
        traversal = str(panako_media / ".." / "outside.wav")
        status, body = await _post(panako_app, "/ingest", file_path=traversal)
        assert status == 400
        assert "must resolve under" in body["detail"]

    async def test_symlink_escape_outside_media_root_rejected(self, panako_app: ModuleType, panako_media: Path, tmp_path: Path) -> None:
        """A symlink INSIDE media root pointing OUTSIDE it must not launder a path through confinement."""
        outside = tmp_path / "outside.wav"
        outside.write_bytes(b"RIFF")
        escape = panako_media / "escape.wav"
        escape.symlink_to(outside)
        status, body = await _post(panako_app, "/ingest", file_path=str(escape))
        assert status == 400
        assert "must resolve under" in body["detail"]

    async def test_rejected_path_never_reaches_the_cli(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        def _fail_if_called(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            nonlocal called
            called = True
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _fail_if_called)
        status, _ = await _post(panako_app, "/ingest", file_path="not/absolute.wav")
        assert status == 400
        assert called is False


class TestMultiRootConfinement:
    """phaze-64w1 changes-requested: a single MEDIA_ROOT can't cover a deployment with more
    than one media mount (mirrors ``PHAZE_AGENT_SCAN_ROOTS``, which is itself comma-separated
    for exactly this reason). ``MEDIA_ROOTS`` must accept several roots, confining to
    "resolves under ANY configured root" -- and, critically, an unset/empty config must fail
    CLOSED (refuse every path) rather than falling back to permitting anything.
    """

    async def test_file_under_either_configured_root_is_accepted(
        self, panako_app: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root_a = tmp_path / "roots" / "a"
        root_b = tmp_path / "roots" / "b"
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)
        monkeypatch.setattr(panako_app, "MEDIA_ROOTS", [root_a, root_b])
        monkeypatch.setattr(panako_app, "STAGING_DIR", tmp_path / "stage")
        monkeypatch.setattr(panako_app, "INGEST_IDENTITY_DIR", tmp_path / "identities")
        _patch_run(monkeypatch, panako_app, "_run_ingest")

        file_in_a = root_a / "x.wav"
        file_in_a.write_bytes(b"RIFF")
        status, _ = await _post(panako_app, "/ingest", file_path=str(file_in_a))
        assert status == 200

        file_in_b = root_b / "y.wav"
        file_in_b.write_bytes(b"RIFF")
        status, _ = await _post(panako_app, "/ingest", file_path=str(file_in_b))
        assert status == 200

    async def test_file_outside_every_configured_root_is_rejected(
        self, panako_app: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root_a = tmp_path / "roots" / "a"
        root_b = tmp_path / "roots" / "b"
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)
        monkeypatch.setattr(panako_app, "MEDIA_ROOTS", [root_a, root_b])
        monkeypatch.setattr(panako_app, "STAGING_DIR", tmp_path / "stage")

        outside = tmp_path / "elsewhere" / "z.wav"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"RIFF")
        status, body = await _post(panako_app, "/ingest", file_path=str(outside))
        assert status == 400
        assert "must resolve under one of" in body["detail"]
        assert str(root_a) in body["detail"]
        assert str(root_b) in body["detail"]

    async def test_unset_media_roots_rejects_every_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An unconfigured sidecar (no PANAKO_MEDIA_ROOTS) must refuse everything -- never
        fall back to a guessed/plausible default that may not match the real mount(s).
        """
        monkeypatch.delenv("PANAKO_MEDIA_ROOTS", raising=False)
        mod = load_service_module("panako", "phaze_test_panako_media_roots_unset")
        assert mod.MEDIA_ROOTS == []

        media = tmp_path / "media.wav"
        media.write_bytes(b"RIFF")
        status, body = await _post(mod, "/ingest", file_path=str(media))
        assert status == 400
        assert "no PANAKO_MEDIA_ROOTS configured" in body["detail"]
        assert "fail closed" in body["detail"]

    async def test_empty_media_roots_env_rejects_every_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An explicitly empty/whitespace value must behave identically to unset -- fail closed."""
        monkeypatch.setenv("PANAKO_MEDIA_ROOTS", "  , ,")
        mod = load_service_module("panako", "phaze_test_panako_media_roots_blank")
        assert mod.MEDIA_ROOTS == []

        media = tmp_path / "media.wav"
        media.write_bytes(b"RIFF")
        status, _ = await _post(mod, "/ingest", file_path=str(media))
        assert status == 400

    def test_media_roots_env_is_comma_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mirrors ``AgentSettings._split_scan_roots`` -- comma-separated, entries stripped."""
        monkeypatch.setenv("PANAKO_MEDIA_ROOTS", "/data/downloads , /data/staging ,")
        mod = load_service_module("panako", "phaze_test_panako_media_roots_split")
        expected = [mod.Path("/data/downloads"), mod.Path("/data/staging")]
        assert expected == mod.MEDIA_ROOTS


class TestIngestQueryIdentityRoundTrip:
    """phaze-64w1 third bounce: a stored fingerprint's identity must survive past the request
    that created it and resolve back to the REAL archive path on query -- not an ephemeral
    per-request staging name already deleted by the time a later query needs to look it up.

    The uuid4-per-request staging scheme (second bounce's fix) broke exactly this: Panako's
    `store` persists the operand it was given verbatim, and echoes it back as a `query`
    match's "match path" -- but that operand had already been unlinked in the ingest
    request's `finally`, so every match resolved to a temp name nothing on disk still
    answers to. Caught by the docker-validate smoke self-match assertion, not by any
    app-level test -- these exist so it can't regress silently outside CI again.
    """

    async def test_query_after_ingest_returns_the_real_path_as_track_id(
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored_operand: dict[str, str] = {}

        def _fake_cli(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            verb, operand = cmd[-2], cmd[-1]
            if verb == "store":
                stored_operand["path"] = operand
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            # verb == "query": Panako echoes back EXACTLY the operand it was given at store
            # time as the match record's "match path" field -- the real behavior this test
            # pins down, captured via the same fixed-arity shape as REAL_MATCH_ROW above.
            row = f"1 ; 1 ; {operand} ; 1.376 ; 24.432 ; {stored_operand['path']} ; 19515506 ; 1.376 ; 24.432 ; 36 ; 1.000 % ; 1.000 %; 0.75"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{HEADER}\n{row}\n", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _fake_cli)

        ingest_status, _ = await _post(panako_app, "/ingest", file_path=str(smoke_file))
        assert ingest_status == 200
        # The identity handed to the CLI must NOT be the caller-controlled path itself (the
        # injection defense) -- but it must have been recorded regardless.
        assert stored_operand["path"] != str(smoke_file)
        assert stored_operand["path"].startswith(str(panako_app.INGEST_IDENTITY_DIR))

        query_status, query_body = await _post(panako_app, "/query", file_path=str(smoke_file))
        assert query_status == 200
        assert len(query_body["matches"]) == 1
        # The critical assertion: the app must resolve the staged identity BACK to the real
        # archive path before it ever reaches a caller -- an unresolved staging name here
        # means every stored fingerprint is orphaned.
        assert query_body["matches"][0]["track_id"] == str(smoke_file)

    def test_reingesting_the_same_path_reuses_the_same_identity(self, panako_app: ModuleType, smoke_file: Path) -> None:
        """Re-ingesting the same archive location must not fragment its identity across
        multiple staged names -- each ingest of the SAME resolved path must stage to the
        SAME identity symlink (deterministic per phaze-64w1 third bounce).
        """
        first = panako_app._ingest_identity_path(str(smoke_file))
        second = panako_app._ingest_identity_path(str(smoke_file))
        assert first == second
        assert first.is_symlink()
        assert first.resolve() == smoke_file.resolve()

    def test_destage_of_an_unrecognized_path_is_a_no_op(self, panako_app: ModuleType) -> None:
        """A path that isn't one of our identity symlinks (legacy data, or a bug elsewhere)
        must degrade to "return it unchanged", never raise into a 500.
        """
        assert panako_app._destage_track_id("/some/other/path.wav") == "/some/other/path.wav"
        assert panako_app._destage_track_id("null") == "null"


class TestHealthObservesTheStoreDirectory:
    """phaze-25cc: the LMDB store under HOME is a core dependency too, and /health was blind to it.

    ``_probe_jar`` only ever looked at ``/app/panako.jar``. HOME (pinned to ``/data/fprint``,
    the ``panako_data`` named volume) is where lmdbjava creates ``~/.panako/dbs``; if it is not
    writable, EVERY store and query exits nonzero and the sidecar 500s on every request -- while
    /health returned 200 throughout. That is the identical shape as the 2026.7.7 hardcoded-healthy
    outage this endpoint was rewritten to prevent, one dependency over.

    Docker seeds a named volume's ownership from the image only when the volume is EMPTY at first
    mount, so the image-layer `chown panako:panako /data/fprint` cannot reach a volume created by
    a pre-uid-pin image. A read-only remount or a full filesystem produces the same symptom, which
    is why the probe attempts the real operation rather than reading permission bits.
    """

    async def test_unwritable_store_home_reports_unhealthy(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "fprint"
        home.mkdir()
        home.chmod(0o500)  # r-x: the uid-999-owned-volume symptom, reproduced by mode
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(panako_app, "_probe_jar", lambda: None)
        try:
            status, body = await _get_health(panako_app)
        finally:
            home.chmod(0o700)

        assert status == 503
        assert body["status"] == "unhealthy"
        assert "not writable" in body["detail"]

    async def test_missing_store_home_reports_unhealthy(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "never-mounted"))
        monkeypatch.setattr(panako_app, "_probe_jar", lambda: None)
        status, body = await _get_health(panako_app)
        assert status == 503
        assert "store directory missing" in body["detail"]

    async def test_writable_store_home_is_healthy_and_leaves_nothing_behind(
        self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "fprint"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(panako_app, "_probe_jar", lambda: None)
        status, body = await _get_health(panako_app)
        assert status == 200
        assert body["status"] == "healthy"
        assert list(home.iterdir()) == [], "the writability probe must not leave an artifact in the store"

    def test_probe_is_read_only_with_respect_to_the_real_store(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A pre-existing .panako store must survive the probe untouched."""
        home = tmp_path / "fprint"
        (home / ".panako" / "dbs").mkdir(parents=True)
        (home / ".panako" / "dbs" / "data.mdb").write_bytes(b"lmdb")
        monkeypatch.setenv("HOME", str(home))

        assert panako_app._probe_store_home() is None
        assert (home / ".panako" / "dbs" / "data.mdb").read_bytes() == b"lmdb"


class TestHealthExercisesTheJar:
    """/health must observe the engine, not assert wellness unconditionally."""

    @pytest.fixture(autouse=True)
    def _writable_store_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Isolate HOME so these jar-focused tests never depend on the runner's own home dir."""
        home = tmp_path / "fprint"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

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
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_run(monkeypatch, panako_app, "_run_ingest", stderr="Error: Unable to access jarfile /app/panako.jar", returncode=1)
        with caplog.at_level("ERROR"):
            status, _ = await _post(panako_app, "/ingest", file_path=str(smoke_file))
        assert status == 500
        assert "Unable to access jarfile" in caplog.text
        assert "ingest FAILED" in caplog.text

    async def test_query_failure_logs_stderr(
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_run(monkeypatch, panako_app, "_run_query", stderr="boom", returncode=1)
        with caplog.at_level("ERROR"):
            status, _ = await _post(panako_app, "/query", file_path=str(smoke_file))
        assert status == 500
        assert "query FAILED" in caplog.text

    async def test_successful_ingest_returns_200(self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, panako_app, "_run_ingest", stdout="1; 1; smoke.wav; 00:00:25; 433.00 ms; 56.70")
        status, body = await _post(panako_app, "/ingest", file_path=str(smoke_file))
        assert status == 200
        assert body["status"] == "ingested"

    def test_log_helper_handles_absent_stderr(self, panako_app: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="")
        with caplog.at_level("ERROR"):
            panako_app._log_subprocess_failure("ingest", "/audio/x.wav", result)
        assert "<no stderr>" in caplog.text


class TestSubprocessTimeout:
    """Duration-appropriate, env-wired timeout with structured TimeoutExpired handling (phaze-mv1f).

    SUBPROCESS_TIMEOUT was a hardcoded 120 (the README documented an env var that was
    never wired), so every multi-hour concert set -- the archive's primary content --
    deterministically timed out, and subprocess.TimeoutExpired propagated uncaught as a
    raw 500 traceback.
    """

    def test_default_timeout_is_sized_for_long_sets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUBPROCESS_TIMEOUT", raising=False)
        mod = load_service_module("panako", "phaze_test_panako_timeout_default")
        assert mod.SUBPROCESS_TIMEOUT == 3600

    def test_timeout_env_override_is_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUBPROCESS_TIMEOUT", "7000")
        mod = load_service_module("panako", "phaze_test_panako_timeout_override")
        assert mod.SUBPROCESS_TIMEOUT == 7000

    def test_run_commands_pass_configured_timeout(self, panako_app: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(panako_app, "SUBPROCESS_TIMEOUT", 1234)
        seen: list[object] = []

        def _capture(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(panako_app.subprocess, "run", _capture)
        panako_app._run_ingest("/audio/x.wav")
        panako_app._run_query("/audio/x.wav")
        assert seen == [1234, 1234]

    @staticmethod
    def _raise_timeout(_file_path: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="java", timeout=1234)

    async def test_ingest_timeout_returns_structured_504(
        self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(panako_app, "_run_ingest", self._raise_timeout)
        with caplog.at_level("ERROR"):
            status, body = await _post(panako_app, "/ingest", file_path=str(smoke_file))
        assert status == 504
        assert "timed out after" in body["detail"]
        assert str(smoke_file) in body["detail"]
        assert "timed out" in caplog.text

    async def test_query_timeout_returns_structured_504(self, panako_app: ModuleType, smoke_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(panako_app, "_run_query", self._raise_timeout)
        status, body = await _post(panako_app, "/query", file_path=str(smoke_file))
        assert status == 504
        assert "timed out after" in body["detail"]


class TestLmdbModuleFlag:
    """lmdbjava needs java.nio opened or EVERY store/query dies on JDK 16+."""

    def test_add_opens_flag_is_present(self, panako_app: ModuleType) -> None:
        assert "--add-opens=java.base/java.nio=ALL-UNNAMED" in panako_app.JAVA_BASE_CMD
