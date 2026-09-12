"""Proposal context scenarios moved from ``tests/review/services/test_proposal.py``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest


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

    def test_repairs_mojibake_in_original_filename(self):
        """phaze-x4ux: the ONE call site that must never carry garble into an LLM rename proposal.

        `FileRecord.original_filename` itself is untouched by this function (it stays the
        byte-faithful record) -- only the copy handed to the LLM context is repaired.
        """
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        file_rec.original_filename = "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"

        ctx = build_file_context(file_rec, None, [])

        assert ctx["original_filename"] == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven Väth - LIVE @ Timewarp 2003.mp3"
        # The FileRecord itself is never mutated.
        assert file_rec.original_filename == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"

    def test_no_op_on_already_clean_filename(self):
        from phaze.services.proposal import build_file_context

        file_rec = _make_file_record()
        ctx = build_file_context(file_rec, None, [])
        assert ctx["original_filename"] == "999999999-Live_At_Boiler_Room-WEB-2019.mp3"


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

    def test_llm_batch_size_rejects_zero(self):
        """phaze-ceuvd: batch_size is a range() step in get_proposal_pending_batches, so a
        non-positive value must fail validation (gt=0) at config-construction time rather than
        booting green and only detonating (ValueError: range() arg 3 must not be zero) when
        GENERATE ALL is clicked."""
        from pydantic import ValidationError

        from phaze.config import Settings

        with pytest.raises(ValidationError, match="llm_batch_size"):
            Settings(llm_batch_size=0)

    def test_llm_batch_size_rejects_negative(self):
        """phaze-ceuvd: a negative value used to silently no-op (empty range()), enqueuing zero
        batches while reporting success. It must now fail validation instead."""
        from pydantic import ValidationError

        from phaze.config import Settings

        with pytest.raises(ValidationError, match="llm_batch_size"):
            Settings(llm_batch_size=-5)

    def test_llm_max_companion_chars_default(self):
        from phaze.config import Settings

        s = Settings()
        assert s.llm_max_companion_chars == 3000


class TestLoadCompanionContents:
    """Tests for load_companion_contents -- the CONTROL-PLANE half (phaze-6bkk).

    The read itself moved to the owning agent. ``generate_proposals`` runs on the CONTROLLER worker,
    which docker-compose.yml documents as "fileless -- never touches SCAN_PATH" (DIST-01), so its
    ``open()`` on an agent-reported companion path could not succeed in any documented production
    topology. It failed INVISIBLY: a bare ``except OSError: continue`` treated a permanent,
    architectural failure exactly like one unreadable sidecar, so every proposal was silently built
    with an empty companion context.

    What is asserted here is the dispatch + the pure post-processing (cleaning, NUL sanitizing).
    Containment, the bounded read, and the off-loop offload are asserted on the agent side, in
    ``tests/review/tasks/test_companion_read.py``.
    """

    @staticmethod
    def _router(contents: list[dict[str, str]] | Exception) -> MagicMock:
        """A task-router double whose meta-lane queue ``apply`` returns ``contents`` (or raises)."""
        queue = MagicMock()
        queue.connect = AsyncMock()
        if isinstance(contents, Exception):
            queue.apply = AsyncMock(side_effect=contents)
        else:
            queue.apply = AsyncMock(return_value={"contents": contents})
        router = MagicMock()
        router.queue_for = MagicMock(return_value=queue)
        router.queue = queue
        return router

    @staticmethod
    def _session(companion_records: list[MagicMock]) -> AsyncMock:
        """A session double: one FileCompanion query, then ONE batched FileRecord query.

        phaze-p2p6u: load_companion_targets now resolves every companion's FileRecord with a
        single ``IN`` query instead of one SELECT per companion (io_in_loop fix), so this double
        mirrors two ``session.execute`` calls, not one-plus-N. Each companion mock's
        ``companion_id`` is wired to the matching record's ``id`` so the implementation's
        dict-by-id lookup finds it, same as the real FK relationship would.
        """
        session = AsyncMock()
        companions = []
        for rec in companion_records:
            companions.append(MagicMock(companion_id=rec.id))
        companions_result = MagicMock()
        companions_result.scalars.return_value.all.return_value = companions
        records_result = MagicMock()
        records_result.scalars.return_value.all.return_value = companion_records
        session.execute.side_effect = [companions_result, records_result]
        return session

    @staticmethod
    def _companion_record(filename: str, path: str, agent_id: str = "test-agent") -> MagicMock:
        rec = MagicMock()
        rec.id = uuid.uuid4()
        rec.original_filename = filename
        rec.current_path = path
        rec.agent_id = agent_id
        return rec

    @pytest.mark.asyncio
    async def test_dispatches_to_the_owning_agents_meta_lane(self):
        """The read is a ``read_companion_files`` job on the OWNING agent's meta lane."""
        from phaze.services.proposal import load_companion_contents

        rec = self._companion_record("info.nfo", "/data/music/<set-01>/info.nfo", agent_id="fileserver-02")
        router = self._router([{"filename": "info.nfo", "content": "Artist: DJ Test\nVenue: Club"}])

        result = await load_companion_contents(self._session([rec]), uuid.uuid4(), 3000, task_router=router)

        router.queue_for.assert_called_once_with("fileserver-02", "meta")
        task_name = router.queue.apply.await_args.args[0]
        kwargs = router.queue.apply.await_args.kwargs
        assert task_name == "read_companion_files"
        assert kwargs["companions"] == [{"filename": "info.nfo", "path": "/data/music/<set-01>/info.nfo"}]
        assert kwargs["max_chars"] == 3000
        # A bounded wait: companion text is enrichment, so one unresponsive file server must never
        # wedge a whole proposal batch.
        assert kwargs["timeout"] > 0

        assert len(result) == 1
        assert result[0]["filename"] == "info.nfo"
        assert "Artist: DJ Test" in result[0]["content"]

    @pytest.mark.asyncio
    async def test_cleans_and_truncates_the_returned_text(self):
        """``clean_companion_content`` still runs control-side -- it is pure, and this is where the
        value is persisted."""
        from phaze.services.proposal import load_companion_contents

        rec = self._companion_record("huge.nfo", "/data/music/<set-01>/huge.nfo")
        router = self._router([{"filename": "huge.nfo", "content": "x" * 1_000_000}])

        result = await load_companion_contents(self._session([rec]), uuid.uuid4(), 100, task_router=router)

        assert len(result) == 1
        assert result[0]["content"].endswith("[...truncated]")
        assert len(result[0]["content"]) < 200

    @pytest.mark.asyncio
    async def test_strips_nul_bytes_from_companion_content(self):
        """phaze-qj9e: NUL/lone-surrogate stripping stays control-side, at the persist boundary.

        PostgreSQL jsonb rejects U+0000 outright, which would abort store_proposals for the WHOLE
        batch and poison every retry with identical content.
        """
        from phaze.services.proposal import load_companion_contents

        rec = self._companion_record("set\x00info.nfo", "/data/music/<set-01>/setinfo.nfo")
        router = self._router([{"filename": "set\x00info.nfo", "content": "A\x00r\x00t\x00i\x00s\x00t"}])

        result = await load_companion_contents(self._session([rec]), uuid.uuid4(), 3000, task_router=router)

        assert len(result) == 1
        assert "\x00" not in result[0]["content"]
        assert "\x00" not in result[0]["filename"]

    @pytest.mark.asyncio
    async def test_offline_agent_degrades_to_no_companion_context(self):
        """An offline agent / saturated lane / timeout must degrade the proposal, never block it.

        Same net behavior as the pre-move ``except OSError: continue``, but the failure is LOGGED
        rather than silently indistinguishable from "this file has no sidecars".
        """
        from phaze.services.proposal import load_companion_contents

        rec = self._companion_record("info.nfo", "/data/music/<set-01>/info.nfo")
        router = self._router(TimeoutError("agent did not respond"))

        result = await load_companion_contents(self._session([rec]), uuid.uuid4(), 3000, task_router=router)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_task_router_returns_empty_without_dispatching(self):
        """No dispatcher available -> no companion context, and definitely no local ``open()``.

        This is the DIST-01 regression guard on the control side: the controller must never try to
        read the archive itself, however it is called.
        """
        from phaze.services.proposal import load_companion_contents

        rec = self._companion_record("info.nfo", "/data/music/<set-01>/info.nfo")

        assert await load_companion_contents(self._session([rec]), uuid.uuid4(), 3000) == []

    @pytest.mark.asyncio
    async def test_no_companions_dispatches_nothing(self):
        """A media file with no sidecars costs zero agent round-trips."""
        from phaze.services.proposal import load_companion_contents

        router = self._router([])

        assert await load_companion_contents(self._session([]), uuid.uuid4(), 3000, task_router=router) == []
        router.queue_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_groups_companions_by_owning_agent(self):
        """phaze-c9w9 affinity: each owner reads its OWN files -- a path only means anything on the
        mount it was reported from."""
        from phaze.services.proposal import load_companion_contents

        recs = [
            self._companion_record("a.nfo", "/data/music/a.nfo", agent_id="fileserver-01"),
            self._companion_record("b.nfo", "/data/music/b.nfo", agent_id="fileserver-02"),
        ]
        router = self._router([{"filename": "a.nfo", "content": "text"}])

        await load_companion_contents(self._session(recs), uuid.uuid4(), 3000, task_router=router)

        assert [c.args[0] for c in router.queue_for.call_args_list] == ["fileserver-01", "fileserver-02"]
