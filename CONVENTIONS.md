# Conventions

## No local identifiers in tracked files

phaze is developed against a real personal music archive on real hardware. Investigation output —
spike docs, planning notes, debug write-ups, benchmark records — is where traces of that archive
accumulate, because the honest way to report a measurement is to say what it was measured on.

**Do not commit them.** Scrub as you write, not afterwards.

### Never commit

- **Filenames, directory names or absolute paths from the real archive.** This is the category that
  matters most. A release name in a log excerpt, a staging-mount path in a traceback, a directory
  name in a table of sampled files.
- **Content digests and file UUIDs taken from live data** — fingerprint hashes, `sha256` values,
  database row ids pasted from a real query.

### Acceptable

- **Invented example filenames** that illustrate a naming format — `Artist - Event - Title
  (2024).mp3`. These teach the format without evidencing what is in the collection.
- **Synthetic test fixtures** — `song.mp3`, `dup.mp3`, `reference.wav`.
- **Host and account names in local instruction material.** Committed source, scripts and published
  docs should refer to hosts by role instead.

### Use these placeholders

Follow the vocabulary already established in `docs/spikes/`, so scrubbed documents read
consistently:

| For | Use |
| --- | --- |
| Individual tracks | `<track-01>`, `<track-02>`, … |
| Concert sets / long recordings | `<set-01>`, `<set-02>`, … |
| Archive mount, host side | `<archive-mount>` |
| Archive mount, in-container | `<archive-mount-in-container>` |
| Local scratch directories | `<scratch>/…` |
| Fingerprint digests | `fp_<hash-1>`, `fp_<hash-2>`, … |
| File UUIDs | `<uuid-1>`, `<uuid-2>`, … |
| Hosts, where a role name will not do | `host-prod`, `host-store` |

### Replace identifiers, never quantities

This is the rule that gets broken when scrubbing is rushed, and it destroys the value of the
document it was meant to protect.

Every measured value stays exact — row counts, durations, latencies, sample sizes, percentages. The
point of an investigation record is that its conclusions are checkable.

> **Good:** "36 files totalling 42.34 h, stratified across the duration distribution"
> **Bad:** "a few dozen files" — scrubbed, but now worthless as evidence

If a scrub changes a number, it is a bug in the scrub. A useful check after any pass: diff the
numeric tokens of the before and after, and confirm the only digits lost were part of a removed
identifier.

### Scope

Any tracked file: spike and design docs, `.planning/**`, source comments, scripts, SQL. **Also
commit messages and PR bodies** — they are just as permanent and just as public as the files.

### The history caveat

Scrubbing a file does not scrub git history. Once an identifier is committed, removing it from the
working tree leaves it fully readable via `git show <old-sha>`, and removing it from history means
a rewrite and a force-push — which is disruptive, and on a shared branch may not be possible at all.

**Prefer never committing the identifier over fixing it later.** When writing up a measurement, use
the placeholder in the first draft rather than the real name you intend to replace before pushing.

## Group thousands with a comma, never a space

This sits beside "Replace identifiers, never quantities" above — it is the other half of how numbers
are written in tracked prose: not just that a quantity must survive a scrub intact, but which digits
it is written with.

Grouped numbers use a **comma**: `4,383`, `11,428`, `350,000`. Four-digit numbers are grouped too —
`4,383`, never the bare `4383` — so there is one rule, not a threshold to remember for "big enough"
numbers. Decimals keep the comma on the integer part and the period as the decimal point: `4,761.835`,
never `4.761,835`.

> **Good:** "11,428 files totalling 11,492 h"
> **Bad:** "11 428 files totalling 11 492 h" — space grouping, matches no rule, and now disagrees
> with the rest of the corpus

**Never group an identifier that merely looks numeric** — years (`2026`), ports (`5433`), bead ids
(`phaze-b2qs9`), version numbers (`3.14`), line numbers, SHAs. Grouping is for quantities, not for
labels, and telling the two apart is a judgement call about what the digits *mean*, not a pattern a
script can apply: `.planning/milestones/2026.7.5-ROADMAP.md` contains the string `PERF-02 200K`,
where `02 200` matches "digits, space, three digits" exactly as well as a genuine quantity would, and
must never be grouped — the `02` belongs to the identifier `PERF-02`, not to `200K`. That is also why
this rule has no mechanical guard: a check that grouped numbers on pattern match would reproduce
exactly this false positive.

### Where this applies

Tracked prose: `docs/**`, root-level `*.md`, spike and design docs, planning notes, commit messages
and PR bodies. **Not source code** — there, the language's own literal syntax governs, and this rule
has no opinion on it.

### Why this is written down now

MEASURED 2026-08-26: the corpus already splits on this, and it splits by **genre, not by author**.
The six MEASUREMENT spikes are space-grouped —
`docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` (105 space / 0 comma),
`phaze-han03-essentia-seek.md` (61/0), `phaze-u1n7j-vox-fix-verification.md` (50/0),
`phaze-rc1q-streaming-vs-standard-mode.md` (36/0), `phaze-i93a-cpp-rewrite-evaluation.md` (33/1),
`phaze-8r6t4-concurrency-knee-recheck.md` (10/0) — while the DIAGNOSIS spikes and the rest of the
docs are comma-grouped: `phaze-p3hj.1-audfprint-total-outage-diagnosis.md` (0/13),
`phaze-d2hgv.6-artifact-seam-inventory.md` (0/3), and the non-spike docs overall (41 space / 105
comma). Neither style was ever written down anywhere in this repo, so the two families forked
independently and both look locally consistent.

That gap surfaced concretely: bead `phaze-zaf2l`'s spike (PR #542) came out space-grouped because its
brief pointed it at `phaze-b2qs9` and `phaze-u1n7j` as "the house standard for a measurement record
here" — a correct read of those two exemplars, which are 105/0 and 50/0 space-grouped. Matching your
exemplars exactly is how a convention forks when there is no rule to check against, only neighbours to
imitate.

**Operator decision 2026-08-26.** Question as put: *"Still open from earlier: whether to write the
number-formatting convention into CONVENTIONS.md."* Answer as given, verbatim: *"YES. Write the
convention there. that's why we have that file."* Durable record: bead `phaze-xp9nx`. In the same
discussion the operator named comma grouping as the standard and space grouping as the deviation —
*"US standards: 4,383"* against *"European standards of numbers: 4 383"* — which is why the rule
above is comma grouping rather than space grouping.

**The existing corpus is being converted, not left as-is.** Writing the rule down does not by itself
touch the ~440 space-grouped matches across 44 tracked files measured above (six spikes alone account
for 295 of them). Converting them is bead `phaze-3x7xt`, dispatched separately so the rule and the
backfill do not ride the same diff. A reader who greps `phaze-b2qs9` mid-transition and still finds
space-grouped numbers there is looking at a corpus in the middle of `phaze-3x7xt`, not at a
disagreement with this rule.

## Cite ADRs by filename, never by bare number

Write `docs/design/0015-shared-session-gather.md`, not "ADR-0015". Where the prose reads better
with the number, keep the number *and* add the disambiguator — "ADR-0015 (shared session gather)"
— so the citation carries its own meaning.

**A bare number is a pointer with no redundancy, so nothing can check it.** ADR numbers are
reassignable: renaming a file frees its number, and the next ADR to claim that number silently
inherits every citation ever written against the old occupant. The citation stays correctly
formed and greppable, and starts resolving to a different, currently-valid document. A filename
carries the title, so the same event breaks the citation *visibly* instead of silently.

### The incident this rule comes from (measured, 2026-08-24, bead `phaze-f70y9`)

Two commits landed concurrently. `4a08e873` renumbered `0004-tracklist-candidate-sets.md` to
`0014-tracklist-candidate-sets.md` to resolve a duplicate 0004; `d4f673ac` introduced the
shared-session-gather ADR *as* 0014. They collided, session-gather was pushed to 0015, and every
citation written against the earlier numbering repointed. A census of the 3,200-bead corpus found
**8 bare "ADR-0014" citations, all in one bead, all meaning session gather, all now resolving to
`docs/design/0014-tracklist-candidate-sets.md`.**

**It was caught exactly once, by a human reading prose** — a review gate bounced a bead citing
ADR-0014 in `pyproject.toml`. No grep, link checker or CI check found it, or could have: "ADR-0014"
implies no path, so there is nothing to dereference and nothing to 404.

### What does not cover this

`phaze-x2z38`'s duplicate-leading-number guard (`tests/shared/test_adr_numbering.py`, landing in the
same wave as this rule) guards that no two ADRs share a leading number. **That guard does not cover
this failure mode and must not be read as if it does.** These numbers were never
duplicated at any instant — 0014 was legally *reused* after a rename freed it. The guard is still
worth having: it removes the principal *cause* of renumbers, since an ADR that cannot collide at
authoring time never needs renaming later.

### When you renumber an ADR, sweep both numbers

Sweep the number **vacated** and the number newly **occupied**. The second is the one that gets
missed, and missing it is a general property of renumbers rather than anyone's lapse: at the time
the work is planned, the newly-occupied number is not yet anybody's, so there is nothing obvious to
sweep for. `phaze-kbue9` swept 0004 thoroughly and never swept 0014, which is exactly how the
citations above survived.

Also **do not cite a number before its file exists.** A forward citation is dangling when written
and silently becomes *wrong* rather than dangling once something else claims the number — which is
precisely how the `pyproject.toml` instance above arose (`f4c39654` cited ADR-0014 when
`docs/design/` topped out at `0013-ffmpeg-pin.md`).

*The general form:* a pointer with no redundancy cannot be checked by any tool, so the redundancy
has to be written into the citation at authoring time. This is the same shape as the
[`ADR-0012`](docs/design/0012-verification-fidelity-and-operator-attribution.md) rule that a
decision attributed to the operator carries its question, answer, date and durable record — a bare
label that asserts provenance without carrying it is not a citation.
