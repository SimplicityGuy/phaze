"""Pure source parsers used by the accessibility structural guards.

These helpers intentionally model the small JavaScript and Jinja shapes used by
the guards instead of treating whole attributes as undifferentiated regex
matches. Keeping them total over malformed input lets a guard stay conservative:
unknown template syntax contributes no invented classes, while malformed block
structure never produces a fabricated branch split.
"""

from __future__ import annotations

import re


DARK_TEXT_COLOUR_PATTERN = re.compile(r"dark:text-(?:[a-z-]+)-\d{2,3}\b")

# ``{% ... %}`` / ``{{ ... }}``; DOTALL because wrapped attributes can put
# newlines inside both forms.
_JINJA_BRANCH_TAG = re.compile(r"\{%-?\s*(if|elif|else|endif)\b.*?-?%\}", re.DOTALL)
_JINJA_ANY_TAG = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)
_JINJA_EXPR = re.compile(r"(\{\{.*?\}\})", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'([^']*)'")

# Comments are prose, not markup. Blank them out at the call site so source
# offsets remain stable for diagnostic line numbers.
COMMENTS_PATTERN = re.compile(r"<!--.*?-->|\{#.*?#\}", re.DOTALL)

# A value cannot contain an unescaped double quote because that closes the
# attribute, so ``[^\"]*`` bounds wrapped attributes exactly.
CLASS_ATTRIBUTE_PATTERN = re.compile(r'(?P<alpine>:)?class="(?P<value>[^"]*)"')

# Deliberately resolve only bare string-literal assignments. Guessing values for
# concatenations, filters, other variables, or multi-target assignments could
# manufacture a false positive.
_SET_STRING = re.compile(r"\{%-?\s*set\s+(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\")\s*-?%\}")
_BARE_VAR = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def extract_method_body(component: str, name: str) -> str:
    """Return the balanced brace-delimited body of a named Alpine method."""
    start = component.find(f"{name}()")
    assert start != -1, f"expected an Alpine method {name}() on the detail-pane shell"
    open_brace = component.find("{", start)
    assert open_brace != -1, f"{name}() has no body"
    depth = 0
    for index in range(open_brace, len(component)):
        if component[index] == "{":
            depth += 1
        elif component[index] == "}":
            depth -= 1
            if depth == 0:
                return component[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces in {name}()")


def enclosing_if_predicate(body: str, index: int) -> str | None:
    """Return the predicate of the innermost ``if`` block enclosing ``index``.

    ``None`` means the statement is at method top level, its innermost block is
    not an ``if``, or the surrounding delimiter structure is incomplete.
    """
    stack: list[int] = []
    for position in range(index):
        if body[position] == "{":
            stack.append(position)
        elif body[position] == "}" and stack:
            stack.pop()
    if not stack:
        return None
    head = body[: stack[-1]].rstrip()
    if not head.endswith(")"):
        return None
    depth = 0
    for position in range(len(head) - 1, -1, -1):
        if head[position] == ")":
            depth += 1
        elif head[position] == "(":
            depth -= 1
            if depth == 0:
                return head[position + 1 : len(head) - 1] if re.search(r"\bif\s*$", head[:position]) else None
    return None


def extract_literal_set_variables(source: str) -> dict[str, list[str]]:
    """Return same-file Jinja string-literal assignments grouped by name."""
    resolved: dict[str, list[str]] = {}
    for match in _SET_STRING.finditer(source):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        values = resolved.setdefault(name, [])
        if value not in values:
            values.append(value)
    return resolved


def expression_alternatives(expr: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Return the mutually exclusive class strings an expression can contribute."""
    literals = _SINGLE_QUOTED.findall(expr)
    if literals:
        # The empty alternative covers a falsy/no-literal branch, preventing an
        # expression from fabricating a duplicate on its own.
        return [*literals, ""]
    bare = _BARE_VAR.match(expr)
    if bare and set_vars and bare.group(1) in set_vars:
        return list(set_vars[bare.group(1)])
    return [""]


def alpine_class_alternatives(expr: str) -> list[str]:
    """Return the class strings an Alpine ``:class`` value can apply."""
    return expression_alternatives(expr) if "?" in expr else [expr]


def _expand_expressions(text: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Expand a branch-free fragment into every class string it can emit."""
    emitted = [""]
    for part in _JINJA_EXPR.split(text):
        alternatives = expression_alternatives(part, set_vars) if part.startswith("{{") else [_JINJA_ANY_TAG.sub(" ", part)]
        emitted = [f"{done} {alternative}" for done in emitted for alternative in alternatives]
    return list(dict.fromkeys(emitted))


def split_top_level_if(text: str) -> tuple[str, list[str], str] | None:
    """Split the first balanced top-level Jinja ``if`` into prefix, arms, suffix."""
    depth = 0
    opened: re.Match[str] | None = None
    separators: list[re.Match[str]] = []
    for tag in _JINJA_BRANCH_TAG.finditer(text):
        keyword = tag.group(1)
        if keyword == "if":
            depth += 1
            if depth == 1:
                opened, separators = tag, []
        elif keyword == "endif":
            depth -= 1
            if depth == 0 and opened is not None:
                branches, cut = [], opened.end()
                for separator in separators:
                    branches.append(text[cut : separator.start()])
                    cut = separator.end()
                branches.append(text[cut : tag.start()])
                if not any(separator.group(1) == "else" for separator in separators):
                    branches.append("")
                return text[: opened.start()], branches, text[tag.end() :]
        elif depth == 1:
            separators.append(tag)
    return None


def emitted_class_strings(attr: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Return every class string an attribute can render."""
    split = split_top_level_if(attr)
    if split is None:
        return _expand_expressions(attr, set_vars)
    prefix, branches, suffix = split
    emitted = [
        f"{before} {inside} {after}"
        for before in _expand_expressions(prefix, set_vars)
        for branch in branches
        for inside in emitted_class_strings(branch, set_vars)
        for after in emitted_class_strings(suffix, set_vars)
    ]
    return list(dict.fromkeys(emitted))


def worst_case_dark_text_colour_count(
    attr: str,
    *,
    alpine: bool = False,
    set_vars: dict[str, list[str]] | None = None,
) -> int:
    """Return the most dark-text colours emitted together by any one render."""
    renders = alpine_class_alternatives(attr) if alpine else emitted_class_strings(attr, set_vars)
    return max(len(DARK_TEXT_COLOUR_PATTERN.findall(emitted)) for emitted in renders)
