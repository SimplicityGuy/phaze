"""Tests for the proposal service — response models, prompt loading, companion cleaning, context building."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# FileProposalResponse tests
# ---------------------------------------------------------------------------


class TestFileProposalResponse:
    """Tests for FileProposalResponse Pydantic model."""

    def test_validates_complete_proposal(self):
        from phaze.services.proposal import FileProposalResponse

        data = {
            "file_index": 0,
            "proposed_filename": "Artist - Live @ Coachella 2024.04.12.mp3",
            "confidence": 0.92,
            "artist": "Disclosure",
            "event_name": "Coachella 2024",
            "venue": "Empire Polo Club",
            "date": "2024.04.12",
            "source_type": "WEB",
            "stage": "Sahara",
            "day_number": 1,
            "b2b_partners": [],
            "reasoning": "Rich metadata from NFO file and filename parsing",
        }
        resp = FileProposalResponse(**data)
        assert resp.file_index == 0
        assert resp.proposed_filename == "Artist - Live @ Coachella 2024.04.12.mp3"
        assert resp.confidence == 0.92
        assert resp.artist == "Disclosure"
        assert resp.reasoning == "Rich metadata from NFO file and filename parsing"

    def test_accepts_none_for_optional_fields(self):
        from phaze.services.proposal import FileProposalResponse

        data = {
            "file_index": 1,
            "proposed_filename": "Unknown - Track.mp3",
            "confidence": 0.2,
            "reasoning": "Very little metadata available",
        }
        resp = FileProposalResponse(**data)
        assert resp.artist is None
        assert resp.event_name is None
        assert resp.venue is None
        assert resp.date is None
        assert resp.source_type is None
        assert resp.stage is None
        assert resp.day_number is None
        assert resp.b2b_partners == []

    def test_confidence_accepts_any_float(self):
        """Confidence has NO ge/le constraints per Pitfall 2 (Anthropic compatibility)."""
        from phaze.services.proposal import FileProposalResponse

        # Should accept values outside 0-1 without validation error
        data = {
            "file_index": 0,
            "proposed_filename": "test.mp3",
            "confidence": 1.5,
            "reasoning": "test",
        }
        resp = FileProposalResponse(**data)
        assert resp.confidence == 1.5

        data2 = {**data, "confidence": -0.3}
        resp2 = FileProposalResponse(**data2)
        assert resp2.confidence == -0.3


# ---------------------------------------------------------------------------
# BatchProposalResponse tests
# ---------------------------------------------------------------------------


class TestBatchProposalResponse:
    """Tests for BatchProposalResponse Pydantic model."""

    def test_validates_list_of_proposals(self):
        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse

        proposals = [
            FileProposalResponse(
                file_index=0,
                proposed_filename="Artist A - Live @ Event 2024.04.12.mp3",
                confidence=0.9,
                reasoning="good metadata",
            ),
            FileProposalResponse(
                file_index=1,
                proposed_filename="Artist B - Track.flac",
                confidence=0.3,
                reasoning="sparse metadata",
            ),
        ]
        batch = BatchProposalResponse(proposals=proposals)
        assert len(batch.proposals) == 2
        assert batch.proposals[0].file_index == 0
        assert batch.proposals[1].file_index == 1


# ---------------------------------------------------------------------------
# load_prompt_template tests
# ---------------------------------------------------------------------------


class TestLoadPromptTemplate:
    """Tests for load_prompt_template function."""

    def test_returns_nonempty_string_with_naming_markers(self):
        from phaze.services.proposal import load_prompt_template

        content = load_prompt_template()
        assert len(content) > 0
        assert "{files_json}" in content
        assert "YYYY.MM.DD" in content

    def test_raises_filenotfounderror_for_missing_template(self):
        from phaze.services.proposal import load_prompt_template

        with pytest.raises(FileNotFoundError):
            load_prompt_template("nonexistent_template_xyz")


# ---------------------------------------------------------------------------
# clean_companion_content tests
# ---------------------------------------------------------------------------


class TestCleanCompanionContent:
    """Tests for clean_companion_content function."""

    def test_truncates_long_text(self):
        from phaze.services.proposal import clean_companion_content

        long_text = "a" * 4000
        result = clean_companion_content(long_text, max_chars=3000)
        assert len(result) <= 3000 + len("\n[...truncated]")
        assert result.endswith("[...truncated]")

    def test_strips_ascii_art_lines(self):
        from phaze.services.proposal import clean_companion_content

        text = "Release Info\n==============\nArtist: DJ Test\n--------------\nDate: 2024"
        result = clean_companion_content(text)
        assert "==============" not in result
        assert "--------------" not in result
        assert "Release Info" in result
        assert "Artist: DJ Test" in result
        assert "Date: 2024" in result

    def test_preserves_informational_lines(self):
        from phaze.services.proposal import clean_companion_content

        text = "Artist: Deadmau5\nVenue: Red Rocks\nDate: 2024.05.15\nSource: SBD"
        result = clean_companion_content(text)
        assert "Artist: Deadmau5" in result
        assert "Venue: Red Rocks" in result
        assert "Date: 2024.05.15" in result
        assert "Source: SBD" in result


# ---------------------------------------------------------------------------
# build_file_context tests
# ---------------------------------------------------------------------------


def _make_file_record() -> MagicMock:
    """Create a mock FileRecord."""
    rec = MagicMock()
    rec.original_filename = "999999999-Live_At_Boiler_Room-WEB-2019.mp3"
    rec.original_path = "/data/music/unsorted/999999999-Live_At_Boiler_Room-WEB-2019.mp3"
    rec.file_type = "mp3"
    return rec


def _make_analysis() -> MagicMock:
    """Create a mock AnalysisResult."""
    analysis = MagicMock()
    analysis.bpm = 140.0
    analysis.musical_key = "Am"
    analysis.mood = "dark"
    analysis.style = "techno"
    analysis.features = {"energy": 0.85}
    return analysis


class TestBuildFileContext:
    """Tests for build_file_context function."""

    def test_assembles_correct_dict_structure(self):
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        analysis = _make_analysis()
        companions = [{"filename": "info.nfo", "content": "Artist: 999999999"}]

        ctx = build_file_context(file_rec, analysis, companions)
        assert ctx["index"] == 0
        assert ctx["original_filename"] == "999999999-Live_At_Boiler_Room-WEB-2019.mp3"
        assert ctx["original_path"] == "/data/music/unsorted/999999999-Live_At_Boiler_Room-WEB-2019.mp3"
        assert ctx["file_type"] == "mp3"
        assert ctx["analysis"]["bpm"] == 140.0
        assert ctx["analysis"]["musical_key"] == "Am"
        assert ctx["analysis"]["mood"] == "dark"
        assert ctx["analysis"]["style"] == "techno"
        assert ctx["analysis"]["features"] == {"energy": 0.85}
        assert ctx["companions"] == companions

    def test_handles_missing_analysis(self):
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        ctx = build_file_context(file_rec, None, [])
        assert ctx["analysis"] is None

    def test_handles_empty_companions(self):
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        analysis = _make_analysis()
        ctx = build_file_context(file_rec, analysis, [])
        assert ctx["companions"] == []


# ---------------------------------------------------------------------------
# Settings LLM fields tests
# ---------------------------------------------------------------------------


class TestSettingsLlmFields:
    """Tests for LLM configuration fields in Settings."""

    def test_llm_model_default(self):
        from phaze.config import Settings

        s = Settings()
        assert s.llm_model == "claude-sonnet-4-20250514"

    def test_anthropic_api_key_default_none(self):
        from phaze.config import Settings

        s = Settings()
        assert s.anthropic_api_key is None

    def test_llm_max_rpm_default(self):
        from phaze.config import Settings

        s = Settings()
        assert s.llm_max_rpm == 30

    def test_llm_batch_size_default(self):
        from phaze.config import Settings

        s = Settings()
        assert s.llm_batch_size == 10

    def test_llm_max_companion_chars_default(self):
        from phaze.config import Settings

        s = Settings()
        assert s.llm_max_companion_chars == 3000
