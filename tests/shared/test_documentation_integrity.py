"""Focused tests for the maintained-document integrity gate."""

import json
from pathlib import Path
import shutil

import httpx
import pytest

from scripts import check_documentation_integrity as integrity, check_historical_mermaid as historical


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_current_maintained_documentation_passes_deterministic_checks() -> None:
    report = integrity.check_documentation(REPO_ROOT)

    assert report["errors"] == []
    assert report["migration_heads"] == ["094"]
    assert report["generator_markers"] == 4
    assert report["diagrams"] > 0
    assert report["diagram_relationships"] > 0
    assert report["local_links_checked"] >= 298
    assert report["external_targets"]
    assert report["external_errors"] == []


def test_percent_decoded_anchor_and_duplicate_heading_slugs(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text('# Café & Tea\n# Repeat\n# Repeat\n<a id="manual-anchor"></a>\n', encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text("[encoded](target.md#caf%C3%A9--tea)\n", encoding="utf-8")

    resolution = integrity.resolve_link(tmp_path, source, "target.md#caf%C3%A9--tea")

    assert resolution.fragment == "café--tea"
    assert resolution.path == target
    assert integrity.document_anchors(target.read_text(encoding="utf-8")) == {
        "café--tea",
        "repeat",
        "repeat-1",
        "manual-anchor",
    }


def test_missing_anchor_and_repository_escape_are_local_failures(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Present\n", encoding="utf-8")
    (docs / "source.md").write_text("[missing](target.md#absent)\n[escape](../../outside.md)\n", encoding="utf-8")

    texts = {"docs/source.md": (docs / "source.md").read_text(encoding="utf-8"), "docs/target.md": (docs / "target.md").read_text(encoding="utf-8")}
    checked, errors, external = integrity._local_link_errors(tmp_path, ("docs/source.md",), texts)

    assert checked == 2
    assert external == ()
    assert any("missing local anchor" in error for error in errors)
    assert any("escapes repository" in error for error in errors)


def test_only_historical_evidence_may_retain_generator_markers(tmp_path: Path) -> None:
    current = "docs/current.md"
    historical = ".planning/old.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / ".planning").mkdir()
    (tmp_path / current).write_text("<!-- generated-by: stale-tool -->\n", encoding="utf-8")
    (tmp_path / historical).write_text("<!-- generated-by: evidence-tool -->\n", encoding="utf-8")

    texts = {current: (tmp_path / current).read_text(encoding="utf-8"), historical: (tmp_path / historical).read_text(encoding="utf-8")}
    errors = integrity._generator_errors((current, historical), frozenset({current, historical}), texts)

    assert errors == ("docs/current.md: unsupported generator ownership marker: stale-tool",)


def test_migration_heads_follow_literal_parent_relationships(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001_first.py").write_text('revision: str = "001"\ndown_revision: str | None = None\n', encoding="utf-8")
    (versions / "002_second.py").write_text('revision: str = "002"\ndown_revision: str | None = "001"\n', encoding="utf-8")

    assert integrity.migration_heads(tmp_path) == {"002"}


def test_external_failures_remain_separate_from_deterministic_results() -> None:
    targets = ("https://example.invalid/one", "mailto:operator@example.invalid")

    errors = integrity.check_external_targets(targets, lambda target: f"{target}: unavailable" if target.startswith("https://") else None)

    assert errors == ("https://example.invalid/one: unavailable",)


def test_external_url_uses_httpx_head_and_follows_redirects(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str], int, bool]] = []

    def fake_head(url: str, *, headers: dict[str, str], timeout: int, follow_redirects: bool) -> httpx.Response:
        calls.append((url, headers, timeout, follow_redirects))
        return httpx.Response(204, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(integrity.httpx, "head", fake_head)

    assert integrity._check_external_url("https://example.invalid/docs#section") is None
    assert calls == [
        (
            "https://example.invalid/docs",
            {"User-Agent": "phaze-documentation-integrity/1"},
            10,
            True,
        )
    ]


def test_external_url_reports_httpx_status_and_transport_failures(monkeypatch) -> None:
    target = "https://example.invalid/missing"
    request = httpx.Request("HEAD", target)
    responses: list[httpx.Response | httpx.HTTPError] = [
        httpx.Response(404, request=request),
        httpx.ConnectError("unavailable", request=request),
    ]

    def fake_head(*_args: object, **_kwargs: object) -> httpx.Response:
        result = responses.pop(0)
        if isinstance(result, httpx.HTTPError):
            raise result
        return result

    monkeypatch.setattr(integrity.httpx, "head", fake_head)

    assert integrity._check_external_url(target) == f"{target}: HTTP 404"
    assert integrity._check_external_url(target) == f"{target}: unavailable"


def test_external_url_skips_non_http_schemes_without_a_request(monkeypatch) -> None:
    def unexpected_head(*_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError("non-HTTP targets must not reach the HTTP client")

    monkeypatch.setattr(integrity.httpx, "head", unexpected_head)

    assert integrity._check_external_url("mailto:operator@example.invalid") is None


def test_maintained_mermaid_reintroduction_is_rejected() -> None:
    errors = integrity._mermaid_sources(("docs/new.md",), {"docs/new.md": "```mermaid\nflowchart LR\nA --> B\n```\n"})
    assert errors == ("docs/new.md:1: maintained Mermaid fence must use a source-cited diagram asset",)


@pytest.fixture
def diagram_tree(tmp_path: Path) -> Path:
    """A genuine rendered corpus, pinned receipt and helpers with local source evidence."""
    from scripts.check_diagram_assets import check_diagram_assets
    from scripts.diagram_inputs import render_input_digest

    shutil.copytree(REPO_ROOT / "docs/diagrams", tmp_path / "docs/diagrams")
    shutil.copytree(REPO_ROOT / "scripts/vendor/diagram_design", tmp_path / "scripts/vendor/diagram_design")
    for relative in ("scripts/render_documentation_diagrams.py", "scripts/diagram_inputs.py"):
        shutil.copyfile(REPO_ROOT / relative, tmp_path / relative)
    manifest = json.loads((tmp_path / "docs/diagrams/manifest.json").read_text())
    for diagram in manifest["diagrams"]:
        for edge in diagram["edges"]:
            for relative in edge["evidence"]:
                target = tmp_path / relative.split(":", 1)[0]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
        diagram["preview_source_sha256"] = diagram["source_sha256"]
        diagram["preview_svg_sha256"] = diagram["svg_sha256"]
        diagram["render_input_sha256"] = render_input_digest(tmp_path, diagram)
    (tmp_path / "docs/diagrams/manifest.json").write_text(json.dumps(manifest))
    assert check_diagram_assets(tmp_path)["errors"] == []
    return tmp_path


@pytest.mark.parametrize(
    "damage",
    [
        "missing-preview",
        "stale-svg",
        "orphan",
        "missing-evidence",
        "bad-origin",
        "stale-preview",
        "vendor-change",
        "manifest-label",
        "manifest-endpoint",
        "profile-change",
        "missing-baseline",
        "missing-origin",
        "missing-format",
    ],
)
def test_diagram_asset_gate_rejects_material_drift(diagram_tree: Path, damage: str) -> None:
    from scripts.check_diagram_assets import check_diagram_assets

    path = diagram_tree / "docs/diagrams/manifest.json"
    manifest = json.loads(path.read_text())
    diagram = manifest["diagrams"][0]
    if damage == "missing-preview":
        (diagram_tree / diagram["artifacts"]["png"]).unlink()
    elif damage == "stale-svg":
        with (diagram_tree / diagram["artifacts"]["svg"]).open("a") as stream:
            stream.write("<!-- changed -->")
    elif damage == "orphan":
        (diagram_tree / "docs/diagrams/orphan.svg").write_text("<svg/>")
    elif damage == "missing-evidence":
        diagram["edges"][0]["evidence"] = []
    elif damage == "bad-origin":
        diagram["origin"] = {"relationships": [{"id": "old", "rationale": "", "edges": ["missing"]}]}
    elif damage == "stale-preview":
        diagram["preview_source_sha256"] = "stale"
    elif damage == "manifest-label":
        diagram["nodes"][0]["label"] = "Changed after export"
    elif damage == "manifest-endpoint":
        diagram["edges"][0]["target"] = diagram["nodes"][-1]["id"]
    elif damage == "profile-change":
        profile = diagram_tree / "docs/diagrams/profile.json"
        profile.write_text(profile.read_text().replace("#f3f4f6", "#eeeeee"))
    elif damage == "missing-baseline":
        manifest.pop("migration_baseline")
    elif damage == "missing-origin":
        diagram.pop("origin")
    elif damage == "missing-format":
        diagram["artifacts"].pop("png")
    else:
        (diagram_tree / "scripts/vendor/diagram_design/scripts/verify-geometry.py").write_text("# changed")
    path.write_text(json.dumps(manifest))
    assert check_diagram_assets(diagram_tree)["errors"]


def test_signaled_upstream_checker_is_a_failure_and_other_checks_still_run(monkeypatch) -> None:
    from types import SimpleNamespace

    from scripts import verify_diagram_design

    statuses = iter((-9, 0, 0, 0))
    calls = []

    def checker(command, *, check):
        calls.append(command)
        return SimpleNamespace(returncode=next(statuses))

    monkeypatch.setattr(verify_diagram_design.subprocess, "run", checker)
    assert verify_diagram_design.main() == 1
    assert len(calls) == 4


class _FakeRenderer:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "", running: bool = False) -> None:
        self.pid = 12345
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.running = running

    def poll(self) -> int | None:
        return None if self.running else self.returncode

    def communicate(self) -> tuple[str, str]:
        return self.stdout, self.stderr

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.running = False
        return self.returncode


def test_renderer_normal_exit_uses_fresh_output_per_block(monkeypatch) -> None:
    invocations: list[tuple[str, ...]] = []
    output_parents: list[Path] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeRenderer:
        invocations.append(tuple(command))
        output = Path(command[command.index("-o") + 1])
        output.write_text("<svg></svg>", encoding="utf-8")
        output_parents.append(output.parent)
        return _FakeRenderer(returncode=0)

    monkeypatch.setattr(historical.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(historical, "_process_group_exists", lambda _pid: False)
    sources = (
        historical.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),
        historical.MermaidSource("docs/two.md", 8, "sequenceDiagram\nA->>B: hello"),
    )

    assert historical.render_mermaid_sources(sources, renderer="mmdc") == historical.MermaidRenderReport((), ())
    assert len(invocations) == 2
    assert all(invocation[0] == "mmdc" for invocation in invocations)
    assert len(set(output_parents)) == 2


def test_complete_output_with_exited_parent_cleans_lingering_group(monkeypatch) -> None:
    renderer = _FakeRenderer(returncode=0)
    cleanup: list[_FakeRenderer] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeRenderer:
        Path(command[command.index("-o") + 1]).write_text("<svg></svg>", encoding="utf-8")
        return renderer

    def fake_stop(process: _FakeRenderer) -> None:
        cleanup.append(process)

    monkeypatch.setattr(historical.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(historical, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(historical, "_stop_renderer", fake_stop)

    report = historical.render_mermaid_sources((historical.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report == historical.MermaidRenderReport((), ("docs/one.md:4",))
    assert cleanup == [renderer]


def test_complete_output_hang_is_reported_and_process_group_is_cleaned(monkeypatch) -> None:
    renderer = _FakeRenderer(returncode=0, running=True)
    cleanup: list[_FakeRenderer] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeRenderer:
        output = Path(command[command.index("-o") + 1])
        output.write_text("<svg></svg>", encoding="utf-8")
        return renderer

    def fake_stop(process: _FakeRenderer) -> None:
        cleanup.append(process)
        process.running = False

    monkeypatch.setattr(historical.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(historical, "_stop_renderer", fake_stop)
    monkeypatch.setattr(historical, "_RENDER_EXIT_GRACE_SECONDS", 0)
    monkeypatch.setattr(historical.time, "sleep", lambda _seconds: None)

    report = historical.render_mermaid_sources((historical.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report == historical.MermaidRenderReport((), ("docs/one.md:4",))
    assert cleanup == [renderer]


def test_process_group_cleanup_is_verified(monkeypatch) -> None:
    renderer = _FakeRenderer(returncode=0, running=True)
    signals: list[int] = []
    group_states = iter((True, False))

    monkeypatch.setattr(historical.os, "killpg", lambda _pid, sent_signal: signals.append(sent_signal))
    monkeypatch.setattr(historical, "_process_group_exists", lambda _pid: next(group_states))
    monkeypatch.setattr(historical.time, "sleep", lambda _seconds: None)

    assert historical._stop_renderer(renderer) is None
    assert signals == [historical.signal.SIGTERM]


@pytest.mark.parametrize("output_text", ["", "<svg>"])
def test_missing_or_partial_renderer_output_fails(monkeypatch, output_text: str) -> None:
    def fake_popen(command: list[str], **_kwargs: object) -> _FakeRenderer:
        if output_text:
            Path(command[command.index("-o") + 1]).write_text(output_text, encoding="utf-8")
        return _FakeRenderer(returncode=0)

    monkeypatch.setattr(historical.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(historical, "_process_group_exists", lambda _pid: False)

    report = historical.render_mermaid_sources((historical.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report.errors == ("docs/one.md:4: Mermaid renderer produced no complete SVG",)
    assert report.terminated_after_render == ()


def test_nonzero_renderer_exit_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        historical.subprocess,
        "Popen",
        lambda _command, **_kwargs: _FakeRenderer(returncode=2, stderr="syntax error"),
    )
    monkeypatch.setattr(historical, "_process_group_exists", lambda _pid: False)

    report = historical.render_mermaid_sources((historical.MermaidSource("docs/one.md", 4, "not mermaid"),), renderer="mmdc")

    assert report.errors == ("docs/one.md:4: Mermaid renderer failed: syntax error",)
    assert report.terminated_after_render == ()
