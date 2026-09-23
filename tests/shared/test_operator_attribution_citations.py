"""phaze-d2hgv.7: mechanizes ADR-0012 (verification fidelity and operator attribution) section 7's R2 -- CLAUDE.md guardrail G2, ``"Operator
decision" is a citation, not an emphasis marker``, made mechanical instead of cultural.

Generalizes ``tests/shared/test_no_exclude_newer_cooldown.py``, ADR-0012 (verification fidelity and operator attribution) section 5's *only*
operator decision in the repo with a machine-checked citation, from one ``pyproject.toml`` key to
every tracked file's use of the attribution vocabulary: any paragraph asserting that a decision
WAS an operator's carries an ISO date and a bead id somewhere in that same paragraph, or it fails
the build, with a message that says what to add and where to copy the shape from.

TWO REQUIREMENTS THAT ARE DELIBERATELY SEPARATE, both learned the hard way over this molecule's
four rounds of sweeps (dispatcher comment on phaze-d2hgv.7, 2026-08-21):

1. **Wrap-tolerant DETECTION.** Real citations in this repo wrap across source lines --
   ``src/phaze/services/video_audio.py`` had ``"... -- operator\\ndecision (phaze-3ea41)."`` at
   HEAD before phaze-d2hgv.1 repaired it. A regex anchored to one line cannot match that at all;
   every line-scoped sweep in this audit missed six such sites, including ADR-0012 (verification fidelity and operator attribution) section 5's
   own original inventory. The fix is NOT a bigger regex -- it is scanning whole paragraphs of
   already-whitespace-normalized text (see ``_normalize_paragraph`` below) so a mid-word line
   wrap is invisible to the vocabulary regex by construction.
2. **Paragraph-scoped CITATION WINDOW.** Separate from (1): the date and the bead id need only
   appear somewhere in the SAME paragraph as the vocabulary match, not on the same line. A
   line-scoped citation window would force real citations to be written badly to satisfy it
   (bead's own design section). Satisfying only one of (1)/(2) still misses real sites -- five of
   the six wrapped claims above span a comment/docstring wrap AND would have failed a paragraph
   check that only matched the vocabulary per-line.

SCANNING SURFACE: DOCSTRINGS AND COMMENTS ONLY, not arbitrary string literals. An attribution
claim is prose a human wrote to explain a decision -- that is where every real citation in this
repo lives. Scanning every ``ast.Constant`` string (assert messages, f-string UI text, membership
-check targets) was tried first and immediately produced false positives structurally unrelated to
citations: ``tests/shared/test_no_exclude_newer_cooldown.py`` itself contains
``assert "Operator decision 2026-08-03" in text`` -- a substring being searched FOR, not a claim
being made. Restricting to ``ast.get_docstring`` (module/class/function/async-function) plus
``tokenize`` comment runs removes that whole class of noise -- but this file's OWN docstrings
(this one included) discuss the vocabulary as their subject matter just as heavily as ADR-0012 (verification fidelity and operator attribution)
does, so ``_EXEMPT_FILES`` below exempts this file by its own path for the identical reason it
exempts the ADR (found the hard way: invisible while this file was untracked, since
``_scope_files()`` walks ``git ls-files``, and only surfaced on the first commit).

SCOPE: ``src/``, ``tests/``, ``scripts/``, ``alembic/``, ``docs/``, plus ``CLAUDE.md``, the
``Dockerfile*`` family and ``justfile`` -- the populations ADR-0012 (verification fidelity and operator attribution) section 5 actually swept.
``.planning/`` is deliberately excluded: ADR-0012 (verification fidelity and operator attribution) counted it as a superseded historical archive
and declined to audit it, and failing the build on it now would re-litigate that settled call.

ALLOWLIST BY SHAPE, NOT BY ENUMERATED FILE (dispatcher comment on phaze-d2hgv.7, 2026-08-21). A
wrap-tolerant sweep of this molecule found ``tasks/filename_convention.py`` carrying a SECOND
instance of the exact pattern ADR-0012 (verification fidelity and operator attribution) section 5 flagged only once, at
``tasks/controller.py:401`` -- a statement that a choice BELONGS TO the operator at runtime,
versus a claim that a decision WAS MADE. Both were reworded away (commit ``96c79af2``) before this
check landed, so neither appears in ``_KNOWN_SHAPE_EXCLUSIONS`` below as a per-file entry -- but a
per-file allowlist is itself an enumeration produced by a search and inherits that search's blind
spots, so ``_SHAPE_EXCLUSIONS`` recognizes the GRAMMATICAL SHAPE (an authority statement, not a
decision record) rather than any specific site. Section 6's authority-contrast pattern was found
independently at ``docs/spikes/phaze-han03-essentia-seek.md:519`` (*"that is an operator decision,
not a developer one"*) and ``docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md:592``
(*"...are operator decisions, not spike decisions"*) -- two more sites the same shape rule clears
without being told about either by name.

The three UI-domain shapes are the ones ADR-0012 (verification fidelity and operator attribution) section 5 counted as its 13 non-attribution
matches: an ``operator-chosen``/``operator-approved`` adjective describing an artifact ("an
operator-chosen sort key", "a manual, operator-approved cleanup"); an ``an operator choice/decision
of ...`` noun phrase ('an operator choice of "zero threads"'); and ``awaiting an operator
decision``, a PENDING state, not a record of one made. Together with the authority-contrast shape
above, these four regexes are ``_SHAPE_EXCLUSIONS`` -- the whole allowlist mechanism for uses that
are not attribution claims at all. Nothing here waives the date+bead requirement for a genuine
claim; it only recognizes text that was never a claim to begin with.

Two DIFFERENT exemption mechanisms cover the remaining meta-discussion, chosen deliberately per
site rather than reusing one for both:

``docs/design/0012-verification-fidelity-and-operator-attribution.md`` is excluded WHOLESALE, by
path, via ``_EXEMPT_FILES``. It is the ADR that DEFINES this vocabulary and inventories 55
existing matches by quoting and discussing them throughout -- not just in one section, exactly as
its own section 5 excluded itself when it counted only 9 "docs/" matches despite this file alone
containing far more. A needle-scoped exemption was tried first and rejected: the vocabulary
recurs in section 1 (Finding 1), section 4 (the guardrails), section 5 (the inventory) and section
7 (the recommendations) in prose that differs every time, so a needle list would either miss a
paraphrase or become as large as the file. THE TRADEOFF, stated rather than hidden: a genuinely
NEW, uncited attribution claim added to this ADR in the future -- one that is not quoting or
discussing an existing claim -- would not be caught by this test. That risk is accepted for this
one file because of what it is (the definition, not a claim site); it is not extended to any other
file in scope.

``CLAUDE.md``'s guardrail G2 paragraph, the verbatim copy of that paragraph that now also lives
in ``docs/git-topology-and-verification.md``, and ``scripts/recover_operator_decisions.py``'s
module docstring are narrower, via ``_KNOWN_SHAPE_EXCLUSIONS`` (path, needle) pairs: each is prose
ABOUT the citation requirement (defining it, or explaining a provenance-recovery tool's limits),
not an uncited claim that some specific decision was made -- but none of those files is
vocabulary-saturated the way the ADR is, so anchoring the exemption to its exact paragraph is both
possible and strictly better: a real, uncited attribution added anywhere ELSE in any of them is
still caught (see ``test_known_meta_exclusions_are_still_narrowly_scoped_to_their_needle`` below,
which runs against EVERY path listed in ``_KNOWN_SHAPE_EXCLUSIONS`` rather than one of them).

WHY THAT SECOND PATH EXISTS, AND THE GENERAL FORM IT BOUGHT (phaze-i5bs3, 2026-08-25). Commit
``a7205962`` extracted CLAUDE.md's verification-fidelity section into
``docs/git-topology-and-verification.md``, carrying guardrail G2's definition paragraph with it
while CLAUDE.md kept its own copy. The needle was already allowlisted -- but a
``_KNOWN_SHAPE_EXCLUSIONS`` entry is a (path, needle) pair, so the extracted copy was scanned as
an ordinary paragraph and reddened ``main``. The general form: **a path-scoped allowlist entry is
a citation to a LOCATION, and an extraction moves the location without moving the entry.** When
prose carrying an allowlisted needle is copied or extracted into a new file, that needle's path
set has to move with it, and nothing mechanical can notice on the extractor's behalf -- at the
moment the extraction is written the new path does not exist yet, so there is nothing to sweep
for. It is the same failure family as CLAUDE.md's rule to sweep both the ADR number VACATED and
the number newly OCCUPIED on a renumber, and it has the same mitigation: the seat performing the
move is the only one positioned to do the sweep.

A GAP THAT LOOKED UNCITABLE AND WASN'T: ``src/phaze/tasks/tracklist_drain_control.py:68`` --
*"phaze-6nrrf, operator decision: auto-disarm after 3 consecutive CRON-ENQUEUED slice failures."*
-- carried a bead id but no date, and unlike every other gap this sweep found, the specific claim
(3 consecutive failures being the OPERATOR's number, not the implementer's) was not independently
verified anywhere else in the tree. ``scripts/recover_operator_decisions.py --bead phaze-6nrrf``
found no ``AskUserQuestion`` record for it -- but that was the tool's documented false-negative
mode, not a genuine null: the question ("How many consecutive slice failures should auto-disarm
the drain?", asked in session ``4b27bb6e`` at 2026-08-11T16:13:19.880Z, answered "3 consecutive
failures" at 16:15:16.020Z, over "1 failure -- disarm immediately" and "5 consecutive failures")
never names the bead it was asked about, so a ``--bead`` search structurally cannot find it; a
``--since``/``--until`` date-range search does. This is now cited in place (date + bead id, per
the fix). The general lesson -- ``--bead`` mode's search form cannot return part of what it claims
to enumerate whenever the deciding exchange never names the bead -- is recorded here rather than
just fixed silently, per this molecule's own rule that a lesson learned at one site states its
general form.

AN ALLOWLIST THAT SWALLOWED ITS OWN SUBJECT (phaze-71q9y, found 2026-08-22 by dev/w4-jktlb by
deliberately breaking its own citations and expecting this guard to complain -- two of three did
not). Two of the four ``_SHAPE_EXCLUSIONS`` were written as GRAMMATICAL patterns, and the sentences
the guard exists to check share the grammar:

* the noun-phrase shape ``an operator (choice|decision) of`` also matches *"AN OPERATOR DECISION
  OF 2026-08-22"* -- the most natural way in English to date a decision;
* the authority-contrast shape ``is an operator decision ..., not`` also matches *"...that IS AN
  OPERATOR DECISION of 2026-08-22 recorded on bead phaze-jktlb, NOT an implementer's shortcut."*

Both still excluded those sentences whole, so deleting the date or the bead id from either passed
silently. The fix NARROWS them rather than deleting them -- both were added for false positives
that are still in the tree (``test_ui_domain_shapes_are_not_flagged`` and
``test_authority_contrast_shape_is_not_flagged`` below pin them verbatim), and trading a silent false
negative for a noisy false positive is a different failure, not a fix. The semantic distinction
both were reaching for is "names a configurable value, or says WHO decides" versus "records THAT
a decision was taken", and the one observable difference is citation evidence: a sentence carrying
an ISO date or a bead id is citing. So these two exclusions YIELD -- they do not fire on an
occurrence whose own sentence carries either -- and with only one of the two present the sentence
is then judged like any other and fails for the missing half.

WHY THE SENTENCE, NOT THE PARAGRAPH: measured, a paragraph-scoped gate reddened
``docs/spikes/phaze-han03-essentia-seek.md``, whose authority contrast is a genuine non-claim in a
paragraph that names a bead elsewhere. WHY ONLY THOSE TWO: the adjective shape
(``operator-chosen``) and the pending shape (``awaiting an operator decision``) were not shown to
swallow a real citation, and making them yield would be widening on a hypothesis. Measured at the
time of the fix, 2 of the 19 excluded occurrences in scope sit in a sentence with citation evidence
and belong to the adjective shape: ``docs/k8s-burst.md``'s table cell (an ``operator-chosen``
configurable -- a non-claim) and ``scripts/redis-seat-registry.sh``'s *"phaze-robzi.1's
operator-approved contract change"*, which reads as a claim and is left for its own bead rather
than widened into this one. THE RESIDUAL, stated rather than hidden: a sentence in a
yielding shape with NEITHER a date NOR a bead id is still excused, because with no evidence at all
it is grammatically indistinguishable from the non-claims above; the guard catches a HALF-cited
claim in these shapes, not an entirely uncited one.

THE ADJECTIVE SHAPE YIELDS TOO (phaze-h8ug4, 2026-09-23). The redis-seat-registry.sh sentence
above was the instance the previous paragraph was waiting for: *"(phaze-robzi.1's operator-approved
contract change is scoped to reclaim alone)"* carried a bead id and no date, and the unyielding
adjective shape excused it whole. So ``operator-chosen``/``operator-approved`` now yields on the same
sentence-scoped rule as the other two (pinned by
``test_the_adjective_shape_no_longer_swallows_the_registry_claim``, built from that sentence's own
text). The claim itself was recovered rather than relabelled: the decision is on record in the
phaze-robzi epic description, dated 2026-08-25, and is now cited in place. The UI-domain adjectives
still pass, because none of them shares a sentence with a date or a bead id. The one adjective that
did, ``docs/k8s-burst.md``'s table cell, was a non-claim naming a configurable cap whose bead ids cite
the RECOMMENDATION. So it was reworded to ``operator-configurable``, which is outside the vocabulary,
rather than allowlisted. That reword was the dispatcher's decision on phaze-h8ug4, not an operator's.

WHY EXCLUDED OCCURRENCES ARE NOT REPORTED ON A GREEN RUN (phaze-71q9y acceptance criterion 5,
decided here). Reporting was weighed and declined. After the narrowing, a yielding exclusion cannot
fire on any sentence that carries a date or a bead id -- that is, on the text of anyone who was
trying to cite -- so the population "I expected to be checked and was silently excused" is exactly
the residual above. The rest of what the allowlist excuses is stable UI adjectives, pending
states and authority contrasts; a block reprinting them on every green run is read once and then
habituated, which is invisible in a different way, and pytest captures a passing test's output
anyway. THE CHECK AN AUTHOR SHOULD RUN INSTEAD -- and the general form this bead exists for: **a
green guard proves the guard did not object, not that it examined you.** For any check carrying an
allowlist, the only way to know your case is covered is to break it deliberately -- delete the date
or the bead id from your paragraph -- and confirm this test goes red.

SCANNING SURFACE INCLUDES UNTRACKED FILES (phaze-71q9y, acceptance criterion 4). ``_scope_files``
used to enumerate with ``git ls-files`` (tracked files only), so a NEW, not-yet-staged file was not
scanned at all -- a developer writing a citation in a new module got a green run that meant nothing,
at exactly the moment they were most likely to be writing one. It now lists ``--cached --others
--exclude-standard``: tracked files plus untracked files git would add, minus anything
``.gitignore`` excludes (gate ``*.log`` files, ``.venv``). In CI the untracked set is empty, so this
changes nothing there; locally, an untracked in-scope scratch file IS scanned and can fail the run
-- deliberately, since it would fail the moment it was committed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import io
from pathlib import Path
import re
import subprocess
import tokenize
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


# tests/shared/test_operator_attribution_citations.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCOPE_PATHSPECS = ("src", "tests", "scripts", "alembic", "docs", "CLAUDE.md", "justfile", "Dockerfile*")

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# This hive's beads are always `phaze-<slug>` (optionally `.{child}` for a sub-issue) -- the
# provider/org/repo triplet CLAUDE.md's beadhive section describes. A bare bead-id shape without
# the `phaze-` prefix is deliberately NOT accepted: it is the difference between a citation and a
# word that merely looks like one.
_BEAD_ID_RE = re.compile(r"\bphaze-[0-9a-z]+(?:\.[0-9]+)*\b", re.IGNORECASE)
# ADR-0012 (verification fidelity and operator attribution) section 5's own inventory regex, `operator[- ](decision|confirmed|approved|directed|
# granted|chose|ruling)`, widened with `choice`/`chosen`/`decided` (both appear in this tree, e.g.
# `tests/analyze/services/pipeline/test_analysis_sizing.py`'s "operator choice of ...") and with
# `[-\s]+` in place of `[- ]` so a hyphen/space run of any length -- including one that used to
# cross a line wrap before paragraphs were normalized to single-spaced text below -- still matches.
_VOCAB_RE = re.compile(
    r"operator[-\s]+(decision|decided|confirmed|approved|directed|granted|chose|chosen|choice|ruling)",
    re.IGNORECASE,
)

# Grammatical shapes that match the vocabulary but are not attribution claims -- see the module
# docstring's "ALLOWLIST BY SHAPE" section for what each one is and where it was found. The third
# field is YIELDS TO CITATION EVIDENCE (phaze-71q9y): when True, the exclusion does not fire on an
# occurrence whose own sentence carries an ISO date or a bead id -- see the module docstring's
# "AN ALLOWLIST THAT SWALLOWED ITS OWN SUBJECT" section, and "THE ADJECTIVE SHAPE YIELDS TOO" (phaze-h8ug4)
# for the third.
_SHAPE_EXCLUSIONS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (
        re.compile(r"\boperator[- ](chosen|approved)\b", re.IGNORECASE),
        "'operator-chosen'/'operator-approved' is an adjective describing an artifact or affordance "
        "(a sort key, a cue sheet, a cleanup step), not a claim that a decision was made.",
        True,
    ),
    (
        re.compile(r"\ban?\s+operator\s+(?:choice|decision)\s+of\b", re.IGNORECASE),
        "'an operator choice/decision of ...' names what an operator can configure -- ADR-0012 (verification fidelity and operator attribution) "
        "section 5's own example is 'an operator choice of \"zero threads\"'.",
        True,
    ),
    (
        re.compile(r"\bawait(?:s|ing)?\s+an?\s+operator\s+decision\b", re.IGNORECASE),
        "'awaiting an operator decision' is a PENDING state -- no decision has been made yet to cite.",
        False,
    ),
    (
        re.compile(r"\b(?:is|are|remains?|stays?)\s+(?:an?\s+)?operator\s+decisions?\b[^.]{0,60}?,?\s*not\s", re.IGNORECASE),
        "an authority-contrast statement ('...is an operator decision, not a developer one') says WHO "
        "decides a class of choice, not that one specific decision WAS made on a date.",
        True,
    ),
)

# A sentence ends at `.`, `!` or `?` followed by whitespace or end of text -- so the dot inside a
# child bead id (`phaze-bk9el.19`) or a filename (`foo.py`) does not end one. Abbreviations like
# "e.g. " do end one; that only ever SHRINKS the window a yielding exclusion inspects for evidence.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")

# Narrow, file-scoped exemptions for text the shape rules above genuinely cannot express: prose
# ABOUT the citation vocabulary/requirement itself, not a new claim. Each entry is a (path, needle)
# pair -- the needle anchors the exemption to the specific paragraph so a NEW, unrelated citation
# added later to the same file is not silently exempted too. See the module docstring for why each
# of these three sites is meta-discussion rather than an attribution claim.
_KNOWN_SHAPE_EXCLUSIONS: tuple[tuple[str, str], ...] = (
    (
        "CLAUDE.md",
        "is a citation, not an emphasis marker",
    ),
    # The SAME paragraph, at a second path. Commit `a7205962` (2026-08-25) extracted CLAUDE.md's
    # verification-fidelity section into `docs/git-topology-and-verification.md` verbatim, so
    # guardrail G2's own definition now exists in two tracked files; CLAUDE.md kept its copy.
    # Both are prose ABOUT the citation requirement, so both take the same needle -- the wording
    # is a verbatim extraction and rewording it to dodge the vocabulary would corrupt the rule's
    # own statement of itself. Deliberately NOT an `_EXEMPT_FILES` entry: that document is not
    # vocabulary-saturated the way ADR-0012 (verification fidelity and operator attribution) is, so anchoring to the paragraph keeps every OTHER
    # paragraph in it checked (proved by `test_known_meta_exclusions_are_still_narrowly_scoped_
    # to_their_needle` below, which runs against every path listed here rather than just one).
    (
        "docs/git-topology-and-verification.md",
        "is a citation, not an emphasis marker",
    ),
    (
        "scripts/recover_operator_decisions.py",
        "reported nine",
    ),
    (
        "scripts/recover_operator_decisions.py",
        "Referenced from CLAUDE.md's guardrail G2",
    ),
    (
        "scripts/recover_operator_decisions.py",
        "is the standing example of a genuine null",
    ),
)

# tests/shared/test_operator_attribution_citations.py -> parents[2] == repo root, matching
# _REPO_ROOT above; computed once so the exemption below tracks a rename automatically instead
# of drifting from a hardcoded string.
_SELF_PATH = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()

# The ADR that defines and inventories this vocabulary discusses dozens of quoted claims by their
# original wording -- it is the one file in the tree that is EXPECTED to say "operator decision"
# far more often than it makes new ones. ADR-0012 (verification fidelity and operator attribution) section 5 excluded itself from its own docs/
# count for the same reason (9 matches across 8 files is far fewer than this file alone carries).
# TRADEOFF (deliberate, not overlooked): a whole-file exemption, not a needle-scoped one -- the
# vocabulary recurs in prose that differs every time across sections 1/4/5/7, so a needle would
# either miss a paraphrase or grow as large as the file. A genuinely NEW, uncited claim added to
# this ADR in the future would NOT be caught here. Accepted for this one file only, because of
# what it is (the vocabulary's definition site); see the module docstring for the full argument.
#
# THIS FILE IS EXEMPT FOR THE SAME REASON, discovered the hard way: once this file is `git add`ed
# and its own paragraphs enter `_scope_files()`'s population, its module/helper docstrings --
# which discuss the attribution vocabulary as their subject matter, same as ADR-0012 (verification fidelity and operator attribution) -- trip the
# very check they implement. `git ls-files` did not then include an untracked file, so this was
# invisible until the first commit (untracked files have been scanned since phaze-71q9y); catch it
# here rather than relying on that accident of timing.
# Same tradeoff as ADR-0012 (verification fidelity and operator attribution): a genuinely new, uncited claim added to a docstring in THIS file would
# not be caught either.
_EXEMPT_FILES = frozenset(
    {
        "docs/design/0012-verification-fidelity-and-operator-attribution.md",
        _SELF_PATH,
    }
)


@dataclass(frozen=True)
class _Paragraph:
    path: str
    lineno: int
    text: str


def _normalize_paragraph(body: str) -> str:
    """Collapse all whitespace runs (including newlines) to single spaces.

    This is the wrap-tolerance mechanism: once a paragraph's text has passed through here, a
    vocabulary match that used to straddle a line wrap (``"...operator\\ndecision..."``) reads
    identically to one written on a single line, so ``_VOCAB_RE`` needs no newline-awareness of
    its own.
    """
    return re.sub(r"\s+", " ", body).strip()


def _split_into_paragraphs(path: str, lineno: int, body: str) -> list[_Paragraph]:
    """Split ``body`` on blank lines into normalized paragraphs, all reporting ``lineno``.

    ``lineno`` is the body's own start line (the enclosing docstring/comment-run's first line),
    not each paragraph's true offset within it -- good enough to locate a failure by hand, and
    exact per-paragraph line tracking would add real complexity for no behavioural payoff here.
    """
    return [_Paragraph(path, lineno, _normalize_paragraph(block)) for block in re.split(r"\n\s*\n", body) if block.strip()]


def _merge_docstring_title(paragraphs: list[_Paragraph]) -> list[_Paragraph]:
    """Merge a docstring's first two paragraphs (PEP 257 one-line summary + extended description).

    A bead id is idiomatically named once, in the one-line summary (``\"\"\"phaze-XXXXX: does
    thing.``), while the decision prose and its date live in the paragraph below the mandatory
    blank line. Treating summary and body as two unrelated citation windows would fail the very
    precedent this test generalizes -- ``tests/shared/test_no_exclude_newer_cooldown.py``'s own
    module docstring is shaped exactly this way. Only paragraphs 0 and 1 of the SAME docstring are
    merged; a third paragraph of the same docstring is scoped on its own, as is every comment run.
    """
    if len(paragraphs) < 2:
        return paragraphs
    merged = _Paragraph(paragraphs[0].path, paragraphs[0].lineno, f"{paragraphs[0].text} {paragraphs[1].text}")
    return [merged, *paragraphs[2:]]


def _comment_run_paragraphs(path: str, text: str) -> list[_Paragraph]:
    """Paragraphs from ``#``-prefixed comment lines, for files with no Python AST to walk.

    A paragraph is a maximal run of comment lines with STRICTLY CONSECUTIVE line numbers and no
    blank ``#`` line in between -- a non-comment (code) line or a bare ``#`` always ends the run,
    which is what keeps two adjacent but unrelated comment blocks (a very common shape right above
    a list literal, e.g. ``tasks/controller.py``'s ``cron_jobs`` list) from being fused into one.
    """
    out: list[_Paragraph] = []
    buf: list[str] = []
    buf_start = 0
    for i, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            content = stripped[1:].strip()
            if not content:
                if buf:
                    out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
                    buf = []
                continue
            if not buf:
                buf_start = i
            buf.append(content)
        elif buf:
            out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
            buf = []
    if buf:
        out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
    return out


def _comment_run_paragraphs_python(path: str, text: str) -> list[_Paragraph]:
    """Same as :func:`_comment_run_paragraphs`, but token-based so it never misreads a `#` that
    appears inside a string literal as the start of a comment.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        return []
    out: list[_Paragraph] = []
    buf: list[str] = []
    buf_start = 0
    prev_line: int | None = None
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        content = tok.string.lstrip("#").strip()
        line = tok.start[0]
        if prev_line is not None and line != prev_line + 1 and buf:
            out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
            buf = []
        if not content:
            if buf:
                out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
                buf = []
            prev_line = line
            continue
        if not buf:
            buf_start = line
        buf.append(content)
        prev_line = line
    if buf:
        out.append(_Paragraph(path, buf_start, _normalize_paragraph(" ".join(buf))))
    return out


def _docstring_paragraphs(path: str, tree: ast.AST) -> list[_Paragraph]:
    """Every module/class/function/async-function docstring's paragraphs, title-merged.

    ``ast.get_docstring`` recognizes only the classic docstring shape (a bare string literal as
    the first statement of the body) -- an ordinary string literal anywhere else (an assert
    message, an f-string, a membership-check target) is never visited. That is deliberate: see the
    module docstring's "SCANNING SURFACE" section for why scanning every string literal was tried
    and rejected.
    """
    out: list[_Paragraph] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        lineno = node.body[0].lineno if node.body else getattr(node, "lineno", 1)
        out.extend(_merge_docstring_title(_split_into_paragraphs(path, lineno, doc)))
    return out


def _paragraphs_for_python(path: str, text: str) -> list[_Paragraph]:
    out = _comment_run_paragraphs_python(path, text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    return [*out, *_docstring_paragraphs(path, tree)]


def _paragraphs_for_file(repo_path: Path, *, rel: str | None = None) -> list[_Paragraph]:
    """Extract paragraphs from ``repo_path``, reporting it as ``rel`` (default: relative to the
    real repo root). ``rel`` exists for :class:`TestScannerMechanics`, whose fixtures live under
    ``tmp_path`` -- not under ``_REPO_ROOT`` -- but still want to exercise this exact function.
    """
    rel = rel if rel is not None else repo_path.relative_to(_REPO_ROOT).as_posix()
    try:
        text = repo_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    if repo_path.suffix == ".py":
        return _paragraphs_for_python(rel, text)
    if repo_path.suffix == ".md":
        # Markdown has no comment syntax of its own here -- the whole file is already prose, so a
        # blank-line split is the paragraph boundary directly.
        return _split_into_paragraphs(rel, 1, text)
    if repo_path.name.startswith("Dockerfile") or repo_path.name == "justfile" or repo_path.suffix in {".sh", ".yml", ".yaml"}:
        return _comment_run_paragraphs(rel, text)
    # Anything else in scope (patches, fixtures, HTML) is treated as plain prose: safe default,
    # and nothing in the current tree's scope needs a third comment dialect.
    return _split_into_paragraphs(rel, 1, text)


def _scope_files(root: Path = _REPO_ROOT) -> list[Path]:
    """Every in-scope file git knows about OR would add: tracked (``--cached``) plus untracked but
    not ignored (``--others --exclude-standard``).

    phaze-71q9y: ``--cached`` alone made a brand-new, not-yet-staged file invisible, so an author
    writing a citation in a new module got a green run that had not read it -- see the module
    docstring's "SCANNING SURFACE INCLUDES UNTRACKED FILES" section.
    """
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted git binary
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *_SCOPE_PATHSPECS],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / line for line in dict.fromkeys(completed.stdout.splitlines()) if line]


def _sentence_carries_citation_evidence(text: str, start: int, end: int) -> bool:
    """True if the sentence enclosing ``text[start:end]`` carries an ISO date or a bead id.

    phaze-71q9y: a sentence naming a date or a bead is CITING a decision, not naming a
    configurable value or saying whose authority a class of choice belongs to -- so a yielding
    shape exclusion must not excuse it. Scoped to the SENTENCE, not the paragraph, on measurement:
    ``docs/spikes/phaze-han03-essentia-seek.md``'s authority contrast (*"that is an operator
    decision, not a developer one"*) sits in a paragraph that names a bead elsewhere, and a
    paragraph-scoped gate turned that genuine non-claim into a false positive.
    """
    left = max((m.end() for m in _SENTENCE_END_RE.finditer(text, 0, start)), default=0)
    right_match = _SENTENCE_END_RE.search(text, end)
    sentence = text[left : right_match.end() if right_match else len(text)]
    return bool(_ISO_DATE_RE.search(sentence) or _BEAD_ID_RE.search(sentence))


def _has_unexcluded_vocab_match(text: str) -> bool:
    """True if at least one ``_VOCAB_RE`` occurrence in ``text`` is NOT covered by a shape
    exclusion's own match span.

    Checked per OCCURRENCE rather than "does any shape pattern match somewhere in the paragraph":
    a paragraph that mentions an excluded shape ('an operator-chosen sort key ties often') AND,
    separately, an uncited claim ('...was an operator decision') in the same paragraph must still
    fail on the second occurrence -- a shape exclusion recognizes a non-claim, it does not launder
    an unrelated claim sitting next to it. Every ``_SHAPE_EXCLUSIONS`` regex is written so its own
    match span always contains the ``operator ... <word>`` text it is excusing, so span
    containment is enough to associate an exclusion with the specific occurrence it covers.

    A YIELDING exclusion's span is dropped when its own sentence carries citation evidence (see
    :func:`_sentence_carries_citation_evidence`), so the occurrence it would have excused is judged
    like any other.
    """
    vocab_matches = list(_VOCAB_RE.finditer(text))
    if not vocab_matches:
        return False
    shape_spans = [
        m.span()
        for pattern, _, yields_to_citation in _SHAPE_EXCLUSIONS
        for m in pattern.finditer(text)
        if not (yields_to_citation and _sentence_carries_citation_evidence(text, *m.span()))
    ]
    return any(not any(start <= vm.start() and vm.end() <= end for start, end in shape_spans) for vm in vocab_matches)


def _iter_uncited_claims(paragraphs: Iterator[_Paragraph]) -> Iterator[_Paragraph]:
    """Yield every paragraph that asserts an operator decision without a co-located date + bead id."""
    for paragraph in paragraphs:
        if paragraph.path in _EXEMPT_FILES:
            continue
        if any(paragraph.path == path and needle in paragraph.text for path, needle in _KNOWN_SHAPE_EXCLUSIONS):
            continue
        if not _has_unexcluded_vocab_match(paragraph.text):
            continue
        has_date = bool(_ISO_DATE_RE.search(paragraph.text))
        has_bead = bool(_BEAD_ID_RE.search(paragraph.text))
        if has_date and has_bead:
            continue
        yield paragraph


def _format_violation(paragraph: _Paragraph, *, has_date: bool, has_bead: bool) -> str:
    missing = []
    if not has_date:
        missing.append("an ISO date (YYYY-MM-DD)")
    if not has_bead:
        missing.append("a bead id (phaze-XXXXX)")
    excerpt = paragraph.text if len(paragraph.text) <= 220 else paragraph.text[:217] + "..."
    return (
        f"{paragraph.path}:{paragraph.lineno} asserts an operator decision but is missing "
        f"{' and '.join(missing)} in the same paragraph.\n"
        f"    text: {excerpt!r}\n"
        "    Add both to the SAME paragraph -- see ADR-0007 (windowed analysis) section 7 (the phaze-w55w1 windowed-"
        "analysis decision) or the phaze-b62ri bead comment for the shape to copy: the question as it was "
        "put, the answer as it was given, the date, and the bead id. If this is NOT a claim that a "
        "decision was made -- a UI-domain adjective like 'operator-chosen', or a statement of whose "
        "authority a choice belongs to -- reword it so it no longer reads as one; do not silence "
        "this test with a new allowlist entry for a genuine claim."
    )


def test_every_operator_decision_claim_carries_a_date_and_a_bead_id() -> None:
    """ADR-0012 (verification fidelity and operator attribution) guardrail G2, mechanized: every tracked-file attribution claim is citable."""
    paragraphs: list[_Paragraph] = []
    for path in _scope_files():
        paragraphs.extend(_paragraphs_for_file(path))

    violations = [
        _format_violation(p, has_date=bool(_ISO_DATE_RE.search(p.text)), has_bead=bool(_BEAD_ID_RE.search(p.text)))
        for p in _iter_uncited_claims(iter(paragraphs))
    ]
    assert not violations, "operator-decision claim(s) without a date + bead id:\n\n" + "\n\n".join(violations)


class TestScannerMechanics:
    """Proves the mechanism fires, rather than asserting it in prose (per this bead's acceptance
    criteria) -- each case below runs the SAME extraction + detection pipeline the real-tree test
    above uses, just against small in-memory/temp-file fixtures instead of the repo.
    """

    def test_a_malformed_comment_is_flagged(self) -> None:
        paragraphs = _comment_run_paragraphs_python(
            "fixture.py",
            "# Operator decision: ship it faster.\nx = 1\n",
        )
        violations = list(_iter_uncited_claims(iter(paragraphs)))
        assert len(violations) == 1
        assert "Operator decision: ship it faster." in violations[0].text

    def test_a_malformed_docstring_is_flagged(self, tmp_path: Path) -> None:
        """End-to-end through the real file-walking path, not just the paragraph helpers."""
        fixture = tmp_path / "malformed.py"
        fixture.write_text('"""Module doing a thing.\n\nOperator decision: ship it faster.\n"""\n')
        paragraphs = _paragraphs_for_file(fixture, rel="fixture/malformed.py")
        violations = list(_iter_uncited_claims(iter(paragraphs)))
        assert len(violations) == 1
        assert "Operator decision: ship it faster." in violations[0].text

    def test_wrap_tolerance_catches_a_mid_word_line_wrap(self) -> None:
        """The exact shape this bead's design calls out: 'operator' and the vocabulary word split
        across a line wrap, with no date or bead id anywhere -- phaze-d2hgv.7's motivating example
        (``src/phaze/services/video_audio.py`` before phaze-d2hgv.1 repaired it read
        ``"... -- operator\\ndecision (phaze-3ea41)."``, but WITHOUT the bead id this shape must
        still be caught).
        """
        paragraphs = _comment_run_paragraphs_python(
            "fixture.py",
            "# The remux behavior ships unconditionally -- operator\n# decision, ship it as-is.\n",
        )
        violations = list(_iter_uncited_claims(iter(paragraphs)))
        assert len(violations) == 1
        assert "operator decision" in violations[0].text.lower()

    def test_a_wrap_tolerant_but_properly_cited_claim_passes(self) -> None:
        """The same wrap, but WITH a date and bead id in the paragraph -- must NOT be flagged."""
        paragraphs = _comment_run_paragraphs_python(
            "fixture.py",
            "# The remux behavior ships unconditionally -- operator\n# decision (2026-08-12, phaze-3ea41), ship it as-is.\n",
        )
        assert list(_iter_uncited_claims(iter(paragraphs))) == []

    def test_ui_domain_shapes_are_not_flagged(self) -> None:
        """ADR-0012 (verification fidelity and operator attribution) section 5's own three UI-domain examples, verbatim -- none is a claim."""
        cases = [
            "# an operator-chosen sort key ties more often than the default one does\n",
            '# not an operator choice of "zero threads"\n',
            '# f"{n} proposal(s) await an operator decision."\n',
        ]
        for source in cases:
            paragraphs = _comment_run_paragraphs_python("fixture.py", source)
            assert list(_iter_uncited_claims(iter(paragraphs))) == [], f"wrongly flagged: {source!r}"

    def test_authority_contrast_shape_is_not_flagged(self) -> None:
        """'is/are an operator decision, not a X's' -- WHO decides, not a record that one was made.

        This is the shape phaze-d2hgv.2 reworded away at ``tasks/controller.py:401`` and
        ``tasks/filename_convention.py`` (commit ``96c79af2``) and the shape independently found in
        two spike docs (``phaze-han03-essentia-seek.md``, ``phaze-b2qs9-...-measurement.md``) --
        recognized here by SHAPE so a future third site does not need its own allowlist entry.
        """
        paragraphs = _comment_run_paragraphs_python(
            "fixture.py",
            "# if MTG ask for a CLA, that is an operator decision, not a developer one.\n",
        )
        assert list(_iter_uncited_claims(iter(paragraphs))) == []

    def test_a_shape_exclusion_does_not_waive_a_real_claim_in_the_same_paragraph(self) -> None:
        """A UI-domain adjective earlier in a paragraph must not shield an uncited claim later in
        the SAME paragraph -- shape exclusion is meant to recognize non-claims, not to launder one.
        """
        paragraphs = _comment_run_paragraphs_python(
            "fixture.py",
            "# an operator-chosen sort key ties often, and separately, shipping the remux unconditionally was an operator decision.\n",
        )
        violations = list(_iter_uncited_claims(iter(paragraphs)))
        assert len(violations) == 1

    # phaze-71q9y: dev/w4-jktlb's two measured examples, verbatim in their load-bearing phrase --
    # each a real citation that a yielding shape exclusion used to excuse whole.
    _NOUN_PHRASE_CITATION = "# Pinning the floor here was AN OPERATOR DECISION OF 2026-08-22, recorded on bead phaze-jktlb.\n"
    _AUTHORITY_CONTRAST_CITATION = (
        "# Pinning the floor here is deliberate: that IS AN OPERATOR DECISION of 2026-08-22 recorded on bead phaze-jktlb,\n"
        "# NOT an implementer's shortcut.\n"
    )

    @pytest.mark.parametrize(
        ("source", "shape_index"),
        [(_NOUN_PHRASE_CITATION, 1), (_AUTHORITY_CONTRAST_CITATION, 3)],
        ids=["noun-phrase-shape", "authority-contrast-shape"],
    )
    def test_a_yielding_shape_no_longer_swallows_a_half_cited_claim(self, source: str, shape_index: int) -> None:
        """Acceptance criterion 1: delete the date OR the bead id and the guard fires.

        The first assertion is what makes the other three mean anything: it proves the fixture
        really is inside the shape exclusion under test, so a red result is the exclusion yielding
        -- not the vocabulary regex simply never matching an excluded shape to begin with.
        """
        pattern = _SHAPE_EXCLUSIONS[shape_index][0]
        assert pattern.search(_normalize_paragraph(source.replace("#", ""))), "fixture no longer exercises the shape"

        cited = _comment_run_paragraphs_python("fixture.py", source)
        assert list(_iter_uncited_claims(iter(cited))) == [], "the fully cited form must pass"

        for removed in ("2026-08-22", "phaze-jktlb"):
            paragraphs = _comment_run_paragraphs_python("fixture.py", source.replace(removed, ""))
            assert len(list(_iter_uncited_claims(iter(paragraphs)))) == 1, f"removing {removed!r} was swallowed"

    # phaze-h8ug4: scripts/redis-seat-registry.sh's pre-fix sentence, verbatim -- a bead id, no date.
    _ADJECTIVE_REGISTRY_CLAIM = (
        "# Only `cmd_reclaim`'s `--apply` loop passes 1 (phaze-robzi.1's operator-approved contract change is\n# scoped to reclaim alone).\n"
    )

    def test_the_adjective_shape_no_longer_swallows_the_registry_claim(self) -> None:
        """phaze-h8ug4: the adjective shape yields to a bead id in its own sentence, so the half-cited
        registry claim fails for its missing date and passes once the date is added.

        The first assertion proves the fixture is inside the adjective shape, so the red result is
        the exclusion yielding, not the vocabulary simply missing. The last one pins the residual:
        with the bead id deleted too, the sentence has no evidence and is excused like any UI adjective.
        """
        source = self._ADJECTIVE_REGISTRY_CLAIM
        assert _SHAPE_EXCLUSIONS[0][0].search(_normalize_paragraph(source.replace("#", ""))), "fixture no longer exercises the shape"

        violations = list(_iter_uncited_claims(iter(_comment_run_paragraphs_python("fixture.py", source))))
        assert len(violations) == 1, "the bead-id-only claim was swallowed"

        dated = source.replace("contract change", "contract change of 2026-08-25")
        assert list(_iter_uncited_claims(iter(_comment_run_paragraphs_python("fixture.py", dated)))) == []

        undated_unbeaded = source.replace("phaze-robzi.1's ", "the ")
        assert list(_iter_uncited_claims(iter(_comment_run_paragraphs_python("fixture.py", undated_unbeaded)))) == []

    def test_a_yielding_shape_still_excuses_its_non_claim_when_a_bead_is_named_in_another_sentence(self) -> None:
        """Acceptance criterion 2's other half: the yield is SENTENCE-scoped, not paragraph-scoped.

        The shape of ``docs/spikes/phaze-han03-essentia-seek.md``, where an authority contrast sits
        in a paragraph that names a bead elsewhere -- a paragraph-scoped gate flagged it.
        """
        cases = [
            "# See phaze-han03 for the patch. If MTG ask for a CLA, that is an operator decision, not a developer one.\n",
            '# Tuned under phaze-bk9el.19. `FOO=` in a ConfigMap is not an operator choice of "zero threads".\n',
        ]
        for source in cases:
            paragraphs = _comment_run_paragraphs_python("fixture.py", source)
            assert list(_iter_uncited_claims(iter(paragraphs))) == [], f"wrongly flagged: {source!r}"

    def test_a_child_bead_id_does_not_end_a_sentence(self) -> None:
        """``phaze-bk9el.19``'s dot is not a sentence end, so the bead id before it stays in the sentence.

        The ONLY evidence is the bead id, and it sits before the dot: a terminator regex that ends a
        sentence at any ``.`` would cut it off and report no evidence.
        """
        text = "Scoped under phaze-bk9el.19 as an operator decision of the hive."
        match = _SHAPE_EXCLUSIONS[1][0].search(text)
        assert match is not None
        assert _sentence_carries_citation_evidence(text, *match.span())

    def test_scope_includes_untracked_files_but_not_ignored_ones(self, tmp_path: Path) -> None:
        """Acceptance criterion 4: a new, never-staged file is scanned; a gitignored one is not."""

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)  # noqa: S603, S607

        git("init", "-q")
        (tmp_path / "src").mkdir()
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "src" / "tracked.py").write_text("x = 1\n")
        git("add", "src/tracked.py")
        (tmp_path / "src" / "untracked.py").write_text("# Operator decision: ship it faster.\n")
        (tmp_path / "src" / "gate.log").write_text("operator decision\n")

        found = {path.relative_to(tmp_path).as_posix() for path in _scope_files(tmp_path)}
        assert found == {"src/tracked.py", "src/untracked.py"}

    def test_known_meta_exclusions_are_still_narrowly_scoped_to_their_needle(self) -> None:
        """A `_KNOWN_SHAPE_EXCLUSIONS` entry is (path, needle) -- an unrelated, uncited claim added
        later to the SAME file must still be caught; only the paragraph matching the needle is
        exempt.

        Runs against EVERY path in the list rather than one representative of it: the list grows
        (``docs/git-topology-and-verification.md`` joined it for phaze-i5bs3), and a check that
        exercises one entry says nothing about the entry added after it.
        """
        for path in dict.fromkeys(path for path, _ in _KNOWN_SHAPE_EXCLUSIONS):
            paragraphs = [_Paragraph(path, 1, "unrelated operator decision, no date, no bead id.")]
            assert list(_iter_uncited_claims(iter(paragraphs))) != [], f"{path} is exempted wholesale, not by needle"

    def test_every_known_shape_exclusion_still_resolves_to_a_real_paragraph(self) -> None:
        """The complement of the test above, and the mechanical half of what phaze-i5bs3 cost.

        A ``_KNOWN_SHAPE_EXCLUSIONS`` entry is a citation to a LOCATION. Rename the file, reword
        the paragraph, or extract the prose into a second file, and the entry goes DEAD: it
        exempts nothing, the allowlist describes a tree that no longer exists, and the only signal
        is a red build somewhere else (which is exactly how the extraction to
        ``docs/git-topology-and-verification.md`` announced itself). Requiring every entry to
        still resolve to a paragraph in its own file is the one direction a needle-scoped
        exemption cannot check on its own behalf.
        """
        stale = [
            f"{path}: no paragraph contains the needle {needle!r}"
            for path, needle in _KNOWN_SHAPE_EXCLUSIONS
            if not any(needle in paragraph.text for paragraph in _paragraphs_for_file(_REPO_ROOT / path))
        ]
        assert not stale, "stale _KNOWN_SHAPE_EXCLUSIONS entries (file renamed, prose reworded, or text moved):\n  " + "\n  ".join(stale)
