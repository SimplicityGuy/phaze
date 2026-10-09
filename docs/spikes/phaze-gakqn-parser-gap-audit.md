# phaze-gakqn — what the detail parser misses versus the fields other tools extract

> Historical evidence only. The acquisition implementation, parser, captures and follow-up work are retired. Counts below describe the original measurements; referenced source artifacts no longer exist. See `docs/design/0024-tracklist-source-retirement.md`.


- **Bead:** `phaze-gakqn` (epic `phaze-5d0wg`), a spike. **Docs-only; no product code changed.**
- **Date:** 2026-10-06
- **Feeds:** `phaze-i5sp6` (which spike-gated paths to build — parser extras).
- **Fixtures:** `tests/identify/fixtures/tracklist_render/25fhn7c9-ok.html` (52 rows, "anchor") and
  `19h6nw7t-ok.html` (12 rows, "short"). Both are public pages about public events. No local
  archive identifier is involved.

## Question

Of the page fields the surveyed libraries extract, which does `src/phaze/services/tracklist_parser.py`
already capture, which are present in the two recorded captures but dropped, and which are worth
adopting? Candidates: the inline `cueValuesEntry` cue map, `w/` mashup rows, ID Remix status
(`span.trackStatus`), the `/source/` festival-venue-stage taxonomy, labels and genres, per-player
source headers (`bItmH` "Player N"), set-level YouTube/SoundCloud/Apple links, and a markup-drift
canary.

## Method

Zero live requests; the network was not touched. `parse_tracklist_tracks` was run over both
recorded captures, and its output was diffed against what the markup contains, using BeautifulSoup
(lxml) scripts of my own kept outside the repo and run with `uv run python -I`. For cues, the
visible `.cue` text, the hidden `*_cue_seconds` inputs, the `playPosition(... cue: ...)` handlers and
every `cueValuesEntry` assignment in the page script were compared row by row. Each candidate was
counted per capture; "present" means present in the fixture markup in the form the candidate names,
not merely a substring hit (several raw substring hits turned out to be CSS, nav links or URL
fragments, noted below).

## Evidence

Counts are anchor (52 rows) / short (12 rows).

| Candidate field | Captured today | Present in fixtures | Value |
|---|---|---|---|
| Position (`data-trno`) | yes, 52 / 12 | 52 / 12 | already done |
| Artist / title / remix text | yes, 52 / 12 each; remix_info on 12 / 2 (all 12 / 2 rows whose `.trackValue` holds a parenthesised span) | 52 / 12 | already done |
| Record label (`.trackLabel`) | yes, 38 / 9 | 38 / 9 (and `itemprop=publisher` 38 / 9) | already done; the `/label/<id>/<slug>` link in each (38 / 9) is not kept |
| Visible cue time (`.cue`) | read, but `.cue` text is empty on every row: 0 / 0 timestamps stored | 0 / 0 rows with cue text | nothing to capture in either fixture |
| Inline `cueValuesEntry` cue map | not read | 1 entry per capture, a placeholder: `seconds = 0`, `number = '1'`, `ids[0] = 'tlp0_content'`; hidden `_cue_seconds` inputs are all `0` (52 / 12); only row 1 has a `playPosition(... cue: '0')` handler | no cue data in either fixture, so the map cannot be shown to differ from the visible text |
| `w/` mashup rows | `is_mashup` False on 52 / 12 | 0 / 0. `mashupTrack` appears once per file, only inside a CSS rule (`.italic, .mashupTrack { font-style:italic }`); `w/` appears once per file, only inside the nav URL `.../the-martin-garrix-show/index.html`. One row (position 10 of the anchor) is a `vs.` pairing (`Run DMC vs. Jason Nevins - It's Like That (Raxon Edit)`); it carries no mashup marker class | cannot verify; the parser's own docstring and `docs/design/0014-tracklist-candidate-sets.md` already say so |
| ID Remix status (`span.trackStatus`) | not read | 0 / 0 (`trackStatus` appears nowhere; no class containing "status" in any row) | cannot verify. Unresolved rows exist but are marked differently, see next row |
| Unresolved "ID - ID" rows | stored as artist `ID`, title `ID`: 10 / 2 rows | 10 / 2; each lacks `data-isided="true"` (resolved rows: 42 / 10), and no schema.org `byArtist` microdata | a stored placeholder today; the page itself says `IDed 42 / 52` and `IDed 10 / 12`, matching 52-10 and 12-2 |
| `/source/` taxonomy | not captured (`Tracklist.event` is free text) | title `h1#pageTitle` links 2 / 3 `/source/` entries; the sidebar `#csLeft` carries the same plus role text: "Open Air / Festival" (Time Warp), "Event Location" (Maimarkthalle Mannheim), "Radio Show" and "Broadcasted @" (short capture: BBC Radio 1 Dance Presents, BBC Radio 1). Stage: 0 / 0 | festival and venue are present with a role label in both; stage is absent from both |
| Row genre (`itemprop=genre`) | not captured | 31 / 10 rows (anchor values: Techno 15, House 6, Trance 3, Melodic House/Techno 2, Indie Dance 2, Rock 1, Progressive House 1, Electronica 1) | no `TracklistTrack` column exists |
| Set genre | not captured | anchor: one (`Techno`); short: two (`Tech House`, `Techno`), in `itemprop=genre` and the "Tracklist Genre(s)" sidebar | no `Tracklist` column exists |
| Row duration (`itemprop=duration`, ISO 8601) | not captured | 34 / 10 rows | not a requested candidate; free signal for duration scoring |
| Set headers (`.bItmH.flex`) | not captured | anchor: 2 ("First Set:" before row 1, "Second Set:" before row 30); short: 0 | not "Player N" (see next row); a real set-boundary marker |
| Per-player headers "Player N [h:mm:ss]" | not captured | these are media tab buttons (`li.tBtn`), not `bItmH`: 1 / 2 tabs, `Player 1 [2:08:31]` and `Player 1 [1:00:00]` (x2, one hidden); the bracket is the recording's length (anchor: also `ytPlayer.duration = "7711"`, equal to 2:08:31) | recording duration is a matching signal |
| Set-level YouTube | not captured | anchor: 1 embedded video (id, 7711 s duration, `ytPlayer.cue = "1373"`, upload date). short: 0 | |
| Set-level SoundCloud | not captured | 0 / 0. The two raw `soundcloud.com` hits are the site's own profile link | |
| Set-level Apple | not captured | 0 / 0. The one raw `music.apple.com` hit per file is the artist's contact link | |
| Set-level Mixcloud / hearthis | not captured | short: 1 Mixcloud iframe + 1 hearthis iframe; anchor: 0 | not requested; shows media sources vary per page |
| Markup-drift canary | partial: `TracklistParseError` fires only when a matched row yields no artist and no title | `itemprop=numTracks` (52 / 12), the `og:description` "with N ... tracks" (52 / 12) and the `IDed X / N` sidebar all equal the container count | an under-matching selector (fewer containers, all parseable) passes today |

## Verdict

**Is any stored cue time wrong? No: 0 of 52 rows in the anchor and 0 of 12 in the short capture.**
Every row stores `timestamp = None`. That is correct for these pages, because neither carries a
cue: the visible `.cue` is empty on all 64 rows, the hidden inputs all read `0`, and the only
`cueValuesEntry` is a one-entry placeholder for row 1 at 0 seconds. The parser never reads the
hidden inputs, so the "default to 0 and corrupt uncued rows" failure cannot occur here.

**The bead's premise is partly contradicted.** The brief expects a cue map to compare against
visible text on the 52-row capture. There is none: the capture is of an uncued set. So the
inline-map question cannot be answered from these fixtures. It remains open for cued pages, and
the existing cue handling is correct only for uncued ones. Whether a cued page fills `.cue` in the
served markup or by script is unknown from this evidence (the `.cue` div has `data-mode="hours"`
and an `onclick="toggleCue"`, so script fill is plausible); `docs/design/0014-tracklist-candidate-sets.md`
already records that no capture held a populated cue. Three other candidates have no positive
evidence either: `span.trackStatus` (0 hits), `w/` mashup rows (0 real hits) and set-level
SoundCloud/Apple links (0 set-level hits, only site and artist links). Stage taxonomy is absent too.
No test can be written against these from the repo's fixtures without fabricating markup.

Two findings the candidate list did not name, both provable from the fixtures:

1. **12 rows (10 + 2) store the literal placeholder `ID` as both artist and title.** The resolved
   discriminator is exact in both captures: `data-isided="true"` is absent on exactly those rows.
2. **The canary gap is real.** Container count is confirmed by three independent page-level
   numbers in both captures, none of which the parser consults.

## Recommendation

Ranked by value over cost, each proven by a fixture.

1. **Drift canary: compare the parsed row count to `itemprop=numTracks`.** Proof: both
   `25fhn7c9-ok.html` (52) and `19h6nw7t-ok.html` (12) agree with the container count. Cheap and
   independent of every row selector. Also usable: `IDed X / N`.
2. **Mark unresolved "ID - ID" rows instead of storing `ID`/`ID`** (use `data-isided` absence).
   Proof: `25fhn7c9-ok.html` (10 rows), `19h6nw7t-ok.html` (2 rows). This is a correctness fix to
   what is stored today; it needs a decision on representation and is the one item here that
   corrects the existing output rather than adding a field.
3. **Recording duration from the media tab (`Player N [h:mm:ss]`) and per-row duration.** Proof:
   `25fhn7c9-ok.html` (`[2:08:31]`, 34 row durations), `19h6nw7t-ok.html` (`[1:00:00]`, 10 row
   durations). A scoring signal against the archive file's own duration.
4. **`/source/` festival and venue, with the sidebar role text.** Proof: `25fhn7c9-ok.html`
   (Time Warp as "Open Air / Festival", Maimarkthalle Mannheim as "Event Location"),
   `19h6nw7t-ok.html` (adds Radio Show and Broadcasted @). Needs new `Tracklist` columns or a
   side table; stage is not adoptable (0 / 0).
5. **Set and row genres.** Proof: `25fhn7c9-ok.html` (set `Techno`; 31 row genres),
   `19h6nw7t-ok.html` (set `Tech House`, `Techno`; 10 row genres). Needs schema; lowest direct
   value to the rename workflow.
6. **Set headers ("First Set:", "Second Set:").** Proof: `25fhn7c9-ok.html` only (2 headers).
   Low value, single-fixture evidence.
7. **Set-level YouTube/Mixcloud link.** Proof: `25fhn7c9-ok.html` (YouTube), `19h6nw7t-ok.html`
   (Mixcloud). SoundCloud and Apple are not adoptable (0 set-level hits).

**Do not adopt yet, evidence needed first:** the inline cue map, `w/` mashup rows and
`span.trackStatus`. Each needs a new capture of a page that actually contains the thing (a cued
set; a set with a mashup row; a set with an ID Remix row), which costs host requests at one per
8 s and should be filed as its own capture bead before any parser change. Until then the current
cue handling stays as it is: it is correct for the two uncued fixtures and untested elsewhere.
