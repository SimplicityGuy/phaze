"""Tests for `scripts/recover_operator_decisions.py` (phaze-d2hgv.4).

Two layers, matching the two claims the tool makes:

1. SCHEMA-FAITHFUL SYNTHETIC FIXTURES. Every fixture record below uses the exact field names and
   nesting observed by directly inspecting this machine's real `~/.claude/projects/*/*.jsonl`
   corpus while building the tool (see the module docstring's evidence for each exclusion category).
   These give deterministic, CI-safe coverage of the classification and pairing logic without
   depending on any particular machine's transcript history.
2. A REAL-TRANSCRIPT INTEGRATION TEST (`test_real_corpus_reproduces_the_three_regression_fixtures`).
   Layer 1 alone would repeat exactly the verification-fidelity mistake ADR-0012 documents: a parser
   that has only ever been checked against a hand-built stand-in for its own input format. This test
   runs the real `main()`/`recover()` against this developer's actual `~/.claude/projects`, if
   present, and asserts on the three fixtures the bead specifies verbatim -- the same evidence used
   to confirm the tool works, not a re-description of it. It is skipped, not failed, when the
   directory is absent (a fresh checkout, CI, another operator's machine): this tool is explicitly
   local-only and unversioned (see its own LIMITS_NOTICE), so "the transcripts aren't here" is an
   expected, non-failing outcome for anyone but the machine that produced them.

This file lives under `tests/shared/` alongside the other bare-`scripts/*.py` gates
(`test_coverage_floor.py`, `test_lint_template_attr_quotes.py`), which set the import pattern below.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


# tests/shared/test_recover_operator_decisions.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "recover_operator_decisions.py"

_REAL_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_HAS_REAL_TRANSCRIPTS = _REAL_PROJECTS_ROOT.is_dir()


def _load_module() -> ModuleType:
    """Import the real `scripts/recover_operator_decisions.py` as a module.

    `scripts/` is not an importable package, so load it from its file path -- same pattern as
    `tests/shared/test_coverage_floor.py`. Exercising the real module (not a copy) keeps these tests
    honest about the shipped tool's behaviour.
    """
    assert _SCRIPT.is_file(), f"recovery script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("recover_operator_decisions", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the script's dataclasses combine `from __future__ import
    # annotations` with plain (non-generic) field types, and `@dataclass` still resolves each
    # annotation string against `sys.modules[cls.__module__]` while checking for `ClassVar`/
    # `InitVar` -- an unregistered module fails that lookup at class-definition time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_session(tmp_path: Path, project: str, session_id: str, records: list[dict]) -> Path:
    project_dir = tmp_path / project
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --- Schema-faithful fixture builders -----------------------------------------------------------
# Field shapes below are copied from real records inspected on 2026-08-21 while building this tool.


def _ask_user_question_pair(
    session_id: str,
    ask_ts: str,
    answer_ts: str,
    questions: list[dict],
    answers: dict[str, str],
    tool_use_id: str = "toolu_test1",
) -> list[dict]:
    ask = {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": ask_ts,
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "AskUserQuestion", "input": {"questions": questions}}],
        },
    }
    summary = ", ".join(f'"{q["question"]}"="{answers.get(q["question"], "")}"' for q in questions)
    result = {
        "type": "user",
        "isSidechain": False,
        "timestamp": answer_ts,
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": f"Your questions have been answered: {summary}"}],
        },
        "toolUseResult": {"questions": questions, "answers": answers, "annotations": {}},
    }
    return [ask, result]


def _human_turn(session_id: str, ts: str, text: str, prompt_source: str = "typed") -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "isMeta": False,
        "timestamp": ts,
        "sessionId": session_id,
        "origin": {"kind": "human"},
        "promptSource": prompt_source,
        "message": {"role": "user", "content": text},
    }


def _task_notification(session_id: str, ts: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": ts,
        "sessionId": session_id,
        "origin": {"kind": "task-notification"},
        "promptSource": "system",
        "message": {"role": "user", "content": "<task-notification>\n<task-id>abc</task-id>\n<summary>Monitor event</summary>\n</task-notification>"},
    }


def _peer_message(session_id: str, ts: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": ts,
        "sessionId": session_id,
        "origin": {"kind": "peer"},
        "message": {"role": "user", "content": 'Another Claude session sent a message:\n<agent-message from="teammate">hi</agent-message>'},
    }


def _teammate_message_legacy(session_id: str, ts: str) -> dict:
    """No `origin` field -- the shape seen on older records in this corpus."""
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": ts,
        "sessionId": session_id,
        "message": {"role": "user", "content": '<teammate-message teammate_id="team-lead">\ndo the thing\n</teammate-message>'},
    }


def _security_review_prompt(session_id: str, ts: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": ts,
        "sessionId": session_id,
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": "Review this change for security vulnerabilities.\n\nChanged files (you may Read these):\n  - src/phaze/x.py",
        },
    }


def _meta_skill_load(session_id: str, ts: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "isMeta": True,
        "timestamp": ts,
        "sessionId": session_id,
        "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill: /some/path\n# Some Skill"}]},
    }


def _sidechain_human_shaped(session_id: str, ts: str, text: str) -> dict:
    """A record that WOULD pass the human-turn gate except for `isSidechain: true`."""
    return {
        "type": "user",
        "isSidechain": True,
        "isMeta": False,
        "timestamp": ts,
        "sessionId": session_id,
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }


def _human_turn_with_image(session_id: str, ts: str, text: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "isMeta": False,
        "timestamp": ts,
        "sessionId": session_id,
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "message": {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "..."}}, {"type": "text", "text": text}]},
    }


# --- Classification / noise filtering -----------------------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [_task_notification, _peer_message, _teammate_message_legacy, _security_review_prompt, _meta_skill_load],
)
def test_machine_noise_categories_are_excluded(builder: object) -> None:
    module = _load_module()
    record = builder("sess-1", "2026-01-01T00:00:00.000Z")  # type: ignore[operator]
    assert module._is_human_turn(record) is False


def test_sidechain_record_is_excluded_even_if_shaped_like_a_human_turn() -> None:
    module = _load_module()
    record = _sidechain_human_shaped("sess-1", "2026-01-01T00:00:00.000Z", "hello")
    assert module._is_human_turn(record) is False


def test_typed_queued_and_suggestion_accepted_all_count_as_human() -> None:
    """All three `promptSource` variants observed on real `origin.kind == "human"` records qualify."""
    module = _load_module()
    for source in ("typed", "queued", "suggestion_accepted"):
        record = _human_turn("sess-1", "2026-01-01T00:00:00.000Z", "hi", prompt_source=source)
        assert module._is_human_turn(record) is True


def test_human_turn_with_image_block_extracts_the_text_block() -> None:
    module = _load_module()
    record = _human_turn_with_image("sess-1", "2026-01-01T00:00:00.000Z", "please do the thing")
    assert module._is_human_turn(record) is True
    text = module._extract_text(record["message"]["content"])
    assert text == "please do the thing"


def test_image_only_turn_has_no_text_and_is_excluded() -> None:
    module = _load_module()
    record = {
        "type": "user",
        "isSidechain": False,
        "isMeta": False,
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "..."}}]},
    }
    assert module._is_human_turn(record) is False


# --- AskUserQuestion pairing ---------------------------------------------------------------------


def test_ask_user_question_pairs_via_structured_tool_use_result(tmp_path: Path) -> None:
    module = _load_module()
    questions = [
        {
            "question": "phaze-test: pick a format",
            "header": "Format",
            "multiSelect": False,
            "options": [{"label": "A (Recommended)", "description": "desc A"}, {"label": "B", "description": "desc B"}],
        }
    ]
    records = _ask_user_question_pair(
        "sess-1", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:30.000Z", questions, {"phaze-test: pick a format": "A (Recommended)"}
    )
    _write_session(tmp_path, "-proj-", "sess-1", records)

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    assert len(result.ask_user_question_events) == 1
    event = result.ask_user_question_events[0]
    assert event.session_id == "sess-1"
    assert event.questions[0].text == "phaze-test: pick a format"
    assert event.questions[0].options[0].label == "A (Recommended)"
    assert event.answers["phaze-test: pick a format"] == "A (Recommended)"


def test_ask_user_question_falls_back_to_summary_string_when_structured_result_absent(tmp_path: Path) -> None:
    """Older records may lack `toolUseResult.answers`; the summary-string parse must still work."""
    module = _load_module()
    questions = [{"question": "Q1?", "header": "H", "multiSelect": False, "options": [{"label": "Yes", "description": "d"}]}]
    ask = {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "sessionId": "sess-2",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x", "name": "AskUserQuestion", "input": {"questions": questions}}],
        },
    }
    result_record = {
        "type": "user",
        "isSidechain": False,
        "timestamp": "2026-01-01T00:00:10.000Z",
        "sessionId": "sess-2",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_x", "content": 'Your questions have been answered: "Q1?"="Yes". You can now continue.'}
            ],
        },
        # No toolUseResult at all.
    }
    _write_session(tmp_path, "-proj-", "sess-2", [ask, result_record])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    assert len(result.ask_user_question_events) == 1
    assert result.ask_user_question_events[0].answers["Q1?"] == "Yes"


def test_ask_user_question_asked_by_a_sidechain_is_excluded(tmp_path: Path) -> None:
    module = _load_module()
    questions = [{"question": "sub-agent Q?", "header": "H", "multiSelect": False, "options": []}]
    ask = {
        "type": "assistant",
        "isSidechain": True,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "sessionId": "sess-3",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_y", "name": "AskUserQuestion", "input": {"questions": questions}}],
        },
    }
    _write_session(tmp_path, "-proj-", "sess-3", [ask])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    assert result.ask_user_question_events == []


# --- Date-range filtering -------------------------------------------------------------------------


def test_date_range_is_inclusive_at_both_boundaries(tmp_path: Path) -> None:
    module = _load_module()
    turn = _human_turn("sess-4", "2026-01-01T00:00:00.000Z", "boundary turn")
    _write_session(tmp_path, "-proj-", "sess-4", [turn])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:00:00.000Z")

    assert len(result.human_turns) == 1


def test_date_range_excludes_turns_outside_the_window(tmp_path: Path) -> None:
    module = _load_module()
    turn = _human_turn("sess-5", "2026-01-01T00:05:00.000Z", "outside")
    _write_session(tmp_path, "-proj-", "sess-5", [turn])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    assert result.human_turns == []


# --- Bead-id filtering: literal (default) vs. context (opt-in) -----------------------------------


def test_bead_id_literal_match_is_precise(tmp_path: Path) -> None:
    module = _load_module()
    questions = [{"question": "phaze-abc123: pick one", "header": "H", "multiSelect": False, "options": [{"label": "X", "description": "d"}]}]
    records = _ask_user_question_pair("sess-6", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:10.000Z", questions, {"phaze-abc123: pick one": "X"})
    # An unrelated question in a different tool_use, further down the same session.
    unrelated = _ask_user_question_pair(
        "sess-6",
        "2026-01-01T00:05:00.000Z",
        "2026-01-01T00:05:10.000Z",
        [{"question": "unrelated question", "header": "H", "multiSelect": False, "options": []}],
        {"unrelated question": "Y"},
        tool_use_id="toolu_test2",
    )
    _write_session(tmp_path, "-proj-", "sess-6", [*records, *unrelated])

    result = module.recover(tmp_path, bead_id="phaze-abc123")

    assert len(result.ask_user_question_events) == 1
    assert result.ask_user_question_events[0].questions[0].text == "phaze-abc123: pick one"
    assert result.ask_user_question_events[0].match_note is None


def test_bead_id_default_does_not_context_match(tmp_path: Path) -> None:
    """Default `--bead` (context_window=0) must NOT recover a turn that doesn't name the id."""
    module = _load_module()
    mention = _human_turn("sess-7", "2026-01-01T00:00:00.000Z", "Filed as phaze-xyz999, dispatching now.")
    reply = _human_turn("sess-7", "2026-01-01T00:00:05.000Z", "yes go ahead, no need for a special release")
    _write_session(tmp_path, "-proj-", "sess-7", [mention, reply])

    result = module.recover(tmp_path, bead_id="phaze-xyz999")

    # Only the turn that names the id literally comes back.
    assert len(result.human_turns) == 1
    assert result.human_turns[0].text.startswith("Filed as")


def test_bead_id_with_context_window_recovers_the_unlabelled_reply_and_labels_it(tmp_path: Path) -> None:
    module = _load_module()
    mention = _human_turn("sess-8", "2026-01-01T00:00:00.000Z", "Filed as phaze-xyz999, dispatching now.")
    reply = _human_turn("sess-8", "2026-01-01T00:00:05.000Z", "yes go ahead, no need for a special release")
    _write_session(tmp_path, "-proj-", "sess-8", [mention, reply])

    result = module.recover(tmp_path, bead_id="phaze-xyz999", context_window=5)

    texts = {t.text: t.match_note for t in result.human_turns}
    assert "yes go ahead, no need for a special release" in texts
    assert texts["yes go ahead, no need for a special release"] is not None
    assert "CONTEXT MATCH" in texts["yes go ahead, no need for a special release"]


def test_bead_id_context_window_does_not_reach_beyond_the_window(tmp_path: Path) -> None:
    module = _load_module()
    mention = _human_turn("sess-9", "2026-01-01T00:00:00.000Z", "Filed as phaze-far999.")
    filler = [_human_turn("sess-9", f"2026-01-01T00:01:{i:02d}.000Z", f"filler {i}") for i in range(10)]
    reply = _human_turn("sess-9", "2026-01-01T00:02:00.000Z", "yes go ahead")
    _write_session(tmp_path, "-proj-", "sess-9", [mention, *filler, reply])

    result = module.recover(tmp_path, bead_id="phaze-far999", context_window=2)

    # The mention itself is always recovered (literal match), and a 2-record backward window also
    # reaches the two filler turns immediately after it -- expected, since they ARE within 2 records
    # of the mention. What must NOT happen is the far-away reply, 11 records later, being pulled in.
    texts = {t.text for t in result.human_turns}
    assert "yes go ahead" not in texts
    assert "Filed as phaze-far999." in texts


# --- Rendering: honesty and no local-path leakage -------------------------------------------------


def test_null_result_message_never_claims_the_decision_was_never_made(tmp_path: Path) -> None:
    module = _load_module()
    result = module.recover(tmp_path, bead_id="phaze-does-not-exist")
    rendered = module.render_markdown(result)

    assert "No operator-decision evidence found" in rendered
    # The LIMITS notice explicitly disclaims the wrong reading ("...it NEVER means 'the decision
    # was never made'") -- that phrase is expected to appear, but only inside that negation, never
    # asserted on its own as a standalone claim.
    assert 'it never means "the decision was never made"' in rendered.lower()
    assert "no operator decision was made" not in rendered.lower()
    assert "never decided" not in rendered.lower()


def test_rendered_output_never_contains_the_scan_root_path(tmp_path: Path) -> None:
    """Output is anchored by session id + timestamp only -- never a filesystem path (no account name)."""
    module = _load_module()
    turn = _human_turn("sess-10", "2026-01-01T00:00:00.000Z", "a decision")
    _write_session(tmp_path, "-proj-", "sess-10", [turn])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:00:00.000Z")
    rendered = module.render_markdown(result)

    assert str(tmp_path) not in rendered
    assert "-proj-" not in rendered
    assert "sess-10" in rendered  # the session id itself IS the anchor, and is expected to appear


def test_render_json_round_trips_and_carries_match_note(tmp_path: Path) -> None:
    module = _load_module()
    mention = _human_turn("sess-11", "2026-01-01T00:00:00.000Z", "Filed as phaze-json1.")
    reply = _human_turn("sess-11", "2026-01-01T00:00:05.000Z", "go ahead")
    _write_session(tmp_path, "-proj-", "sess-11", [mention, reply])

    result = module.recover(tmp_path, bead_id="phaze-json1", context_window=3)
    payload = json.loads(module.render_json(result))

    assert payload["query"] == "bead phaze-json1"
    turns = {t["text"]: t["match_note"] for t in payload["human_turns"]}
    assert turns["Filed as phaze-json1."] is None
    assert turns["go ahead"] is not None


# --- Label vs. description attribution (the recurring citation defect this tool must not repeat) --
#
# An AskUserQuestion option is shown to the operator as a bolded LABEL plus a longer DESCRIPTION
# written by the assistant to explain it. The operator selects the LABEL; the DESCRIPTION is never
# the operator's words. Conflating the two -- quoting a description as if the operator said it --
# has produced real citation defects more than once in this repo's history (see CLAUDE.md's G2
# addendum and the module's ATTRIBUTION_NOTE). These tests hold the line mechanically.


def _labelled_ask_user_question_result(tmp_path: Path) -> ModuleType:
    module = _load_module()
    questions = [
        {
            "question": "phaze-attr1: pick one",
            "header": "Pick",
            "multiSelect": False,
            "options": [
                {"label": "Short label (Recommended)", "description": "A much longer explanation an operator never actually said."},
                {"label": "Other label", "description": "Another explanation, also never said by the operator."},
            ],
        }
    ]
    records = _ask_user_question_pair(
        "sess-attr", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:10.000Z", questions, {"phaze-attr1: pick one": "Short label (Recommended)"}
    )
    _write_session(tmp_path, "-proj-", "sess-attr", records)
    return module


def test_answer_field_is_the_label_only_never_the_description(tmp_path: Path) -> None:
    module = _labelled_ask_user_question_result(tmp_path)

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    event = result.ask_user_question_events[0]
    answer = event.answers[event.questions[0].text]
    assert answer == "Short label (Recommended)"
    assert "much longer explanation" not in answer


def test_rendered_markdown_separates_answer_label_from_option_descriptions(tmp_path: Path) -> None:
    module = _labelled_ask_user_question_result(tmp_path)
    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    rendered = module.render_markdown(result)

    # The ATTRIBUTION_NOTE is present because this result carries an AskUserQuestion event.
    assert module.ATTRIBUTION_NOTE in rendered
    # The ANSWER line names only the label, never the description text.
    answer_lines = [line for line in rendered.splitlines() if line.startswith("**ANSWER")]
    assert len(answer_lines) == 1
    assert "Short label (Recommended)" in answer_lines[0]
    assert "much longer explanation" not in answer_lines[0]
    # The description text appears only under "Options offered", never attributed as the answer.
    options_index = rendered.index("Options offered")
    description_index = rendered.index("A much longer explanation an operator never actually said.")
    answer_index = rendered.index(answer_lines[0])
    assert options_index < description_index < answer_index


def test_rendered_json_keeps_answer_separate_from_option_descriptions(tmp_path: Path) -> None:
    module = _labelled_ask_user_question_result(tmp_path)
    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:01:00.000Z")

    payload = json.loads(module.render_json(result))

    assert payload["attribution_note"] == module.ATTRIBUTION_NOTE
    question = payload["ask_user_question_events"][0]["questions"][0]
    assert question["answer"] == "Short label (Recommended)"
    assert question["options"][0]["label"] == "Short label (Recommended)"
    assert question["options"][0]["description"] != question["answer"]


def test_attribution_note_is_absent_when_no_ask_user_question_events(tmp_path: Path) -> None:
    """A pure human-typed-turn result carries no AskUserQuestion, so the note is not needed."""
    module = _load_module()
    turn = _human_turn("sess-noattr", "2026-01-01T00:00:00.000Z", "just a typed statement")
    _write_session(tmp_path, "-proj-", "sess-noattr", [turn])

    result = module.recover(tmp_path, since="2026-01-01T00:00:00.000Z", until="2026-01-01T00:00:00.000Z")
    rendered = module.render_markdown(result)
    payload = json.loads(module.render_json(result))

    assert module.ATTRIBUTION_NOTE not in rendered
    assert payload["attribution_note"] is None


# --- CLI argument validation -----------------------------------------------------------------------


def test_cli_requires_bead_or_date_range() -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module._parse_args([])


def test_cli_requires_since_and_until_together() -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module._parse_args(["--since", "2026-01-01T00:00:00Z"])


def test_cli_rejects_invalid_timestamp() -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module._parse_args(["--since", "not-a-timestamp", "--until", "2026-01-01T00:00:00Z"])


def test_cli_rejects_negative_context_window() -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module._parse_args(["--bead", "phaze-x", "--context-window", "-1"])


def test_cli_accepts_bead_only() -> None:
    module = _load_module()
    args = module._parse_args(["--bead", "phaze-x"])
    assert args.bead == "phaze-x"
    assert args.context_window == 0


def test_main_end_to_end_against_a_missing_projects_root_errors(tmp_path: Path) -> None:
    module = _load_module()
    missing = tmp_path / "does-not-exist"
    exit_code = module.main(["--bead", "phaze-x", "--projects-root", str(missing)])
    assert exit_code == 1


def test_main_end_to_end_prints_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()
    turn = _human_turn("sess-12", "2026-01-01T00:00:00.000Z", "a decision, end to end")
    _write_session(tmp_path, "-proj-", "sess-12", [turn])

    exit_code = module.main(
        [
            "--since",
            "2026-01-01T00:00:00.000Z",
            "--until",
            "2026-01-01T00:00:00.000Z",
            "--projects-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "a decision, end to end" in out


# --- Real-transcript integration: the actual bead fixtures ----------------------------------------


@pytest.mark.skipif(
    not _HAS_REAL_TRANSCRIPTS,
    reason="~/.claude/projects is absent on this machine -- this tool is local-only by design (see its LIMITS_NOTICE); skip rather than fail.",
)
class TestRealCorpusRegressionFixtures:
    """Reproduces the three fixtures phaze-d2hgv.4's acceptance criteria name verbatim.

    Run against THIS developer's real `~/.claude/projects` -- not a stand-in -- because a forensic
    tool over real transcripts that has only ever been checked against a synthetic corpus is exactly
    the verification-fidelity gap ADR-0012 (G3) documents: the real consumer of this tool's claims is
    the real transcript format, and only the real corpus can confirm the parser holds against it.
    """

    def test_phaze_3ea41_ask_user_question_is_recovered_with_all_options_and_the_answer(self) -> None:
        module = _load_module()
        result = module.recover(_REAL_PROJECTS_ROOT, since="2026-08-12T00:30:00Z", until="2026-08-12T00:32:00Z")

        matches = [e for e in result.ask_user_question_events if any("phaze-3ea41" in q.text for q in e.questions)]
        assert matches, "expected the phaze-3ea41 AskUserQuestion batch in this window"
        event = matches[0]
        assert event.ask_timestamp.startswith("2026-08-12T00:30:59")
        assert event.answer_timestamp is not None
        assert event.answer_timestamp.startswith("2026-08-12T00:31:36")

        by_text = {q.text: q for q in event.questions}
        format_q = next(q for q in event.questions if "which video containers" in q.text)
        assert {o.label for o in format_q.options} == {
            "Probe-based, any container (Recommended)",
            "Common concert formats",
            "mkv + avi only",
        }
        assert event.answers[format_q.text] == "Probe-based, any container (Recommended)"
        assert "where should audio extraction run" in " ".join(by_text)

    def test_phaze_kj8dl_pair_is_recovered_by_date_range(self) -> None:
        module = _load_module()
        result = module.recover(_REAL_PROJECTS_ROOT, since="2026-08-11T19:03:00Z", until="2026-08-11T19:05:00Z")

        matches = [e for e in result.ask_user_question_events if e.ask_timestamp.startswith("2026-08-11T19:03:30")]
        assert matches, "expected the phaze-kj8dl AskUserQuestion batch in this window"
        event = matches[0]
        assert event.answer_timestamp is not None
        assert event.answer_timestamp.startswith("2026-08-11T19:04:21")
        rollout_q = next(q for q in event.questions if "How should the one-time script feed them in" in q.text)
        assert event.answers[rollout_q.text] == "Enqueue all, let lanes drain (Recommended)"

    def test_phaze_6r39_free_text_statement_is_recovered_by_date_range(self) -> None:
        module = _load_module()
        result = module.recover(_REAL_PROJECTS_ROOT, since="2026-08-04T19:19:00Z", until="2026-08-04T19:20:00Z")

        matches = [t for t in result.human_turns if t.timestamp.startswith("2026-08-04T19:19:29")]
        assert matches, "expected the phaze-6r39 human-typed reply in this window"
        assert "we'll deploy it in the next release" in matches[0].text

    def test_real_scan_output_never_leaks_a_filesystem_path(self) -> None:
        """The account-name-free-output acceptance criterion, proven against the real corpus.

        `LIMITS_NOTICE` legitimately spells out the generic ``~/.claude/projects/*/*.jsonl``
        pattern -- that is documentation, not a leak, and carries no account name. What must never
        appear is an actual resolved path: this machine's home directory, or the literal
        ``/Users/`` account-name prefix that path uses.
        """
        module = _load_module()
        result = module.recover(_REAL_PROJECTS_ROOT, since="2026-08-12T00:30:00Z", until="2026-08-12T00:32:00Z")
        rendered = module.render_markdown(result)

        assert str(Path.home()) not in rendered
        assert "/Users/" not in rendered
