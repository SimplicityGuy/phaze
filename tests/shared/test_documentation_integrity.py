"""Focused tests for the maintained-document integrity gate."""

from pathlib import Path

import pytest

from scripts import check_documentation_integrity as integrity


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_current_maintained_documentation_passes_deterministic_checks() -> None:
    report = integrity.check_documentation(REPO_ROOT)

    assert report["errors"] == []
    assert report["mermaid_errors"] == []
    assert report["migration_heads"] == ["063"]
    assert report["generator_markers"] == 4
    assert report["mermaid_blocks"] == 25
    assert report["mermaid_cli_version"] == "11.12.0"
    assert report["mermaid_terminated_after_render"] == []
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

    monkeypatch.setattr(integrity.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(integrity, "_process_group_exists", lambda _pid: False)
    sources = (
        integrity.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),
        integrity.MermaidSource("docs/two.md", 8, "sequenceDiagram\nA->>B: hello"),
    )

    assert integrity.render_mermaid_sources(sources, renderer="mmdc") == integrity.MermaidRenderReport((), ())
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

    monkeypatch.setattr(integrity.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(integrity, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(integrity, "_stop_renderer", fake_stop)

    report = integrity.render_mermaid_sources((integrity.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report == integrity.MermaidRenderReport((), ("docs/one.md:4",))
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

    monkeypatch.setattr(integrity.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(integrity, "_stop_renderer", fake_stop)
    monkeypatch.setattr(integrity, "_RENDER_EXIT_GRACE_SECONDS", 0)
    monkeypatch.setattr(integrity.time, "sleep", lambda _seconds: None)

    report = integrity.render_mermaid_sources((integrity.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report == integrity.MermaidRenderReport((), ("docs/one.md:4",))
    assert cleanup == [renderer]


def test_process_group_cleanup_is_verified(monkeypatch) -> None:
    renderer = _FakeRenderer(returncode=0, running=True)
    signals: list[int] = []
    group_states = iter((True, False))

    monkeypatch.setattr(integrity.os, "killpg", lambda _pid, sent_signal: signals.append(sent_signal))
    monkeypatch.setattr(integrity, "_process_group_exists", lambda _pid: next(group_states))
    monkeypatch.setattr(integrity.time, "sleep", lambda _seconds: None)

    assert integrity._stop_renderer(renderer) is None
    assert signals == [integrity.signal.SIGTERM]


@pytest.mark.parametrize("output_text", ["", "<svg>"])
def test_missing_or_partial_renderer_output_fails(monkeypatch, output_text: str) -> None:
    def fake_popen(command: list[str], **_kwargs: object) -> _FakeRenderer:
        if output_text:
            Path(command[command.index("-o") + 1]).write_text(output_text, encoding="utf-8")
        return _FakeRenderer(returncode=0)

    monkeypatch.setattr(integrity.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(integrity, "_process_group_exists", lambda _pid: False)

    report = integrity.render_mermaid_sources((integrity.MermaidSource("docs/one.md", 4, "flowchart LR\nA --> B"),), renderer="mmdc")

    assert report.errors == ("docs/one.md:4: Mermaid renderer produced no complete SVG",)
    assert report.terminated_after_render == ()


def test_nonzero_renderer_exit_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        integrity.subprocess,
        "Popen",
        lambda _command, **_kwargs: _FakeRenderer(returncode=2, stderr="syntax error"),
    )
    monkeypatch.setattr(integrity, "_process_group_exists", lambda _pid: False)

    report = integrity.render_mermaid_sources((integrity.MermaidSource("docs/one.md", 4, "not mermaid"),), renderer="mmdc")

    assert report.errors == ("docs/one.md:4: Mermaid renderer failed: syntax error",)
    assert report.terminated_after_render == ()


def test_external_failures_remain_separate_from_deterministic_results() -> None:
    targets = ("https://example.invalid/one", "mailto:operator@example.invalid")

    errors = integrity.check_external_targets(targets, lambda target: f"{target}: unavailable" if target.startswith("https://") else None)

    assert errors == ("https://example.invalid/one: unavailable",)
