# Phaze analysis evaluation run

Status: **PENDING — do not mark pass until the isolated run and independent-label review finish.**

## Provenance

| Item | Recorded value |
| --- | --- |
| UTC run window | pending |
| Contract version | 1 |
| Corpus manifest SHA256 | pending |
| Phaze branch SHA | pending |
| Production job image digest | pending |
| Model-tree SHA256 and file count | pending |
| vox CPU / memory request and limit | pending |
| Analyzer thread settings | pending |
| Annotation source, split, and annotator counts | pending |

## Operational isolation and restoration

| Check | UTC time and evidence |
| --- | --- |
| Backend registry capture and checksum | pending |
| Kueue `Hold` set; no `HoldAndDrain` | pending |
| In-flight jobs completed; node quiescent | pending |
| `host-compute` absent from Phaze registry and lane snapshot | pending |
| Benchmark pod has no live queue label or callback credentials | pending |
| Benchmark pod and scratch removed | pending |
| Registry restored byte-identically; checksum matches | pending |
| Queue released to `None`; lane available | pending |
| New real work admitted after restoration | pending |

## Baseline measurements

Record the path to each *scratch* `summary.json` in the operator's private run record. Copy only aggregate numbers and SHA/ID references into a tracked report. Include separate first and repeat passes at concurrency 1 and 4, per-file ratios to audio duration, p50/p95, CPU-hours/audio-hour, peak RSS against the production memory limit, full fine/coarse counts, missing attributes, failure count, and stage times. A run with incomplete coverage is a finding, not a valid complete baseline.

| Phase / concurrency | Batch wall | Audio-hours/hour | p50 / p95 file wall | Peak RSS | Contract failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| First / 1 | pending | pending | pending | pending | pending |
| Repeat / 1 | pending | pending | pending | pending | pending |
| First / 4 | pending | pending | pending | pending | pending |
| Repeat / 4 | pending | pending | pending | pending | pending |

## Independent quality evidence

State the number of independently labeled files/windows per result family. Fill each score from `analysis_eval_score.py` or a separately documented transition evaluator. Leave cells **unscored** if labels are unavailable; never substitute agreement with current Phaze output. State class semantics for gender, tonality, and voice/instrumental before scoring them.

| Family | Labeled count | Current baseline score | Candidate score | Paired difference and interval | Pass? |
| --- | ---: | ---: | ---: | ---: | --- |
| BPM strict / octave | pending | unscored | unscored | pending | pending |
| Key exact / MIREX | pending | unscored | unscored | pending | pending |
| Seven moods | pending | unscored | unscored | pending | pending |
| Danceability | pending | unscored | unscored | pending | pending |
| Gender | pending | unscored | unscored | pending | pending |
| Tonality | pending | unscored | unscored | pending | pending |
| Voice/instrumental | pending | unscored | unscored | pending | pending |
| Discogs400 genre/style | pending | unscored | unscored | pending | pending |
| Timeline transitions | pending | unscored | unscored | pending | pending |

## Verdict

**Pending.** A candidate passes only when semantic output, exhaustive granularity, independent quality, and paired performance all satisfy `docs/analysis-evaluation.md`. List every exception and its evidence here. Do not infer a quality pass from a fast, complete run.
