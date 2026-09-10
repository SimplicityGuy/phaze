# ADR-0018 — Set projection and file viewer: projection shape, energy weights, Camelot table, boundaries

| | |
| --- | --- |
| **Status** | Accepted — energy weights pending the operator blind check (`phaze-x1qr3.12`) |
| **Date** | 2026-09-06 |
| **Bead** | `phaze-x1qr3.4` (epic `phaze-x1qr3`) |
| **Supersedes / superseded by** | nothing |

______________________________________________________________________

## 1. Context

Phaze stores every fine and coarse window of every file: BPM and key every 30 s, seven mood
probabilities, danceability, tonality, voice and gender every 180 s, plus a scraped tracklist
with a timestamp per track. Almost nothing reads it beyond one BPM polyline and three argmax
ribbons in the record drawer. Epic `phaze-x1qr3` builds a per-file viewer — an energy arc, a
mood river, key and BPM lanes on one time axis with tracklist boundaries, pointer inspection,
per-track derived stats, a set glyph, a right-sidebar "more like this set", and a printable
poster — all reading the same underlying data.

**The deciding question is not "what should the viewer show" — it is "where does it read
from".** The 7 mood scores, danceability, tonality, voice and gender live inside a ~5 KB JSONB
blob per coarse window (`analysis_window.features`, shape
`features[set_name][variant] -> [{label, prediction}]`, with the positive class selected BY
LABEL — see `services/analysis_derive._positive_class_prediction` for the alphabetical-ordering
trap that makes a positional read of that JSONB wrong for three of the seven moods). Reading
that JSONB per request does not scale to the 200,000-file target (on the order of 4,000,000
window rows): every lane on every page view would deserialize and re-derive the same ~5 KB blob
across however many windows a file has, every time anyone opens it.

## 2. Decision

**Add a narrow, typed projection alongside the existing windows and files, backfilled from the
stored JSONB with no re-analysis, and make every later surface in the epic a query over the
projection rather than a re-derivation of the raw features.** "One projection, then everything
rides it": the projection is computed once (at analysis completion, and by a batch backfill for
existing rows) and every rendering surface — the timeline lanes, the tracklist index, the set
glyph, the Camelot journey, similarity — reads the same small set of columns.

The projection lands in two beads beneath this one: `phaze-x1qr3.1` (schema — migration `063`,
new nullable columns on `AnalysisWindow`, the new `SetProfile` table) and `phaze-x1qr3.2`
(math — pure functions in `services/set_projection.py` from stored rows to the projection, no
I/O). This ADR is `phaze-x1qr3.4` and records the shape those two beads shipped, the judgment
calls their implementers made, and the standing boundaries the rest of the epic must respect.

## 3. The per-window and per-file shapes

**Per window** — three new nullable columns on `analysis_window` (migration
`alembic/versions/063_set_projection.py`), purely additive: no existing row is rewritten and no
existing read changes.

| column | tier | type | meaning |
| --- | --- | --- | --- |
| `energy` | coarse | `FLOAT`, `[0, 1]` | the energy scalar (§4) for this window |
| `camelot` | fine | `VARCHAR(3)` | Camelot wheel position derived from `musical_key` (§5) |
| `mood_scores` | coarse | `JSONB` | the 11 positive-class means, keyed by `MOOD_ORDER` name (§6), averaged over the 3 analysis variants |

**Per file** — a new `set_profile` table (`src/phaze/models/set_profile.py`), 1:1 with `files`
via `file_id` as its own primary key, `ON DELETE CASCADE`. A sidecar table rather than columns
on `AnalysisResult`, because `AnalysisResult.features` is already "the longest window's dict" —
a *sample* — and every column below is a *summary* over the whole file; putting a summary next
to a sample invites reading the wrong one.

| column | type | meaning |
| --- | --- | --- |
| `mean_vector` | `FLOAT[]` (11) | mean positive-class vector, positional in `MOOD_ORDER` |
| `arc` | `FLOAT[]` (64) | the coarse energy series, linearly resampled to a fixed length; gaps are `NaN`, never bridged |
| `glyph` | `JSONB` | one `{camelot_number, energy}` cell per coarse window, ordered by `window_index` |
| `camelot_modal` | `VARCHAR(3)` | duration-weighted modal Camelot position across fine windows |
| `harmonic_discipline` | `FLOAT`, `[0, 1]` | share of key transitions that are wheel-adjacent after the flicker filter (§5) |
| `peak_sec` | `FLOAT` | elapsed seconds of the arc's maximum sample — where the page rests with nothing hovered |
| `projection_version` | `INTEGER`, not null, default `1` | which projection run produced this row, so a weight change re-backfills only stale rows |

`mean_vector` and `arc` have no Postgres length constraint — 11 and 64 are writer contracts
carried by `MOOD_ORDER`'s own length and the resample width (`ARC_POINTS`), not DDL. A file with
no coarse windows at all (661 fine-only files exist today per `phaze-hia9z`) gets `NULL` in
`mean_vector`, `arc`, `glyph` and `peak_sec` — an honest gap, never a manufactured zero vector
that would read as a measurement of a silent set. `camelot_modal` and `harmonic_discipline` are
built from FINE-tier windows (§5) and so carry real values on such a file; `projection_version`
is never null (`NOT NULL`, `server_default` `1`) — a row exists only because the projection ran,
so there is no "no version" state for it to represent.

The write path (`phaze-x1qr3.3`) computes each window's `energy` / `camelot` / `mood_scores`
first — via `camelot_code`, `positive_class_vector` and `energy` below — and only then calls
`build_profile` over the now-populated windows to get the per-file row. `build_profile` itself
does no per-window computation and touches no database; see §7.1.

## 4. The energy scalar, and the weights as the implementer's decision

One function, `services/set_projection.energy`, over one linear combination:

```text
energy = clamp01(w_d·danceability + w_p·party + w_a·aggressive
                  − w_r·relaxed − w_s·sad + w_b·z(bpm))
```

where `z(bpm)` is the fine-window BPM z-score over the file's own fine windows, and a file with
no usable fine BPM data at all passes `z(bpm) = 0` (recorded in the profile's `sources` field —
see §7.1). "Usable" is a plausible-tempo band rather than merely "not null" — see §10's
`phaze-aswsz` amendment. The weights live in exactly one place,
`services/set_projection.ENERGY_WEIGHTS`, and
`tests/shared/services/test_set_projection.py` greps the module for literal weight floats
outside that table so a weight can never quietly reappear as an inline constant elsewhere.

| weight | value | sign rationale |
| --- | ---: | --- |
| `danceability` | `0.30` | raises energy |
| `mood_party` | `0.30` | raises energy |
| `mood_aggressive` | `0.15` | raises energy |
| `mood_relaxed` | `-0.20` | lowers energy |
| `mood_sad` | `-0.15` | lowers energy |
| `bpm_z` | `0.10` | a faster-than-the-file's-own-average window raises energy |

**These magnitudes are the implementer's own choice.** They were picked for plausible sign and
rough relative weight only, with no operator input and no measurement against real sets —
nothing above should be read or cited as though the operator has already weighed in on them.

`phaze-x1qr3.12` is where the operator judges the resulting arcs and peaks against 20 real
sets in a blind check, stratified across the duration and style distribution, and records the
outcome in the form `docs/design/0012-verification-fidelity-and-operator-attribution.md`
requires: the question as put, the answer as given, the date, and the durable record. If that
blind check changes a value in the table above, `phaze-x1qr3.12` bumps `projection_version`,
re-runs the backfill, and appends the result as a dated amendment below (§10) rather than
editing the table above in place.

`tests/shared/test_adr_0018_weights.py` reads this table and `ENERGY_WEIGHTS` and fails the
build the moment either the name set or a value drifts from the other — this document and the
code are pinned together, not merely cross-referenced.

## 5. The Camelot table and the flicker filter

`camelot_code` maps essentia's `musical_key` string (`"<Note> <mode>"`, e.g. `"A minor"`) to a
Camelot wheel position via a 24-entry table keyed on the sharp spelling of every key, plus a
10-entry alias table for essentia's flat spellings of the five ambiguous pitch classes
(G♯/A♭, D♯/E♭, A♯/B♭, C♯/D♭, F♯/G♭). An unrecognised or empty string returns `None`, never
raises — the same "not yet projected" gap every other nullable projection column renders.

Two Camelot codes are **wheel-adjacent** — the harmonic-mixing relation `harmonic_discipline`
and the similarity scorer both use — when they share a mode and are a semitone step apart on
the 12-position wheel (`±1` with wraparound), or share a wheel position and differ in mode (the
relative major/minor pair).

`harmonic_discipline` is the share of a file's fine-tier key **transitions** that are
wheel-adjacent, after a **flicker filter**: the fine-tier `camelot` sequence is run-length
encoded, and any run shorter than two consecutive fine windows is dropped as noise rather than
counted as a real transition, before re-collapsing adjacent equal survivors. A single-window
blip (`…, 8A, 3A, 8A, …`) therefore reads as uninterrupted `8A`, not as two transitions through
`3A`. A file with no usable Camelot sequence at all is `None`; a filtered sequence with zero
transitions (the key never actually changes) is `1.0` — the trivial fully-disciplined case,
never `None`.

`camelot_modal` and each glyph cell's Camelot number are read from **fine-tier windows only**
(`camelot` is a fine-tier column, derived from `musical_key`); a coarse cell's colour borrows
the duration-weighted modal Camelot of the fine windows whose time range overlaps it, which is
the cross-tier join the glyph macro (a later bead) needs to pair a colour with a lightness per
cell.

## 6. The fixed mood order, and hues

`services/set_projection.MOOD_ORDER` is one fixed, archive-wide 11-name tuple:

```text
mood_acoustic, mood_electronic, mood_aggressive, mood_relaxed, mood_happy, mood_sad,
mood_party, danceability, gender, tonality, voice_instrumental
```

Every name is the `name` of a `ModelSetConfig` in `services/analysis_models.MODEL_SETS`, and
also the key that model set's predictions occupy in `analysis_window.features`. The names are
kept identical to the stored feature keys so the backfill is a direct read with no translation
table; `test_set_projection.py` fails the build if the name set ever stops matching
`MODEL_SETS`. The **order** is pinned as a literal rather than derived from `MODEL_SETS`,
because it is simultaneously a display contract (the mood river's stacking order, the legend,
the tracklist's mood dots) and baked into stored data (`SetProfile.mean_vector` is positional).
Reordering `MODEL_SETS` is a purely internal analysis concern and must not silently re-colour
every rendered set or invalidate every stored vector; changing `MOOD_ORDER` is therefore a
`projection_version` bump and a re-backfill, never an in-place edit.

Mood hues are **not** derived from `hue_for(label)`, the hash-based hue already used for the
existing key/style ribbons (`sum(ord(c) for c in label) % 360`) — that was tried and rejected.
`phaze-x1qr3.5`'s implementer measured it against the fixed 7-mood set and found it puts
`mood_electronic` and `mood_aggressive` eight degrees apart, indistinguishable where two
river bands touch. Because `phaze-x1qr3.5`'s acceptance requires a validated categorical
palette, the mood river, its legend, and the tracklist's mood dots instead share one
module-level constant, `MOOD_HUES` in `src/phaze/services/analysis_timeline.py` — a
hand-validated mapping from each `MOOD_ORDER` name to a hue, asserted by test as the single
source every one of those three surfaces reads. This document does not reproduce its values:
they are `phaze-x1qr3.5`'s own artifact and had not landed on `main` as of this writing, and
citing them here ahead of that landing would risk a second, driftable copy of the palette
existing in prose. The Camelot glyph is unrelated to `MOOD_HUES`: `phaze-x1qr3.7`'s hue comes
from the Camelot wheel *number* (`hue = (camelot_number − 1) × 30`), not from a mood name, and
its lightness from the cell's `energy`.

## 7. Judgment calls, recorded honestly

Two implementers made calls the acceptance criteria above left open. Recorded here rather than
only in bead comments, per `CLAUDE.md`'s attribution rule (a claim without a question-as-put,
answer-as-given, date and durable record is the implementer's decision, not the operator's).

### 7.1 `phaze-x1qr3.2` (dev/x1qr3-2, 2026-09-06)

1. `build_profile` is a pure aggregator over **already-populated** per-window fields. It does
   not call `camelot_code`, `positive_class_vector` or `energy` itself; the writer
   (`phaze-x1qr3.3`) must call those three per window — `camelot_code` per fine window's
   `musical_key`, `positive_class_vector` per coarse window's `features`, `energy` with a
   file-local BPM z-score — before calling `build_profile` over the now-populated rows.
2. The `sources` field (`{"bpm": "fine" | "none"}`) exists only on the `SetProfileProjection`
   dataclass this module returns; `SetProfile` has no column for it. `phaze-x1qr3.3` decides
   whether to log it, emit it as telemetry, or drop it — this ADR does not decide that for it.
3. `ENERGY_WEIGHTS` (§4) was the implementer's own pick; `phaze-x1qr3.12` is where the
   operator's judgment enters, via the blind check.
4. `positive_class_vector` reuses `services.analysis_derive._positive_class_prediction` for
   by-label positive-class selection rather than re-implementing it, so the alphabetical-
   ordering trap that function exists to fix has exactly one place it could regress.
5. `camelot_modal` and each glyph cell's Camelot number read fine-tier windows only; a coarse
   cell borrows the duration-weighted modal Camelot of the fine windows overlapping it (§5).

### 7.2 Independent review findings (dev/rev-x1qr3-2, 2026-09-06) — all non-blocking, approval stood

1. A type mismatch between `SetProfileProjection.glyph` (`list[dict] | None`) and
   `SetProfile.glyph` (`Mapped[dict | None]`); `phaze-x1qr3.3` resolves it by widening the ORM
   annotation, not by narrowing the projection, and the dataclass docstring's field-for-field
   parity claim should be read with that one exception in mind.
2. `_wheel_adjacent` parses a Camelot code's numeric half with a bare `int()`, unlike
   `_camelot_number`'s guarded parse elsewhere in the module — safe only as long as every
   caller stores `camelot_code`'s own output rather than an arbitrary string.
3. The `sources` field is an *inference* from fine-window BPM presence, not an *observation* of
   how `bpm_z` was actually computed; `phaze-x1qr3.3`'s writer must derive `bpm_z` from the
   file's own fine windows, or the field misreports provenance.

Findings 1 and 3 above (plus one on `SetProfile.glyph`'s type) were forwarded to
`dev/x1qr3-3`, the writer bead, for resolution there rather than here.

## 8. Boundaries this epic does not reopen

None of the following is decided by this ADR; each is a standing decision this projection and
viewer must respect unchanged.

- **No embeddings, no ANN index, no CLAP** — `docs/design/0001-audiomuse-ai-no-go.md` stands.
  Every surface in this epic, including similarity, reads stored classifier outputs; the
  similarity bead is the baseline arm that ADR already asked for, not a reopening of it.
- **Dedup stays sha256 exact-match, permanently** — decision bead `phaze-pw7v.6`. The set
  glyph's visual resemblance across files is a display aid for a human eyeballing "sets that
  look similar," and never writes, proposes, or influences a dedup resolution.
- **Not a player.** Nothing in the projection or the viewer needs playback; pointer inspection
  drives a readout and tooltip over static, precomputed lanes, not a transport.
- **Proposals stay behind Changes Review** — `docs/design/0008-changes-review-approval-boundary.md`.
  No surface this epic adds approves, moves, or tags a file; Changes Review remains the only
  workspace that authorizes a rename, move, or tag write.
- **Honest coverage, never sampled** — `docs/design/0007-windowed-analysis.md` §7. Every
  window-derived picture (the energy arc, the mood river, the glyph) shows a gap exactly where
  windows are missing, the same convention the existing BPM polyline already follows, and never
  bridges a gap by interpolating across it or by striding/sampling the underlying windows.

## 9. Verification

Per `docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 3, projection math
is tested against the real stored feature-dict shape, not a mocked one.

| claim | discharged by |
| --- | --- |
| `positive_class_vector` selects the positive class BY LABEL, never by position | `tests/shared/services/test_set_projection.py`, run against the real stored feature dicts in `tests/agents/routers/test_agent_analysis_real_payload.py`'s payloads |
| the Camelot table covers all 24 keys and every enharmonic spelling; an unknown string returns `None` | the same file, asserted per key and per alias |
| energy is monotone in danceability/party, anti-monotone in relaxed/sad, clamped to `[0, 1]` | the same file |
| `ENERGY_WEIGHTS` is the only place the weight literals live | the same file, via a grep-the-module assertion |
| the arc resample handles 44-window, 3-window, and gapped files without bridging a gap | the same file |
| `harmonic_discipline` drops a one-window flicker and counts a real two-window change | the same file |
| this ADR's weights table and `ENERGY_WEIGHTS` cannot drift apart | `tests/shared/test_adr_0018_weights.py` (this bead) |
| every ADR citation in this document resolves and carries no duplicate leading number | `tests/shared/test_adr_numbering.py`, `tests/shared/test_adr_citation_resolution.py` |
| one out-of-band fine window does not move any coarse energy | `tests/shared/services/test_set_projection_writer.py`, over real `AnalysisWindow` rows carrying real coarse `features` (`phaze-aswsz`) |
| the band gates each coarse window's overlap set as well as the file-wide reference | the same file — gating only the reference is measurably WORSE than not gating at all |
| the real extractor returns an out-of-band BPM for digital silence, and the band contains the extractor's own search range | `tests/analyze/services/pipeline/test_rhythm_silence_bpm.py`, against real essentia |

## 10. Amendments

`phaze-x1qr3.12`'s operator blind check, when it runs, appends its question-as-put,
answer-as-given, date and durable record here rather than editing §4's table in place.

### 10.1 `phaze-aswsz` (dev/aswsz, 2026-09-09) — a plausible-tempo band on the z-score reference, and `projection_version` 1 → 2

**The defect.** `services/analysis.py` stores every fine window's `bpm` unconditionally, and the
extractor's own `confidence` never reaches the database — `FineWindow.as_payload_dict` omits it and
`analysis_window` has no confidence column. `services/set_projection_writer.py` built the z-score's
reference distribution with a null check alone, so a junk value entered it as a tempo. Measured in
this environment (essentia 2.1-beta6-dev, macOS arm64, the deployed
`RhythmExtractor2013(method="multifeature")`, 44.1 kHz digital silence): bpm 738.3, at confidence
0.0 over a 5 s buffer and 4.69 over a 30 s one.

Over a 61-window fixture — sixty ~128 BPM fine windows plus one silent one, eleven coarse windows
carrying real `features` — the single junk window lifts the file's population stdev from 0.7957 to
77.5069. Every coarse window's z-score collapses from a real −0.201..+0.553 spread onto a
near-uniform −0.13, and the one coarse window the silent window overlaps is pushed the other way,
+0.553 to +1.188. The energies move by up to 0.0635, and `arc` and `peak_sec` move with them.

**The gate, and why it is a band.** `MIN_PLAUSIBLE_BPM` / `MAX_PLAUSIBLE_BPM` (40.0 / 220.0) in
`services/set_projection_writer.py`, applied to the file-wide reference distribution **and** to
each coarse window's own overlap set — gating only the reference measured *worse* than not gating
at all (0.546 versus 0.064 maximum energy shift), because the junk value then stays out of the mean
and stdev while still dragging one window's overlap average. Confidence is the sharper gate and is
not available: it would need a column, a migration, a wire field, and a backfill that could not
recover the confidence of any already-analysed window. The bounds contain the deployed extractor's
own declared search range (`minTempo` 40, `maxTempo` 208, read off the constructed algorithm in
this environment), with headroom on the ceiling so the gate fails open on the musical side.

**This is the implementer's decision, not the operator's** — the same footing as §4's weights.
Persisting fine-tier confidence stays the better fix, and is what a junk value landing *inside* the
band would call for.

**`projection_version` 1 → 2.** The corpus backfill has not run, but that is not the same as "no
rows exist": the live path (`routers/agent_analysis.py::_replace_analysis_windows`) has written a
`set_profile` row at version 1 on every analysis completed since `phaze-x1qr3.3` landed, and those
rows carry the distortion. The bump puts exactly them in
`services/set_projection_backfill.py`'s `projection_version <` predicate.

**Dependency on `phaze-x1qr3.12`.** The blind check judges `energy`, `arc` and `peak_sec` — the
scalars this amendment changes. It must be run against a corpus re-derived at version 2 or later;
judging version-1 rows would tune the weights against the distortion rather than against the
projection. If that blind check then changes a weight, its own bump takes `projection_version`
to 3.
