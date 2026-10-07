"""phaze-9v49r: ONE button macro, one case rule, one primary colour, and one empty-state macro.

Buttons mixed uppercase Jura (OPEN METADATA, GENERATE ALL) with Title Case ('Start Scan', 'Open Tracklists'), and the
primary action was amber-filled on Propose, green on Rename, cyan on a record page and teal elsewhere. ``ui.btn`` is the
one definition; empty tables and lists each rendered a different bare-text or icon-plus-CTA block, and ``ui.state`` is
the one empty-state macro.

The case rule (the implementer's decision, not an operator one): every button label paints UPPERCASE Jura, applied
with the ``uppercase`` utility so the DOM text stays as the caller wrote it -- the same rule ``status_pill`` follows.
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.templating import Jinja2Templates
import pytest

from phaze.web.template_globals import register_page_name_globals


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_page_name_globals(_templates.env)


def _render(source: str) -> str:
    return _templates.env.from_string('{% import "ui/primitives.html" as ui %}' + source).render()


# --- the macro ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["primary", "secondary", "destructive", "ghost"])
def test_every_kind_shares_one_case_rule_and_typeface(kind: str) -> None:
    classes = _render(f"{{{{ ui.btn('{kind}') }}}}").split()

    assert "uppercase" in classes
    assert "font-jura" in classes
    assert "tracking-wider" in classes


def test_primary_is_the_one_brand_fill() -> None:
    assert "bg-action-brand" in _render("{{ ui.btn('primary') }}").split()
    for kind in ("secondary", "destructive", "ghost"):
        assert "bg-action-brand" not in _render(f"{{{{ ui.btn('{kind}') }}}}").split()


def test_destructive_is_the_danger_fill_and_secondary_is_outlined() -> None:
    assert "bg-action-danger" in _render("{{ ui.btn('destructive') }}").split()
    secondary = _render("{{ ui.btn('secondary') }}").split()
    assert "border" in secondary
    assert not any(token.startswith("bg-action") for token in secondary)


def test_sizes_and_extra_utilities() -> None:
    assert "h-9" in _render("{{ ui.btn('primary', 'md') }}").split()
    assert "h-7" in _render("{{ ui.btn('primary', 'sm') }}").split()
    assert "w-full" in _render("{{ ui.btn('primary', 'md', 'w-full') }}").split()


def test_an_unknown_kind_falls_back_to_secondary_rather_than_rendering_unstyled() -> None:
    assert _render("{{ ui.btn('bogus') }}") == _render("{{ ui.btn('secondary') }}")


# --- the guard -----------------------------------------------------------------------------------------------------

_ELEMENT = re.compile(r"<(button|a)\b((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>", re.S)
_CLASS = re.compile(r"\bclass=\"([^\"]*)\"")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
# A fill colour on a button or button-shaped link. Whole tokens only: `focus:bg-action-brand` (the skip link) is a
# different token and a deliberate one.
_FILL = re.compile(
    r"(?<![\w:\[-])(?:bg-action-(?:brand|danger|ok|warn)|bg-amber-[678]00|bg-cyan-[567]00|bg-blue-[67]00|bg-green-[67]00|bg-red-[67]00)(?![\w-])"
)
# The numbered page chips in the pager are a different component: the CURRENT page is brand-filled.
_ALLOWED_FILL = {"pipeline/partials/_list_pager.html"}


def _spelled_fill_buttons() -> list[str]:
    found: list[str] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        relative = path.relative_to(TEMPLATES_DIR).as_posix()
        if relative == "ui/primitives.html" or relative in _ALLOWED_FILL:
            continue
        text = _JINJA_COMMENT.sub(lambda m: " " * len(m.group(0)), path.read_text(encoding="utf-8"))
        for match in _ELEMENT.finditer(text):
            cls = _CLASS.search(match.group(2))
            if cls and _FILL.search(cls.group(1)):
                found.append(f"{relative}:{text.count(chr(10), 0, match.start()) + 1}")
    return found


def test_no_template_spells_its_own_button_fill() -> None:
    offenders = _spelled_fill_buttons()

    assert not offenders, f"a button or button-shaped link spells its own fill colour instead of ui.btn: {offenders}"


def test_the_guard_would_catch_an_amber_primary() -> None:
    sample = '<a href="/s/rename" class="inline-flex h-9 rounded-lg bg-amber-700 px-3 font-jura">Open Review</a>'
    match = _ELEMENT.search(sample)
    assert match is not None
    cls = _CLASS.search(match.group(2))
    assert cls is not None
    assert _FILL.search(cls.group(1))
    assert not _FILL.search("sr-only focus:bg-action-brand focus:text-white")


# The header's status chips ("Agents online 1 . Lanes active 0", the forced-local pill) are Jura status readouts that
# happen to be links, not action buttons, and they carry counts in mixed case on purpose.
_HEADER_CHROME = {
    "shell/header.html",
    "shell/partials/header.html",
    "shell/partials/_force_local_pill.html",
    "shell/partials/_routing_override_warning.html",
}


def test_every_jura_button_is_uppercase() -> None:
    offenders: list[str] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        if path.relative_to(TEMPLATES_DIR).as_posix() in _HEADER_CHROME:
            continue
        text = _JINJA_COMMENT.sub(lambda m: " " * len(m.group(0)), path.read_text(encoding="utf-8"))
        for match in _ELEMENT.finditer(text):
            cls = _CLASS.search(match.group(2))
            if cls and "font-jura" in cls.group(1).split() and "uppercase" not in cls.group(1).split() and "normal-case" not in cls.group(1).split():
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{text.count(chr(10), 0, match.start()) + 1}")
    assert not offenders, f"a Jura button that is not uppercase breaks the one case rule: {offenders}"


def test_confirmation_dialogs_use_the_button_macro() -> None:
    html = _render("{{ ui.confirmation(id='x', title='T', message='M', action='/a', confirm_label='Go') }}")

    assert "bg-action-brand" in html
    assert "bg-action-warn" not in html
    danger = _render("{{ ui.confirmation(id='x', title='T', message='M', action='/a', tone='danger') }}")
    assert "bg-action-danger" in danger


# --- empty states --------------------------------------------------------------------------------------------------


def test_the_empty_state_macro_renders_icon_title_message_and_optional_actions() -> None:
    html = _render(
        "{% call ui.state('empty', 'No scans yet', 'Start one above.') %}<a href='/s/discover' class=\"{{ ui.btn('primary') }}\">Open</a>{% endcall %}"
    )

    assert 'role="status"' in html
    assert "No scans yet" in html
    assert "Start one above." in html
    assert "bg-action-brand" in html


@pytest.mark.parametrize(
    ("template", "bare_block"),
    [
        ("pipeline/partials/dedupe_workspace.html", "No duplicates found</p>"),
        ("pipeline/partials/_file_table.html", "{{ empty_heading }}</p>"),
        ("pipeline/partials/_changes_list.html", "p-8 text-center"),
        ("duplicates/partials/bulk_resolve_response.html", "py-12 text-center"),
        ("admin/partials/agents_table.html", "text-center py-8"),
    ],
)
def test_empty_tables_and_lists_render_through_the_one_macro(template: str, bare_block: str) -> None:
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")

    assert bare_block not in source, f"{template} still hand-writes its empty state"
    assert ".state('empty'" in source
