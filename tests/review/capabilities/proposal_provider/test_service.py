"""Proposal provider scenarios moved from ``tests/review/services/test_proposal.py``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


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


class TestProposalServiceInit:
    """Tests for ProposalService constructor."""

    def test_stores_model_and_prompt_and_max_rpm(self):
        from phaze.services.proposal import ProposalService

        svc = ProposalService(model="test-model", prompt_template="Hello {files_json}", max_rpm=60)
        assert svc.model == "test-model"
        assert svc.prompt_template == "Hello {files_json}"
        assert svc.max_rpm == 60


class TestGenerateBatch:
    """Tests for ProposalService.generate_batch."""

    @pytest.mark.asyncio
    async def test_calls_acompletion_with_correct_args(self):
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        from unittest.mock import patch

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
