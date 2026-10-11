"""Scene note framing shared by bounded local interpreters; offsets refer to original text."""

import re


_LEFT = re.compile(r"^[\s|│║┃/\\─━═┌┐└┘╔╗╚╝█▓▒░▀▄▌▐■]+")
_RIGHT = re.compile(r"[\s|│║┃█▓▒░]+$")
LABELED = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ._/-]{0,63}?)\s*[:=]\s*(?P<value>.*?)\s*$")


def cue_line(line: str) -> str:
    """Remove writer semicolon comments outside quotes, preserving prefix offsets."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:index].rstrip()
    return line


def note_line(raw: str) -> tuple[str, int]:
    """Remove only scene frame characters; return the retained line's original start."""
    left = _LEFT.match(raw)
    start = left.end() if left else 0
    line = raw[start:]
    right = _RIGHT.search(line)
    return (line[: right.start()] if right else line), start
