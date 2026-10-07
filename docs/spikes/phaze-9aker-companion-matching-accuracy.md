# phaze-9aker — companion-to-media linking accuracy: content references, duplicates, close-name fallback

- **Bead:** `phaze-9aker` (spike, molecule `phaze-gyj6i` — companion-file intelligence)
- **Date:** 2026-10-07 (UTC)
- **Tree:** branch `wt/bead/issue/phaze-9aker`, forked from the molecule container at `ce79b05d`
- **Code under test:** none. The linking methods were prototyped in the per-bead scratch directory only. Method (a) imports the
  shipped `companion_match_key` from `src/phaze/constants.py` unchanged. **No product code, test code or build config changed.**
- **Production access:** read-only, as in `docs/spikes/phaze-lm73u-companion-content-survey.md`. One Postgres session on
  `host-prod` (`SET default_transaction_read_only = on; SET statement_timeout = '60s'`, `SELECT` only) and two read passes
  inside the `phaze-agent-worker-meta` container on `host-store` (`nice -n 19`, archive mounts `ro`). The probe scripts were
  removed from both containers and both hosts afterwards.
- **Scrubbing:** archive roots are `<root-1>` … `<root-8>` as in the survey; the 18,537-file flat folder under `<root-5>` is
  `<dump>`; release-site stamp files are `<site-a>.nfo`; a radio-show series is `<series-a>`. Archive names in the shapes
  below are rewritten with placeholders. The labels, excerpts and the placeholder mapping stay in the per-bead scratch
  directory. All quantities are exact.

______________________________________________________________________

## Question

Epic `phaze-gyj6i` asks, as its second question: *how accurately can content references and close-name matching link
companions to media, including inside the 18,537-file dump folder, at what thresholds?*

The bead was re-scoped before it ran. Operator decision 2026-10-07 (dispatch session, `AskUserQuestion`; durable record: bead
`phaze-9aker` comment of 2026-10-07). Question as put: *"The content survey changes the premise of the next spike (phaze-9aker,
matching accuracy). Content references are near-exact: .cue/.m3u/.txt name their media and resolve exactly to a same-folder
file 95–98% of the time. Most orphans aren't waiting for a parent-folder match. 2,797 of 4,580 are byte-identical copies of an
already-imported companion, and most orphans' references resolve to a media file elsewhere, usually in a same-named release
folder. Only 838 have any media in their parent folder. How should phaze-9aker change before it runs?"* Answer as given
(selected option label, verbatim): **"Re-scope to content + duplicates (Recommended)"**.

So this bead measures, on a hand-labeled set:

- **(a)** the shipped exact-key rule (`companion_match_key`, unchanged) as the baseline;
- **(b)** a content reference resolved to a media row in the companion's own folder;
- **(c)** a content reference resolved to a media row anywhere on the same agent;
- **(d)** close-name matching with release-group, source and copy tags stripped, used only when (b) and (c) give nothing;

plus what to do with byte-identical duplicate orphans, false links inside `<dump>`, and how much of the population each
recommended method would link.

Who decides the true answer was settled earlier. Operator decision 2026-10-06 (dispatch session, `AskUserQuestion`; durable
record: epic `phaze-gyj6i` description). Question as put: *"Who decides the \"correct answer\" for the hand-labeled sample
that matching accuracy is measured against?"* Answer as given (selected option label, verbatim): **"Agent labels, you
spot-check (Recommended)"**.

______________________________________________________________________

## Method

### 1. Inputs (counted)

The population is the survey's snapshot (`phaze-lm73u`, scratch read-only): every `files` row on the fileserver agent
(145,057 rows: 104,247 media, 40,810 admitted companions), every `orphan_companion_diagnostics` row (4,580), the 14,438
companion ids linked in `file_companions`, and the survey's per-file features for all 45,390 companions (CUE `FILE` values,
M3U and PLS entries, NFO/TXT tokens ending in a media extension, sha256, and the junk label).

**Drift check.** Before measuring I re-read production (one session, counts only): 145,057 rows on the agent with the same
per-type counts, 4,580 orphan rows, 14,438 distinct linked companions (19,983 link rows), newest `files.created_at` older than
the snapshot. Nothing had moved.

### 2. The methods (prototype code, scratch only)

| Method | Rule |
| --- | --- |
| **(a)** shipped | `associate_companions`' rule, re-implemented on the snapshot: media in the companion's own folder → link to **all** of it; otherwise parent-folder media whose `companion_match_key` stem equals the key of the companion's folder, its parent folder or its own stem. `companion_match_key` is imported from `src/phaze/constants.py`, unchanged. |
| **(b)** ref → own folder | Each reference (CUE `FILE`, M3U entry, PLS `FileN=`, NFO/TXT token ending in a media extension) resolved by basename to a media row in the companion's own folder, exact then case-insensitive; a relative path (`<dir>\<file>`) is followed from the companion's folder; for NFO/TXT a token that *ends with* a sibling media filename also counts (survey §4.2). |
| **(b′)** | (b) plus "same stem, different extension" (a `.wav` sheet beside an `.mp3`), as its own rule (survey Recommendation 1). |
| **(c)** ref → anywhere | (b) first. Otherwise the reference's basename against every media row on the agent: a **unique** basename links; a non-unique one links only when exactly one candidate sits in a folder with the same release-folder name (alphanumerics, case-folded); otherwise nothing. |
| **(d)** close-name, parent pool | Only when (c) gives nothing. Candidates are the media in the companion's own folder, else its parent folder. Names are tag-stripped: scene index, ` (1)`/` copy` markers, a `-GROUP` tail, and source/format/copy tags (`WEB`, `SAT`, `FM`, `SBD`, `320`, `TT`, `MP3`, …). Score = max over {companion stem, companion folder} of max(token Jaccard, `SequenceMatcher` ratio) against each media stem. Link only the **unique best** at or above the threshold, with at least 0.05 over the runner-up. |

Measured alongside, because the first results showed (d)'s parent pool is the wrong place to look:

| Method | Rule |
| --- | --- |
| **(s)** own-folder stem | Own-folder media whose `companion_match_key` equals the companion's own stem key. Narrows (a) in multi-media folders. |
| **(e)** release-folder twin | Companion folder holds no media, and **exactly one** other folder on the agent has the same release-folder name (12 or more alphanumerics) → that folder's media. |
| **(dw)** close-name, agent pool | (d)'s scoring against every media row on the agent, by folder name and stem. Candidates come from a token index (tokens with fewer than 2,000 postings), the best 30 by Jaccard re-scored. **Date guard:** if the companion's name carries a full date, the candidate must carry the same date (day/month order free). Unique best with the same 0.05 margin, where rivals in the best candidate's own folder do not count. |
| **J:** junk veto | No link for a companion the survey's classifier labels `junk:*` (stamp, empty or all-NUL, site ad). |

The recommended chain is **J: (c) → (s) → (a) → (e) → (dw)**: the first step that returns anything wins.

### 3. The labeled set (sampled)

164 companions, drawn by stratum with a fixed seed (`9`), all read successfully. Strata were drawn in the order below, and
a file is counted once, in the first stratum that drew it.

| Stratum | Population | Drawn | How |
| --- | ---: | ---: | --- |
| Known junk | 3,874 stamp + 396 empty + 41 site-ad admitted; 350 orphan | 21 | 8 stamp, 5 empty, 4 site-ad (admitted), 4 orphan |
| Numbered episode files (`NNN.txt`) | 40 | 8 | `003.txt` (the only one whose audio exists) forced in, 7 random |
| Byte-identical duplicate orphans of an admitted companion | 2,464 non-junk | 30 | `.m3u` 9, `.nfo` 8, `.txt` 9, `.cue` 4 |
| Orphans whose reference resolves elsewhere (not duplicates) | 140 | 25 | random |
| Orphans with media in their parent folder (the 838) | 838 | 35 | by best (d) score: ≥ 0.8 (47 → 12), 0.6–0.8 (219 → 10), < 0.6 (552 → 13) |
| Admitted same-folder companions (not junk) | 40,810 less junk | 45 | per extension (`.cue` 10, `.m3u` 10, `.txt` 10, `.nfo` 12, `.pls` 3), half single-media and half multi-media folders |

Each stratum's population counts the files not already drawn by an earlier stratum, which is why the three 838 bins sum to
818. 43 of the 164 are orphans in an immediate child folder of `<dump>`, so their parent pool is `<dump>`'s 18,537 media
rows.

**Labeling.** For each of the 164 I read, on `host-store`, up to 14 meaningful lines of the file and every line naming a
media file (only excerpts left the host). Beside the excerpt I listed every candidate any method proposed, the own-folder
media, the media in any same-named release folder elsewhere, and the best three by a whole-agent name search. Each label is
the media file(s) the content describes, or `none`. Three conventions, all put to the operator in the spot-check:

1. A stamp, advert or empty file beside a recording is `none`: its content describes nothing, although it was downloaded
   with that recording.
2. A companion whose recording sits in **another copy** of the same release folder is labeled with that recording. The label
   records what the content describes; whether phaze should *link* it is a separate policy question (§4.6, Recommendation).
3. A release note with no filename inside (a scene NFO, a multi-part tracklist) is labeled with **every part** in its own
   folder, except in a collection folder (one folder of many episodes), where only the episode it names counts.

**The labels are not independent of the name search.** For 21 labels the true recording sits outside the companion's folder
and its reference does not resolve; I found it through the name-search or same-named-folder candidate and confirmed it
against the file's own header (artist, title, date). Methods (e) and (dw) look in the same place, so their agreement on those 27 is partly by
construction. The operator spot-check is the independent check on them.

### 4. Operator spot-check

The dispatcher published all 164 proposed labels (path, excerpt, top-three candidates with method and score, proposed label,
reason) to the operator as a private claude.ai page and relayed the corrections. They are recorded in §4.2, and every number
in §4.3 onward uses the corrected labels.

A further 60 labels (§4.8) were drawn **from the population of links** the fallbacks produce, to measure their precision
where it is applied. They were published on the same page and are operator-checked in the same review.

### 5. Scoring

- **Link precision** = correct links ÷ links proposed. A multi-part release counts every part.
- **Companion precision** = linked companions with no false link ÷ linked companions.
- **Recall** = companions with a true recording that got at least one correct link ÷ companions with a true recording.
- **`none` kept** = companions labeled `none` that the method left unlinked.
- **`<dump>` false links** = proposed links to a media row directly in `<dump>` that are not in the label.

The strata are not population-weighted, so the whole-set figures describe this sample. The per-stratum figures and the
population counts in §4.7 are the ones to carry into a decision.

______________________________________________________________________

## Evidence

### 4.1 What the labels say (counted on the labeled set)

118 of the 164 have a true recording and 46 are `none`. By stratum:

| Stratum | n | Has a recording | `none` | Where the recording is |
| --- | ---: | ---: | ---: | --- |
| Admitted same-folder | 45 | 45 | 0 | own folder: 26 named by a reference, 19 only by being the release beside it |
| Duplicate orphans | 30 | 30 | 0 | another copy of the same release folder, every one |
| Orphans resolving elsewhere | 25 | 25 | 0 | another folder of the same release name (23), a season playlist's sub-folders (1), a collection list (1) |
| The 838 (parent media) | 35 | 16 | 19 | elsewhere on the agent, every one; the parent folder holds it only for one set filed twice under two names |
| Numbered `NNN.txt` | 8 | 1 | 7 | `003.txt` → the grandparent folder's only episode; the other 7 episodes are not in the archive |
| Known junk | 21 | 1 | 20 | 1 is mislabeled junk (below) |

Of the 19 `none` among the 838, 10 are stamps or adverts, 8 describe a recording that is not in the archive, and 1 is a
collection readme naming no file.

**One junk label is wrong.** The survey's classifier labels one `.nfo` `junk:site-ad`, but it is a real release note (artist,
date, venue, set length) for the one recording beside it. The junk veto therefore costs one true link in this sample.

### 4.2 Operator corrections

The operator reviewed the labels on 2026-10-07 (source: the phaze-9aker review page, a private claude.ai page; the export
is kept in per-bead scratch). The result:

- **All 224 labels reviewed (164 main + 60 addendum), all 224 marked correct. There were zero corrections**, so the proposed
  labels are the ground truth and every number below is final.
- **The three labeling conventions** (§3: stamps beside a recording are `none`; a recording in another copy of the release
  is the label; release notes take every part except in collection folders) were all marked **"agree"**.
- **14 notes, all on junk-category items**, each "correct, but …". Quoted verbatim:
  - on repeated stamps and site adverts (9 notes): *"it's correct, but this is a generic file indicating where the file was
    downloaded from. it can be marked for deletion"*
  - on empty files (5 notes): *"it's correct, but if the file is empty, then i think we should mark the file for deletion"*

  These bear on the junk-review design (`phaze-ib3ub`, decision bead `phaze-5qymm`), not on the matching numbers here.

### 4.3 Precision and recall per method (corrected labels)

All 164, whole-set figures:

| Method | Linked | Links | Link precision | Companion precision | Recall | `none` kept (of 46) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| (a) shipped key | 66 | 282 | 0.340 | 39/66 | 46/118 | 26 |
| (b) ref → own folder | 27 | 93 | 1.000 | 27/27 | 27/118 | 46 |
| (b′) + other-extension stem | 27 | 93 | 1.000 | 27/27 | 27/118 | 46 |
| (c) ref → anywhere | 77 | 179 | 1.000 | 77/77 | 77/118 | 46 |
| (d) close-name, parent pool, 0.8 | 29 | 29 | 0.345 | 10/29 | 10/118 | 27 |
| (d) with junk veto | 11 | 11 | 0.818 | 9/11 | 9/118 | 44 |
| (e) release-folder twin | 47 | 50 | 0.940 | 45/47 | 47/118 | 46 |
| (dw) close-name, agent pool, junk veto, date guard, 0.9 | 35 | 35 | 1.000 | 35/35 | 35/118 | 46 |
| **J: c → s → a** | 96 | 232 | 0.974 | 95/96 | 96/118 | 46 |
| **J: c → s → a → e** | 105 | 241 | 0.975 | 104/105 | 105/118 | 46 |
| **J: c → s → a → e → dw (0.9)** | 115 | 251 | 0.976 | 114/115 | **115/118** | **46** |

Wilson 95% intervals on companion precision: (c) 77/77 → 0.952–1.000; the full chain 114/115 → 0.952–0.998; (a) 39/66 →
0.470–0.701; (d) 10/29 → 0.199–0.527.

By stratum, for the methods that decide the recommendation (link precision; recall as found/with-a-recording):

| Stratum (n) | (a) | (b) | (c) | (d) 0.8 | (e) | (dw) 0.9 | Chain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Admitted (45) | 0.38; 45/45 | 1.00; 26/45 | 1.00; 26/45 | 1.00; 8/45 | —; 0/45 | 1.00; 17/45 | **0.94; 45/45** |
| Duplicate orphans (30) | —; 0/30 | —; 0/30 | 1.00; 21/30 | —; 0/30 | 1.00; 24/30 | 1.00; 7/30 | **1.00; 29/30** |
| Orphans resolving elsewhere (25) | —; 0/25 | 1.00; 1/25 | 1.00; 25/25 | —; 0/25 | 0.88; 22/25 | —; 0/25 | **1.00; 25/25** |
| The 838 (35) | 0.00; 0/16 | —; 0/16 | 1.00; 5/16 | 0.125; 1/16 | 1.00; 1/16 | 1.00; 11/16 | **1.00; 16/16** |
| Numbered (8) | —; 0/1 | —; 0/1 | —; 0/1 | —; 0/1 | —; 0/1 | —; 0/1 | —; 0/1 |
| Known junk (21) | 0.034; 1/1 | —; 0/1 | —; 0/1 | 0.077; 1/1 | —; 0/1 | —; 0/1 | —; 0/1 |

What the rows show:

- **(a) over-links, and links junk.** In a multi-media folder it links the companion to everything there: 250 links for 45
  admitted companions, 95 of them right. In the 838 its 3 links are stamps tied to a set in `<dump>`. In the junk stratum it
  links 17 of 20 junk files.
- **(b) and (c) never produced a false link** (179 links). (c) is the only method that finds the orphans' recordings when
  they carry a filename. (b′) adds nothing on this sample; on the population it adds 52 of the unlinked admitted companions.
- **Who links what in the chain, on the set:** (c) 77, (s) 5, (a) 14, (e) 9, (dw) 10; 32 vetoed as junk, 17 left
  unlinked. The one wrong link is (a)'s.
- **(c) misses the companions with no usable reference:** release NFOs (no filename inside), and references to recordings
  that are not in the archive.
- **(e) is exact but blunt.** Its 2 errors (orphans resolving elsewhere) are per-part CUE sheets whose twin folder holds
  every part: (e) links the whole folder. (c) catches both first, and behind (c), (s) and (a) the step adds 9 companions on
  the set, all correct.
- **The chain's one wrong companion** is a tracklist for one episode, in a month folder holding four episodes. Its stem does
  not match its episode's files, so (s) does not narrow it and (a) links all four.
- **The three misses:** a duplicate playlist whose recording exists twice, in two folders of the same name (c refuses the
  ambiguity, and (e) needs exactly one twin); `003.txt`, whose episode is two folders up; and the mislabeled-junk release note.

### 4.4 Threshold curves (corrected labels)

**(d), parent pool, as specified** — the 51 orphans of the set where (c) gives nothing (margin 0.05):

| Threshold | 0.5 | 0.6 | 0.7 | 0.8 | 0.85 | 0.9 | 0.95 | 1.0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Links | 11 | 10 | 8 | 8 | 7 | 4 | 4 | 4 |
| Correct | 2 | 1 | 1 | 1 | 1 | 0 | 0 | 0 |
| Recall (of 21) | 2 | 1 | 1 | 1 | 1 | 0 | 0 | 0 |
| False links into `<dump>` | 9 | 9 | 7 | 7 | 6 | 4 | 4 | 4 |

With the junk veto (36 orphans):

| Threshold | 0.5 | 0.6 | 0.7 | 0.8 | 0.85 | 0.9 | 0.95 | 1.0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Links | 6 | 5 | 3 | 3 | 2 | 0 | 0 | 0 |
| Correct | 2 | 1 | 1 | 1 | 1 | 0 | 0 | 0 |

**No threshold makes (d) useful.** Raising it removes the correct link before the false ones: the links that survive at 0.9
and above are stamps whose folder is named exactly like a set in `<dump>`. With junk vetoed, (d) links nothing at 0.9. The
recordings these orphans describe are not in the parent folder (§4.1).

**(dw), agent pool, junk vetoed** — the same 36 orphans:

| Threshold | 0.5 | 0.6 | 0.7 | 0.8 | 0.85 | 0.9 | 0.95 | 1.0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Links, no date guard | 21 | 21 | 21 | 19 | 19 | 19 | 19 | 18 |
| Correct, no date guard | 18 | 18 | 18 | 18 | 18 | 18 | 18 | 18 |
| Links, date guard | 20 | 20 | 20 | 18 | 18 | 18 | 18 | 18 |
| Correct, date guard | 18 | 18 | 18 | 18 | 18 | 18 | 18 | 18 |

Recall is 18 of 21 at every threshold. The 3 misses are the episode two folders up, the twice-filed playlist and the
mislabeled-junk note (§4.3). On the 60-label population sample (§4.8) the date-guarded curve is flat at 23 correct of 25
links from 0.8 to 0.85 and **23 of 23 from 0.9**; below 0.8 it cannot be read, because the sample was drawn at 0.8.
**0.9 is chosen on that sample and then scored on it**, so treat its 23/23 as an upper bound; the 164-label set, which did
not choose it, is unchanged between 0.8 and 1.0.

### 4.5 False links inside `<dump>` (18,537 media rows in one folder)

`<dump>` holds no companions directly; 706 of the 838 orphans (and 9,897 admitted companions, all with media of their own)
sit in its immediate child folders. Only a method that consults the parent folder, or searches the whole agent, can link
into it.

| Method | Labeled set: links into `<dump>` / false | Population: orphans linked into `<dump>` |
| --- | --- | ---: |
| (a) shipped | 3 / 3 (all three are stamps) | 15 (of its 23 orphan links; 18 of the 23 are junk) |
| (b) | 0 | 0 |
| (c) | 0 | 0 |
| (d) parent, 0.8 | 8 / 7 | 42 (26 of them junk) |
| (d) parent, 0.8, junk veto | 3 / 2 | 16 |
| (e) | 0 | 0 |
| (dw) 0.9, date guard | 0 | 0 |
| Chain | 0 | 1 (the step (a) link of one non-junk orphan) |

The one correct (d) link into `<dump>` is a set filed twice: once as a release folder elsewhere, once under another name
directly in `<dump>`. Against 18,537 neighbours, name proximity alone links the wrong set 7 times in 8.

### 4.6 Byte-identical duplicate orphans (counted, whole population)

2,797 of the 4,580 orphans are byte-identical to an admitted companion (`.nfo` 1,002, `.txt` 995, `.m3u` 791, `.cue` 9).

- **Every one sits in a folder with no media.** For 2,204 the admitted copy is in a folder with the same release-folder name;
  for 593 the name differs.
- 2,380 have exactly one admitted copy, 78 have two, 339 have three or more.
- 333 are junk (stamps, mostly). Of the other 2,464, the chain resolves 2,380: by (c) 1,684, (e) 593, (dw) 103.
- **In 2,379 of those 2,380, the recording the duplicate's own content resolves to is exactly the recording its admitted
  copy links to.** One resolves to a different recording. 84 resolve to nothing, while their admitted copy links to something.

So "link the duplicate to the same recording as its original" and "resolve the duplicate's own content" are the same answer,
measured. Which of these phaze should **do** is not a measurement:

1. link the copy to the same recording, giving the recording a second, identical companion row;
2. treat the copy as a junk duplicate and send it to the junk-review deletion queue designed in `phaze-ib3ub`;
3. leave it as an orphan diagnostic.

**This is a question for the operator** (Recommendation, question 1).

### 4.7 Whole-population counts (read-only, chain thresholds as recommended)

**The 4,580 orphans:**

| Step that links (first to fire) | Orphans | Links | Of them duplicates | Into `<dump>` |
| --- | ---: | ---: | ---: | ---: |
| Junk veto (not linked) | 350 | — | 333 | — |
| (c) reference → anywhere | 1,824 | 2,079 | 1,684 | 0 |
| (s) own-folder stem | 0 | — | — | — |
| (a) shipped rule | 5 | 5 | 0 | 1 |
| (e) release-folder twin | 608 | 628 | 593 | 0 |
| (dw) close-name, agent, 0.9, date guard | 213 | 213 | 103 | 0 |
| Nothing | 1,580 | — | 84 | — |
| **Linked** | **2,650 (57.9%)** | 2,925 | 2,380 | 1 |

Of the 838 with parent media: 335 junk, 133 by (c), 5 by (a), 17 by (e), 128 by (dw), 220 unlinked.

For comparison, single methods on the orphans: (a) 23 (18 junk), (b) 1, (b′) 1, (c) 1,824, (e) 2,281, (d) parent 0.8 42
(all into `<dump>`), (d) parent 0.8 as fallback with junk veto 10.

**The 26,372 admitted-but-unlinked companions** (admitted under `<root-1>`, `<root-5>` and `<root-6>`; `associate_companions`
has not run since those scans — survey Recommendation 5):

| Step | Companions | Links |
| --- | ---: | ---: |
| Junk veto | 4,231 | — |
| (c) | 10,948 | 14,032 |
| (s) | 6,950 | 7,273 |
| (a) | 4,242 | 12,200 |
| Nothing | 1 | — |
| **Linked** | **22,140** | **33,505** |

The shipped rule alone would link 26,370 of them with 78,789 links. On the 14,438 companions already linked under `<root-4>`,
the chain keeps a link for all but **80, which are junk**.

### 4.8 Population sample of the fallbacks (60 labels)

The 164-label set has few fallback links where they matter, so I drew 60 more, at random (seed `19`), from what the fallbacks
actually produce across the population (excluding the 164):

| Drawn from | Population | n | Method's companion precision | Errors |
| --- | ---: | ---: | --- | --- |
| (dw) links, 0.8, date guard, orphans with no (c) link | 942 | 25 | 23/25 at 0.8; 23/23 at 0.9 | 2: a different artist from the same event and date |
| (e) links, orphans with no (c) link | 599 | 15 | 15/15 | — |
| Admitted, no reference, multi-media folder (where (a) links everything) | 2,748 | 20 | (a) 18/20; chain 20/20 | (a): 2 tracklists in folders of 25 and 38 different mixes; (s) narrows both to the right file |

All 60 addendum labels were marked correct in the operator review (§4.2).

### 4.9 Not measured

- **Grandparent resolution.** `003.txt` is the only one of the 40 numbered files whose audio exists, two folders up. It is
  the chain's only structural miss. Survey Recommendation 4 already judged a rule not worth building for one file.
- **`.mp2`.** 26 orphans reference `.mp2` media, which is not ingested (survey §4.2). No method can link them.
- **Ambiguous basenames.** 42 orphans name a basename held by several media rows, none of them in the companion's
  same-named release folder. (c) refuses them. In the one I labeled, the recording exists twice in two same-named folders,
  and either copy would do.

______________________________________________________________________

## Verdict

| Method | Verdict | Evidence |
| --- | --- | --- |
| **(a)** shipped exact key | **NO-GO as the rule; GO as the last own-folder fallback** | Link precision 0.340 on the set, 17 of 20 junk files linked, all 3 links into `<dump>` false. In single-media folders, and in multi-media folders behind (c) and (s), it is right: chain 0.94 on admitted, 20/20 on the population sample. |
| **(b)** reference → own folder | **GO** | 93/93 links correct. (b′) is GO as part of (b): no error, +52 unlinked admitted companions on the population. |
| **(c)** reference → anywhere on the agent | **GO** | 179/179 links correct (companion precision 0.952–1.000 at 95%); the only method that finds most orphans' recordings; 1,824 orphans and 10,948 unlinked admitted companions; 0 links into `<dump>`. |
| **(d)** close-name, parent pool | **NO-GO** | Link precision 0.125–0.345 at every threshold. 7 of 8 links into `<dump>` false. With junk vetoed it links 1 correct file at 0.8 and nothing at 0.9. The recordings are not in the parent folder. |
| (e) release-folder twin | **GO behind (c)** | 15/15 drawn from its own population links, 19/19 more inside the (dw) sample; 9/9 behind (c) on the set; 608 orphans. |
| (dw) close-name, agent pool, junk veto, date guard | **GO at 0.9, behind (c) and (e)** | 35/35 on the set; 23/23 at 0.9 on the population sample (upper bound, §4.4); 213 orphans. |
| Junk veto | **GO** | Removes every junk link (20/20 `none` kept). It costs 1 true link in this sample (§4.1). |

Content is the linking signal; names are not, except as a whole-agent search for releases that carry no filename.

______________________________________________________________________

## Recommendation

1. **Link in this order, first hit wins, with junk vetoed first:** (c) reference → anywhere on the agent, including (b) and
   (b′); (s) own-folder stem; (a) the shipped own-folder rule; (e) release-folder twin; (dw) whole-agent close-name at 0.9
   with the date guard. On the 164 labels: 114/115 companions linked correctly, 115/118 found, every `none` kept, 0 links into
   `<dump>`. On the population: 2,650 of 4,580 orphans and 22,140 of 26,372 unlinked admitted companions.
2. **Retire the parent-folder name fallback** (the ehryj rule inside (a) for companions with no own media, and (d) as
   specified). It produces the `<dump>` false links. On the population it links 23 orphans, 18 of them junk.
3. **The scan's admission rule is the real gate.** Orphans exist because `tasks/scan.py` admits a companion only beside media
   or name-matched parent media. A content rule at association time can only link what the scan admitted. Linking the 2,650
   orphans means admitting a companion on its content (a resolvable reference) or a release-folder twin, not on its location.
   That is a design decision for `phaze-5qymm`, not this bead.
4. **Questions for the operator** (policy, not measurement):
   1. **Duplicate orphans** (§4.6): 2,464 non-junk byte-identical copies, 2,380 of them resolving to exactly their original's
      recording. Link the copy to that recording too, send it to the junk-review queue as a duplicate, or leave it as an
      orphan?
   2. **Stamps beside a recording (answered):** labeled `none` here (convention 1). Confirm that a release-site stamp, advert or empty
      file is never linked, even beside the one recording it was downloaded with. The operator agreed with this convention on 2026-10-07 (§4.2), and noted that such files *"can be marked for deletion"*. So this question is answered: never link them.
   3. **Collection folders:** a tracklist for one episode in a folder of many episodes. (s) fixes the case where the stems
      match; one labeled case (month folder, stems differ) still links all episodes. Accept, or require a reference for
      folders with more than N media?

______________________________________________________________________

## Lessons and their general form

*A rule keyed on where a file is answers a different question from one keyed on what it says.* The shipped rule and (d) both
ask "what is near this companion?". In this archive the honest answer is often "a dump of 18,537 unrelated sets" or "nothing:
the recording was kept in another copy of the release". The content's own reference, or a name search over the whole agent,
asks "what does this companion describe?". The general form is `CLAUDE.md`'s verification-fidelity rule 3: **a proxy that
cannot see the failure class cannot rule it out.** Proximity cannot see that the recording lives elsewhere. Written down here
because it is specific to this linking design.

*Tag-stripping makes names converge, and that cuts both ways.* Stripping source, format and group tags is what lets (dw) match
`Artist-Live_at_Venue-FM-DD-MM-YYYY-GROUP` to `Artist - Live at Venue (DD-MM-YYYY) MP3`. It also makes two artists' sets
from the same event and date differ only by the artist token, and both population-sample errors were exactly that. The date
guard cannot help when the date is shared. The general form, already in `CLAUDE.md` (rule 3, the discriminating consumer):
**feed a matcher the near-miss it will meet, not only the hit.** The population sample was drawn for that reason.
