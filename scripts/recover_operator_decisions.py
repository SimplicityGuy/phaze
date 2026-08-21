"""Recover operator-decision provenance from local Claude Code session transcripts (phaze-d2hgv.4).

WHY THIS EXISTS. ADR-0012 section 5 swept the tree for "operator decision" claims and reported nine
as "not traceable to any recorded answer". Six of those nine WERE traceable -- the 2026-08-20
archaeology that corrected the ADR found them in one session's own transcript in about an hour of
ad-hoc `grep`/`jq`. The blind spot is specific and non-obvious: `AskUserQuestion` is recorded as an
assistant `tool_use` carrying the question and every option, and the operator's selection as the
matching `tool_result`. Neither is a `user` turn in the ordinary sense, so both are invisible to the
searches anyone would naturally run -- grepping bead comments, grepping user messages, or reading the
bead itself. This script closes that blind spot mechanically, so the next claim can be cited from the
primary record when it is written, not reconstructed three weeks later.

Referenced from CLAUDE.md's guardrail G2 ("Operator decision" is a citation, not an emphasis
marker) -- that is where an agent writing an attribution will actually be looking.

WHAT IT IS, AND WHAT IT IS NOT (read before citing its output). This is a read-only FORENSIC AID
over ``~/.claude/projects/*/*.jsonl``, not a system of record. Session transcripts are:

  * LOCAL to this machine -- a decision made in a session on another machine is invisible here;
  * UNVERSIONED and NOT GUARANTEED RETAINED -- they can be pruned, and nothing in this repo backs
    them up;
  * a record of SESSIONS RUN ON THIS MACHINE ONLY.

A NULL RESULT MEANS "NOT FOUND HERE" -- IT NEVER MEANS "NEVER DECIDED". `phaze-ldvmy` ("keep
ubuntu-latest per operator decision") is the standing example of a genuine null: a full sweep of the
corpus did not find it, and that is evidence of nothing except that this machine's transcripts do not
contain it. Do not write "no operator decision was made" on the strength of this tool's silence --
write "not found in local session transcripts as of <date>" and let a human confirm or supply the
citation. The durable record remains a bead comment or an ADR section; this tool exists to make
writing a correct one cheap enough that it actually happens, not to replace it.

Quoted turn text is reproduced VERBATIM from the transcript and is NOT scrubbed. If a recovered turn
happens to contain a local path or other identifier, redact it by hand before pasting into a tracked
file -- see CLAUDE.md's "No local identifiers in tracked files" convention. What this script's OWN
output never contains is a filesystem path or the project-directory name (both embed the local
account name on this machine, e.g. ``/Users/<account>/...``) -- every citation is anchored by
SESSION ID and TIMESTAMP alone, which is sufficient, stable, and portable.

SUB-AGENT TRANSCRIPTS. Claude Code's classic non-team shape records a Task-tool sub-agent's turns
inline in the parent transcript with ``isSidechain: true``. This installation's multi-agent sessions
instead run each teammate/sub-agent as its OWN top-level session (its own ``*.jsonl``, own session
id) and record cross-agent messages as ``origin.kind == "peer"`` in the recipient's own transcript --
confirmed against the corpus scanned while building this tool: zero ``isSidechain: true`` records
across roughly 360,000 scanned entries in every phaze-related project directory on this machine.
Because a directory-wide scan walks every ``*.jsonl`` file under the target root, those separate
sub-agent sessions ARE covered by default -- no special-casing needed. ``isSidechain: true`` records
are excluded outright wherever they DO occur (belt-and-suspenders, for the classic shape or a future
client version): a sidechain turn is an agent-to-subagent exchange, never a human at an interactive
terminal, and `AskUserQuestion` requires exactly that, so a sidechain record can never be a genuine
operator answer.

WHAT COUNTS AS A GENUINE OPERATOR TURN. Two shapes, both timestamped:

  1. An ``AskUserQuestion`` pair: an assistant ``tool_use`` (name ``AskUserQuestion``) carrying every
     question and its options, matched by ``tool_use_id`` to the ``user``-role ``tool_result`` that
     carries the operator's selection (in ``toolUseResult.answers``, keyed by question text).
  2. A human-typed turn: a ``user``-role record whose ``origin.kind == "human"`` -- covers turns
     typed live, turns approved from a queued suggestion, and turns accepted from an autocomplete
     suggestion (all three were observed in this corpus with that same origin marker).

THE LABEL IS THE ANSWER; THE DESCRIPTION IS NOT (read this before citing an ``AskUserQuestion``
result -- this exact conflation has produced real citation defects three times in this molecule).
Each option the operator was shown carries a bolded LABEL and a longer DESCRIPTION explaining it;
the operator selects the LABEL, and the DESCRIPTION was written by the assistant presenting the
choice, never by the operator. ``recover()`` keeps them as separate fields (``Option.label`` /
``Option.description``, and ``AskUserQuestionEvent.answers`` holds only the selected label); both
renderers print them under separate headings (``ANSWER (operator selected this LABEL only)`` vs.
``Options offered``) and prepend ``ATTRIBUTION_NOTE`` whenever an ``AskUserQuestion`` result is
present. Quoting a description as the operator's own words manufactures operator authority for text
the operator never wrote -- cite the label (and the question), never the description, as the
decision.

Everything else in the corpus is machine noise and is excluded: task notifications
(``origin.kind == "task-notification"``), teammate/peer messages (``origin.kind == "peer"``, or
content starting with ``<teammate-message`` / ``<agent-message`` / "Another Claude session sent a
message" for older records lacking that field), meta-injected prompts (``isMeta: true`` -- skill
loads, scheduled status-update prompts), the auto-generated "Review this change for security
vulnerabilities." prompt, ``<system-reminder>`` / ``<task-notification>`` content, local slash-command
output (``<local-command-...>``), and session-continuation summaries. Every one of these was found,
by direct inspection of this machine's real corpus, sharing metadata or a stable text prefix with no
overlap against the ~700 confirmed human turns -- see ``_NOISE_PREFIXES`` and ``_is_human_turn`` for
the specific evidence each one is excluded on.

TWO WAYS TO QUERY, AND WHICH TO TRUST. ``--since``/``--until`` matches on the turn's own
timestamp and is exact -- this is the mode all three of this tool's regression fixtures are proven
against (see the bead). ``--bead <id>`` matches turns that name the id LITERALLY, which is exact but
incomplete: a decision is often named once and then omitted from the very question/answer that
settles it (true of 2 of the 3 regression fixtures -- neither the kj8dl question nor the 6r39 human
reply mentions its own bead id). ``--bead`` combined with ``--context-window N`` additionally matches
a turn preceded, within N records, by a mention of the id -- but this is a heuristic that can
over-match in a session discussing the same id pervasively (measured while building this tool: a
30-record window on the phaze-3ea41 dispatcher session pulled in an unrelated question about doc
placement). Every context-matched result is rendered with an explicit "CONTEXT MATCH, VERIFY" label
and the mention that triggered it, and ``--context-window`` defaults to 0 (off) so a plain ``--bead``
run never emits an unlabelled false positive. When precision matters, prefer ``--since``/``--until``
over ``--context-window``.

USAGE::

    uv run python scripts/recover_operator_decisions.py --since 2026-08-12T00:00:00Z \\
        --until 2026-08-12T01:00:00Z
    uv run python scripts/recover_operator_decisions.py --bead phaze-3ea41
    uv run python scripts/recover_operator_decisions.py --bead phaze-3ea41 --context-window 10 --format json

Output goes to stdout in Markdown by default (quotable directly into a bead comment or ADR
section) or as JSON with ``--format json`` for programmatic use. Exit code is 0 for a completed
scan regardless of whether anything was found -- a null result is a legitimate, non-error outcome
of this tool, per the honesty requirement above. Exit code is non-zero only for a usage error or an
unreadable ``--projects-root``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterator


DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# `--bead` matches literally by default (safe: zero false positives observed). `--context-window N`
# opts into the fuzzy fallback below for a session that names the bead id once and then omits it
# from the follow-up question/answer that actually decides it (true of 2 of this tool's 3 regression
# fixtures) -- but it is a heuristic, and it over-matches badly in a session that discusses the same
# bead id pervasively (measured while building this tool: a 30-record window on the phaze-3ea41
# dispatcher session pulled in an unrelated question about doc placement, because the session had
# said "phaze-3ea41" dozens of times elsewhere). Every context-matched result is labelled with the
# text that triggered it, in the rendered output, so a reviewer can reject a bad match on sight.
BEAD_CONTEXT_WINDOW_DEFAULT = 0

# Content-prefix backstop for records that lack (or predate) the `origin`/`isMeta` fields this tool
# primarily gates on. Every prefix below was found, by direct inspection, on real machine-noise
# records in this corpus's oldest sessions -- see the module docstring for the categories.
_NOISE_PREFIXES = (
    "<teammate-message",
    "<agent-message",
    "Another Claude session sent a message",
    "<task-notification",
    "<system-reminder",
    "<local-command-",
    "Review this change for security vulnerabilities.",
    "This session is being continued from a previous conversation",
)


@dataclass(frozen=True)
class Option:
    """One offered choice on an `AskUserQuestion` question."""

    label: str
    description: str


@dataclass(frozen=True)
class Question:
    """One question inside an `AskUserQuestion` tool call."""

    text: str
    header: str
    options: tuple[Option, ...]


@dataclass(frozen=True)
class AskUserQuestionEvent:
    """A matched (asked, answered) `AskUserQuestion` pair."""

    session_id: str
    ask_timestamp: str
    answer_timestamp: str | None
    questions: tuple[Question, ...]
    answers: dict[str, str]
    record_index: int
    match_note: str | None = None
    """Set only under `--bead` + `--context-window`: how a non-literal match was justified."""


@dataclass(frozen=True)
class HumanTurn:
    """One human-typed turn (`origin.kind == "human"`)."""

    session_id: str
    timestamp: str
    text: str
    record_index: int
    match_note: str | None = None
    """Set only under `--bead` + `--context-window`: how a non-literal match was justified."""


@dataclass
class RecoveryResult:
    """Everything recovered for one query, ready to render."""

    query_description: str
    ask_user_question_events: list[AskUserQuestionEvent] = field(default_factory=list)
    human_turns: list[HumanTurn] = field(default_factory=list)
    files_scanned: int = 0
    records_scanned: int = 0


def _iter_transcript_files(root: Path) -> Iterator[Path]:
    """Yield every session transcript under `root` (one level of project directories down)."""
    if not root.is_dir():
        return
    yield from sorted(root.glob("*/*.jsonl"))


def _iter_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield `(index, record)` for every parseable JSON line in `path`.

    A malformed line (truncated write, concurrent append) is skipped rather than raising --
    this is read-only forensics over a live, append-only log, not a validated data format.
    """
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield index, record


def _origin_kind(record: dict[str, Any]) -> str | None:
    origin = record.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        return kind if isinstance(kind, str) else None
    if isinstance(origin, str):
        return origin
    return None


def _extract_text(content: Any) -> str | None:
    """Pull the human-readable text out of a `message.content` value, or `None` if there is none.

    `content` is a bare string for most turns, or a list of typed blocks (`text`, `image`, ...) when
    an image is attached -- observed on 11 of 709 real human turns in this corpus. Only the `text`
    blocks are joined; a turn that is image-only yields `None` and is not reported (nothing to quote).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _is_machine_noise_text(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _is_human_turn(record: dict[str, Any]) -> bool:
    """True for a genuine operator-typed turn -- see the module docstring for the evidence."""
    if record.get("type") != "user":
        return False
    if record.get("isSidechain"):
        return False
    if record.get("isMeta"):
        return False
    if _origin_kind(record) != "human":
        return False
    text = _extract_text(record.get("message", {}).get("content"))
    if text is None:
        return False
    # Belt-and-suspenders: every real human turn inspected while building this tool was free of
    # these prefixes, but a wrongly-stamped `origin.kind` costs nothing to guard against.
    return not _is_machine_noise_text(text)


def _in_range(timestamp: str | None, since: str | None, until: str | None) -> bool:
    if timestamp is None:
        return False
    if since is not None and timestamp < since:
        return False
    return not (until is not None and timestamp > until)


def _collect_ask_user_question_events(
    records: list[tuple[int, dict[str, Any]]],
    session_id: str,
) -> list[AskUserQuestionEvent]:
    """Pair every `AskUserQuestion` tool_use in `records` with its matching tool_result answer."""
    pending: dict[str, tuple[int, str, tuple[Question, ...]]] = {}
    events: list[AskUserQuestionEvent] = []

    for index, record in records:
        if record.get("type") == "assistant" and not record.get("isSidechain"):
            content = record.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"):
                    continue
                tool_use_id = block.get("id")
                questions_input = block.get("input", {}).get("questions", [])
                questions = tuple(
                    Question(
                        text=q.get("question", ""),
                        header=q.get("header", ""),
                        options=tuple(Option(label=o.get("label", ""), description=o.get("description", "")) for o in q.get("options", [])),
                    )
                    for q in questions_input
                    if isinstance(q, dict)
                )
                if tool_use_id and questions:
                    pending[tool_use_id] = (index, record.get("timestamp", ""), questions)
        elif record.get("type") == "user" and not record.get("isSidechain"):
            content = record.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                tool_use_id = block.get("tool_use_id")
                if tool_use_id not in pending:
                    continue
                ask_index, ask_timestamp, questions = pending.pop(tool_use_id)
                tool_use_result = record.get("toolUseResult")
                answers: dict[str, str]
                if isinstance(tool_use_result, dict) and isinstance(tool_use_result.get("answers"), dict):
                    answers = dict(tool_use_result["answers"])
                else:
                    # Older records may lack the structured `toolUseResult`; fall back to parsing
                    # the human-readable summary string ("Q"="A", "Q"="A", ...).
                    answers = _parse_answers_from_summary(block.get("content"))
                events.append(
                    AskUserQuestionEvent(
                        session_id=session_id,
                        ask_timestamp=ask_timestamp,
                        answer_timestamp=record.get("timestamp"),
                        questions=questions,
                        answers=answers,
                        record_index=ask_index,
                    )
                )

    return events


def _parse_answers_from_summary(content: Any) -> dict[str, str]:
    """Fallback parse of the `"Q"="A", "Q"="A"` summary string `AskUserQuestion` results carry.

    Used only when `toolUseResult.answers` is absent. The summary text is generated by the client,
    not typed by a person, so a regex split on this fixed format is a legitimate parse rather than a
    guess -- but it is a secondary path, and `toolUseResult.answers` is preferred whenever present.
    """
    text = content if isinstance(content, str) else ""
    answers: dict[str, str] = {}

    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"', text):
        question = match.group(1).replace('\\"', '"')
        answer = match.group(2).replace('\\"', '"')
        answers[question] = answer
    return answers


def _nearest_bead_mention(records: list[tuple[int, dict[str, Any]]], upto_index: int, bead_id: str, window: int) -> str | None:
    """Best-effort: does `bead_id` appear in the `window` records immediately before `upto_index`?

    Returns the matched snippet's own text (truncated) if found, else `None`. This is a heuristic,
    not a guarantee: it recovers the common shape observed in this corpus (a bead id is named once,
    then omitted from the follow-up question/answer that decides it -- true for 2 of this tool's 3
    regression fixtures), but it can both under-match (the mention is further back than `window`) and
    over-match (an unrelated later mention of the same id, inside the window, gets attributed). Every
    context-matched result is labelled as such in the rendered output so a reviewer can check it.
    """
    lowered = bead_id.lower()
    start = max(0, upto_index - window)
    for _index, record in reversed(records[start:upto_index]):
        text = _extract_text(record.get("message", {}).get("content")) if isinstance(record.get("message"), dict) else None
        if text and lowered in text.lower():
            return text[:200]
    return None


def recover(
    projects_root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    bead_id: str | None = None,
    context_window: int = BEAD_CONTEXT_WINDOW_DEFAULT,
) -> RecoveryResult:
    """Run one recovery query over every transcript under `projects_root`.

    `since`/`until` are inclusive ISO-8601 bounds (UTC, `Z`-suffixed) applied to an
    `AskUserQuestionEvent`'s ask-or-answer timestamp and to a `HumanTurn`'s own timestamp.

    `bead_id` narrows to records that name it literally -- the trustworthy default, with zero
    observed false positives. Passing `context_window > 0` additionally matches an
    `AskUserQuestion` batch or human turn that does not name the bead itself but was preceded,
    within that many records, by a turn that did; every such match carries a `match_note` recording
    what triggered it, and is rendered with an explicit "verify" label (see the module docstring and
    `_nearest_bead_mention` for why this is a heuristic that can over-match).
    """
    if bead_id:
        query_description = f"bead {bead_id}"
        if since or until:
            query_description += f", {since or '...'}..{until or '...'}"
    else:
        query_description = f"{since or '...'}..{until or '...'}"

    result = RecoveryResult(query_description=query_description)
    bead_lower = bead_id.lower() if bead_id else None

    for path in _iter_transcript_files(projects_root):
        result.files_scanned += 1
        records = list(_iter_records(path))
        result.records_scanned += len(records)
        if not records:
            continue
        session_id = records[-1][1].get("sessionId") or records[0][1].get("sessionId") or path.stem

        for event in _collect_ask_user_question_events(records, session_id):
            if (since or until) and not (_in_range(event.ask_timestamp, since, until) or _in_range(event.answer_timestamp, since, until)):
                continue
            if bead_lower:
                matched_questions = tuple(q for q in event.questions if bead_lower in q.text.lower())
                if matched_questions:
                    event = AskUserQuestionEvent(
                        session_id=event.session_id,
                        ask_timestamp=event.ask_timestamp,
                        answer_timestamp=event.answer_timestamp,
                        questions=matched_questions,
                        answers={q.text: event.answers.get(q.text, "") for q in matched_questions},
                        record_index=event.record_index,
                    )
                else:
                    context = _nearest_bead_mention(records, event.record_index, bead_id, context_window)  # type: ignore[arg-type]
                    if context is None:
                        continue
                    event = AskUserQuestionEvent(
                        session_id=event.session_id,
                        ask_timestamp=event.ask_timestamp,
                        answer_timestamp=event.answer_timestamp,
                        questions=event.questions,
                        answers=event.answers,
                        record_index=event.record_index,
                        match_note=f"CONTEXT MATCH, VERIFY: nearby mention of {bead_id!r} — {context!r}",
                    )
            result.ask_user_question_events.append(event)

        for record_index, record in records:
            if not _is_human_turn(record):
                continue
            timestamp = record.get("timestamp")
            if (since or until) and not _in_range(timestamp, since, until):
                continue
            text = _extract_text(record.get("message", {}).get("content")) or ""
            match_note = None
            if bead_lower:
                literal = bead_lower in text.lower()
                if not literal:
                    context = _nearest_bead_mention(records, record_index, bead_id, context_window)  # type: ignore[arg-type]
                    if context is None:
                        continue
                    match_note = f"CONTEXT MATCH, VERIFY: nearby mention of {bead_id!r} — {context!r}"
            result.human_turns.append(
                HumanTurn(session_id=session_id, timestamp=timestamp or "", text=text, record_index=record_index, match_note=match_note)
            )

    return result


LIMITS_NOTICE = (
    "LIMITS: this is a read-only scan of local Claude Code session transcripts "
    "(~/.claude/projects/*/*.jsonl) on THIS machine only. Transcripts are unversioned and not "
    'guaranteed retained. A NULL RESULT MEANS "not found in local session transcripts" -- it '
    'NEVER means "the decision was never made". Confirm with a human before writing either '
    "conclusion into a durable record."
)

# The recurring citation defect this note exists to prevent, seen three times in this molecule
# (most recently self-caught by another developer): an AskUserQuestion option is shown to the
# operator as a bolded LABEL plus a longer DESCRIPTION written by the assistant to explain it. The
# operator selects the LABEL. Quoting the description alongside it as if the operator said both
# manufactures operator authority for text the operator never wrote -- the description is
# dispatcher framing, not testimony.
ATTRIBUTION_NOTE = (
    "ATTRIBUTION: for every AskUserQuestion below, ANSWER is the option LABEL the operator "
    "selected -- the only text the operator is responsible for. Each option's DESCRIPTION was "
    "written by the assistant presenting the choice, not by the operator. Cite the label (and the "
    "question) as what the operator decided; never quote a description as the operator's own words."
)


def render_markdown(result: RecoveryResult) -> str:
    lines: list[str] = []
    lines.append(f"# Operator-decision recovery: {result.query_description}")
    lines.append("")
    lines.append(LIMITS_NOTICE)
    if result.ask_user_question_events:
        lines.append("")
        lines.append(ATTRIBUTION_NOTE)
    lines.append("")
    lines.append(f"Scanned {result.files_scanned} session transcript(s), {result.records_scanned} record(s).")
    lines.append("")

    if not result.ask_user_question_events and not result.human_turns:
        lines.append("No operator-decision evidence found for this query among the sessions scanned on this machine.")
        return "\n".join(lines) + "\n"

    for event in sorted(result.ask_user_question_events, key=lambda e: e.ask_timestamp):
        lines.append(f"## AskUserQuestion — session `{event.session_id}`")
        lines.append(f"Asked {event.ask_timestamp}, answered {event.answer_timestamp or '(no matching answer found)'}.")
        if event.match_note:
            lines.append(f"⚠️ {event.match_note}")
        lines.append("")
        for question in event.questions:
            lines.append(f'**Q ("{question.header}"):** {question.text}')
            lines.append("")
            lines.append("Options offered (LABEL: description -- description is dispatcher framing, not the operator's words):")
            for option in question.options:
                lines.append(f"  - **{option.label}**: {option.description}")
            answer = event.answers.get(question.text)
            lines.append("")
            lines.append(f"**ANSWER (operator selected this LABEL only):** {answer if answer is not None else '(not recorded in this event)'}")
            lines.append("")
        lines.append("")

    for turn in sorted(result.human_turns, key=lambda t: t.timestamp):
        lines.append(f"## Human-typed turn — session `{turn.session_id}`, {turn.timestamp}")
        if turn.match_note:
            lines.append(f"⚠️ {turn.match_note}")
        lines.append("")
        for text_line in turn.text.splitlines() or [""]:
            lines.append(f"> {text_line}")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_json(result: RecoveryResult) -> str:
    payload = {
        "query": result.query_description,
        "limits": LIMITS_NOTICE,
        "attribution_note": ATTRIBUTION_NOTE if result.ask_user_question_events else None,
        "files_scanned": result.files_scanned,
        "records_scanned": result.records_scanned,
        "ask_user_question_events": [
            {
                "session_id": e.session_id,
                "ask_timestamp": e.ask_timestamp,
                "answer_timestamp": e.answer_timestamp,
                "questions": [
                    {
                        "question": q.text,
                        "header": q.header,
                        # `answer` is the selected LABEL only -- see `attribution_note`. Each
                        # option's `description` is dispatcher framing and is never the answer.
                        "options": [{"label": o.label, "description": o.description} for o in q.options],
                        "answer": e.answers.get(q.text),
                    }
                    for q in e.questions
                ],
                "match_note": e.match_note,
            }
            for e in result.ask_user_question_events
        ],
        "human_turns": [
            {"session_id": t.session_id, "timestamp": t.timestamp, "text": t.text, "match_note": t.match_note} for t in result.human_turns
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="ISO-8601 UTC lower bound, e.g. 2026-08-12T00:00:00Z")
    parser.add_argument("--until", help="ISO-8601 UTC upper bound, e.g. 2026-08-12T01:00:00Z")
    parser.add_argument("--bead", help="Bead id to search for, e.g. phaze-3ea41")
    parser.add_argument(
        "--context-window",
        type=int,
        default=BEAD_CONTEXT_WINDOW_DEFAULT,
        metavar="N",
        help=(
            "With --bead, also match a turn that does not name the bead itself but was preceded, "
            "within N records, by one that did. Off by default (literal match only) -- this is a "
            "heuristic that can over-match; see the module docstring. Ignored without --bead."
        ),
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help="Root directory of Claude Code project transcripts (default: ~/.claude/projects)",
    )
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args(argv)

    if not args.bead and not (args.since or args.until):
        parser.error("pass --bead, or --since/--until (or both)")
    if bool(args.since) != bool(args.until):
        parser.error("--since and --until must be given together")
    if args.context_window < 0:
        parser.error("--context-window must be >= 0")
    for name in ("since", "until"):
        value = getattr(args, name)
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                parser.error(f"--{name} is not a valid ISO-8601 timestamp: {value!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.projects_root.is_dir():
        print(f"error: --projects-root {args.projects_root} is not a directory", file=sys.stderr)  # noqa: T201
        return 1

    result = recover(args.projects_root, since=args.since, until=args.until, bead_id=args.bead, context_window=args.context_window)
    output = render_json(result) if args.format == "json" else render_markdown(result)
    print(output, end="")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
