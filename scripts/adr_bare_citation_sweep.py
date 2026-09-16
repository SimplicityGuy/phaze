"""phaze-dnez9: mechanical sweep for CLAUDE.md's "Cite ADRs by filename, never by bare number"
convention, plus the detector `tests/shared/test_adr_bare_citation_convention.py` imports so the
sweep tool and the guard can never drift apart.

WHAT "BARE" MEANS HERE, AND HOW IT DIFFERS FROM `test_adr_citation_resolution.py`. That file's
guard asks "does the cited number RESOLVE to some `docs/design/NNNN-*.md`" -- it is silent on a
citation that resolves but carries no redundancy. This module asks the complementary question:
does a citation carry the redundancy CLAUDE.md's convention asks for at all -- a nearby filename
(`NNNN-slug.md`, at any relative depth: `docs/design/`, `design/`, `../design/`, or bare) or a
parenthetical disambiguator immediately after the number, `ADR-0015 (shared session gather)`. A
citation with neither is BARE even when the number it names resolves correctly today, because a
future renumber can silently repoint it -- exactly the `phaze-f70y9` shape both guards exist to
prevent, from opposite ends: resolution catches a number that names nothing; bareness catches a
number that would not announce a repoint if one happened.

EXEMPTIONS -- deliberately narrow, and each one is one of only four shapes:

1. `FILE_EXEMPT`: whole files this sweep must not write to. `CLAUDE.md` (root) is a separate docs
   bead's scope, not this one's -- see the bead. `tests/shared/test_adr_citation_resolution.py` is
   the self-referential fixture file for the OTHER guard: it quotes the `f4c39654` forward
   citation verbatim and constructs synthetic bad-citation literals (`ADR-9999`) as test data, so
   "fixing" its bareness would corrupt its own fixtures -- the same reasoning that file's own
   `_EXEMPT_FILES` already applies to itself. `docs/documentation-audit-2026-08-19.md` is one of
   `scripts.audit_historical_evidence.ARCHIVE_BOUNDARIES`' own boundary keys (see (2)) that happens
   to be a single file rather than a directory.
2. `DIR_EXEMPT_PREFIXES`: derived from `scripts.audit_historical_evidence.ARCHIVE_BOUNDARIES` --
   imported, not hand-copied, so the two lists cannot drift apart -- rather than hardcoded, because
   a first pass here hardcoded only `docs/telemetry/measurements/` and an actual run against
   `tests/docs_floor.txt` caught 12 files under `.planning/` and `docs/spikes/` it had silently
   corrupted: `tests/shared/test_historical_evidence_audit.py`'s audit treats its whole archive
   (`.planning/`, `docs/spikes/`, `docs/superpowers/specs/`, `docs/telemetry/measurements/`, plus
   the one file above) as evidence whose point-in-time claims must never be rewritten, and an ADR
   disambiguator insertion is not one of its approved identifier-only substitutions. This sweep
   must not write under ANY of these roots, regardless of how few or many bare citations they hold.
3. `LINE_GRANDFATHER`: specific lines that quote a bare citation verbatim as prose evidence, where
   inserting today's mapping would misstate what is being illustrated. Two distinct sub-shapes,
   found by grepping the pre-sweep census for "bare" within ~15 characters of an "ADR-" citation
   (`/bare[^.]{0,15}ADR|ADR[^.]{0,10}bare/i`) rather than assumed complete on the first pass --
   the second sub-shape below was found exactly this way, after an initial sweep had already
   corrupted it, which is why this note says so:
   a. A HISTORICAL retelling of the `phaze-f70y9` renumber: CONVENTIONS.md's and CLAUDE.md's own
      account, and one each in `scripts/select_impacted_tests.py`, `tests/shared/test_fast_gate.py`
      and `tests/shared/test_adr_numbering.py`. At the time each quoted citation was written,
      `ADR-0014` meant shared-session-gather, not the tracklist ADR that legally reused the number
      afterward -- stamping today's mapping onto the quote (`ADR-0014 (tracklist candidate sets)`)
      would assert something false about what was cited, directly contradicting the sentence's own
      "meaning the shared session gather decision" half. (`docs/documentation-audit-2026-08-19.md`'s
      own retelling of the same incident needs no entry here -- it is archive-boundary FILE_EXEMPT
      per (1), which already covers it.)
   b. An ILLUSTRATIVE example of bareness itself: `docs/design/0016-transferred-model-verification.md`'s
      comparison table has a cell reading "a citation *without* redundancy (a bare `ADR-0015`...)" --
      inserting the disambiguator turns the example into the opposite of what it is illustrating,
      since the whole point of the cell is that this citation has no parenthetical.
   Both are prose *discussing* a bare citation, not *making* one -- the same category
   `test_adr_citation_resolution.py`'s own docstring names for why `CLAUDE.md`/`CONVENTIONS.md` are
   in scope for ITS guard yet clean: the number discussed there merely happens to still resolve.
   Neither sub-shape here is safe to fix mechanically, so both are grandfathered rather than swept
   (or, for (a), left as a candidate for a hand-written historical aside -- a content decision, not
   a sweep).
4. An ADR file's own first line is coded rather than listed: `# ADR-NNNN -- <title>`. That
   heading's title tail already carries the disambiguation CLAUDE.md's rule asks for -- inserting
   `(slug words)` there would duplicate it (`# ADR-0004 (ledger replay safety) -- Regenerate
   expiring ledger payloads...` says the same thing twice). `docs/design/0017-...` already writes
   this form by hand (`# ADR-0017 (telemetry export topology): ...`); the other 17 files' plain
   title tail is treated as equivalent.

NEVER TOUCHES A NUMBER: `main()` only ever inserts a trailing ` (slug words)` immediately after an
existing citation. It cannot alter, remove or renumber anything a swept document already said.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# isort: off
from scripts.audit_historical_evidence import ARCHIVE_BOUNDARIES  # noqa: E402
# isort: on

CITATION_RE = re.compile(r"\bADR[ -](\d{4})\b(?![-\d])", re.IGNORECASE)
# A `NNNN-slug.md` filename anywhere on the line, at any relative depth -- `docs/design/`,
# `design/`, `../design/`, or bare (from within docs/design/ itself). No prefix is required: this
# checks only whether a filename naming the SAME number sits somewhere on the line, and a bare
# `\d{4}-...\.md` tail is sufficient evidence of that regardless of what comes before it.
FILENAME_RE = re.compile(r"\b(\d{4})-[A-Za-z0-9][A-Za-z0-9_-]*\.md")
ADR_FILENAME_RE = re.compile(r"^docs/design/(\d{4})-[^/]*\.md$")

FILE_EXEMPT = frozenset(
    {
        "CLAUDE.md",
        "tests/shared/test_adr_citation_resolution.py",
    }
    | {boundary for boundary in ARCHIVE_BOUNDARIES if not boundary.endswith("/")}
)
DIR_EXEMPT_PREFIXES = tuple(boundary for boundary in ARCHIVE_BOUNDARIES if boundary.endswith("/"))

LINE_GRANDFATHER = frozenset(
    {
        ("CONVENTIONS.md", 219),
        ("CONVENTIONS.md", 223),
        ("CONVENTIONS.md", 245),
        ("scripts/select_impacted_tests.py", 75),
        ("tests/shared/test_fast_gate.py", 621),
        ("tests/shared/test_adr_numbering.py", 25),
        ("docs/design/0016-transferred-model-verification.md", 266),
    }
)


def _git(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted git binary
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def adr_disambiguators() -> dict[str, str]:
    """Map each ADR number `docs/design/` holds, in the working tree, to its slug with hyphens as spaces."""
    out: dict[str, str] = {}
    for line in _git("ls-files", "--", "docs/design/").splitlines():
        match = ADR_FILENAME_RE.match(line)
        if match is None:
            continue
        number = match.group(1)
        slug = line.rsplit("/", 1)[-1][len(number) + 1 : -3]
        out[number] = slug.replace("-", " ")
    return out


def citation_lines() -> list[tuple[str, int, str]]:
    """Every `(path, 1-based lineno, line text)` for a line the ADR citation prefilter matches."""
    out = _git("grep", "-I", "-n", "-i", "-E", "ADR[ -]?[0-9]{4}")
    results: list[tuple[str, int, str]] = []
    for raw in out.splitlines():
        path, _, rest = raw.partition(":")
        lineno_text, _, text = rest.partition(":")
        if not lineno_text.isdigit():
            continue
        results.append((path, int(lineno_text), text))
    return results


def is_sweepable_line(
    path: str,
    *,
    file_exempt: frozenset[str] = FILE_EXEMPT,
    dir_exempt_prefixes: tuple[str, ...] = DIR_EXEMPT_PREFIXES,
) -> bool:
    """False for a file this sweep must never write to (see the module docstring).

    ``file_exempt`` / ``dir_exempt_prefixes`` are parameters, not module lookups, so the guard
    test's mechanics tests can exercise this with synthetic exemption sets -- same pattern as
    `test_adr_citation_resolution.py`'s `_dangling(..., exempt_files=..., exempt_lines=...)`.
    """
    if path in file_exempt:
        return False
    return not any(path.startswith(prefix) for prefix in dir_exempt_prefixes)


def is_self_title_line(path: str, lineno: int) -> bool:
    """True for an ADR's own first-line H1, whose title tail already disambiguates itself."""
    return lineno == 1 and path.startswith("docs/design/")


def bare_spans(
    path: str,
    lineno: int,
    line: str,
    disambiguators: dict[str, str],
    *,
    file_exempt: frozenset[str] = FILE_EXEMPT,
    dir_exempt_prefixes: tuple[str, ...] = DIR_EXEMPT_PREFIXES,
    line_grandfather: frozenset[tuple[str, int]] = LINE_GRANDFATHER,
) -> list[tuple[int, int, str]]:
    """`(match_start, match_end, number)` for each citation on this line that is BARE.

    Excludes citations to a number `docs/design/` does not hold at all -- an unassigned number is
    `test_adr_citation_resolution.py`'s concern, not this one's -- and applies every exemption in
    the module docstring. Does not mutate anything; `main()` and the guard test both call this.
    The three exemption sets are parameters (defaulting to the module constants) for the same
    reason `is_sweepable_line`'s are: so a mechanics test can pass its own fixture sets rather
    than mutate module state, and so a reader can see at each call site which set was in force.
    """
    if (
        not is_sweepable_line(path, file_exempt=file_exempt, dir_exempt_prefixes=dir_exempt_prefixes)
        or is_self_title_line(path, lineno)
        or (path, lineno) in line_grandfather
    ):
        return []
    spans: list[tuple[int, int, str]] = []
    for match in CITATION_RE.finditer(line):
        number = match.group(1)
        if number not in disambiguators:
            continue
        if any(fm.group(1) == number for fm in FILENAME_RE.finditer(line)):
            continue
        if re.match(r"\s*\(", line[match.end() :]):
            continue
        spans.append((match.start(), match.end(), number))
    return spans


def find_bare_citations() -> list[tuple[str, int, str, str]]:
    """`(path, lineno, number, line)` for every bare, non-exempt, non-grandfathered citation in the live tree.

    Read-only, and always evaluated against the CURRENT module constants -- the guard test calls
    this directly for its live-tree assertion; `main()` below is the only writer.
    """
    disambiguators = adr_disambiguators()
    found: list[tuple[str, int, str, str]] = []
    for path, lineno, line in citation_lines():
        for _start, _end, number in bare_spans(path, lineno, line, disambiguators):
            found.append((path, lineno, number, line))
    return found


def main() -> None:
    disambiguators = adr_disambiguators()
    lines_by_file: dict[str, set[int]] = {}
    for path, lineno, _line in citation_lines():
        lines_by_file.setdefault(path, set()).add(lineno)

    total_insertions = 0
    changed_files = 0
    for path, linenos in lines_by_file.items():
        file_path = REPO_ROOT / path
        if not file_path.is_file():
            continue
        raw_lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        touched = False
        for lineno in linenos:
            idx = lineno - 1
            if idx >= len(raw_lines):
                continue
            raw_line = raw_lines[idx]
            has_nl = raw_line.endswith("\n")
            body = raw_line[:-1] if has_nl else raw_line
            spans = bare_spans(path, lineno, body, disambiguators)
            if not spans:
                continue
            new_body = body
            for _start, end, number in sorted(spans, key=lambda s: s[0], reverse=True):
                new_body = new_body[:end] + f" ({disambiguators[number]})" + new_body[end:]
                total_insertions += 1
            raw_lines[idx] = new_body + ("\n" if has_nl else "")
            touched = True
        if touched:
            file_path.write_text("".join(raw_lines), encoding="utf-8")
            changed_files += 1

    print(f"Inserted {total_insertions} disambiguator(s) across {changed_files} file(s).")  # noqa: T201


if __name__ == "__main__":
    main()
    sys.exit(0)
