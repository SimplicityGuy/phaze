# phaze-fi8rs — which recording facts can drive a remote search

- **Bead:** `phaze-fi8rs` (spike, epic `phaze-oprdu`)
- **Date:** 2026-10-12 (measurements run 2026-10-11 and 2026-10-12)
- **Status:** measurement write-up only. **No product code changed.** The counts below were taken by an
  earlier step against the archive database; this document was written from the counts recorded on the bead and
  ran no query, touched no database and made no network request.

## Question

A remote tracklist provider can only be asked about a local recording through the facts phaze is allowed to send.
Which facts does the archive actually hold for its long-form recordings, and does `RecordingContext`
(`src/phaze/tracklist_providers/domain.py`) need a **source-URL fact** in addition to its existing `artist`,
`event`, `date` and `duration_seconds` facts, or is keyword discovery from those facts the only honest path?

## Method

Two read-only, counts-only measurements. Neither selected, printed or stored a filename, path, tag value, URL, host
or identifier; each was a single `BEGIN READ ONLY` transaction of `count(*) FILTER (...)` expressions that was rolled
back. Each was authorised by the operator, and the authorisation is recorded on the bead.

**Population.** Long-form means `duration >= 1200 s` (20 minutes). There are **93,744** long-form recordings. Both runs
report the same figure. The first run counted rows of the `metadata` table (104,445 rows in all, 104,402 with no
recorded failure). The second run counted files joined to `metadata` and not marked missing.

**Run 1 (2026-10-11, bead `phaze-fi8rs`; authorised under the epic's measurement record, bead `phaze-oprdu`).** The
question as put and the answer as given, quoted from the epic's record 6: *"May I run a read-only count query against
the archive database to see how many recordings carry a platform URL? I'd report counts only."* Answer: *"yes"*.
Patterns, in words: a tag blob containing any `http(s)://` URL; and a tag blob containing a URL whose host is within one
of four TrackId-supported families (SoundCloud, Mixcloud, YouTube including the short-link host, hearthis). Plus plain
non-null/non-empty counts of the `artist` and `year` columns.

**Run 2 (2026-10-12, bead `phaze-fi8rs`).** The dispatcher asked whether to extend the read-only counts to cover event
and date hints that the metadata columns do not show. The operator answered *"Yes, counts only (Recommended)"* (recorded
on bead `phaze-fi8rs`, 2026-10-12). Patterns, in words, all applied to the file name or path and none ever extracted:

| Pattern | Shape |
|---|---|
| full date, year first | a four-digit year (19xx or 20xx), then a valid month, then a valid day, with separators between |
| full date, compact | eight digits forming a valid year-month-day, not embedded in a longer digit run |
| full date, day first | a valid day, then a valid month, then a four-digit year, with separators between |
| year in name | a standalone four-digit year (19xx or 20xx) in the file name |
| year directory | a directory component of the path that is a four-digit year |
| dash pattern | two or more " - " separators in the file name (the artist - event - date shape) |
| live hint | the words "live at", "live from", or an at-sign in the file name |
| artist tag | `artist` column non-null and non-empty |
| some year | `year` tag, or a year in the name, or a year directory |
| no usable hint | no artist tag, no year anywhere (tag, name, path) and no dash pattern |

**Reading the tables.** Counts overlap and **do not sum**: a name can match several patterns, and the three full-date
shapes themselves overlap (13,758 + 385 + 30,248 = 44,391, which exceeds the 44,112 files with any full date by 279,
so some names match more than one shape). Every percentage is the count over 93,744 rounded to two places. The arithmetic was
re-checked here; two figures on the bead carry looser rounding than shown below (61,061 is 65.14%, bead text "65.1%";
29,403 is 31.37%, bead text "31.36%"), and the tables use the recomputed values.

## Evidence

### Platform URL families in raw tags (run 1)

Of the rows below, only the last is restricted to long-form; the per-family counts are over all durations, so they
have **no** percentage of 93,744 and are not comparable to it.

| Measure | Count | Population | Share of 93,744 |
|---|---:|---|---:|
| metadata rows | 104,445 | all | n/a |
| metadata rows with no recorded failure | 104,402 | all | n/a |
| any `http(s)` URL in raw tags | 9,340 | all durations | n/a |
| SoundCloud URL | 1,025 | all durations | n/a |
| Mixcloud URL | 294 | all durations | n/a |
| YouTube URL | 96 | all durations | n/a |
| hearthis URL | 3 | all durations | n/a |
| **long-form with any of the four families** | **1,305** | long-form | **1.39%** |

1,305 / 93,744 = 1.392%, checked.

### Artist and year tags (run 1)

| Measure | Count | Population | Share of 93,744 |
|---|---:|---|---:|
| artist tag | 67,004 | all durations | n/a |
| year tag | 60,116 | all durations | n/a |
| long-form with artist tag | 61,061 | long-form | 65.14% |
| long-form with year tag | 54,767 | long-form | 58.42% |

### Date, event and combined hints (run 2, long-form only)

| Measure | Count | Share of 93,744 |
|---|---:|---:|
| full date in the file name (any shape) | 44,112 | 47.06% |
| of which year-first with separators | 13,758 | 14.68% |
| of which compact eight-digit | 385 | 0.41% |
| of which day-first with separators | 30,248 | 32.27% |
| a year anywhere in the name | 65,138 | 69.48% |
| a year directory in the path | 5,690 | 6.07% |
| two or more " - " separators in the name | 27,601 | 29.44% |
| "live at / from / at-sign" hint in the name | 29,403 | 31.37% |
| artist tag and some year (tag, name or path) | 56,206 | 59.96% |
| artist tag and a full date in the name | 29,439 | 31.40% |
| artist tag and the dash pattern | 11,355 | 12.11% |
| **no usable hint at all** | **9,530** | **10.17%** |

At least one usable hint: 93,744 - 9,530 = 84,214 recordings, 89.83%.

## Verdict

This section is the bead's own analysis of the counts. It is **not** a decision attributed to the operator.

1. **`RecordingContext` does not need a source-URL fact.** 1,305 of 93,744 long-form recordings, 1.39%, carry a
   TrackId-supported platform URL in their tags. A URL-keyed lookup would serve about one recording in 72 and leave
   98.61% to some other path regardless. The epic's recorded constraint (record 3, 2026-10-11, bead `phaze-oprdu`) also
   limits queries to `RecordingContext` facts, and the existing contract document already allows a URL to enter later
   only as a query value from core-held recording data, never dereferenced.
2. **Keyword discovery from artist plus year or date hints is the only path that covers the archive.** 56,206
   recordings (59.96%) have an artist tag and some year; 29,439 (31.40%) have an artist tag and a full date. 89.83% have
   at least one hint of some kind.
3. **Day-first dates dominate, and are locale-ambiguous.** Of the 44,112 names with a full date, 30,248 are day-first
   with separators against 13,758 year-first. A day-first or month-first form with a day of 12 or less reads two ways, so
   per `docs/design/tracklist-provider-matching.md` (date kind, precision and ambiguity rules) such a date keeps its
   alternatives and is not a certain date. A query builder must not collapse it to one reading.
4. **Event hints come only from file name and path structure.** The `metadata` table has no event column, so an event
   fact can only be inferred from the dash pattern (29.44%) or a live hint (31.37%). These are inferred facts, not known
   ones, and how many of them actually name an event is unmeasured.

## Recommendation

For the two provider beads:

- **Facts to place in `RecordingContext`:** `artist` from the tag when present (certainty `known`); `date` and `event`
  parsed from name or path structure with certainty `inferred` and the source token recorded as evidence; `duration_seconds`
  as it is. Do **not** add a URL fact. Reopen that question only if a later measurement shows a materially larger share.
- **Ambiguous dates:** a day-first or month-first form that reads two ways is carried as a date with its alternatives and
  reduced precision, never as one reading. Queries may be issued per alternative within the provider's discovery budget,
  and results from different readings remain separate candidates for review. A year alone is a year-precision date, not
  a day.
- **The 9,530 recordings with no usable hint (10.17%):** issue no remote search and say so in the operator-facing result
  (no hint available). Do not guess an artist, event or date from unrelated tokens, and do not fall back to sending any
  local content.

**Not measured, all unknown:**

- the precision of any hint (how often a name-derived artist, event or date is actually right);
- how many of the dash-pattern and live-hint names carry an event name rather than some other text;
- per-provider recall: how often a query built from these facts finds the right remote tracklist;
- whether the 1,305 URL-bearing recordings overlap the recordings that also have an artist and a date, which would
  say how much a URL could add on top of keywords.
