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
