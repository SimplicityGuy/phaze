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
``httpx.MockTransport``. The bytes that reach ``model_validate_json`` are therefore produced by
litellm's own provider transformation code, not by phaze's pydantic model. Only the socket is
faked.

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

MEASURED BEHAVIOUR, 2026-08-22, BEFORE ANY BEHAVIOUR CHANGE (AC 3)
------------------------------------------------------------------
litellm 1.97.0 / pydantic 2.13.4 / openai 2.54.0 / httpx 0.28.1.
"Anthropic" is ``claude-sonnet-4-20250514``, the configured default (``config.llm_model``).
"OpenAI" is ``gpt-4o``, reachable by changing that one setting.

| # | Mode                     | Anthropic default path        | OpenAI path                   | Handled today? |
|---|--------------------------|-------------------------------|-------------------------------|----------------|
| 1 | markdown fences          | ValidationError (json_invalid)| ValidationError (json_invalid)| NO -- kills batch |
| 2 | prose preamble           | ValidationError (json_invalid)| ValidationError (json_invalid)| NO -- kills batch |
| 3 | ``content=None``         | ValidationError (**json_type**)| ValidationError (**json_type**)| NO -- kills batch |
| 4 | empty ``choices`` list   | not reachable (see below)     | **litellm.InternalServerError**| YES -- by litellm |
| 5 | truncation at max_tokens | ValidationError (missing)     | ValidationError (json_invalid)| NO -- kills batch |

TWO CORRECTIONS TO THE BEAD'S OWN PREMISE, both measured here:

* **``content=None`` raises ``pydantic.ValidationError``, NOT ``TypeError``.** The bead, the
  dispatcher's brief and inventory row E6 all state TypeError and draw the conclusion that it
  "does not even land in the same except clause". Against pydantic 2.13.4 it does:
  ``model_validate_json(None)`` raises ``ValidationError`` with error type ``json_type`` and message
  "JSON input should be string, bytes or bytearray". ``ValidationError`` subclasses ``ValueError``,
  and ``isinstance(exc, TypeError)`` is False -- so a test written to the bead's expectation would
  have FAILED. This is exactly the hazard AC 2 names, arriving from the opposite direction: the
  stated exception type was itself an unverified inference. ``test_content_none_raises_validation_error_not_type_error``
  pins both halves so a pydantic upgrade that reintroduces TypeError is caught.

* **The empty-``choices`` list never reaches phaze's code at all.** litellm 1.97.0 raises
  ``litellm.InternalServerError`` ("provider returned a response with no 'choices'") inside
  ``acompletion``, so ``response.choices[0]`` is never evaluated and there is no ``IndexError``.
  This mode is already handled -- by the library, not by phaze -- and needs no guard. It is pinned
  anyway, because that guarantee lives entirely in a pinned dependency.

On the Anthropic path modes 1 and 2 arrive only via a **text-only** response. litellm converts
``response_format`` into a forced ``json_tool_call`` tool call for Anthropic models
(``AnthropicConfig.map_response_format_to_anthropic_tool`` + ``tool_choice``), and when that tool
call comes back it replaces ``message.content`` with ``json.dumps(args)`` -- clean JSON, no fence
possible. A text-only reply (refusal, or a stop before the tool block) is what re-opens them. Mode 5
also splits by provider: Anthropic's tool ``input`` is a parsed object, so truncation yields
*syntactically valid but incomplete* JSON (missing required fields), whereas OpenAI's raw string
content truncates mid-token into *syntactically invalid* JSON. Both are covered.

BLAST RADIUS OF A SINGLE MALFORMED COMPLETION (measured from config, not adjectival)
-----------------------------------------------------------------------------------
``generate_batch`` is called once per ``generate_proposals`` SAQ job (``tasks/proposal.py:140``)
with ``config.llm_batch_size`` files, default **10**. Nothing between it and the job boundary
catches the exception, and ``generate_proposals`` carries no entry in ``_FUNCTION_JOB_POLICY``, so
it takes the default ``config.worker_max_retries`` = **4** -- **5 LLM round trips burned** per
malformed batch, then all 10 files are left with no proposal until the operator re-clicks.

WHAT IS DELIBERATELY NOT DONE HERE
----------------------------------
Nothing in ``src/phaze`` changes in this file's bead-half. Whether the fix is to strip fences, to
salvage per item, or to keep failing the batch loudly is a product decision reserved for the
operator (AC 4); these tests record today's behaviour so that conversation is concrete. Every
assertion below is therefore a CHARACTERISATION of current behaviour, not an endorsement of it --
when the operator's answer lands, the cases for the modes it changes are expected to be rewritten.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
import tomllib
from typing import Any
from unittest.mock import patch

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
import openai
import pydantic
import pytest


# litellm starts a per-event-loop background logging worker on every ``acompletion``. pytest-asyncio
# tears the loop down before that worker drains, so litellm's own teardown emits a "coroutine ...
# was never awaited" RuntimeWarning per call. It is an artifact of litellm's internals meeting a
# per-test event loop, not of anything under test, and there is no public knob to disable it.
pytestmark = pytest.mark.filterwarnings("ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning")


# The configured default (``config.llm_model``) and the most likely alternative. Both are named
# explicitly because litellm's transform -- and therefore three of the five verdicts above --
# differs between them.
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
OPENAI_MODEL = "gpt-4o"

# One valid proposal, as a plain dict. NEVER built from BatchProposalResponse: that is the whole
# defect this file exists to close. It is embedded into provider wire payloads below and only ever
# reaches the model through litellm.
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
# Mode 1 -- markdown fences
# ---------------------------------------------------------------------------


class TestMarkdownFences:
    """```json ... ``` around the payload. The single most common LLM JSON deviation."""

    FENCED = f"```json\n{VALID_JSON}\n```"

    async def test_anthropic_text_only_fenced_response_fails_the_whole_batch(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_text(self.FENCED))

        assert _error_types(exc_info.value) == ["json_invalid"]
        # There is no fence-stripping anywhere on this path: the backticks reach pydantic verbatim.
        assert "```" in str(exc_info.value)

    async def test_openai_fenced_response_fails_the_whole_batch(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.FENCED))

        assert _error_types(exc_info.value) == ["json_invalid"]

    async def test_bare_fence_without_a_language_tag_also_fails(self) -> None:
        """``` with no ``json`` tag -- the other half of the fence family, equally undefended."""
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(f"```\n{VALID_JSON}\n```"))

        assert _error_types(exc_info.value) == ["json_invalid"]


# ---------------------------------------------------------------------------
# Mode 2 -- prose preamble
# ---------------------------------------------------------------------------


class TestProsePreamble:
    """A conversational lead-in before the JSON -- "Here is the JSON you requested:" and friends."""

    PREAMBLED = f"Here is the JSON you requested:\n\n{VALID_JSON}"

    async def test_anthropic_text_only_preamble_fails_the_whole_batch(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_text(self.PREAMBLED))

        assert _error_types(exc_info.value) == ["json_invalid"]

    async def test_openai_preamble_fails_the_whole_batch(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(self.PREAMBLED))

        assert _error_types(exc_info.value) == ["json_invalid"]

    async def test_trailing_prose_after_valid_json_also_fails(self) -> None:
        """The mirror image -- valid JSON followed by commentary. Also unparsed, also fatal."""
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(f"{VALID_JSON}\n\nLet me know if you would like these adjusted."))

        assert _error_types(exc_info.value) == ["json_invalid"]


# ---------------------------------------------------------------------------
# Mode 3 -- content is None
# ---------------------------------------------------------------------------


class TestContentIsNone:
    """The mode whose stated exception type was wrong. See the module docstring's corrections."""

    async def test_content_none_raises_validation_error_not_type_error(self) -> None:
        """AC 2, and the correction: it is ``ValidationError``/``json_type``, NOT ``TypeError``.

        Both halves are asserted. The negative half is the load-bearing one -- the bead's premise
        was that ``content=None`` lands in a different ``except`` clause from a malformed-JSON
        failure, and it does not: both are ``ValidationError``, both are ``ValueError``. A guard
        written against ``TypeError`` would never fire.
        """
        from phaze.services.proposal import BatchProposalResponse

        with pytest.raises(pydantic.ValidationError) as exc_info:
            BatchProposalResponse.model_validate_json(None)  # type: ignore[arg-type]

        assert _error_types(exc_info.value) == ["json_type"]
        assert not isinstance(exc_info.value, TypeError)
        assert isinstance(exc_info.value, ValueError)

    async def test_anthropic_empty_content_array_yields_none_and_fails(self) -> None:
        """``max_tokens`` reached before any block was emitted.

        litellm builds ``content=merged_text or None`` (``AnthropicConfig.transform_parsed_response``),
        so an empty ``content`` array with no tool call becomes ``None`` -- not ``""``.
        """
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_response([], stop_reason="max_tokens"))

        assert _error_types(exc_info.value) == ["json_type"]

    async def test_openai_null_content_fails(self) -> None:
        """``"content": null`` is in-contract for OpenAI (it is how a pure tool-call reply looks)."""
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(None))

        assert _error_types(exc_info.value) == ["json_type"]

    async def test_empty_string_content_fails_differently_from_none(self) -> None:
        """``""`` is a THIRD error type (``json_invalid``), so a guard keyed on one misses the other."""
        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(""))

        assert _error_types(exc_info.value) == ["json_invalid"]


# ---------------------------------------------------------------------------
# Mode 4 -- empty choices list
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
        assert "response.choices[0].message.content" in source
        assert "len(response.choices)" not in source


# ---------------------------------------------------------------------------
# Mode 5 -- truncation at max_tokens
# ---------------------------------------------------------------------------


class TestTruncationAtMaxTokens:
    """Truncation splits by provider: incomplete-but-valid JSON vs syntactically invalid JSON."""

    async def test_openai_truncated_content_is_syntactically_invalid_json(self) -> None:
        """A raw string cut mid-token. ``finish_reason`` says ``length``; nothing reads it."""
        truncated = VALID_JSON[: len(VALID_JSON) // 2]

        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(truncated, finish_reason="length"))

        assert _error_types(exc_info.value) == ["json_invalid"]

    async def test_anthropic_truncated_tool_input_is_valid_json_but_missing_fields(self) -> None:
        """The subtler half: Anthropic's tool ``input`` is a parsed object, so it re-serialises clean.

        Truncation there costs FIELDS, not syntax -- the failure is ``missing``, not ``json_invalid``.
        A fence-stripping or json-repair fix would not touch this shape at all, which is why the two
        halves of mode 5 are separate cases.
        """
        partial = {"proposals": [{"file_index": 0, "proposed_filename": "Artist - Event 2024 - Set"}]}

        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(ANTHROPIC_MODEL, anthropic_tool_use(partial, stop_reason="max_tokens"))

        assert set(_error_types(exc_info.value)) == {"missing"}
        assert {tuple(error["loc"]) for error in exc_info.value.errors()} == {
            ("proposals", 0, "confidence"),
            ("proposals", 0, "reasoning"),
        }

    async def test_one_malformed_item_discards_every_good_item_in_the_batch(self) -> None:
        """The blast radius, asserted rather than described.

        Nine well-formed proposals and one missing ``reasoning``: the exception carries a single
        error against item 9, and ``generate_batch`` returns nothing at all -- there is no per-item
        salvage, so the nine survivors are lost with it. ``llm_batch_size`` defaults to 10, so this
        is the production batch shape, not a contrived one.
        """
        good = VALID_PROPOSALS["proposals"][0]
        items = [{**good, "file_index": index} for index in range(9)]
        items.append({"file_index": 9, "proposed_filename": "Artist - Event 2024 - Truncated.mp3", "confidence": 0.4})

        with pytest.raises(pydantic.ValidationError) as exc_info:
            await call_generate_batch(OPENAI_MODEL, openai_message(json.dumps({"proposals": items})))

        assert [tuple(error["loc"]) for error in exc_info.value.errors()] == [("proposals", 9, "reasoning")]


# ---------------------------------------------------------------------------
# The pin the whole file rests on
# ---------------------------------------------------------------------------


def test_litellm_pin_is_unchanged() -> None:
    """AC 6, and load-bearing for mode 4.

    The ``>=1.97.0,<1.98.0`` pin is a supply-chain control (CLAUDE.md, March 2026 incident), and it
    is separately what makes "empty ``choices`` is already handled" true -- that guarantee lives in
    litellm, not in phaze. Widening the pin invalidates half this file's verdicts, so it fails here
    rather than silently.
    """
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]

    assert "litellm>=1.97.0,<1.98.0" in dependencies
