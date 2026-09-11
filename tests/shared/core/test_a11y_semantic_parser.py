"""Direct contract tests for the accessibility guard's semantic source parser."""

from __future__ import annotations

import pytest

from tests.shared.core._a11y_semantic_parser import (
    alpine_class_alternatives,
    emitted_class_strings,
    enclosing_if_predicate,
    extract_literal_set_variables,
    extract_method_body,
    split_top_level_if,
    worst_case_dark_text_colour_count,
)


def test_extract_method_body_balances_nested_delimiters() -> None:
    component = "before: true, onLoaded() { if (ready) { this.$nextTick(() => { focus(); }); } }, after() { close(); }"

    assert extract_method_body(component, "onLoaded") == " if (ready) { this.$nextTick(() => { focus(); }); } "


@pytest.mark.parametrize(
    ("component", "message"),
    [
        ("other() { return true; }", r"expected an Alpine method onLoaded\(\)"),
        ("onLoaded()", r"onLoaded\(\) has no body"),
        ("onLoaded() { if (ready) { focus(); }", r"unbalanced braces in onLoaded\(\)"),
    ],
)
def test_extract_method_body_rejects_malformed_input(component: str, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        extract_method_body(component, "onLoaded")


def test_enclosing_if_predicate_balances_nested_parentheses() -> None:
    body = "if (!this.open && canFocus(check(thing))) { this.$nextTick(() => { focus(); }); }"

    assert enclosing_if_predicate(body, body.index("this.$nextTick")) == "!this.open && canFocus(check(thing))"
    assert enclosing_if_predicate(body, body.index("focus();")) is None
    assert enclosing_if_predicate("focus();", 0) is None


def test_enclosing_if_predicate_returns_none_for_malformed_or_non_if_blocks() -> None:
    malformed = "if (ready { focus();"
    callback = "run(() => { focus(); });"

    assert enclosing_if_predicate(malformed, malformed.index("focus")) is None
    assert enclosing_if_predicate(callback, callback.index("focus")) is None


def test_split_top_level_if_keeps_nested_branches_inside_their_parent() -> None:
    source = "pre {% if outer %}a {% if inner %}b{% else %}c{% endif %} d{% elif fallback %}e{% else %}f{% endif %} post"

    assert split_top_level_if(source) == (
        "pre ",
        ["a {% if inner %}b{% else %}c{% endif %} d", "e", "f"],
        " post",
    )


def test_split_top_level_if_includes_the_no_branch_render_without_else() -> None:
    assert split_top_level_if("before {% if enabled %}active{% endif %} after") == ("before ", ["active", ""], " after")


@pytest.mark.parametrize(
    "source",
    [
        "plain classes only",
        "{% if enabled %}active",
        "{% endif %}{% if enabled %}active{% endif %}",
    ],
)
def test_split_top_level_if_is_total_for_malformed_input(source: str) -> None:
    assert split_top_level_if(source) is None


def test_class_rendering_preserves_known_false_positive_exemptions() -> None:
    assert worst_case_dark_text_colour_count("{{ 'dark:text-gray-100' if selected else 'dark:text-gray-400' }}") == 1
    assert worst_case_dark_text_colour_count("$store.theme.dim ? 'dark:text-gray-500' : 'dark:text-gray-300'", alpine=True) == 1
    assert worst_case_dark_text_colour_count("dark:text-gray-400 dark:hover:text-gray-300") == 1
    assert alpine_class_alternatives("{'dark:text-gray-400': selected, 'dark:text-gray-500': muted}") == [
        "{'dark:text-gray-400': selected, 'dark:text-gray-500': muted}"
    ]


def test_class_rendering_still_detects_classes_that_can_co_emit() -> None:
    sequential = "{% if a %}dark:text-gray-400{% endif %} {% if b %}dark:text-gray-500{% endif %}"
    nested = "{% if a %}{% if b %}dark:text-gray-100 dark:text-gray-200{% endif %}{% endif %}"

    assert worst_case_dark_text_colour_count(sequential) == 2
    assert worst_case_dark_text_colour_count(nested) == 2


def test_literal_set_resolution_stays_narrow_and_semantic() -> None:
    source = "{% set c = 'dark:text-blue-300' %}{% set c = 'dark:text-blue-300' %}{% set c = dynamic|trim %}"
    set_vars = extract_literal_set_variables(source)

    assert set_vars == {"c": ["dark:text-blue-300"]}
    assert any("dark:text-blue-300" in rendered for rendered in emitted_class_strings("{{ c }} font-semibold", set_vars))
    assert [rendered.strip() for rendered in emitted_class_strings("{{ unknown|trim }} font-semibold", set_vars)] == ["font-semibold"]
