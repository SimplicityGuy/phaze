"""Proposal persistence scenarios moved from ``tests/review/services/test_proposal.py``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest


class TestStoreProposalsPath:
    """Tests for proposed_path handling in store_proposals."""

    @pytest.mark.asyncio
    async def test_persists_proposed_path(self):
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        from unittest.mock import patch

        from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, store_proposals

        session = AsyncMock()
        file_id = str(uuid.uuid4())
        file_record = MagicMock()
        file_record.state = "analyzed"

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
        from unittest.mock import patch

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
            row = mock_pg_insert.return_value.values.call_args.kwargs
            assert row["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_stores_context_used_with_metadata(self):
        from unittest.mock import patch

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
        from unittest.mock import patch

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
