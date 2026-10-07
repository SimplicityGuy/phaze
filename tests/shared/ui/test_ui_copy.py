"""phaze-9v49r: rendered UI copy carries no decision-record references and no ``--`` stand-in for an em dash.

A decision-record citation printed inside parentheses inside parentheses on Apply, and the Runtime config page cited a
numbered record and section: internal decision-record numbers are meaningless to the operator reading the page and
rot when records are renumbered. ``--`` was typed where an em dash belongs (``Group resolved -- 3 files``).

Two guards, because each is blind to what the other sees:

* the TEMPLATE guard reads every template's visible text -- everything outside Jinja comments, HTML comments, script and
  style blocks and tag markup, plus the user-visible attributes (title, aria-label, placeholder, alt) -- so a branch
  no test renders is still covered;
* the RENDERED guard fetches every shell page through the real app, so copy built in Python (a preflight exclusion
  reason, a config key's description) is covered too.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
import pytest

from phaze.web.template_globals import PAGE_NAMES


if TYPE_CHECKING:
    from httpx import AsyncClient


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"

_SKIP = re.compile(r"\{#.*?#\}|<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>", re.S)
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_VISIBLE_ATTR = re.compile(r"\b(?:title|aria-label|placeholder|alt)=\"([^\"]*)\"")
_ADR_PREFIX = "ADR-"
_ADR = re.compile(r"\bADR-\d")
_DOUBLE_DASH = re.compile(r"(?<=\S) -- (?=\S)")


def visible_text(source: str) -> str:
    """The copy a reader of the rendered page could see, from a template's source."""
    text = _SKIP.sub(" ", source)
    parts: list[str] = []
    position = 0
    for tag in _TAG.finditer(text):
        parts.append(text[position : tag.start()])
        parts.extend(_VISIBLE_ATTR.findall(tag.group(3)))
        position = tag.end()
    parts.append(text[position:])
    return "\n".join(parts)


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        for number, line in enumerate(visible_text(path.read_text(encoding="utf-8")).splitlines(), start=1):
            if pattern.search(line):
                found.append(f"{path.relative_to(TEMPLATES_DIR)}: {line.strip()[:90]}")
                del number
    return found


def test_no_template_prints_an_adr_reference() -> None:
    assert not (offenders := _offenders(_ADR)), f"decision-record numbers in rendered copy: {offenders}"


def test_no_template_types_a_double_dash_for_an_em_dash() -> None:
    assert not (offenders := _offenders(_DOUBLE_DASH)), f"'--' in rendered copy (use an em dash): {offenders}"


def test_the_extractor_sees_copy_and_ignores_comments_and_markup() -> None:
    source = (
        '{# __A__0008 in a comment #}<p title="a -- b" class="x--y">Group resolved -- done</p><!-- __A__0019 --><script>if (n-- > 0) {}</script>'
    ).replace("__A__", _ADR_PREFIX)

    text = visible_text(source)

    assert "ADR-" not in text
    assert "Group resolved -- done" in text
    assert "a -- b" in text
    assert "n--" not in text
    assert _DOUBLE_DASH.search(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", sorted(PAGE_NAMES))
async def test_every_rendered_shell_page_is_free_of_both(client: AsyncClient, stage: str) -> None:
    for headers in ({"HX-Request": "true"}, {}):
        response = await client.get(f"/s/{stage}", headers=headers)
        assert response.status_code == 200, f"/s/{stage} {headers}"
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup(["script", "style"]):
            node.decompose()
        copy = [soup.get_text("\n")]
        copy.extend(
            str(value)
            for element in soup.find_all(True)
            for key, value in element.attrs.items()
            if key in {"title", "aria-label", "placeholder", "alt"}
        )
        joined = "\n".join(copy)
        assert not _ADR.search(joined), f"/s/{stage} {headers}: {_ADR.search(joined)}"
        assert not _DOUBLE_DASH.search(joined), f"/s/{stage} {headers}: {joined[max(0, _DOUBLE_DASH.search(joined).start() - 40) :][:100]!r}"  # type: ignore[union-attr]
