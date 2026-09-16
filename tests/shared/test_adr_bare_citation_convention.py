"""phaze-dnez9: CLAUDE.md's "Cite ADRs by filename, never by bare number" convention, mechanized.

WHAT THIS GUARDS, AND HOW IT DIFFERS FROM `test_adr_citation_resolution.py`. That guard asks
"does the cited number RESOLVE to some `docs/design/NNNN-*.md`" -- it is silent on a citation
that resolves today but carries no redundancy, which is exactly the shape that let the
`phaze-f70y9` incident happen invisibly: a bare `ADR-0014` was well-formed and (eventually)
resolved, just to the wrong document. This guard asks the complementary question: does a
citation carry the redundancy the convention asks for AT ALL -- a nearby filename
(`NNNN-slug.md`, docs/design/-*.md, or bare, an ADR cross-linking a sibling) or a parenthetical
disambiguator immediately after the number, `ADR-0015 (shared session gather)`. A citation with
neither is bare even when the number resolves correctly right now, because nothing about the
citation would announce a future renumber the way a filename or a title would.

`tests/shared/test_adr_numbering.py` (duplicate ADR *files*) and `test_adr_citation_resolution.py`
(citations to a number no file holds) are the supply-side and resolution-side guards. This is the
redundancy-side guard: every one of the three fails with a different message pointing at a
different fix, and this repo's convention (see CONVENTIONS.md's "Cite ADRs by filename" section)
is to keep them apart rather than merge them into one do-everything check.

THE DETECTOR IS `scripts/adr_bare_citation_sweep.py`, IMPORTED, NOT REIMPLEMENTED. That module is
also the mechanical sweep that fixed the ~342 bare citations this bead measured (see its own
module docstring for the exemption rationale in full: the two file-level exemptions, the
docs/design H1 self-title rule, and the nine grandfathered lines that quote a bare citation as
historical or illustrative evidence rather than making a live one). Importing the same
`find_bare_citations()` the sweep tool uses means this guard and the tool that satisfies it can
never drift out of sync with each other -- a second, hand-copied regex here would be exactly the
kind of duplication that goes stale first.

WHAT COUNTS AS A REGRESSION. Any bare citation this guard's `find_bare_citations()` finds in the
live tree that is not one of: an ADR's own first-line H1 (coded, not listed -- its title tail is
inherently the redundancy), or a `LINE_GRANDFATHER` entry (listed, and the list is the point --
see the sweep module's docstring for why each one there is not sweepable). Any OTHER bare
citation this guard would find is new since the sweep and must be fixed at authoring time --
either add the filename or a parenthetical disambiguator -- not grandfathered by widening the
list after the fact, which is exactly the silent-leniency shape `phaze-jnj90` / `phaze-nqawu`
named and this convention exists to avoid repeating.
"""

from pathlib import Path

from scripts.adr_bare_citation_sweep import (
    DIR_EXEMPT_PREFIXES,
    FILE_EXEMPT,
    LINE_GRANDFATHER,
    adr_disambiguators,
    bare_spans,
    citation_lines,
    find_bare_citations,
    is_self_title_line,
    is_sweepable_line,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _format_bare(citation: tuple[str, int, str, str]) -> str:
    path, lineno, number, line = citation
    return (
        f"{path}:{lineno} cites ADR-{number} with neither a nearby filename nor a parenthetical "
        f"disambiguator.\n"
        f"    line: {line.strip()!r}\n"
        "    Add the filename (docs/design/NNNN-slug.md) or a disambiguator right after the "
        'number, e.g. "ADR-NNNN (slug words)" -- see CLAUDE.md\'s "Cite ADRs by filename, never '
        'by bare number" section. If this line DISCUSSES a bare citation as historical or '
        "illustrative evidence rather than making one, add it to LINE_GRANDFATHER in "
        "scripts/adr_bare_citation_sweep.py with a comment saying why -- see that module's "
        "docstring for the two existing shapes."
    )


def test_no_bare_adr_citation_remains_in_the_live_tree() -> None:
    """The live tree: every ADR citation carries a filename or a parenthetical disambiguator,
    except an ADR's own self-titling H1 and the explicitly grandfathered lines."""
    violations = [_format_bare(c) for c in find_bare_citations()]
    assert not violations, "bare ADR citation(s) with no redundancy:\n\n" + "\n\n".join(violations)


def test_every_grandfathered_line_is_still_actually_bare() -> None:
    """`LINE_GRANDFATHER` names lines that ARE bare and are deliberately left that way.

    If a future edit fixes one by hand (adds a disambiguator) without removing its grandfather
    entry, the entry becomes dead weight that silences nothing -- this pins that every entry is
    still doing real work, so a stale one is caught rather than accumulating forever.
    """
    disambiguators = adr_disambiguators()
    stale = []
    for path, lineno in sorted(LINE_GRANDFATHER):
        full_path = REPO_ROOT / path
        assert full_path.is_file(), f"LINE_GRANDFATHER names {path}, which does not exist"
        line = full_path.read_text(encoding="utf-8").splitlines()[lineno - 1]
        # Bypass the grandfather itself (empty set) to ask "would this be flagged WITHOUT the
        # entry protecting it" -- that is the definition of "still actually bare".
        if not bare_spans(path, lineno, line, disambiguators, line_grandfather=frozenset()):
            stale.append(f"{path}:{lineno}")
    assert not stale, "LINE_GRANDFATHER entry no longer bare (safe to remove): " + ", ".join(stale)


def test_the_worked_example_files_are_scanned_and_carry_grandfathered_lines() -> None:
    """CONVENTIONS.md and CLAUDE.md's own retelling of `phaze-f70y9` is real, in-scope prose --
    not path-exempted -- and is bare ONLY on the specific lines `LINE_GRANDFATHER` names.

    Mirrors `test_adr_citation_resolution.py::test_the_worked_example_files_are_scanned_and_clean`:
    pinned so a future path-scoped exemption for either file cannot be added quietly. CLAUDE.md is
    additionally `FILE_EXEMPT` here (a separate docs bead owns fixing its one bare citation), which
    this test also pins so that exemption cannot silently widen to cover a citation the file adds
    later at a different line.
    """
    assert "CONVENTIONS.md" not in FILE_EXEMPT
    assert not any("CONVENTIONS.md".startswith(prefix) for prefix in DIR_EXEMPT_PREFIXES)
    # CONVENTIONS.md's own bare citations are all grandfathered, so `find_bare_citations()`
    # (which excludes grandfathered lines by design) reports none for it -- that is the CORRECT
    # live-tree outcome, not evidence the file went unscanned. Prove it is scanned and bare-except-
    # for-the-grandfather by re-running the same detector with the grandfather lifted.
    disambiguators = adr_disambiguators()
    conventions_lines = [(path, lineno, line) for path, lineno, line in citation_lines() if path == "CONVENTIONS.md"]
    assert conventions_lines, "CONVENTIONS.md should cite ADRs but none were found by the citation prefilter"
    conventions_grandfathered = {
        (path, lineno) for path, lineno, line in conventions_lines if bare_spans(path, lineno, line, disambiguators, line_grandfather=frozenset())
    }
    assert conventions_grandfathered, "CONVENTIONS.md should carry its historical worked-example bare citations"
    assert conventions_grandfathered <= LINE_GRANDFATHER, (
        f"CONVENTIONS.md has a bare citation not in LINE_GRANDFATHER: {conventions_grandfathered - LINE_GRANDFATHER}"
    )

    assert "CLAUDE.md" in FILE_EXEMPT, "CLAUDE.md's one bare citation (line 528) is a separate docs bead's scope"


class TestDetectorMechanics:
    """Proves the parts fire, against in-memory fixtures, rather than asserting it in prose."""

    def test_a_citation_with_no_filename_and_no_paren_is_bare(self) -> None:
        spans = bare_spans("docs/whatever.md", 7, "See ADR-0001 for the rationale.", {"0001": "some slug"})
        assert [(number) for _s, _e, number in spans] == ["0001"]

    def test_a_citation_immediately_followed_by_a_disambiguator_is_not_bare(self) -> None:
        spans = bare_spans("docs/whatever.md", 7, "See ADR-0001 (some slug) for the rationale.", {"0001": "some slug"})
        assert spans == []

    def test_a_citation_with_a_same_line_filename_is_not_bare(self) -> None:
        line = "See [ADR-0001](docs/design/0001-some-slug.md) for the rationale."
        assert bare_spans("docs/whatever.md", 7, line, {"0001": "some slug"}) == []

    def test_a_relative_filename_link_without_the_docs_design_prefix_is_still_recognized(self) -> None:
        """The false positive this guard's sweep found and fixed: an ADR linking a SIBLING by a
        bare or `../design/`-relative path still carries the filename, just not the full
        `docs/design/` prefix -- see the sweep module's `FILENAME_RE` comment."""
        same_dir = "See [ADR-0002](0002-some-slug.md) for the rationale."
        parent_relative = "See [ADR-0002](../design/0002-some-slug.md) for the rationale."
        assert bare_spans("docs/design/0001-x.md", 7, same_dir, {"0002": "some slug"}) == []
        assert bare_spans("docs/spikes/x.md", 7, parent_relative, {"0002": "some slug"}) == []

    def test_a_citation_to_an_unassigned_number_is_not_this_guards_concern(self) -> None:
        """`test_adr_citation_resolution.py` catches a dangling number; this guard is silent on
        one, since it has nothing to say about redundancy for a citation that names nothing."""
        assert bare_spans("docs/whatever.md", 7, "See ADR-9999 for the rationale.", {"0001": "some slug"}) == []

    def test_an_adrs_own_h1_line_is_never_bare_regardless_of_content(self) -> None:
        assert is_self_title_line("docs/design/0004-ledger-replay-safety.md", 1) is True
        assert is_self_title_line("docs/whatever.md", 1) is False
        assert is_self_title_line("docs/design/0004-ledger-replay-safety.md", 2) is False
        line = "# ADR-0004 -- Regenerate expiring ledger payloads at replay time"
        assert bare_spans("docs/design/0004-ledger-replay-safety.md", 1, line, {"0004": "ledger replay safety"}) == []

    def test_a_file_exemption_silences_the_whole_file(self) -> None:
        assert is_sweepable_line("docs/whatever.md", file_exempt=frozenset({"docs/whatever.md"})) is False
        spans = bare_spans(
            "docs/whatever.md",
            7,
            "See ADR-0001 for the rationale.",
            {"0001": "some slug"},
            file_exempt=frozenset({"docs/whatever.md"}),
        )
        assert spans == []

    def test_a_directory_exemption_silences_every_file_under_it(self) -> None:
        prefixes = ("docs/telemetry/measurements/",)
        assert is_sweepable_line("docs/telemetry/measurements/x.md", dir_exempt_prefixes=prefixes) is False
        assert is_sweepable_line("docs/telemetry/exporter.md", dir_exempt_prefixes=prefixes) is True

    def test_a_line_grandfather_silences_only_its_own_line(self) -> None:
        grandfather = frozenset({("docs/whatever.md", 7)})
        excused = bare_spans("docs/whatever.md", 7, "See ADR-0001 for the rationale.", {"0001": "some slug"}, line_grandfather=grandfather)
        unexcused = bare_spans("docs/whatever.md", 9, "See ADR-0001 for the rationale.", {"0001": "some slug"}, line_grandfather=grandfather)
        assert excused == []
        assert [n for _s, _e, n in unexcused] == ["0001"]

    def test_two_citations_on_one_line_are_each_judged_independently(self) -> None:
        """CONVENTIONS.md:223's real shape: the same number cited twice on one line, only some
        occurrences of which may carry the redundancy -- each occurrence gets its own verdict."""
        line = "ADR-0001 (some slug) and later, unrelatedly, ADR-0001 again."
        spans = bare_spans("docs/whatever.md", 3, line, {"0001": "some slug"})
        assert len(spans) == 1
        start, _end, number = spans[0]
        assert number == "0001"
        assert line[start:].startswith("ADR-0001 again")
