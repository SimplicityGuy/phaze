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


# ---------------------------------------------------------------------------
# ProposalService tests (Plan 02)
# ---------------------------------------------------------------------------


class TestProposalServiceInit:
    """Tests for ProposalService constructor."""

    def test_stores_model_and_prompt_and_max_rpm(self):
        from phaze.services.proposal import ProposalService

        svc = ProposalService(model="test-model", prompt_template="Hello {files_json}", max_rpm=60)
        assert svc.model == "test-model"
        assert svc.prompt_template == "Hello {files_json}"
        assert svc.max_rpm == 60


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


class TestGenerateBatch:
    """Tests for ProposalService.generate_batch."""

    @pytest.mark.asyncio
    async def test_calls_acompletion_with_correct_args(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from phaze.services.proposal import BatchProposalResponse, ProposalService

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = BatchProposalResponse(
            proposals=[
                {
                    "file_index": 0,
                    "proposed_filename": "Test.mp3",
                    "confidence": 0.9,
                    "reasoning": "good",
                }
            ]
        ).model_dump_json()

        with patch("phaze.services.proposal.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = mock_response
            svc = ProposalService(model="test-model", prompt_template="Files: {files_json}", max_rpm=30)
            result = await svc.generate_batch([{"index": 0, "original_filename": "test.mp3"}])

            mock_acompletion.assert_called_once()
            call_kwargs = mock_acompletion.call_args
            assert call_kwargs[1]["model"] == "test-model"
            assert call_kwargs[1]["response_format"] is BatchProposalResponse
            assert len(result.proposals) == 1
            assert result.proposals[0].proposed_filename == "Test.mp3"

    @pytest.mark.asyncio
    async def test_builds_prompt_with_files_json(self):
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from phaze.services.proposal import BatchProposalResponse, ProposalService

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = BatchProposalResponse(
            proposals=[]
        ).model_dump_json()

        files_context = [{"index": 0, "original_filename": "a.mp3"}]

        with patch("phaze.services.proposal.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = mock_response
            svc = ProposalService(model="m", prompt_template="DATA: {files_json}", max_rpm=30)
            await svc.generate_batch(files_context)

            prompt = mock_acompletion.call_args[1]["messages"][0]["content"]
            assert json.dumps(files_context, indent=2) in prompt


# ---------------------------------------------------------------------------
# check_rate_limit tests (Plan 02)
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    """Tests for check_rate_limit function."""

    @pytest.mark.asyncio
    async def test_under_limit_returns_immediately(self):
        from unittest.mock import AsyncMock, patch

        from phaze.services.proposal import check_rate_limit

        redis = AsyncMock()
        redis.incr.return_value = 1

        with patch("phaze.services.proposal.asyncio") as mock_asyncio:
            await check_rate_limit(redis, 30)
            mock_asyncio.sleep.assert_not_called()

        redis.incr.assert_called_once_with("phaze:llm:rpm")
        redis.expire.assert_called_once_with("phaze:llm:rpm", 60)

    @pytest.mark.asyncio
    async def test_over_limit_waits_and_retries(self):
        from unittest.mock import AsyncMock, patch

        from phaze.services.proposal import check_rate_limit

        redis = AsyncMock()
        # First call over limit, second call under limit
        redis.incr.side_effect = [31, 1]

        with patch("phaze.services.proposal.asyncio") as mock_asyncio:
            mock_asyncio.sleep = AsyncMock()
            await check_rate_limit(redis, 30)

            mock_asyncio.sleep.assert_called_once_with(2.0)
            redis.decr.assert_called_once_with("phaze:llm:rpm")

    @pytest.mark.asyncio
    async def test_sets_ttl_on_first_increment(self):
        from unittest.mock import AsyncMock

        from phaze.services.proposal import check_rate_limit

        redis = AsyncMock()
        redis.incr.return_value = 1

        await check_rate_limit(redis, 30)

        redis.expire.assert_called_once_with("phaze:llm:rpm", 60)

    @pytest.mark.asyncio
    async def test_no_ttl_on_subsequent_increments(self):
        from unittest.mock import AsyncMock

        from phaze.services.proposal import check_rate_limit

        redis = AsyncMock()
        redis.incr.return_value = 5  # Not first increment

        await check_rate_limit(redis, 30)

        redis.expire.assert_not_called()


# ---------------------------------------------------------------------------
# store_proposals tests (Plan 02)
# ---------------------------------------------------------------------------


class TestStoreProposals:
    """Tests for store_proposals function."""

    @pytest.mark.asyncio
    async def test_creates_rename_proposal_records(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, store_proposals

        session = AsyncMock()
        file_id = str(uuid.uuid4())
        file_record = MagicMock()
        file_record.state = "analyzed"

        # Mock query to return file record
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = file_record
        session.execute.return_value = mock_result

        batch = BatchProposalResponse(
            proposals=[
                FileProposalResponse(
                    file_index=0,
                    proposed_filename="Artist - Live.mp3",
                    confidence=0.85,
                    artist="TestArtist",
                    event_name="TestEvent",
                    reasoning="Test reasoning",
                )
            ]
        )
        files_context = [{"original_filename": "test.mp3"}]

        with patch("phaze.services.proposal.RenameProposal"):
            count = await store_proposals(session, [file_id], batch, files_context)

        assert count == 1
        session.add.assert_called_once()
        assert file_record.state == "proposal_generated"

    @pytest.mark.asyncio
    async def test_clamps_confidence_before_storing(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, store_proposals

        session = AsyncMock()
        file_id = str(uuid.uuid4())
        file_record = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = file_record
        session.execute.return_value = mock_result

        batch = BatchProposalResponse(
            proposals=[
                FileProposalResponse(
                    file_index=0,
                    proposed_filename="test.mp3",
                    confidence=1.5,  # Over 1.0, should be clamped
                    reasoning="test",
                )
            ]
        )

        with patch("phaze.services.proposal.RenameProposal") as MockProposal:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            # Check confidence was clamped to 1.0
            call_kwargs = MockProposal.call_args[1]
            assert call_kwargs["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_stores_context_used_with_metadata(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, store_proposals

        session = AsyncMock()
        file_id = str(uuid.uuid4())
        file_record = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = file_record
        session.execute.return_value = mock_result

        batch = BatchProposalResponse(
            proposals=[
                FileProposalResponse(
                    file_index=0,
                    proposed_filename="test.mp3",
                    confidence=0.9,
                    artist="DJ Test",
                    event_name="Coachella 2024",
                    venue="Empire Polo Club",
                    date="2024.04.12",
                    source_type="WEB",
                    stage="Sahara",
                    day_number=1,
                    b2b_partners=["DJ Partner"],
                    reasoning="test",
                )
            ]
        )
        input_ctx = [{"original_filename": "test.mp3"}]

        with patch("phaze.services.proposal.RenameProposal") as MockProposal:
            await store_proposals(session, [file_id], batch, input_ctx)
            call_kwargs = MockProposal.call_args[1]
            ctx_used = call_kwargs["context_used"]
            assert ctx_used["artist"] == "DJ Test"
            assert ctx_used["event_name"] == "Coachella 2024"
            assert ctx_used["venue"] == "Empire Polo Club"
            assert ctx_used["input_context"] == input_ctx[0]


# ---------------------------------------------------------------------------
# load_companion_contents tests (Plan 02)
# ---------------------------------------------------------------------------


class TestLoadCompanionContents:
    """Tests for load_companion_contents function."""

    @pytest.mark.asyncio
    async def test_loads_and_cleans_companion_files(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        # Mock FileCompanion query result
        companion = MagicMock()
        companion.companion_id = uuid.uuid4()

        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        # Mock FileRecord query for companion
        companion_record = MagicMock()
        companion_record.original_filename = "info.nfo"
        companion_record.current_path = "/data/music/info.nfo"

        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        with patch("phaze.services.proposal.Path") as MockPath:
            mock_path_instance = MagicMock()
            mock_path_instance.read_text.return_value = "Artist: DJ Test\nVenue: Club"
            MockPath.return_value = mock_path_instance

            result = await load_companion_contents(session, media_id, 3000)

        assert len(result) == 1
        assert result[0]["filename"] == "info.nfo"
        assert "Artist: DJ Test" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_skips_unreadable_files(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        companion = MagicMock()
        companion.companion_id = uuid.uuid4()

        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        companion_record = MagicMock()
        companion_record.original_filename = "info.nfo"
        companion_record.current_path = "/nonexistent/info.nfo"

        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        with patch("phaze.services.proposal.Path") as MockPath:
            mock_path_instance = MagicMock()
            mock_path_instance.read_text.side_effect = OSError("File not found")
            MockPath.return_value = mock_path_instance

            result = await load_companion_contents(session, media_id, 3000)

        assert len(result) == 0
