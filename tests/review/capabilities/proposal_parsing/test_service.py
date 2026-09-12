"""Proposal parsing scenarios moved from ``tests/review/services/test_proposal.py``."""

from __future__ import annotations


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


class TestClampConfidence:
    """Tests for ProposalService._clamp_confidence."""

    def test_passthrough_valid(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(0.5) == 0.5

    def test_clamp_negative(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(-0.1) == 0.0

    def test_clamp_above_one(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(1.5) == 1.0

    def test_boundary_zero(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(0.0) == 0.0

    def test_boundary_one(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(1.0) == 1.0

    def test_nan_is_untrusted_not_maximal(self):
        """NaN must not survive min(1.0, value) and clamp to the maximum."""
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(float("nan")) == 0.0

    def test_positive_infinity_is_untrusted_not_maximal(self):
        """+inf must not survive min(1.0, value) and clamp to the maximum."""
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(float("inf")) == 0.0

    def test_negative_infinity_is_untrusted(self):
        from phaze.services.proposal import ProposalService

        assert ProposalService._clamp_confidence(float("-inf")) == 0.0


class TestFileProposalResponsePath:
    """Tests for proposed_path field on FileProposalResponse."""

    def test_accepts_proposed_path(self):
        from phaze.services.proposal import FileProposalResponse

        resp = FileProposalResponse(
            file_index=0,
            proposed_filename="test.mp3",
            confidence=0.9,
            reasoning="test",
            proposed_path="performances/artists/Disclosure",
        )
        assert resp.proposed_path == "performances/artists/Disclosure"

    def test_defaults_proposed_path_to_none(self):
        from phaze.services.proposal import FileProposalResponse

        resp = FileProposalResponse(
            file_index=0,
            proposed_filename="test.mp3",
            confidence=0.9,
            reasoning="test",
        )
        assert resp.proposed_path is None
