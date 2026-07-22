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

    def test_builds_context_with_metadata(self):
        """build_file_context includes tags dict when metadata provided."""
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        metadata = MagicMock()
        metadata.artist = "Disclosure"
        metadata.title = "Latch"
        metadata.album = "Settle"
        metadata.year = 2013
        metadata.genre = "Electronic"
        metadata.raw_tags = {"TPE1": "Disclosure"}

        ctx = build_file_context(file_rec, None, [], metadata=metadata)
        assert "tags" in ctx
        assert ctx["tags"]["artist"] == "Disclosure"
        assert ctx["tags"]["title"] == "Latch"
        assert ctx["tags"]["album"] == "Settle"
        assert ctx["tags"]["year"] == 2013
        assert ctx["tags"]["genre"] == "Electronic"
        assert ctx["tags"]["raw_tags"] == {"TPE1": "Disclosure"}

    def test_builds_context_without_metadata(self):
        """build_file_context returns tags=None when no metadata."""
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        ctx = build_file_context(file_rec, None, [])
        assert "tags" in ctx
        assert ctx["tags"] is None


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
        mock_response.choices[0].message.content = BatchProposalResponse(proposals=[]).model_dump_json()

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


class _FakeRedis:
    """Minimal in-memory async Redis modelling INCR/DECR/TTL/EXPIRE + the rate-limit EVAL.

    Models the exact semantics the atomic rate-limit script relies on: INCR on a
    MISSING key creates it with NO expiry (``ttl`` == -1); TTL returns -2 (missing) /
    -1 (exists, no expiry) / remaining seconds; EXPIRE arms the TTL. ``eval``
    interprets the single script ``check_rate_limit`` uses — INCR then arm-if-TTL==-1
    — atomically, so a regression test can assert the TTL-less-key self-heal that a
    bare ``AsyncMock`` cannot express.
    """

    KEY = "phaze:llm:rpm"

    def __init__(self, *, value: int | None = None, ttl: int | None = None) -> None:
        # value None => key missing. ttl None => exists but no expiry (-1).
        self._value = value
        self._ttl = ttl

    async def incr(self, _key: str) -> int:
        self._value = 1 if self._value is None else self._value + 1
        return self._value

    async def decr(self, _key: str) -> int:
        self._value = -1 if self._value is None else self._value - 1
        return self._value

    async def ttl(self, _key: str) -> int:
        if self._value is None:
            return -2
        return -1 if self._ttl is None else self._ttl

    async def expire(self, _key: str, seconds: int) -> bool:
        if self._value is None:
            return False
        self._ttl = int(seconds)
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, window: int) -> int:
        count = await self.incr(key)
        if await self.ttl(key) == -1:
            await self.expire(key, int(window))
        return count


class TestCheckRateLimit:
    """Tests for check_rate_limit — now backed by an atomic INCR+arm EVAL (phaze-pkgb)."""

    @pytest.mark.asyncio
    async def test_under_limit_returns_immediately_and_arms_ttl(self):
        from unittest.mock import patch

        from phaze.services.proposal import check_rate_limit

        fake = _FakeRedis()  # fresh window: key missing
        with patch("phaze.services.proposal.asyncio") as mock_asyncio:
            await check_rate_limit(fake, 30)
            mock_asyncio.sleep.assert_not_called()

        # First acquisition creates the key AND arms its 60s TTL in one atomic step.
        assert fake._value == 1
        assert await fake.ttl(_FakeRedis.KEY) == 60

    @pytest.mark.asyncio
    async def test_uses_atomic_eval_not_separate_incr_expire(self):
        """The non-atomic INCR-then-EXPIRE pair must be gone; a single EVAL replaces it."""
        from unittest.mock import AsyncMock

        from phaze.services.proposal import check_rate_limit

        redis = AsyncMock()
        redis.eval.return_value = 1

        await check_rate_limit(redis, 30)

        redis.eval.assert_called_once()
        args = redis.eval.call_args.args
        assert args[1] == 1, "EVAL numkeys must be 1"
        assert args[2] == "phaze:llm:rpm", "EVAL key must be the rpm counter"
        # The split INCR/EXPIRE that could lose its TTL must no longer be called directly.
        redis.incr.assert_not_called()
        redis.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_rearms_ttl_less_key_no_permanent_wedge(self):
        """Regression (phaze-pkgb): a key left WITHOUT a TTL by a lost EXPIRE self-heals.

        The old code armed the TTL only when ``count == 1``; a counter stuck above 1
        with no expiry (a lost EXPIRE) could never be re-armed and wedged proposal
        generation forever. The atomic script re-arms whenever TTL == -1, regardless
        of the counter value — so the next call restores the expiry.
        """
        from phaze.services.proposal import check_rate_limit

        # Wedged state: counter at 5 with NO expiry (ttl == -1).
        fake = _FakeRedis(value=5, ttl=None)
        assert await fake.ttl(_FakeRedis.KEY) == -1

        await check_rate_limit(fake, 30)  # 5 -> 6, under limit, returns

        assert fake._value == 6
        assert await fake.ttl(_FakeRedis.KEY) == 60, "atomic script must re-arm a TTL-less key"

    @pytest.mark.asyncio
    async def test_over_limit_waits_then_recovers_when_window_lapses(self):
        from unittest.mock import AsyncMock, patch

        from phaze.services.proposal import check_rate_limit

        # At the limit with a live TTL; the next INCR pushes over.
        fake = _FakeRedis(value=30, ttl=60)

        async def _lapse_window(_delay: float) -> None:
            # Model the 60s TTL expiring during the back-off sleep: the key resets.
            fake._value = None
            fake._ttl = None

        with patch("phaze.services.proposal.asyncio") as mock_asyncio:
            mock_asyncio.sleep = AsyncMock(side_effect=_lapse_window)
            await check_rate_limit(fake, 30)

            mock_asyncio.sleep.assert_called_once_with(2.0)

        # After the window lapsed, the retry re-acquired a fresh slot with a fresh TTL.
        assert fake._value == 1
        assert await fake.ttl(_FakeRedis.KEY) == 60


# ---------------------------------------------------------------------------
# store_proposals tests (Plan 02)
# ---------------------------------------------------------------------------


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


class TestStoreProposalsPath:
    """Tests for proposed_path handling in store_proposals."""

    @pytest.mark.asyncio
    async def test_persists_proposed_path(self):
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
                    reasoning="test",
                    proposed_path="performances/artists/Disclosure",
                )
            ]
        )

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["proposed_path"] == "performances/artists/Disclosure"

    @pytest.mark.asyncio
    async def test_normalizes_leading_trailing_slashes(self):
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
                    reasoning="test",
                    proposed_path="/performances/artists/Disclosure/",
                )
            ]
        )

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["proposed_path"] == "performances/artists/Disclosure"

    @pytest.mark.asyncio
    async def test_collapses_double_slashes(self):
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
                    reasoning="test",
                    proposed_path="performances//artists//Disclosure",
                )
            ]
        )

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["proposed_path"] == "performances/artists/Disclosure"

    @pytest.mark.asyncio
    async def test_leaves_none_path_as_none(self):
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
                    reasoning="test",
                )
            ]
        )

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["proposed_path"] is None


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

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            count = await store_proposals(session, [file_id], batch, files_context)

        assert count == 1
        # store_proposals issues the upsert only (one pg_insert per proposal); it does NOT
        # touch FileRecord.state anymore (SIDECAR-03 cutover removed the file.state cascade).
        mock_pg_insert.assert_called_once()
        session.execute.assert_awaited()

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

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, [{"f": 1}])
            # Check confidence was clamped to 1.0
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["confidence"] == 1.0

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

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, input_ctx)
            row = mock_pg_insert.return_value.values.call_args.kwargs
            ctx_used = row["context_used"]
            assert ctx_used["artist"] == "DJ Test"
            assert ctx_used["event_name"] == "Coachella 2024"
            assert ctx_used["venue"] == "Empire Polo Club"
            assert ctx_used["input_context"] == input_ctx[0]

    @pytest.mark.asyncio
    async def test_sanitizes_nul_bytes_before_persist(self):
        """NUL bytes anywhere in the persisted row are stripped so the JSONB write cannot abort (phaze-qj9e).

        A UTF-16LE companion decodes to text riddled with U+0000; the LLM's reasoning/proposed_filename
        can also carry \\u0000 escapes. PostgreSQL jsonb/text rejects NUL outright, aborting
        store_proposals for the WHOLE batch and poisoning every retry. All string sinks are sanitized.
        """
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, store_proposals

        session = AsyncMock()
        file_id = str(uuid.uuid4())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        session.execute.return_value = mock_result

        batch = BatchProposalResponse(
            proposals=[
                FileProposalResponse(
                    file_index=0,
                    proposed_filename="Artist\x00 - Track.mp3",
                    proposed_path="perf\x00ormances/A",
                    confidence=0.9,
                    artist="DJ\x00 Test",
                    reasoning="because\x00 reasons",
                )
            ]
        )
        # Companion-derived context riddled with NUL (the UTF-16LE .nfo shape).
        files_context = [{"companions": [{"content": "A\x00r\x00t\x00i\x00s\x00t"}]}]

        with patch("phaze.services.proposal.pg_insert") as mock_pg_insert:
            await store_proposals(session, [file_id], batch, files_context)
            row = mock_pg_insert.return_value.values.call_args.kwargs

        assert "\x00" not in row["proposed_filename"]
        assert "\x00" not in row["proposed_path"]
        assert "\x00" not in row["reason"]
        # context_used is deep-sanitized: no NUL survives in nested LLM/companion strings.
        import json

        assert "\x00" not in json.dumps(row["context_used"])


# ---------------------------------------------------------------------------
# load_companion_contents tests (Plan 02)
# ---------------------------------------------------------------------------


class TestLoadCompanionContents:
    """Tests for load_companion_contents function."""

    @pytest.mark.asyncio
    async def test_loads_and_cleans_companion_files(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        import uuid

        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        companion_path = tmp_path / "info.nfo"
        companion_path.write_text("Artist: DJ Test\nVenue: Club", encoding="utf-8")

        # Mock FileCompanion query result
        companion = MagicMock()
        companion.companion_id = uuid.uuid4()

        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        # Mock FileRecord query for companion
        companion_record = MagicMock()
        companion_record.original_filename = "info.nfo"
        companion_record.current_path = str(companion_path)

        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        result = await load_companion_contents(session, media_id, 3000)

        assert len(result) == 1
        assert result[0]["filename"] == "info.nfo"
        assert "Artist: DJ Test" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_skips_unreadable_files(self):
        from unittest.mock import AsyncMock, MagicMock
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

        result = await load_companion_contents(session, media_id, 3000)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_strips_nul_bytes_from_companion_content(self, tmp_path):
        """A UTF-16-decoded .nfo full of U+0000 is sanitized at the read boundary (phaze-qj9e)."""
        from unittest.mock import AsyncMock, MagicMock
        import uuid

        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        companion = MagicMock()
        companion.companion_id = uuid.uuid4()
        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        # errors="replace" leaves raw 0x00 intact on decode -- U+0000 is valid UTF-8.
        companion_path = tmp_path / "setinfo.nfo"
        companion_path.write_text("A\x00r\x00t\x00i\x00s\x00t\x00:\x00 \x00D\x00J", encoding="utf-8")

        companion_record = MagicMock()
        companion_record.original_filename = "set\x00info.nfo"
        companion_record.current_path = str(companion_path)
        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        result = await load_companion_contents(session, media_id, 3000)

        assert len(result) == 1
        assert "\x00" not in result[0]["content"]

    @pytest.mark.asyncio
    async def test_bounds_read_without_slurping_a_huge_companion_file(self, tmp_path):
        """phaze-cycw: the read is bounded at the source -- a companion far larger than
        max_chars must not be fully buffered into memory, and the result still respects
        clean_companion_content's max_chars truncation.
        """
        from unittest.mock import AsyncMock, MagicMock
        import uuid

        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        companion = MagicMock()
        companion.companion_id = uuid.uuid4()
        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        # Far larger than max_chars=100 -- a full read_text() would buffer all of this.
        huge_path = tmp_path / "huge.nfo"
        huge_path.write_text("x" * 1_000_000, encoding="utf-8")

        companion_record = MagicMock()
        companion_record.original_filename = "huge.nfo"
        companion_record.current_path = str(huge_path)
        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        result = await load_companion_contents(session, media_id, 100)

        assert len(result) == 1
        # clean_companion_content truncates to max_chars and appends the truncation marker.
        assert result[0]["content"].endswith("[...truncated]")
        assert len(result[0]["content"]) < 200

    @pytest.mark.asyncio
    async def test_read_companion_bounded_sync_offloaded_via_to_thread(self, tmp_path):
        """phaze-cycw: the bounded read must run off the event loop via asyncio.to_thread."""
        from unittest.mock import AsyncMock, MagicMock, patch
        import uuid

        from phaze.services import proposal as proposal_module
        from phaze.services.proposal import load_companion_contents

        session = AsyncMock()
        media_id = uuid.uuid4()

        companion = MagicMock()
        companion.companion_id = uuid.uuid4()
        mock_companions_result = MagicMock()
        mock_companions_result.scalars.return_value.all.return_value = [companion]

        companion_path = tmp_path / "info.nfo"
        companion_path.write_text("Artist: DJ Test", encoding="utf-8")

        companion_record = MagicMock()
        companion_record.original_filename = "info.nfo"
        companion_record.current_path = str(companion_path)
        mock_file_result = MagicMock()
        mock_file_result.scalar_one_or_none.return_value = companion_record

        session.execute.side_effect = [mock_companions_result, mock_file_result]

        with patch.object(proposal_module.asyncio, "to_thread", wraps=proposal_module.asyncio.to_thread) as mock_to_thread:
            result = await load_companion_contents(session, media_id, 3000)

        mock_to_thread.assert_called_once()
        assert mock_to_thread.call_args.args[0] is proposal_module._read_companion_bounded_sync
        assert len(result) == 1
        assert "\x00" not in result[0]["filename"]
        assert "Artist" in result[0]["content"]
