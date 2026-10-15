# Degenerate audio investigation

Measured 2026-10-03. Unusable measurements are stored as NULL, following the historical record below (2026-10-03).

## Historical operator record (2026-10-03)

Question as put:

> For phaze-nkucu — “Handle degenerate audio values,” what should analysis return when a window has no analyzable signal? The bead explicitly requires your product decision before implementation. Measurements found 55 implausible BPM values among 643,789 windows, and silence can have higher rhythm confidence than valid audio.

Answer as given (selected option label, verbatim):

> Return NULL values for unusable measurements (Recommended)

The other offered options were "Keep values and add an explicit quality field" and "Leave current
behavior and close as wontfix". Asked 2026-10-03T19:36:27Z and answered 2026-10-03T19:39:27Z. Durable
record: this section, recovered on 2026-10-14  from the local Codex session transcript's
`request_user_input_async` call and its question reply, in which the question text above and the label
above appear verbatim. `scripts/recover_operator_decisions.py` does not read Codex transcripts, so the
record was found by direct search.

Completed windows remain present and count as analyzed even when their measurements are
NULL. A missing value therefore means this completed window yielded no usable measurement;
job lifecycle and window counts continue to distinguish completion from pending analysis.
Existing stored rows are not rewritten by this change.

Implementation uses a conservative peak-amplitude cutoff of 1e-5 (-100 dBFS), below one
16-bit PCM quantization step. This catches silence and negligible near-silence without
claiming to classify all non-musical audio. Zero rhythm confidence also makes that window's
BPM absent; zero key strength makes its key absent. Empty classifier evidence yields NULL
mood/style rather than value-column sentinels. Wire bounds stay unchanged.

## Stored population

A read-only transaction against the production database returned:

| Population | Count |
| --- | ---: |
| All `analysis_window` rows | 643789 |
| Windows with `bpm < 40 OR bpm > 300` | 55 |
| Windows with `style = 'unknown'` | 112 |
| Windows with `mood = ''` | 76 |

```sql
BEGIN READ ONLY;
SELECT count(*) AS total_windows,
       count(*) FILTER (WHERE bpm < 40 OR bpm > 300) AS implausible_bpm,
       count(*) FILTER (WHERE style = 'unknown') AS unknown_style,
       count(*) FILTER (WHERE mood = '') AS empty_mood
FROM analysis_window;
COMMIT;
```

These predicates measure candidate data-quality problems, not proven silence. The counts
can overlap. The current table name is singular `analysis_window`.

## Real rhythm-extractor measurements

The deployed analysis-worker Essentia runtime ran `RhythmExtractor2013(method="multifeature")`
over synthetic mono float32 arrays at 44100 Hz. Silence used zero arrays; the tone was a
0.5-amplitude 1000 Hz sine; the control used decaying 220 Hz pulses every 0.5 seconds,
each lasting 0.05 seconds with initial amplitude 0.8 and exponential decay constant 0.005 seconds.
No archive files or trained classifier models were needed for this rhythm measurement.

| Synthetic input | BPM | Confidence | Beat count |
| --- | ---: | ---: | ---: |
| 200 s digital silence | 738.2672119140625 | 4.409790515899658 | 2460 |
| 30 s digital silence (natural fine-window length) | 738.2831420898438 | 4.692869663238525 | 368 |
| 20 s digital silence | 738.28271484375 | 4.592811107635498 | 245 |
| 10 s digital silence | 738.2814331054688 | 4.405712127685547 | 122 |
| 2 s digital silence | 738.2813720703125 | 0.0 | 24 |
| 3 s 1000 Hz tone | 80.33541107177734 | 0.0 | 4 |
| 30 s pulse control, 120 BPM | 119.99017333984375 | 3.813378095626831 | 59 |

Confidence does **not** reliably separate degenerate signal: long silence scored higher
than the rhythmic control. Zero confidence does separate these two short synthetic cases,
but using it alone leaves the long-silence defect. This direct extractor experiment is
not a full `analyze_file` reproduction or a representative measurement of musical recordings.

## Current sentinel paths

`derive_style` and `derive_mood` return `None` for empty predictions. The stored unknown/empty values counted in the dated observation above remain historical evidence, not the current empty-result contract.
`aggregate_bpm` excludes confidence-zero windows, while window payloads omit confidence.

Chunked analysis, progress-based stall liveness, streaming teardown, and payload bounds remain
untouched. No wall-clock bound was added.

## Implementation verification

A real 200 s mono silence WAV at 44100 Hz completed `analyze_file` with 7/7 fine and
2/2 coarse natural windows, all 9 window records retained, and NULL aggregate BPM,
musical key, mood, style and danceability. No classifier weights were needed: absence of
signal is identified from decoded PCM rather than guessed from model output. The real
decode, window geometry, chunk loops and Essentia fine-extractor construction executed;
classifier inference is intentionally avoided for unusable PCM.

The focused degeneracy suite passed 10 tests, including this real reproduction, zero PCM,
near-silence, signal just above the floor, and individually unusable extractor results.
The broader pipeline and model-major regression population passed 562 tests before the
real reproduction was added. Ordinary model predictions and model-major graph release
semantics remain covered. Mock buffers representing valid test windows now carry signal;
their first-sample identity markers remain unchanged.
