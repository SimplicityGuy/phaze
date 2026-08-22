"""Seam E6 (phaze-02v1s): the LLM -> ``BatchProposalResponse`` seam, driven from PROVIDER WIRE BYTES.

WHY THIS FILE EXISTS
--------------------
``TestGenerateBatch`` in ``test_proposal.py`` builds its fixture as
``BatchProposalResponse(proposals=[...]).model_dump_json()`` and hands it back to
``BatchProposalResponse.model_validate_json``. The consumer generates its own input, so the test
proves only that the model can parse what the model emitted -- the purest instance of the ADR-0012
rule 3 defect ("verify with the artifact's real consumer, not with the tool that produced it") in
the ``phaze-d2hgv.6`` seam inventory, where it is row E6.

WHAT THESE TESTS DO INSTEAD
---------------------------
They never construct the JSON string. They construct the **raw provider HTTP response body** and
let the **real, installed litellm** transform it, calling the real ``litellm.acompletion`` over an
``httpx.MockTransport``. The bytes that reach the parser are therefore produced by litellm's own
provider transformation code, not by phaze's pydantic model. Only the socket is faked.

That matters concretely, and not just in principle: two of the five failure modes behave
differently from what the bead predicted, and BOTH corrections come from litellm's transform rather
than from the JSON.

EVIDENTIARY CLASS -- read this before citing these tests (AC 1)
--------------------------------------------------------------
**No real completion was captured.** This environment has no ``ANTHROPIC_API_KEY`` /
``OPENAI_API_KEY``, so no live provider was reachable. The wire payloads below are hand-constructed
to the documented Anthropic Messages API and OpenAI Chat Completions response schemas.

So the chain is: *hand-built wire bytes* -> **real litellm 1.97.0 transform** -> **real
``ProposalService.generate_batch``** -> **real pydantic 2.13.4**. Three of the four links are the
production article; the first is not. That is strictly stronger than a ``model_dump_json()`` fixture
(which has zero production links) and strictly weaker than a captured live completion. Treat the
*shape* of each payload as the assumption under test and everything downstream of it as measured.

**Do not let a later summary upgrade that to "tested against real provider output".**

THE MEASURED TABLE (AC 3) -- BEFORE and AFTER the half-2 defences
------------------------------------------------------------------
"Before" was measured 2026-08-22 against the unmodified parser and is the finding the operator
decision on bead ``phaze-02v1s`` was taken on (that decision is quoted in full in
``services/proposal.py``); it is kept here as the historical record, not as current behaviour.
litellm 1.97.0 / pydantic 2.13.4 / openai 2.54.0 / httpx 0.28.1.
"Anthropic" is ``claude-sonnet-4-20250514``, the configured default (``config.llm_model``).
"OpenAI" is ``gpt-4o``, reachable by changing that one setting.

| # | Mode | BEFORE (both providers unless noted) | AFTER |
|---|------|--------------------------------------|-------|
| 1 | markdown fences | ValidationError (`json_invalid`) -- kills batch | **parsed**, fence stripped |
| 2 | prose preamble | ValidationError (`json_invalid`) -- kills batch | **parsed**, span extracted |
| 3 | `content=None` | ValidationError (**`json_type`**) -- kills batch | `MalformedCompletionError(mode="content_none")`, logged |
| 4 | empty `choices` | `litellm.InternalServerError` -- already handled | unchanged (litellm, not phaze) |
| 5a | truncation, Anthropic (missing fields) | ValidationError (`missing`) -- kills batch | **salvaged**, bad items discarded |
| 5b | truncation, OpenAI (invalid JSON) | ValidationError (`json_invalid`) -- kills batch | `MalformedCompletionError(mode="truncated")`, logged -- **NOT repaired** |

Mode 5b is deliberately still fatal: it is ``phaze-km2x6`` [P3], which does not block this bead and
is gated on reading the logs added here. See ``TestModeFiveBIsNotRepaired``.

TWO CORRECTIONS TO THE BEAD'S OWN PREMISE, both measured here:

* **``content=None`` raises ``pydantic.ValidationError``, NOT ``TypeError``.** The bead, the
  dispatcher's brief and inventory row E6 all state TypeError and draw the conclusion that it
  "does not even land in the same except clause". Against pydantic 2.13.4 it does:
  ``model_validate_json(None)`` raises ``ValidationError`` with error type ``json_type`` and message
  "JSON input should be string, bytes or bytearray". ``ValidationError`` subclasses ``ValueError``,
  and ``isinstance(exc, TypeError)`` is False -- so a test written to the bead's expectation would
  have FAILED, and the ``except TypeError`` guard it implied would never have fired.
  ``test_content_none_raises_validation_error_not_type_error`` pins both halves.

* **The empty-``choices`` list never reaches phaze's code at all.** litellm 1.97.0 raises
  ``litellm.InternalServerError`` ("provider returned a response with no 'choices'") inside
  ``acompletion``, so ``response.choices[0]`` is never evaluated and there is no ``IndexError``.
  This mode is already handled -- by the library, not by phaze -- and needs no guard. It is pinned
  anyway, because that guarantee lives entirely in a pinned dependency.

On the Anthropic path modes 1 and 2 arrive only via a **text-only** response. litellm converts
``response_format`` into a forced ``json_tool_call`` tool call for Anthropic models
(``AnthropicConfig.map_response_format_to_anthropic_tool`` + ``tool_choice``), and when that tool
call comes back it replaces ``message.content`` with ``json.dumps(args)`` -- clean JSON, no fence
possible. See ``TestWhyFenceStrippingLooksDeadOnTheConfiguredModel`` for what makes them reachable
anyway; the defence is not dead code.

BLAST RADIUS (CLAUDE.md rule 4, population measured -- AC 5)
------------------------------------------------------------
**This changes the parse path for every proposal phaze generates** -- one ``generate_batch`` call
per ``generate_proposals`` SAQ job (``tasks/proposal.py``), ``config.llm_batch_size`` files each,
default **10**, across the whole archive on every pipeline drain. There is no flag and no partial
rollout.

*What currently works that this could break:* well-formed completions, which are the overwhelming
majority. The defences are a LADDER -- rung 1 is the unmodified ``model_validate_json`` and every
later rung runs only after it has already raised -- so a valid completion cannot reach any new code.
*The tests that prove it still works:* ``TestWellFormedProviderResponse`` (both provider paths,
end-to-end through real litellm) and ``test_a_well_formed_completion_never_reaches_the_ladder``,
which asserts the fast path is taken by proving no extraction or salvage helper is consulted.

The second hazard is the opposite one: a defence that recovers too eagerly and returns a batch the
model never emitted. ``TestSalvageDiscardsNeverInfers`` is the guard on that, and the design rule
behind it is stated in ``_salvage_proposals``.

Before the change, a single malformed completion cost **5 LLM round trips** (``worker_max_retries``
= 4) and left all 10 files proposal-less until a re-click. After it, modes 1, 2 and 5a cost none of
that; modes 3 and 5b still do, and now say why in the log.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
import tomllib
from typing import Any, ClassVar
from unittest.mock import patch

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
import openai
import pydantic
import pytest
from structlog.testing import capture_logs


# litellm starts a per-event-loop background logging worker on every ``acompletion``. pytest-asyncio
# tears the loop down before that worker drains, so litellm's own teardown emits a "coroutine ...
# was never awaited" RuntimeWarning per call. It is an artifact of litellm's internals meeting a
# per-test event loop, not of anything under test, and there is no public knob to disable it.
pytestmark = pytest.mark.filterwarnings("ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning")


# The configured default (``config.llm_model``) and the most likely alternative. Both are named
# explicitly because litellm's transform -- and therefore several of the verdicts above --
# differs between them.
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
OPENAI_MODEL = "gpt-4o"

# One valid proposal, as a plain dict. NEVER built from BatchProposalResponse: that is the whole
# defect this file exists to close. It is embedded into provider wire payloads below and only ever
# reaches the parser through litellm.
VALID_PROPOSALS: dict[str, Any] = {
    "proposals": [
        {
            "file_index": 0,
            "proposed_filename": "Artist - Event 2024 - Set (2024).mp3",
            "proposed_path": "Live/Artist/2024",
            "confidence": 0.91,
            "artist": "Artist",
            "event_name": "Event 2024",
            "reasoning": "Filename and tags agree on artist and event.",
        }
    ]
}
VALID_JSON = json.dumps(VALID_PROPOSALS)


# ---------------------------------------------------------------------------
# Provider wire payloads -- the Anthropic Messages API and OpenAI Chat Completions
# response schemas, hand-constructed (see EVIDENTIARY CLASS in the module docstring).
# ---------------------------------------------------------------------------


def anthropic_response(content: list[dict[str, Any]], *, stop_reason: str = "tool_use") -> dict[str, Any]:
    """A raw Anthropic ``/v1/messages`` response body."""
    return {
        "id": "msg_01SeamE6",
        "type": "message",
        "role": "assistant",
        "model": ANTHROPIC_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 512, "output_tokens": 128},
    }


def anthropic_tool_use(payload: dict[str, Any], *, stop_reason: str = "tool_use") -> dict[str, Any]:
    """The HAPPY provider shape for ``response_format`` on Anthropic: a forced ``json_tool_call``.

    litellm names the tool ``json_tool_call`` and forces it via ``tool_choice``; on the way back it
    unwraps ``input["values"]`` into ``message.content``. Reproducing that name and nesting is what
    makes this a provider payload rather than a phaze one.
    """
    return anthropic_response(
        [{"type": "tool_use", "id": "toolu_01SeamE6", "name": "json_tool_call", "input": {"values": payload}}],
        stop_reason=stop_reason,
    )


def anthropic_text(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    """A TEXT-only Anthropic response -- the shape that reopens fences and preambles."""
    return anthropic_response([{"type": "text", "text": text}], stop_reason=stop_reason)


def openai_response(choices: list[dict[str, Any]]) -> dict[str, Any]:
    """A raw OpenAI ``/v1/chat/completions`` response body."""
    return {
        "id": "chatcmpl-SeamE6",
        "object": "chat.completion",
        "created": 1_755_820_800,
        "model": OPENAI_MODEL,
        "choices": choices,
        "usage": {"prompt_tokens": 512, "completion_tokens": 128, "total_tokens": 640},
    }


def openai_message(content: str | None, *, finish_reason: str = "stop") -> dict[str, Any]:
    return openai_response([{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}])


# ---------------------------------------------------------------------------
# Driving the REAL litellm over a mocked socket
# ---------------------------------------------------------------------------


def _mock_transport(payload: dict[str, Any], status_code: int = 200) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def _client_for(model: str, payload: dict[str, Any]) -> Any:
    """The provider client litellm will use, wired to a transport that returns *payload*.

    Two client types, because litellm reaches the two providers through different stacks: Anthropic
    goes through litellm's own ``AsyncHTTPHandler``, OpenAI through the ``openai`` SDK. Both accept
    an httpx transport, so in each case everything above the socket is the production code path.
    """
    transport = _mock_transport(payload)
    if model == OPENAI_MODEL:
        return openai.AsyncOpenAI(api_key="sk-test-not-a-real-key", http_client=httpx.AsyncClient(transport=transport))
    handler = AsyncHTTPHandler()
    handler.client = httpx.AsyncClient(transport=transport)
    return handler


async def call_generate_batch(model: str, payload: dict[str, Any]) -> Any:
    """Run ``ProposalService.generate_batch`` against a provider that returns *payload*.

    ``acompletion`` is patched only to bind ``client=`` -- it remains the real
    ``litellm.acompletion``, so litellm's request building, provider routing, response
    transformation and error mapping all execute exactly as in production.
    """
    from phaze.services.proposal import ProposalService

    service = ProposalService(model=model, prompt_template="Files:\n{files_json}", max_rpm=30)
    bound = functools.partial(litellm.acompletion, client=_client_for(model, payload))
    with patch("phaze.services.proposal.acompletion", bound):
        return await service.generate_batch([{"index": 0, "original_filename": "track.mp3"}])


@pytest.fixture(autouse=True)
def _provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """litellm refuses to build a request without a key, and the transport is mocked anyway.

    These are syntactically valid, semantically meaningless placeholders -- no request leaves the
    process (every call goes through ``httpx.MockTransport``).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


def _error_types(exc: pydantic.ValidationError) -> list[str]:
    return [error["type"] for error in exc.errors()]


def _parse_modes(captured: list[dict[str, Any]]) -> list[str]:
    """The ``parse_mode`` of every log event that carried one -- the field phaze-km2x6 counts."""
    return [event["parse_mode"] for event in captured if "parse_mode" in event]


# ---------------------------------------------------------------------------
# Baseline: the happy provider shape, on both paths
# ---------------------------------------------------------------------------


class TestWellFormedProviderResponse:
    """The control case. Without it, every failing assertion below could be a broken harness."""

    async def test_anthropic_forced_tool_call_parses(self) -> None:
        result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(VALID_PROPOSALS))

        assert len(result.proposals) == 1
        assert result.proposals[0].proposed_filename == "Artist - Event 2024 - Set (2024).mp3"
        assert result.proposals[0].confidence == pytest.approx(0.91)

    async def test_openai_json_content_parses(self) -> None:
        result = await call_generate_batch(OPENAI_MODEL, openai_message(VALID_JSON))

        assert len(result.proposals) == 1
        assert result.proposals[0].artist == "Artist"

    async def test_a_well_formed_completion_never_reaches_the_ladder(self) -> None:
        """The blast-radius guard: the fast path must stay the unmodified fast path.

        Asserted by proving the recovery helpers are never consulted, rather than by observing that
        the result happens to be right -- a defence that silently ran and happened to agree would
        pass the weaker check while still having changed the path for every proposal phaze makes.
        """
        from phaze.services import proposal as module

        with (
            patch.object(module, "_extract_json_span", side_effect=AssertionError("fast path must not extract")) as extract,
            patch.object(module, "_salvage_proposals", side_effect=AssertionError("fast path must not salvage")) as salvage,
        ):
            result = await call_generate_batch(OPENAI_MODEL, openai_message(VALID_JSON))

        assert len(result.proposals) == 1
        assert extract.call_count == 0
        assert salvage.call_count == 0

    async def test_a_clean_batch_is_not_marked_salvaged(self) -> None:
        from phaze.services.proposal import SalvagedBatchProposalResponse

        result = await call_generate_batch(OPENAI_MODEL, openai_message(VALID_JSON))

        assert not isinstance(result, SalvagedBatchProposalResponse)

    async def test_the_fixture_never_round_trips_through_the_consumer(self) -> None:
        """Guard on the guard: the bytes litellm hands the parser must not be phaze's own dump.

        This is what row E6 is about, so it is asserted rather than left to convention -- if someone
        later "simplifies" ``VALID_PROPOSALS`` into ``BatchProposalResponse(...).model_dump_json()``
        the whole file silently reverts to the defect it was written to close.
        """
        from phaze.services.proposal import BatchProposalResponse

        consumer_generated = BatchProposalResponse.model_validate(VALID_PROPOSALS).model_dump_json()

        assert isinstance(VALID_PROPOSALS, dict)
        assert consumer_generated != VALID_JSON


# ---------------------------------------------------------------------------
# Mode 1 -- markdown fences (now defended)
# ---------------------------------------------------------------------------


class TestMarkdownFences:
    """```json ... ``` around the payload. The single most common LLM JSON deviation."""

    FENCED = f"```json\n{VALID_JSON}\n```"

    async def test_anthropic_text_only_fenced_response_is_recovered(self) -> None:
        result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_text(self.FENCED))

        assert len(result.proposals) == 1
        assert result.proposals[0].proposed_filename == "Artist - Event 2024 - Set (2024).mp3"

    async def test_openai_fenced_response_is_recovered(self) -> None:
        result = await call_generate_batch(OPENAI_MODEL, openai_message(self.FENCED))

        assert len(result.proposals) == 1

    async def test_bare_fence_without_a_language_tag_is_recovered(self) -> None:
        """``` with no ``json`` tag -- the other half of the fence family."""
        result = await call_generate_batch(OPENAI_MODEL, openai_message(f"```\n{VALID_JSON}\n```"))

        assert len(result.proposals) == 1

    async def test_recovery_is_logged_as_fenced_not_silently(self) -> None:
        """A recovered batch is still an abnormal completion and must leave a trace.

        Recovering silently would hide a prompt or model problem behind a green pipeline, which is
        the objection the "fail loudly" option was built on -- the defence answers it by recovering
        AND reporting, not by recovering quietly.
        """
        with capture_logs() as captured:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.FENCED))

        assert "fenced" in _parse_modes(captured)

    async def test_a_fence_is_never_recovered_by_inventing_content(self) -> None:
        """Fence stripping is substring selection, so a fence around GARBAGE stays fatal."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message("```json\nnot json at all\n```"))


# ---------------------------------------------------------------------------
# Mode 2 -- prose preamble (now defended)
# ---------------------------------------------------------------------------


class TestProsePreamble:
    """A conversational lead-in before the JSON -- "Here is the JSON you requested:" and friends."""

    PREAMBLED = f"Here is the JSON you requested:\n\n{VALID_JSON}"

    async def test_anthropic_text_only_preamble_is_recovered(self) -> None:
        result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_text(self.PREAMBLED))

        assert len(result.proposals) == 1

    async def test_openai_preamble_is_recovered(self) -> None:
        result = await call_generate_batch(OPENAI_MODEL, openai_message(self.PREAMBLED))

        assert len(result.proposals) == 1

    async def test_trailing_prose_after_valid_json_is_recovered(self) -> None:
        """The mirror image -- valid JSON followed by commentary."""
        result = await call_generate_batch(OPENAI_MODEL, openai_message(f"{VALID_JSON}\n\nLet me know if you would like these adjusted."))

        assert len(result.proposals) == 1

    async def test_recovery_is_logged_as_preamble(self) -> None:
        with capture_logs() as captured:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.PREAMBLED))

        assert "preamble" in _parse_modes(captured)


class TestWhyFenceStrippingLooksDeadOnTheConfiguredModel:
    """Why modes 1/2 are near-unreachable on today's model, so nobody deletes the defence.

    Measured in half 1: for Anthropic models litellm turns ``response_format`` into a FORCED
    ``json_tool_call`` and unwraps the tool arguments into ``message.content`` as
    ``json.dumps(args)``. A fence cannot survive that, so on ``claude-sonnet-4-20250514`` -- the
    configured default -- the stripping rung will essentially never fire.

    Three things reopen it, and each is ordinary rather than exotic:

    1. a **refusal or a safety stop**, which returns a text block and no tool call;
    2. an **early stop** before the tool block is emitted, same shape;
    3. a **model switch** -- ``config.llm_model`` is one setting, and every non-Anthropic path
       carries raw string content where a fence survives intact.

    The test below pins mechanism (1)/(2): a text-only Anthropic reply really does arrive with the
    fence intact, so the defence has live work to do on the configured model.
    """

    async def test_a_text_only_anthropic_reply_delivers_the_fence_intact(self) -> None:
        with capture_logs() as captured:
            result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_text(f"```json\n{VALID_JSON}\n```"))

        assert len(result.proposals) == 1
        assert "fenced" in _parse_modes(captured)

    async def test_the_forced_tool_call_path_never_carries_a_fence(self) -> None:
        """The complement: on the tool-call path the content is already clean, so no rung fires."""
        with capture_logs() as captured:
            result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(VALID_PROPOSALS))

        assert len(result.proposals) == 1
        assert _parse_modes(captured) == []


# ---------------------------------------------------------------------------
# Mode 3 -- content is None (now a legible error)
# ---------------------------------------------------------------------------


class TestContentIsNone:
    """The mode whose stated exception type was wrong. See the module docstring's corrections."""

    async def test_content_none_raises_validation_error_not_type_error(self) -> None:
        """The TypeError claim was a PROPAGATED INFERENCE, and measurement refuted it.

        "``content=None`` raises TypeError, not ValidationError -- a different except clause
        entirely" travelled from the ``phaze-d2hgv.6`` seam inventory into the phaze-02v1s bead
        description and from there into the dispatcher's brief, gaining apparent authority at every
        hop, and nobody ran it. Measured against pydantic 2.13.4 it is false:
        ``model_validate_json(None)`` raises ``pydantic.ValidationError`` with error type
        ``json_type``, which subclasses ``ValueError`` and is NOT a ``TypeError``.

        Both halves are asserted, and the negative half is the load-bearing one -- an ``except
        TypeError`` guard written to the inventory's claim would never have fired, and this test is
        what stands between the next reader and repeating the mistake.

        This asserts the raw pydantic behaviour deliberately, not phaze's wrapper: it is the
        DEPENDENCY's contract that was mis-stated, so a pydantic upgrade that reintroduced TypeError
        must fail here even though ``_parse_completion`` no longer relies on the distinction.
        """
        from phaze.services.proposal import BatchProposalResponse

        with pytest.raises(pydantic.ValidationError) as exc_info:
            BatchProposalResponse.model_validate_json(None)  # type: ignore[arg-type]

        assert _error_types(exc_info.value) == ["json_type"]
        assert not isinstance(exc_info.value, TypeError)
        assert isinstance(exc_info.value, ValueError)

    async def test_anthropic_empty_content_array_raises_a_legible_error(self) -> None:
        """``max_tokens`` reached before any block was emitted.

        litellm builds ``content=merged_text or None``, so an empty ``content`` array with no tool
        call becomes ``None`` -- not ``""``.
        """
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_response([], stop_reason="max_tokens"))

        assert exc_info.value.mode == "content_none"

    async def test_openai_null_content_raises_a_legible_error(self) -> None:
        """``"content": null`` is in-contract for OpenAI (it is how a pure tool-call reply looks)."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(None))

        assert exc_info.value.mode == "content_none"

    async def test_empty_string_content_is_reported_as_its_own_mode(self) -> None:
        """``""`` is a different provider event from ``null`` and stays distinguishable in the log."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(""))

        assert exc_info.value.mode == "content_empty"

    async def test_absent_content_is_never_turned_into_an_empty_batch(self) -> None:
        """The silent-success hazard, asserted directly.

        Returning ``BatchProposalResponse(proposals=[])`` here would let the SAQ job report
        ``status: ok, count: 0`` and hide a broken prompt behind a green pipeline. It must raise.
        """
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message(None))

    async def test_the_failure_is_logged_with_the_mode(self) -> None:
        from phaze.services.proposal import MalformedCompletionError

        with capture_logs() as captured, pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message(None))

        assert "content_none" in _parse_modes(captured)


# ---------------------------------------------------------------------------
# Mode 4 -- empty choices list (handled by litellm, unchanged)
# ---------------------------------------------------------------------------


class TestEmptyChoices:
    """The one mode already handled -- by litellm, which is a pinned dependency, so pin it here."""

    async def test_openai_empty_choices_is_raised_by_litellm_before_phaze_indexes_it(self) -> None:
        """No ``IndexError``: litellm 1.97.0 refuses the response inside ``acompletion``.

        Asserted at the type litellm actually raises rather than at ``Exception``, because the whole
        value of this case is that ``response.choices[0]`` is never reached. If a future litellm
        stopped guarding this, the raise would become ``IndexError`` from ``proposal.py`` and this
        test would go red -- which is precisely the notification wanted.
        """
        with pytest.raises(litellm.InternalServerError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_response([]))

        assert "choices" in str(exc_info.value)

    async def test_the_guard_lives_in_litellm_not_in_phaze(self) -> None:
        """The complement: phaze's own code has no length check, so it depends on the pin holding."""
        import inspect

        from phaze.services.proposal import ProposalService

        source = inspect.getsource(ProposalService.generate_batch)
        assert "response.choices[0]" in source
        assert "len(response.choices)" not in source


# ---------------------------------------------------------------------------
# Mode 5a -- truncation costing FIELDS: salvaged
# ---------------------------------------------------------------------------


class TestModeFiveASalvage:
    """Anthropic's tool ``input`` is a parsed object, so truncation costs fields, not syntax."""

    PARTIAL_ITEM: ClassVar[dict[str, Any]] = {
        "file_index": 9,
        "proposed_filename": "Artist - Event 2024 - Truncated.mp3",
        "confidence": 0.4,
    }

    def _mixed_batch(self, good_count: int = 9) -> dict[str, Any]:
        good = VALID_PROPOSALS["proposals"][0]
        items: list[dict[str, Any]] = [{**good, "file_index": index} for index in range(good_count)]
        items.append(self.PARTIAL_ITEM)
        return {"proposals": items}

    async def test_anthropic_truncated_tool_input_keeps_the_complete_items(self) -> None:
        result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(self._mixed_batch(), stop_reason="max_tokens"))

        assert len(result.proposals) == 9
        assert [proposal.file_index for proposal in result.proposals] == list(range(9))

    async def test_the_batch_is_marked_salvaged_not_left_looking_clean(self) -> None:
        """A salvaged batch must be distinguishable from a clean one BY THE CALLER, not only in a log.

        This is the "discard, never infer" corollary: nine good proposals returned as though the
        model had emitted exactly nine is a plausible-looking result produced from a reply it never
        finished. ``store_proposals`` tolerates the gap, so nothing downstream errors -- which is
        exactly why the marking has to be structural.
        """
        from phaze.services.proposal import BatchProposalResponse, SalvagedBatchProposalResponse

        result = await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(self._mixed_batch(), stop_reason="max_tokens"))

        assert isinstance(result, SalvagedBatchProposalResponse)
        assert isinstance(result, BatchProposalResponse)
        assert result.discarded_positions == [9]

    async def test_salvage_is_logged_with_what_it_discarded(self) -> None:
        with capture_logs() as captured:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(self._mixed_batch(), stop_reason="max_tokens"))

        assert "item_invalid" in _parse_modes(captured)
        salvage_events = [event for event in captured if event.get("parse_mode") == "item_invalid"]
        assert salvage_events[0]["discarded_positions"] == [9]
        assert salvage_events[0]["kept"] == 9

    async def test_the_salvage_marker_does_not_change_the_request_schema(self) -> None:
        """``BatchProposalResponse`` doubles as the ``response_format`` schema sent to the provider.

        The salvage marker is a SUBCLASS for exactly this reason: a field added to the parent would
        alter the JSON schema in every prompt phaze sends -- a change to the LLM INPUT smuggled in
        under a change to output handling.
        """
        from phaze.services.proposal import BatchProposalResponse

        assert set(BatchProposalResponse.model_fields) == {"proposals"}


class TestSalvageDiscardsNeverInfers:
    """The guard against the failure mode that is worse than losing a proposal."""

    async def test_a_discarded_item_is_dropped_whole_never_defaulted(self) -> None:
        """No field of a bad item may reappear -- not defaulted, not carried from a sibling.

        The batch below has one complete proposal and one missing ``reasoning`` and ``confidence``.
        A salvage that filled those in would produce a proposal the operator could approve for a
        rename the model never actually justified.
        """
        good = VALID_PROPOSALS["proposals"][0]
        batch = {"proposals": [{**good, "file_index": 0}, {"file_index": 1, "proposed_filename": "Invented.mp3"}]}

        result = await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps(batch)))

        assert [proposal.file_index for proposal in result.proposals] == [0]
        assert "Invented.mp3" not in [proposal.proposed_filename for proposal in result.proposals]

    async def test_kept_items_are_byte_for_byte_what_the_model_emitted(self) -> None:
        """Salvage must not rewrite the survivors while removing their neighbour."""
        good = VALID_PROPOSALS["proposals"][0]
        batch = {"proposals": [{**good, "file_index": 0}, {"file_index": 1}]}

        result = await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps(batch)))

        kept = result.proposals[0]
        assert kept.proposed_filename == good["proposed_filename"]
        assert kept.proposed_path == good["proposed_path"]
        assert kept.confidence == pytest.approx(good["confidence"])
        assert kept.reasoning == good["reasoning"]
        assert kept.artist == good["artist"]

    async def test_an_all_bad_batch_raises_rather_than_returning_nothing(self) -> None:
        """Salvaging zero items is not a success and must not read as an empty batch."""
        from phaze.services.proposal import MalformedCompletionError

        batch = {"proposals": [{"file_index": 0}, {"file_index": 1}]}

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps(batch)))

        assert exc_info.value.mode == "schema_invalid"

    async def test_an_empty_proposals_list_is_left_alone_not_reported_as_salvage(self) -> None:
        """``{"proposals": []}`` is VALID output -- the model saying it proposes nothing.

        It parses on rung 1 and must never be relabelled as a salvage; mislabelling it would put a
        false ``item_invalid`` into the very log phaze-km2x6 is gated on counting.
        """
        from phaze.services.proposal import SalvagedBatchProposalResponse

        with capture_logs() as captured:
            result = await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps({"proposals": []})))

        assert result.proposals == []
        assert not isinstance(result, SalvagedBatchProposalResponse)
        assert _parse_modes(captured) == []


# ---------------------------------------------------------------------------
# Mode 5b -- truncation costing SYNTAX: deliberately NOT repaired (phaze-km2x6)
# ---------------------------------------------------------------------------


class TestModeFiveBIsNotRepaired:
    """Mode 5b stays fatal BY OPERATOR SCOPE, and this file is what keeps it that way.

    The operator's note, verbatim and entire: "let's create a new bead for 5b, bump that to P3, but
    don't block closure of this bead on that work." That bead is ``phaze-km2x6`` [P3].

    These cases assert the CURRENT behaviour so phaze-km2x6 has a baseline to change, and they are
    deliberately not weakened. Two reasons a partial repairer must not creep in here:

    * phaze-km2x6 is gated on reading these logs to answer "has 5b ever actually fired?". A
      repairer landed here would silently make that question unanswerable.
    * A wrong repair produces a plausible VALID document with WRONG content -- a silent bad proposal
      an operator might approve, which is the one outcome worse than losing the batch.
    """

    TRUNCATED = VALID_JSON[: len(VALID_JSON) // 2]

    async def test_openai_truncated_content_still_fails_the_batch(self) -> None:
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.TRUNCATED, finish_reason="length"))

        assert exc_info.value.mode == "truncated"

    async def test_the_underlying_pydantic_error_is_preserved_on_the_cause(self) -> None:
        """The wrapper adds a mode; it must not destroy the diagnosis underneath it."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.TRUNCATED, finish_reason="length"))

        cause = exc_info.value.__cause__
        assert isinstance(cause, pydantic.ValidationError)
        assert _error_types(cause) == ["json_invalid"]

    async def test_truncation_is_detected_without_a_finish_reason(self) -> None:
        """Not every provider reports ``finish_reason`` faithfully, so the EOF signal backs it up."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.TRUNCATED, finish_reason="stop"))

        assert exc_info.value.mode == "truncated"

    async def test_mode_5b_is_distinguishable_from_5a_and_from_a_fence_in_the_log(self) -> None:
        """phaze-km2x6's gating question must be answerable from the logs without guessing.

        The three modes are asserted TOGETHER rather than in separate tests, because the property
        that matters is that they differ from one another -- three passing tests that each accept
        the same string would not catch a collapse.
        """
        from phaze.services.proposal import MalformedCompletionError

        good = VALID_PROPOSALS["proposals"][0]
        mixed = {"proposals": [{**good, "file_index": 0}, {"file_index": 1}]}

        with capture_logs() as truncated_logs, pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message(self.TRUNCATED, finish_reason="length"))
        with capture_logs() as salvage_logs:
            await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps(mixed)))
        with capture_logs() as fence_logs:
            await call_generate_batch(OPENAI_MODEL, openai_message(f"```json\n{VALID_JSON}\n```"))

        modes = (_parse_modes(truncated_logs)[0], _parse_modes(salvage_logs)[0], _parse_modes(fence_logs)[0])

        assert modes == ("truncated", "item_invalid", "fenced")
        assert len(set(modes)) == 3

    async def test_the_log_carries_a_tail_preview_so_truncation_is_visible(self) -> None:
        """A head-only preview of a long reply cut at ``max_tokens`` looks identical to a healthy one."""
        from phaze.services.proposal import MalformedCompletionError

        long_truncated = json.dumps({"proposals": [VALID_PROPOSALS["proposals"][0]] * 12})[:900]

        with capture_logs() as captured, pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message(long_truncated, finish_reason="length"))

        event = next(item for item in captured if item.get("parse_mode") == "truncated")
        assert event["content_preview"].endswith(long_truncated[-40:])
        assert event["content_len"] == len(long_truncated)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([VALID_PROPOSALS["proposals"][0]], id="array-of-objects"),
            pytest.param(["Artist - Event 2024 - Set (2024).mp3"], id="array-of-filenames"),
        ],
    )
    async def test_a_bare_array_instead_of_the_object_is_not_salvaged(self, payload: Any) -> None:
        """The model returning a LIST rather than ``{"proposals": [...]}`` -- a real schema slip.

        Salvage requires the outer document to be the shape it claims to be. Reaching into a bare
        array and treating it AS the proposals list would be inferring the model's intent, so it is
        refused: the batch fails and the log says ``schema_invalid``, not ``item_invalid``.

        Both array shapes are exercised because they take DIFFERENT routes to the same refusal. The
        array of objects contains braces, so span extraction pulls out the first object and salvage
        rejects it for having no ``proposals`` key; the array of bare strings has no braces at all,
        so extraction declines and salvage rejects the top-level list itself.
        """
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps(payload)))

        assert exc_info.value.mode == "schema_invalid"

    async def test_a_proposals_key_that_is_not_a_list_is_not_salvaged(self) -> None:
        """``{"proposals": "..."}`` -- the right key, the wrong type. Nothing to iterate, so refuse."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps({"proposals": "one proposal, actually"})))

        assert exc_info.value.mode == "schema_invalid"

    async def test_an_empty_fence_recovers_nothing_and_stays_fatal(self) -> None:
        """A fence containing nothing at all -- extraction must decline rather than return ``""``."""
        from phaze.services.proposal import MalformedCompletionError

        with pytest.raises(MalformedCompletionError):
            await call_generate_batch(OPENAI_MODEL, openai_message("```json\n\n```"))

    async def test_no_json_repair_helper_exists_in_the_module(self) -> None:
        """A structural guard on the scope boundary, not a style preference.

        If someone adds a repairer while phaze-km2x6 is still open, this fails and forces the
        conversation about that bead's gating question rather than letting the capability arrive
        unannounced.
        """
        import inspect

        from phaze.services import proposal as module

        source = inspect.getsource(module)
        for forbidden in ("json_repair", "repair_json", "_repair_truncated"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# The pin the whole file rests on
# ---------------------------------------------------------------------------


def test_litellm_pin_is_unchanged() -> None:
    """AC 6, and load-bearing for mode 4.

    The ``>=1.97.0,<1.98.0`` pin is a supply-chain control (CLAUDE.md, March 2026 incident), and it
    is separately what makes "empty ``choices`` is already handled" true -- that guarantee lives in
    litellm, not in phaze. Widening the pin invalidates several of this file's verdicts, so it fails
    here rather than silently.
    """
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]

    assert "litellm>=1.97.0,<1.98.0" in dependencies
