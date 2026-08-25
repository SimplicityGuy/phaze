"""phaze-x533t: every ``ADR-NNNN`` cited in a tracked file must name a number that some
``docs/design/NNNN-*.md`` actually holds, and every ADR file's own H1 must agree with its
filename's number.

THE HOLE THIS DOES NOT CLOSE, STATED FIRST BECAUSE MISREADING IT WOULD BE WORSE THAN HAVING NO
GUARD AT ALL. **This is a link check, not a meaning check. It goes GREEN the moment the cited
number is occupied by any file, whatever the citation meant.** A citation that misresolves --
names a number that exists but describes a different document -- is invisible to it by
construction, because nothing in the citation text says which document was intended. That is
exactly the shape ``phaze-f70y9`` measured and documented: ``4a08e873`` renumbered the tracklist
ADR onto 0014 while ``d4f673ac`` had introduced the shared-session-gather ADR as 0014; session
gather was pushed to 0015, and **8 bare citations meaning session gather were left resolving to
the tracklist ADR**. Every one of those 8 passes this guard. ``phaze-f70y9`` concluded that
detecting them mechanically would require judging whether a citation's surrounding prose
describes the document the number resolves to -- a semantic check, not a link check -- and
declined to build a weak approximation of one. So do not cite this file as protection against
citation reuse. The mitigations for that shape are conventions, not code: cite ADRs **by
filename**, and on a renumber sweep both the number VACATED and the number newly OCCUPIED (see
``CONVENTIONS.md`` and the ADR-numbering section of ``CLAUDE.md``).

WHAT IT DOES CLOSE: the AUTHORING-TIME window. A forward citation -- a number written down before
any file claims it -- is dangling from the instant it is written, and stays dangling until
something occupies the number. During that window this guard sees it. That window is not
theoretical; it is the pinned fixture in ``test_the_guard_goes_red_on_the_f4c39654_forward_citation``
below, taken from this repo's own history rather than invented.

WHY A SEPARATE FILE FROM ``tests/shared/test_adr_numbering.py``. That guard (``phaze-x2z38``)
checks that no two ADR *files* share a leading number -- the supply side. This one checks that no
*citation* names a number no file holds -- the demand side. They are complementary halves of the
same invariant ("a number is cited only after it is uniquely and consistently assigned") and
deliberately fail with different messages pointing at different fixes, so they are kept apart.
The third piece, ``test_every_adr_files_h1_matches_its_filename`` below, lives here rather than
with the numbering guard because a disagreeing H1 is a *resolution* defect -- the file resolves to
one number by its name and another by its own text -- not a uniqueness one.

SCOPE. Every tracked file the ``git grep`` below can read as text: ``-I`` skips binaries, and
nothing else is filtered. That is deliberately the whole tree rather than a pathspec list -- a
dangling citation is equally wrong in ``pyproject.toml`` (where the pinned fixture lives), in a
CSS file, in a workflow, or in prose, and 344 citations across ~60 files is one cheap ``git grep``.

EXEMPTIONS, AND WHY THE OBVIOUS ONE IS NOT TAKEN.

- ``_EXEMPT_FILES`` holds exactly one entry: **this file**, which must contain dangling-number
  literals as detector fixtures and quotes the historical ``f4c39654`` line verbatim. Scanning
  itself, it would fail on its own test data. ``tests/shared/test_operator_attribution_citations.py``
  is the in-repo precedent for exempting a file that is about the vocabulary it checks, and it
  found the same thing the same way -- invisible while untracked, since ``git grep`` does not see
  an untracked file, and surfacing only on the first commit.
- ``CONVENTIONS.md`` and ``CLAUDE.md`` are **NOT** exempt, and this is the deliberate call rather
  than an oversight. Both quote "ADR-0014" as the worked example of a bad citation -- prose that
  DISCUSSES a citation rather than making one, which criterion 5 of this bead requires the guard
  not to fire on. It does not fire on them, but not because they are excused: the number they
  name resolves, so there is nothing to fire on. Exempting them by path would be strictly worse
  than useless -- ``CLAUDE.md`` carries more real citations than almost any other file in the
  tree, so a whole-file exemption would blind the guard exactly where it is most likely to earn
  its keep. ``test_the_worked_example_files_are_scanned_and_clean`` pins that they are in the
  scanned population and clean, so a future path-scoped exemption cannot be added quietly.
- ``_EXEMPT_LINES`` is the sanctioned escape hatch for the case that exemption pressure would
  otherwise create: a worked example that must name a genuinely unassigned number. It is a
  (path, needle) pair so an exemption anchors to the one line it excuses rather than the whole
  file, matching ``_KNOWN_SHAPE_EXCLUSIONS`` in the attribution guard. **It is empty today** --
  every discussed number in the tree happens to resolve -- so the mechanism is proved by
  ``TestDetectorMechanics.test_a_line_exemption_silences_only_its_own_line`` rather than by any
  live entry. Adding an entry is an auditable act at the call site; widening the guard's default
  leniency would be invisible to every file downstream, which is the ``phaze-jnj90`` /
  ``phaze-nqawu`` defect class ("exit 0 having measured nothing").

READING HISTORY IN A TEST IS ESTABLISHED HERE AND IS SAFE IN CI.
``tests/review/services/test_review_refactor_parity.py`` (``phaze-b4u3p``) already reads a
pre-refactor file out of git history, and the ``test`` job in ``.github/workflows/tests.yml``
carries ``fetch-depth: 0`` for exactly that reason. The ``shared-rest`` shard that runs this file
is a leg of that same job, so the pinned fixture below needs no workflow change. If the object is
ever genuinely absent the test ERRORS rather than skipping -- a skip would silently retire this
file's only red-on-real-history evidence.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


# tests/shared/test_adr_citation_resolution.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Computed rather than hardcoded so the self-exemption tracks a rename instead of drifting.
_SELF_PATH = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()

# The commit that introduced the forward citation this guard exists to catch, and the file it
# put it in. Pinned, not derived: the point of the fixture is that it is a real shape from this
# repo's history, so it must not move when history does.
_FORWARD_CITATION_REV = "f4c39654"
_FORWARD_CITATION_PATH = "pyproject.toml"

# `git grep`'s prefilter. Kept looser than `_CITATION_RE` on purpose -- git's regex engine and
# Python's need not agree, so git only narrows the candidate lines and Python decides what is a
# citation. Case-insensitive because a lowercase citation is still a citation, even though all
# 344 occurrences in the tree today are uppercase.
_GREP_PATTERN = "ADR[ -]?[0-9]{4}"

# Both separator forms in use: `ADR-0012` (342 occurrences) and `ADR 0008` (2, the H1s of
# docs/design/0008-changes-review-approval-boundary.md and 0009-responsive-accessibility-baseline.md).
# The trailing `(?![-\d])` keeps a date-shaped tail out: "ADR 2026-08-24" is a date sitting next
# to the word, not a citation of a four-digit number.
_CITATION_RE = re.compile(r"\bADR[ -]?(\d{4})\b(?![-\d])", re.IGNORECASE)

# The ADR files themselves. `docs/design/` holds nothing but ADRs today; a file there that does
# not match this shape simply contributes no number, which is the right behaviour for a README.
_ADR_FILENAME_RE = re.compile(r"^docs/design/(\d{4})-[^/]*\.md$")

# Both H1 title forms in use today, tolerated deliberately rather than normalized:
#   `# ADR-0012 — Verification fidelity, ...`   (em dash, 13 files)
#   `# ADR 0008: Changes Review Approval Boundary`  (space + colon, 0008 and 0009)
# The separator between the word and the number is `[ -]`, and what follows the number is left
# entirely unconstrained -- this checks the NUMBER, and policing punctuation would turn a cheap
# correctness guard into a style guard nobody asked for.
_H1_RE = re.compile(r"^#\s+ADR[ -](\d{4})\b")

_EXEMPT_FILES = frozenset({_SELF_PATH})

# (path, needle) -- see the module docstring. Empty today by measurement, not by omission.
_EXEMPT_LINES: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _Citation:
    path: str
    lineno: int
    number: str
    line: str


def _git(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted git binary
        ["git", *args],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _adr_numbers(rev: str | None = None) -> dict[str, str]:
    """Map each number `docs/design/` holds to the filename holding it, in the working tree or at ``rev``.

    A duplicate leading number (which `tests/shared/test_adr_numbering.py` is the guard for)
    collapses here to one entry -- this side of the invariant only asks whether the number is
    occupied at all, so which of two files won the dict is irrelevant to it.
    """
    listing = _git("ls-tree", "--name-only", rev, "docs/design/") if rev else _git("ls-files", "--", "docs/design/")
    numbers: dict[str, str] = {}
    for line in listing.splitlines():
        match = _ADR_FILENAME_RE.match(line)
        if match:
            numbers[match.group(1)] = line
    return numbers


def _citations(rev: str | None = None) -> list[_Citation]:
    """Every ADR citation in every tracked text file, in the working tree or at ``rev``."""
    args = ["grep", "-I", "-n", "-i", "-E", _GREP_PATTERN]
    if rev:
        args.append(rev)
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted git binary
        ["git", *args],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep exits 1 on "no matches found", which is not an error here. Anything above that is.
    if completed.returncode > 1:
        raise RuntimeError(f"git grep failed (rc={completed.returncode}): {completed.stderr.strip()}")
    citations: list[_Citation] = []
    for raw in completed.stdout.splitlines():
        # `<path>:<lineno>:<text>` in the working tree, `<rev>:<path>:<lineno>:<text>` at a rev.
        body = raw.split(":", 1)[1] if rev else raw
        path, _, rest = body.partition(":")
        lineno_text, _, text = rest.partition(":")
        if not lineno_text.isdigit():
            continue
        citations.extend(_Citation(path=path, lineno=int(lineno_text), number=m.group(1), line=text) for m in _CITATION_RE.finditer(text))
    return citations


def _dangling(
    citations: list[_Citation],
    numbers: dict[str, str],
    *,
    exempt_files: frozenset[str] = _EXEMPT_FILES,
    exempt_lines: tuple[tuple[str, str], ...] = _EXEMPT_LINES,
) -> list[_Citation]:
    """The citations naming a number no file holds, after exemptions.

    ``exempt_files`` / ``exempt_lines`` are parameters rather than module lookups so the
    mechanics tests can exercise the filters with their own fixtures instead of mutating module
    state -- and so a reader can see at each call site which exemption set was in force.
    """
    return [
        citation
        for citation in citations
        if citation.number not in numbers
        and citation.path not in exempt_files
        and not any(citation.path == path and needle in citation.line for path, needle in exempt_lines)
    ]


def _format_dangling(citation: _Citation) -> str:
    return (
        f"{citation.path}:{citation.lineno} cites ADR-{citation.number}, but no "
        f"docs/design/{citation.number}-*.md exists.\n"
        f"    line: {citation.line.strip()!r}\n"
        "    Either the number is wrong, or the ADR has not landed yet. Do NOT cite a number "
        "before its file exists: a forward citation is dangling when written and silently becomes "
        "*wrong* once something else claims the number. Cite the ADR by FILENAME "
        "(docs/design/NNNN-slug.md), which cannot dangle silently -- see CONVENTIONS.md. If this "
        "line DISCUSSES a bad citation rather than making one, add a needle-scoped entry to "
        "_EXEMPT_LINES in tests/shared/test_adr_citation_resolution.py with a comment saying why."
    )


def test_every_adr_citation_resolves_to_an_existing_design_doc() -> None:
    """The live tree: no tracked file cites an ADR number that `docs/design/` does not hold."""
    numbers = _adr_numbers()
    assert numbers, "no docs/design/NNNN-*.md files found -- the guard would pass vacuously"

    violations = [_format_dangling(c) for c in _dangling(_citations(), numbers)]
    assert not violations, "dangling ADR citation(s):\n\n" + "\n\n".join(violations)


def test_every_adr_files_h1_matches_its_filename() -> None:
    """Each `docs/design/NNNN-*.md` agrees with itself: its own H1 names the number its filename does.

    Catches the file side of a renumber slip -- a `git mv 0004-x.md 0014-x.md` that leaves
    `# ADR-0004` inside the file. `4a08e873` was exactly that rename and its author did remember
    to change the H1 (one line, in the renamed file); this is for the author who does not.

    Both title forms in the tree are accepted -- see `_H1_RE`. A file whose first heading is not
    an ADR heading at all is reported too, since an ADR that does not say which ADR it is cannot
    be checked against anything.
    """
    violations = []
    for number, path in sorted(_adr_numbers().items()):
        first_line = (_REPO_ROOT / path).read_text(encoding="utf-8").splitlines()[0] if (_REPO_ROOT / path).stat().st_size else ""
        match = _H1_RE.match(first_line)
        if match is None:
            violations.append(f"{path}:1 has no `# ADR-NNNN` / `# ADR NNNN` heading on its first line; got {first_line!r}")
        elif match.group(1) != number:
            violations.append(f"{path}:1 is titled ADR-{match.group(1)} but its filename says {number}; got {first_line!r}")
    assert not violations, "ADR file(s) whose H1 disagrees with the filename:\n\n" + "\n".join(violations)


def test_the_guard_goes_red_on_the_f4c39654_forward_citation() -> None:
    """RED on real history, permanently re-runnable: the shape this guard exists to catch.

    `f4c39654` added `pyproject.toml:365` -- "That is `services/k8s_quantity.py`'s job, at config
    load. See ADR-0014." -- at a commit where `docs/design/` topped out at `0013-ffmpeg-pin.md`.
    There was no 0014. The citation was dangling the moment it was written; a human reviewer
    caught it at `phaze-frq98.2`'s review gate and `1ff74344` repaired it. No automated check
    found it, and this test is the standing proof that this one would have.

    This runs the SAME `_citations` / `_adr_numbers` / `_dangling` pipeline the live-tree test
    above runs -- only the tree it is pointed at differs -- so it cannot pass while the real
    detector is broken. A synthetic fixture would prove much less (the `phaze-jnj90` /
    `phaze-nqawu` standard: a guard that has never failed proves nothing).
    """
    numbers = _adr_numbers(_FORWARD_CITATION_REV)
    assert "0013" in numbers, "fixture drift: 0013 should exist at the pinned commit"
    assert "0014" not in numbers, "fixture drift: 0014 must NOT exist at the pinned commit"

    dangling = _dangling(_citations(_FORWARD_CITATION_REV), numbers)
    offenders = {(c.path, c.number) for c in dangling}
    assert (_FORWARD_CITATION_PATH, "0014") in offenders, f"the pinned forward citation was not flagged; flagged: {sorted(offenders)}"


def test_the_worked_example_files_are_scanned_and_clean() -> None:
    """`CONVENTIONS.md` and `CLAUDE.md` quote a bad citation as a worked example, are IN scope, and pass.

    Pinned so that a future path-scoped exemption for either file cannot be added quietly: they
    are clean because the number they name resolves, not because they are excused. Losing that
    distinction would blind the guard on the two files carrying the most citations in the tree.
    """
    scanned = {c.path for c in _citations()}
    for path in ("CONVENTIONS.md", "CLAUDE.md"):
        assert path in scanned, f"{path} cites ADRs but is not in the scanned population"
        assert path not in _EXEMPT_FILES, f"{path} must not be path-exempt -- see the module docstring"

    numbers = _adr_numbers()
    worked_example = [c for c in _citations() if c.path in {"CONVENTIONS.md", "CLAUDE.md"}]
    assert worked_example, "expected the worked-example citations to be found"
    assert not _dangling(worked_example, numbers)


class TestDetectorMechanics:
    """Proves the parts fire, against in-memory fixtures, rather than asserting it in prose."""

    def test_an_unassigned_number_is_flagged(self) -> None:
        citation = _Citation(path="docs/whatever.md", lineno=7, number="9999", line="See ADR-9999 for the rationale.")
        assert _dangling([citation], {"0001": "docs/design/0001-audiomuse-ai-no-go.md"}) == [citation]

    def test_an_assigned_number_is_not_flagged(self) -> None:
        citation = _Citation(path="docs/whatever.md", lineno=7, number="0001", line="See ADR-0001 for the rationale.")
        assert _dangling([citation], {"0001": "docs/design/0001-audiomuse-ai-no-go.md"}) == []

    def test_a_file_exemption_silences_the_whole_file(self) -> None:
        citation = _Citation(path="docs/whatever.md", lineno=7, number="9999", line="See ADR-9999.")
        assert _dangling([citation], {}, exempt_files=frozenset({"docs/whatever.md"})) == []

    def test_a_line_exemption_silences_only_its_own_line(self) -> None:
        """The empty `_EXEMPT_LINES` mechanism, exercised with a synthetic entry.

        The second citation is in the SAME file with a different line text and must still be
        flagged -- a needle-scoped exemption excuses the line it names, never the file it is in.
        """
        excused = _Citation(path="docs/whatever.md", lineno=7, number="9999", line='the worked example quotes "ADR-9999" as a bad citation')
        unexcused = _Citation(path="docs/whatever.md", lineno=9, number="9999", line="See ADR-9999 for the rationale.")
        exemptions = (("docs/whatever.md", "the worked example quotes"),)
        assert _dangling([excused, unexcused], {}, exempt_lines=exemptions) == [unexcused]

    def test_both_separator_forms_are_recognized_as_citations(self) -> None:
        assert [m.group(1) for m in _CITATION_RE.finditer("See ADR-0012 and ADR 0008.")] == ["0012", "0008"]

    def test_a_lowercase_citation_is_still_a_citation(self) -> None:
        assert [m.group(1) for m in _CITATION_RE.finditer("see adr-0012")] == ["0012"]

    def test_a_date_shaped_tail_is_not_read_as_a_citation(self) -> None:
        assert list(_CITATION_RE.finditer("the ADR 2026-08-24 review")) == []

    def test_both_h1_title_forms_are_accepted(self) -> None:
        em_dash = _H1_RE.match("# ADR-0012 — Verification fidelity, and what may be called an operator decision")
        colon = _H1_RE.match("# ADR 0008: Changes Review Approval Boundary")
        assert em_dash is not None and em_dash.group(1) == "0012"
        assert colon is not None and colon.group(1) == "0008"

    def test_an_h1_naming_the_wrong_number_is_detectable(self) -> None:
        match = _H1_RE.match("# ADR-0004 — Candidate sets for the 1001Tracklists drain")
        assert match is not None
        assert match.group(1) != "0014"
