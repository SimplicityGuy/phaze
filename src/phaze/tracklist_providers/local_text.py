"""Bounded text row syntax; classification never establishes target applicability."""

from dataclasses import dataclass
import re

from phaze.tracklist_providers.local_syntax import LABELED, note_line


ORDERED = re.compile(r"^(?:\[(\d{1,4})\]|(\d{1,4})[.):\-]?)[ \t]+(.+)$")
TIMED = re.compile(r"^(?:\[(\d{1,6}:\d{2}(?::\d{2})?|\d{1,6})\]|(\d{1,6}:\d{2}(?::\d{2})?))[ \t]+(.+)$")
HEADING = re.compile(r"^(?:track\s*list(?:ing)?|setlist|tracks|nr\.?\s+(?:title|track).*)\s*:?$", re.IGNORECASE)
SECTION = re.compile(r"^(?:part|set|disc|disk|cd)\s*\d{1,4}\b.*$", re.IGNORECASE)
DURATION = re.compile(r"\s+\[?(\d{1,6}:\d{2}(?::\d{2})?)\]?(?:\s+min)?$")
DECORATION = re.compile(r"^[\s\-=+*_~|.:/\\\[\]─-▟]+$")
DATE_HEADER = re.compile(r"^(?:\d{4}[-./]\d{2}[-./]\d{2}|\d{2}[-./]\d{2}[-./]\d{4})\b")
MEDIA_NAME = re.compile(r"\.(?:mp3|m4a|mp4|wav|flac|ogg|ape|aiff|wma)\s*$", re.IGNORECASE)
LINEUP = re.compile(r"^\d{1,2}\.\d{2}\s+\D")
UNKNOWN = re.compile(r"^(?:id|unknown|unidentified|\?+)(?:\s|$|[([])", re.IGNORECASE)
ABSENT = re.compile(
    r"^(?:(?:no|unknown|missing|unavailable)\s+(?:tracklist|setlist)|(?:tracklist|setlist)\s+(?:unknown|unavailable|missing))\b", re.IGNORECASE
)


@dataclass(frozen=True)
class TextRowSyntax:
    number: int | None
    body: str
    time: str | None = None
    minute: bool = False


def heading(line: str) -> bool:
    return HEADING.fullmatch(line.strip(" .:=*-_")) is not None


def export_frame(lines: list[str], index: int) -> bool:
    """A border alone does not distinguish an export header from a tracklist."""
    if not lines[index].lstrip().startswith("/") or not re.search(r"-{6,}", lines[index]):
        return False
    for raw in lines[index + 1 : index + 33]:
        if raw.lstrip().startswith("\\") and re.search(r"-{6,}", raw):
            break
        if re.match(r"^(?:files?(?:\(s\))?|export information)\s*:", note_line(raw)[0], re.IGNORECASE):
            return True
    return False


def prefix(line: str) -> TextRowSyntax:
    """Time and ordinal prefixes are distinct; bracket-only integers denote minutes."""
    timed = TIMED.fullmatch(line)
    # [001] has observed minute precision, not an ordinal. Never qualify its origin.
    if timed:
        original = timed[1] or timed[2]
        following = ORDERED.fullmatch(timed[3])
        return TextRowSyntax(
            int(following[1] or following[2]) if following else None,
            re.sub(r"^[-.:]\s+", "", following[3]) if following else timed[3],
            original,
            ":" not in original,
        )
    ordered = ORDERED.fullmatch(line)
    if ordered:
        body = re.sub(r"^[-.:]\s+", "", ordered[3])
        if body.startswith("[") and "]" in body:
            original, body = body[1:].split("]", 1)
            return TextRowSyntax(int(ordered[1] or ordered[2]), body.strip(), original, original.isdigit())
        trailing_time = TIMED.fullmatch(body)
        if trailing_time:
            original = trailing_time[1] or trailing_time[2]
            return TextRowSyntax(int(ordered[1] or ordered[2]), trailing_time[3], original, ":" not in original)
        return TextRowSyntax(int(ordered[1] or ordered[2]), body)
    return TextRowSyntax(None, line)


def next_numbered(lines: list[str], index: int) -> TextRowSyntax | None:
    for other in lines[index + 1 : index + 4]:
        line = note_line(other)[0]
        if line:
            parsed = prefix(line)
            return parsed if parsed.number is not None else None
    return None


def separators(body: str) -> list[tuple[int, int]]:
    """Ignore separators inside balanced parentheses/brackets or double quotes."""
    found: list[tuple[int, int]] = []
    depth = 0
    quoted = False
    for index, char in enumerate(body):
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "([":
            depth += 1
        elif not quoted and char in ")]":
            depth = max(0, depth - 1)
        elif not quoted and depth == 0 and char in "-\u2013\u2014•/":
            left_space = index > 0 and body[index - 1].isspace()
            right_space = index + 1 < len(body) and body[index + 1].isspace()
            # Dash-less whitespace occurs in explicit numbered lists; callers require context.
            if char == "•" or (char == "/" and left_space and right_space) or (char != "/" and (left_space or right_space)):
                found.append((index, index + 1))
    return found


def plain_row(line: str) -> bool:
    return (
        LABELED.fullmatch(line) is None
        and "://" not in line
        and not DATE_HEADER.match(line)
        and not MEDIA_NAME.search(line)
        and len(separators(line)) >= 1
    )


def adjacent_row(lines: list[str], index: int) -> bool:
    """A run may have blank lines between rows; inspect at most two lines each way."""
    for direction in (-1, 1):
        for distance in (1, 2):
            other = index + direction * distance
            if 0 <= other < len(lines):
                line = note_line(lines[other])[0]
                if line:
                    if plain_row(line):
                        return True
                    break
    return False
