"""phaze-nwmsu: no filename or path is rendered under ``text-transform: uppercase``.

``font-jura ... uppercase`` on a heading is the house eyebrow style, and it silently rewrites the
case of anything inside it -- ``Sy_@_Helter_Skelter.mp3`` became ``SY_@_HELTER_SKELTER.MP3`` in the
Dedupe group legend and the record title, which misrepresents a case-sensitive name.

The guard scans every template: for each element whose class list carries the ``uppercase``
utility (and is not countermanded by ``normal-case`` on the same element), the element's whole
source (children included, so an uppercase ANCESTOR is caught) must not interpolate a filename or
path variable.
"""

from __future__ import annotations

from pathlib import Path
import re


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"

_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_VOID = {"input", "br", "hr", "img", "meta", "link", "source", "col", "wbr"}
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
# Variables that carry a real file name or path. Stage / agent / lane NAMES are labels, not filenames.
_FILENAME_VAR = re.compile(
    r"\{\{[^}]*\b(?:original_filename|new_filename|proposed_filename|current_filename|filename|file_path|original_path|proposed_path|"
    r"current_path|scan_path|group_name|f\.name|f\.path|file\.name|file\.path|batch\.scan_path)\b[^}]*\}\}"
)
_UPPERCASE = re.compile(r"(?<![\w-])uppercase(?![\w-])")
_NORMAL_CASE = re.compile(r"(?<![\w-])normal-case(?![\w-])")


def _blank_comments(source: str) -> str:
    return _JINJA_COMMENT.sub(lambda m: " " * len(m.group(0)), source)


def _uppercased_elements(source: str) -> list[tuple[int, str]]:
    """``(line, inner source)`` for every element carrying ``uppercase``."""
    text = _blank_comments(source)
    tags = list(_TAG.finditer(text))
    found: list[tuple[int, str]] = []
    for index, tag in enumerate(tags):
        closing, name, attrs = tag.group(1), tag.group(2).lower(), tag.group(3)
        if closing or name in _VOID or attrs.rstrip().endswith("/"):
            continue
        class_attr = re.search(r"\bclass=(\"[^\"]*\"|'[^']*')", attrs)
        classes = class_attr.group(1) if class_attr else ""
        if not _UPPERCASE.search(classes) or _NORMAL_CASE.search(classes):
            continue
        depth = 1
        end = len(text)
        for later in tags[index + 1 :]:
            later_name = later.group(2).lower()
            if later_name != name or later_name in _VOID:
                continue
            depth += -1 if later.group(1) else 1
            if depth == 0:
                end = later.start()
                break
        found.append((text.count("\n", 0, tag.start()) + 1, text[tag.end() : end]))
    return found


def test_no_template_interpolates_a_filename_under_uppercase() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        elements = _uppercased_elements(path.read_text(encoding="utf-8"))
        scanned += len(elements)
        for line, inner in elements:
            # Text a nested element opts back out of (`normal-case`) is not uppercased; strip those spans.
            inner = re.sub(r"<(\w+)[^>]*\bnormal-case\b[^>]*>.*?</\1>", "", inner, flags=re.DOTALL)
            if _FILENAME_VAR.search(inner):
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{line}")
    assert scanned > 20, "the guard found almost no uppercase elements, so it is not scanning what it thinks it is"
    assert not offenders, f"filename/path variables rendered under text-transform: uppercase: {offenders}"


def test_the_scanner_catches_a_filename_in_an_uppercase_heading() -> None:
    bad = '<h2 class="font-jura uppercase">{{ file.original_filename }}</h2>'
    ancestor = '<div class="uppercase"><p><span>{{ f.path }}</span></p></div>'
    fine = '<h2 class="font-jura uppercase">Pipeline Flow</h2><p class="font-mono">{{ file.original_filename }}</p>'

    assert any(_FILENAME_VAR.search(inner) for _, inner in _uppercased_elements(bad))
    assert any(_FILENAME_VAR.search(inner) for _, inner in _uppercased_elements(ancestor))
    assert not any(_FILENAME_VAR.search(inner) for _, inner in _uppercased_elements(fine))
