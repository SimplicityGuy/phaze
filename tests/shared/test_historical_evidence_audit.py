from functools import cache
from io import BytesIO
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts import audit_historical_evidence as audit, maintenance_inventory


BASE_REVISION = "829594c6546498fff92270d4c569bb5f5bcc8b62"
# Preserve the original completed identifier-only scrub as a closed historical proof.
SCRUBBED_REVISION = "fc771de884d236213c9c4d3b3ae2fb24f3208cef"
# Operator-authorized source-neutral retirement, phaze-tqoty / phaze-3p7s7, 2026-10-08.
# This commit is deliberately retained in history: it is the source-neutral corpus checkpoint for branch review,
# not a file exemption. Future changes still meet the same exact-transform/numeric guard.
# Authority and scope: docs/design/0024-tracklist-source-retirement.md.
RETIREMENT_REVISION = "3fcf3c6934b63215136a23b7c30854c4491d9f2a"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(  # noqa: S603 -- fixed git argv; repository-local fixture reads only
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout


def _snapshot(root: Path, revision: str, destination: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialize a read-only historical artifact with an isolated index and shared objects.

    This never checks out, changes or commits the developer's worktree. The temporary fixture
    lets the original live-tree audit verify a past completed transformation without substituting
    canned result counters, weakening its comparison, or exposing retired source names in tests.
    """
    archive = _git(root, "archive", revision)
    destination.mkdir()
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    objects = Path(_git(root, "rev-parse", "--git-common-dir").decode().strip())
    if not objects.is_absolute():
        objects = root / objects
    _git(destination, "init", "--quiet")
    (destination / ".git/objects/info/alternates").write_text(str((objects / "objects").resolve()) + "\n")
    _git(destination, "update-ref", "refs/heads/audit", revision)
    _git(destination, "symbolic-ref", "HEAD", "refs/heads/audit")
    _git(destination, "read-tree", revision)
    monkeypatch.setattr(audit, "REPO_ROOT", destination)
    monkeypatch.setattr(maintenance_inventory, "REPO_ROOT", destination)


@cache
def _original_source_identifiers() -> frozenset[str]:
    """Learn the original protected identities from immutable bytes, never from a new baseline."""
    before = audit._historical_tree_at_revision(BASE_REVISION)
    after = audit._historical_tree_at_revision(SCRUBBED_REVISION)
    renamed = audit._match_renames(set(before), set(after))
    identifiers: set[str] = set()
    for old_path, raw in before.items():
        try:
            old = raw.decode("utf-8")
            new = after[renamed.get(old_path, old_path)].decode("utf-8")
        except UnicodeDecodeError:
            continue
        identifiers.update(audit._role_source_fragments(old, new))
    return frozenset(identifiers)


def _live_source_identity_leaks() -> list[str]:
    """Keep learned plain host/account identities forbidden, including post-baseline additions."""
    identifiers = set(_original_source_identifiers())
    return [
        path
        for path in sorted(audit._current_historical_paths())
        if audit._count_source_identifier_occurrences(identifiers, [(audit.REPO_ROOT / path).read_bytes().decode("utf-8", errors="replace")])
    ]


def test_identifier_substitutions_are_narrow_and_numeric_evidence_survives() -> None:
    before = "/Users/person/Code/public/phaze on personalbox at 31.31 GiB on 2026-09-11; users_person_code_public_phaze_tests."
    after = "<scratch>/phaze on host-prod at 31.31 GiB on 2026-09-11; scratch_phaze_tests."

    assert audit._approved_identifier_only_change(before, after)
    assert audit.numeric_tokens(before) == audit.numeric_tokens(after)


def test_complete_historical_corpus_matches_the_approved_transform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshot(audit.REPO_ROOT, SCRUBBED_REVISION, tmp_path / "original-scrub", monkeypatch)
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


def test_current_retirement_corpus_remains_exact_and_identifier_free() -> None:
    """New authorization advances the checkpoint; it never turns existing files into exemptions."""
    result = audit.audit(RETIREMENT_REVISION)

    assert result["errors"] == []
    assert result["historical_files_scanned"] == 1_314
    assert result["numeric_files_compared"] == 1_314
    assert result["exact_transform_mismatches"] == []
    assert result["numeric_mismatches"] == []
    assert len(_original_source_identifiers()) == 5
    assert _live_source_identity_leaks() == []


@pytest.mark.parametrize("change", ["numeric", "remove", "identifier"])
def test_retirement_checkpoint_still_rejects_unapproved_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    identifiers = _original_source_identifiers()
    _snapshot(audit.REPO_ROOT, RETIREMENT_REVISION, tmp_path / "retirement", monkeypatch)
    project = audit.REPO_ROOT / ".planning/PROJECT.md"
    if change == "numeric":
        project.write_text(project.read_text() + "\nAn unapproved measured value: 9999.\n")
    elif change == "remove":
        project.unlink()
    else:
        (audit.REPO_ROOT / "docs/spikes/new-identity-leak.md").write_text("Measured on " + sorted(identifiers)[0] + ".\n")

    if change == "identifier":
        assert _live_source_identity_leaks() == ["docs/spikes/new-identity-leak.md"]
    else:
        result = audit.audit(RETIREMENT_REVISION)
        assert result["errors"]
        assert any("numeric evidence" in message if change == "numeric" else "population changed" in message for message in result["errors"])


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
