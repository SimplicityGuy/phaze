#!/usr/bin/env python3
"""Catch a literal `"` truncating a double-quoted Alpine/htmx directive attribute (phaze-e1zby).

THE HAZARD
    HTML offers NO escaping for the delimiter quote character. Inside `x-data="..."`, the FIRST
    literal `"` -- wherever it comes from: a JS string, a JS comment, embedded JSON -- always ends
    the attribute, unconditionally. Everything the author meant to come after it is silently
    dropped from the value. The component's JS then fails to parse, Alpine fails to initialise,
    and there is NO server-side signal: the page renders, the response is 200, and only the
    browser console knows anything is wrong.

    This happened twice in the same file (src/phaze/templates/shell/partials/record_host.html):
    once before this lint existed, and once again on 2026-08-20 -- while a warning comment about
    the FIRST time was sitting right there in the file, visible to the person who hit it again. A
    comment that did not prevent a recurrence is evidence that a comment is the wrong mechanism.
    This script is the replacement: a property of the codebase instead of something a developer
    has to remember.

THE CHECK (a heuristic, not a parser -- see LIMITS below)
    HTML's own truncation rule means "everything up to the first literal `"` after the opening
    quote" is EXACTLY what the browser will treat as the attribute's value -- no HTML or JS parsing
    needed to compute it, just string search. This script:

      1. finds each double-quote-delimited directive attribute in COVERAGE below,
      2. extracts that same prefix (up to the first literal `"` after the opening one),
      3. checks the extracted prefix's `{` / `}` brace balance.

    A premature internal quote almost always truncates mid-expression and leaves the prefix
    brace-unbalanced -- e.g. `typeof target.focus === "function"` inside an `x-data="{ ... }"`
    severs the object literal with several braces left unclosed. That is a strong, cheap,
    low-false-positive signal. See LIMITS below for what it is not.

COVERAGE (AC3)
    Covered directive families -- the ones whose value is JS or JSON, so a literal quote inside
    them is catastrophic rather than merely a stray character:
        x-data, x-show, x-if                     Alpine core directives
        x-on:EVENT  and its  @EVENT  shorthand    Alpine event binding
        :ATTR                                     Alpine attribute/property binding (x-bind)
        hx-on:EVENT  /  hx-on::EVENT               htmx inline event handlers (always JS)
        hx-vals, hx-vars, hx-headers               htmx JSON/JS-ish value attributes

    Deliberately NOT covered:
      - Single-quote-delimited directive attributes (e.g. `hx-vals='{"delta": -10}'`, the
        convention this codebase already uses to hold literal `"` JSON content safely). The
        mirror-image hazard -- a literal `'` truncating a single-quoted value -- is real but is
        the OPPOSITE check (looking for `'`, not `"`) and is out of this bead's stated scope.
      - htmx attributes whose value is a plain string, CSS selector or URL and is never JS/JSON:
        hx-get, hx-post, hx-patch, hx-put, hx-delete, hx-target, hx-swap, hx-swap-oob, hx-trigger,
        hx-push-url, hx-confirm, hx-indicator, hx-include, hx-disabled-elt, hx-sync, hx-ext, etc.
        Braces are not expected there, and the hazard mechanism (an embedded quote breaking a
        JS/JSON *expression*) does not apply to a bare selector or URL string.
      - Ordinary HTML attributes (href, class, id, style, data-*, aria-*, title, ...). A stray
        quote there is not silently catastrophic the same way -- there is no JS to fail to parse.
      - Anything about RENDERED output. This is a static scan of the template SOURCE as authored
        (which is what we want -- it catches the bug regardless of which Jinja branch renders),
        not a check on caller-supplied DATA appearing in an attribute at runtime. A quote coming
        from untrusted data at render time is a Jinja auto-escaping question, not this one.

LIMITS OF THE HEURISTIC -- READ BEFORE TRUSTING A CLEAN RUN (AC6)
    Brace-balance is a SIGNAL, not a proof of correctness, and this script is deliberately NOT a
    full HTML or JS parser -- the narrow structural fact that HTML cannot escape the delimiter
    quote makes a targeted string-search heuristic sufficient here, and a real parser dependency
    would be slower and disproportionate to the bug it defends against.

    Known false NEGATIVES (a real truncation this script will NOT catch):
      - The truncated prefix can be brace-balanced BY COINCIDENCE even though the attribute is
        broken -- e.g. a premature `"` lands right after a sub-expression that already closed its
        own braces (`x-data="{ a: 1 }, extra: ` truncates with `{ a: 1 }` alone looking balanced).
        There is no way to tell "balanced because correct" from "balanced because we got lucky"
        from the truncated text alone.
      - A truncation that leaves NO braces in the prefix at all (0 == 0) is invisible to this
        check by construction -- e.g. a directive whose value never uses `{}` in the first place.

    Known false POSITIVES (a flag with nothing actually wrong):
      - A safely single-quoted JS string that itself contains a literal, unmatched `{` or `}`
        character, e.g. `x-show="msg === 'unexpected }'"`. The double-quote delimiter is never at
        risk here, but the brace count still comes out uneven. Rare in practice (checked empirically
        against every current template -- see the pre-commit hook's own history for the result);
        if it ever happens, use the escape hatch below rather than fighting the heuristic.

    Neither list is exhaustive. Treat a clean run as "no cheaply-detectable instance of this
    specific bug shape", not as "these templates have no quoting bugs".

ESCAPE HATCH
    Put the literal marker `phaze-e1zby:allow-brace-mismatch` anywhere inside the attribute's
    value to suppress a flag on that one attribute. Deliberately per-attribute and typed inline
    (not a file-level or directory-level carve-out) so it never becomes an unread blanket
    exemption -- see tests/browser/conftest.py's untracked-test guard for the same design choice.

USAGE
    uv run python scripts/lint_template_attr_quotes.py [FILE ...]
    (no arguments -> scans every *.html file under src/phaze/templates/)

Exit code 0 if clean, 1 if any attribute looks truncated (or is missing its closing quote
entirely -- a different, equally real bug this same scan happens to also catch for free).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys


_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_ROOT = _REPO_ROOT / "src" / "phaze" / "templates"

_ALLOW_MARKER = "phaze-e1zby:allow-brace-mismatch"

# Attribute-name alternatives for the directive families in COVERAGE above. Order does not matter
# for correctness (each alternative has a distinct, non-overlapping prefix character), but keeping
# the more specific `hx-on:`/`x-on:` forms ahead of the bare `@`/`:` shorthands documents which
# form each shorthand expands from.
_ATTR_NAME = (
    r"x-data"
    r"|x-show"
    r"|x-if"
    r"|x-on:[\w.:-]+"
    r"|hx-on::?[\w.:-]+"
    r"|hx-vals"
    r"|hx-vars"
    r"|hx-headers"
    r"|@[\w.:-]+"
    r"|:[A-Za-z][\w-]*"
)

# `(?<=\s)` anchors the match to an actual attribute position: HTML attributes are always preceded
# by whitespace (either after the tag name or after a previous attribute), so this rejects, e.g., a
# `:` that is merely part of a longer token (`xlink:href` will not match `:href`, because the char
# before the `:` is `k`, not whitespace).
_ATTR_RE = re.compile(rf'(?<=\s)(?P<attr>{_ATTR_NAME})="')


@dataclass(frozen=True, slots=True)
class Finding:
    """One directive attribute whose double-quoted value looks truncated (or unterminated)."""

    path: Path
    line: int
    attr: str
    value_preview: str
    opens: int
    closes: int
    unterminated: bool


def scan_text(text: str, path: Path) -> list[Finding]:
    """Find every truncated-looking (or unterminated) double-quoted directive attribute in `text`."""
    findings: list[Finding] = []
    for match in _ATTR_RE.finditer(text):
        attr = match.group("attr")
        value_start = match.end()
        close_idx = text.find('"', value_start)
        line = text.count("\n", 0, match.start()) + 1

        if close_idx == -1:
            # No closing quote anywhere in the rest of the file -- an unterminated attribute. Not
            # the bug this script was written for, but the same scan surfaces it for free, and it
            # is exactly as silently broken.
            findings.append(
                Finding(
                    path=path, line=line, attr=attr, value_preview="<no closing double quote found before EOF>", opens=0, closes=0, unterminated=True
                )
            )
            continue

        value = text[value_start:close_idx]
        if _ALLOW_MARKER in value:
            continue

        opens, closes = value.count("{"), value.count("}")
        if opens != closes:
            preview = value if len(value) <= 160 else f"{value[:160]}…"
            findings.append(Finding(path=path, line=line, attr=attr, value_preview=preview, opens=opens, closes=closes, unterminated=False))

    return findings


def format_finding(finding: Finding) -> str:
    """A message that names the file/line/attribute and explains the mechanism plainly (AC5)."""
    try:
        rel: Path = finding.path.relative_to(_REPO_ROOT)
    except ValueError:
        # Not under the repo root (e.g. a fixture file used to test this script itself) -- fall
        # back to the path as given rather than crashing on the one path shape that isn't ours.
        rel = finding.path
    header = f'{rel}:{finding.line}: `{finding.attr}="..."` looks truncated (phaze-e1zby template-attr-quote lint)'

    if finding.unterminated:
        return f"{header}\n  No closing double quote was found for this attribute before end of file.\n"

    return (
        f"{header}\n"
        "  HTML cannot escape the delimiter quote inside a double-quoted attribute value, so the FIRST literal "
        f"'\"' after `{finding.attr}=\"` always ends the attribute, no matter what was meant to follow it. Reading "
        "only up to that point (exactly what the browser sees), the value is:\n"
        f"      {finding.value_preview!r}\n"
        f"  which has {finding.opens} '{{' but {finding.closes} '}}' -- unbalanced. That almost always means a "
        "literal '\"' inside the intended expression (a JS string, a comment, or embedded JSON) closed the "
        "attribute early, silently truncating it and everything after it. The component's JS then fails to "
        "parse and Alpine/htmx breaks app-wide with no server-side signal at all.\n"
        "  Fix: use single quotes for any literal string INSIDE this attribute's JS/JSON (matches this "
        "codebase's own convention, e.g. `typeof x === 'function'`), or restructure to avoid a literal double "
        f"quote entirely. If this really is a false positive, add `{_ALLOW_MARKER}` inside the value.\n"
    )


def _default_files() -> list[Path]:
    return sorted(_TEMPLATE_ROOT.rglob("*.html"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    files = [Path(arg) for arg in args] if args else _default_files()
    html_files = [path for path in files if path.suffix == ".html"]

    all_findings: list[Finding] = []
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        all_findings.extend(scan_text(text, path))

    if not all_findings:
        return 0

    for finding in all_findings:
        print(format_finding(finding), file=sys.stderr)  # noqa: T201
    print(  # noqa: T201
        f"{len(all_findings)} finding(s) across {len({f.path for f in all_findings})} file(s). See messages above.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
