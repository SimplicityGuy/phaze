# Phaze analysis evaluation run

Status: **PENDING — do not mark pass until the paired UpCloud comparison and reference review finish.**

## Provenance

| Item | Recorded value |
| --- | --- |
| UTC run window | pending |
| Contract version | 2 |
| Corpus manifest SHA256 | pending |
| Phaze branch SHA | pending |
| Production job image digest | pending |
| Model-tree SHA256 and file count | pending |
| UpCloud host UUID, plan, CPU / memory limit | pending |
| Analyzer thread settings | pending |
| Homelab DB reference export UTC time and SHA256 | pending |
| Source-label provenance, taxonomy mapping, split, and coverage | pending |

## Paired-host conditions

| Check | UTC time and evidence |
| --- | --- |
| Staged input SHA256 verified; same corpus for both runs | pending |
| Same host, worker count, CPU/thread policy, and limits | pending |
| No overlapping benchmark process; host load recorded | pending |
| First/repeat run order and cache-state limitations recorded | pending |
| Distinct output directories; baseline artifacts preserved | pending |
| No production callback or database write | pending |

## Baseline measurements

Record the path to each *scratch* `summary.json` in the operator's private run record. Copy only aggregate numbers and SHA/ID references into a tracked report. Include the completed first pass and repeat pass at concurrency 1, paired candidate passes on the same host, per-file ratios to audio duration, p50/p95, CPU-hours/audio-hour, peak RSS against the host limit, full fine/coarse counts, missing attributes, failure count, and stage times. A run with incomplete coverage is a finding, not a valid complete baseline. Report a 4-worker comparison only if separately performed under matched resources.

| Phase / concurrency | Batch wall | Audio-hours/hour | p50 / p95 file wall | Peak RSS | Contract failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| First / 1 | pending | pending | pending | pending | pending |
| Repeat / 1 | pending | pending | pending | pending | pending |
| Candidate first / 1 | pending | pending | pending | pending | pending |
| Candidate repeat / 1 | pending | pending | pending | pending | pending |

## Homelab result reference and source-label evidence

The DB's `analysis` and `analysis_window` rows are the operational reference for current Phaze behavior. Report field/window coverage and numerical/transition disagreement against the frozen export, with predeclared tolerances. These are agreement measures. For the eight SHA-selected files, the initial DB check found source genre tags on five, but no source BPM, key, mood, characteristic, or transition labels. Review genre provenance and map taxonomy before scoring. Use `unscored` where no suitable source label exists.

State the number of suitably source-labeled files/windows per result family. Fill scores from `analysis_eval_score.py` or a separately documented transition evaluator. Leave cells **unscored** if labels are unavailable; never substitute agreement with current Phaze output as an accuracy score. State class semantics for gender, tonality, and voice/instrumental before scoring them.

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

**Pending.** Report separate verdicts for semantic contract, timeline coverage, agreement with the current DB reference, source-supported accuracy, and paired performance under `docs/analysis-evaluation.md`. List every exception and its evidence. A fast, complete, DB-compatible result does not establish accuracy noninferiority for fields without source labels.
