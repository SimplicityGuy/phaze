from pathlib import Path

import pytest

from scripts import audit_historical_evidence as audit


BASE_REVISION = "829594c6546498fff92270d4c569bb5f5bcc8b62"


def test_identifier_substitutions_are_narrow_and_numeric_evidence_survives() -> None:
    before = "/Users/person/Code/public/phaze on personalbox at 31.31 GiB on 2026-09-11; users_person_code_public_phaze_tests."
    after = "<scratch>/phaze on host-prod at 31.31 GiB on 2026-09-11; scratch_phaze_tests."

    assert audit._approved_identifier_only_change(before, after)
    assert audit.numeric_tokens(before) == audit.numeric_tokens(after)


def test_complete_historical_corpus_matches_the_approved_transform() -> None:
    result = audit.audit(BASE_REVISION)

    assert result["errors"] == []
    assert result["historical_files_scanned"] == 1_313
    assert result["binary_files_scanned"] == 3
    assert result["scrubbed_files"] == 202
    assert result["replacements"] == {
        "archive_directory": 3,
        "encoded_local_root": 10_012,
        "host_or_account": 729,
        "local_absolute_root": 41_853,
    }
    assert result["source_host_or_account_identifiers"] == 5
    assert result["forbidden_occurrences_after"] == 0
    assert result["exact_transform_mismatches"] == []
    assert result["numeric_files_compared"] == 1_313
    assert result["numeric_tokens_compared"] == 319_770
    assert result["numeric_mismatches"] == []
    assert len(result["archive_boundaries"]) == 5
    assert result["missing_archive_boundaries"] == []
    assert result["local_links_valid"] == 123
    assert result["local_links_historical_by_boundary"] == 1
    assert result["mermaid_blocks_valid"] == 1
    assert result["mermaid_blocks_invalid"] == []
    assert result["graph_files_valid"] == 2
    assert result["graph_reference_errors"] == []


def test_post_baseline_addition_is_not_a_population_error() -> None:
    assert audit._population_error({"docs/spikes/a.md", "docs/spikes/new.md"}, {"docs/spikes/a.md"}) is None


def test_removing_a_baseline_path_is_still_a_population_error() -> None:
    error = audit._population_error({"docs/spikes/a.md"}, {"docs/spikes/a.md", "docs/spikes/gone.md"})

    assert error is not None
    assert "docs/spikes/gone.md" in error


def _scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, content: bytes) -> audit.CorpusAudit:
    (tmp_path / name).write_bytes(content)
    monkeypatch.setattr(audit, "REPO_ROOT", tmp_path)
    state = audit.CorpusAudit()
    audit._scan_post_baseline_additions(state, [name])
    return state


def test_clean_new_spike_file_passes_the_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = _scan(monkeypatch, tmp_path, "new.md", b"# Spike\n\nMeasured on <archive-mount>/<set-01> at 12.5 h.\n")

    assert state.forbidden_occurrences_after == 0
    assert state.mermaid_blocks_invalid == []
    assert audit._assemble_errors(state, [], [], False, None) == []


@pytest.mark.parametrize(
    "leak",
    ["/Users/someone/work", "/Volumes/Archive/set", "/media/disk1/set", "users_someone_code_public_phaze"],
)
def test_new_spike_file_with_a_local_identifier_fails_the_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, leak: str) -> None:
    state = _scan(monkeypatch, tmp_path, "new.md", f"# Spike\n\nSee {leak}.\n".encode())

    assert state.forbidden_occurrences_after == 1
    assert audit._assemble_errors(state, [], [], False, None) == ["1 prohibited identifier occurrence(s) remain"]


def test_new_binary_file_with_a_local_identifier_fails_the_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = _scan(monkeypatch, tmp_path, "new.bin", b"\xff\xfe/Users/someone/x")

    assert state.forbidden_occurrences_after == 1


def test_new_spike_file_with_a_broken_mermaid_block_fails_the_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = _scan(monkeypatch, tmp_path, "new.md", b"```mermaid\nnot a diagram\n```\n")

    assert state.mermaid_blocks_invalid == ["new.md:1: missing or unsupported diagram declaration"]
