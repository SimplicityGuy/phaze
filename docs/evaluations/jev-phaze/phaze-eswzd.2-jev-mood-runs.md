# phaze-eswzd.2 — blinded Jev mood runs

## Question

How do seven independent Jev mood probabilities behave over the same frozen pilot and held-out benchmark tracks when Jev receives (a) non-classifier evidence only and (b) that evidence plus the stored Essentia mood vector?

## Method

The pilot runner is [run_jev_pilot.py](run_jev_pilot.py), with offline contract tests in [test_run_jev_pilot.py](test_run_jev_pilot.py). The held-out 500-track runner is [run_jev_benchmark.py](run_jev_benchmark.py), with [checkpoint/resume tests](test_run_jev_benchmark.py); it imports the pilot's exact question, model, state, response parser, and call contract rather than redefining them. The contract freezes:

- schema `phaze-jev-mood-pilot-v1`;
- requested model alias `jev-latest`;
- one HTTP `POST /v1/systemone` per subject, arm, and repeat;
- seven independent Noul questions—acoustic, electronic, aggressive, relaxed, happy, sad, and party—in that single request;
- three repeats per subject per arm: 72 original pilot calls plus 3,000 held-out benchmark calls; and
- latency, attempt count, retry/error labels, resolved model, seven response values, input tokens, output tokens, and public-rate cost.

The **Jev-only** state contains artist, title, album, duration, BPM, and musical key. BPM/key are deterministic audio measurements rather than classifier outputs. It excludes mood probabilities, stored style, danceability, gender, tonality, and voice/instrumental evidence. The **hybrid** state adds only the seven frozen Essentia mood probabilities and labels them as fallible classifier evidence rather than truth. Every state explicitly says that Jev has not heard the audio.

The question text is defined once in `QUESTION_DEFINITIONS` and included verbatim in the result artifact. For each mood it asks:

> Based only on the supplied state, is this track likely to sound {mood}? Treat metadata and measurements as fallible evidence. Do not assume that a missing signal means no.

The runners read `TYPESAFE_API_KEY` from process environment. They never accept the key as a CLI argument, serialize it, or log it. Execution uses a no-echo interactive read followed by a process-local export. Credential scans cover source, fixtures, reports, and generated evidence before commit. The benchmark captures lossless gzip JSON through a same-directory temporary file, flush/fsync, and atomic replace after every response; resume validates the frozen manifest digest, request contract, summary, and unique subject/arm/repeat keys. It sends requests sequentially with a 45-second timeout and at most three internal attempts per call. A deliberate `--retry-terminal-dns` option reissues only previously failed DNS keys, preserving superseded failures in `attempt_history`; three consecutive new terminal DNS failures fail fast.

This reuses the boundary established by Essentia bead `ess-g610 — Prototype TypeSafe Jev post-classification integration` at local-only commit `0f058e83`: direct stdlib HTTP, environment-only credentials, bounded retries, explicit model alias, structured state, batched independent questions, usage capture, and keeping Essentia evidence distinct from Jev probabilities. It rewrites the task-specific state and questions because this experiment compares seven multi-label Noul outputs rather than the earlier goal-conditioned Choice/Score/review smoke test.

## Evidence

Offline verification on 2026-09-20:

- five focused tests pass;
- the test suite proves that the Jev-only serialization contains no classifier field or mood score;
- the hybrid serialization adds the exact frozen mood vector;
- all seven questions are Nouls in one request;
- malformed corpus shape/order is rejected; and
- synthetic responses prove latency, tokens, and cost are retained.

The first attempted execution stopped locally before its first request because the JSON formatter alphabetized mood object keys and an over-strict guard treated object-key order as semantic. The guard was corrected to validate the explicit top-level `mood_order` and the mood key set; all focused tests passed again. This event made no API request and incurred no charge.

After the operator explicitly approved sending the fixed sample's artist, title, album, BPM, key, duration, and classifier scores to TypeSafe, the local run completed. The result is [jev-runs.json](jev-runs.json):

| Arm | Requests | Input tokens | Output tokens | Public-rate cost | p50 latency | p95 latency | Failures / retries |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Jev-only | 36 | 35,892 | 4,428 | $0.001507464 | 171.963 ms | 230.138 ms | 0 / 0 |
| Hybrid | 36 | 44,019 | 4,428 | $0.001848798 | 195.600 ms | 280.872 ms | 0 / 0 |
| Total | 72 | 79,911 | 8,856 | $0.003356262 | 179.686 ms | 256.404 ms | 0 / 0 |

Percentiles use linear interpolation between adjacent sorted observations; the earlier quick readout used a different rank convention. The hybrid maximum was 1,539.898 ms.

Every response resolved `jev-latest` to `jev-1.13.0`. Input usage ranged from 978 to 1,249 tokens per request. The output contains all three repeats and seven Noul values for every subject and arm. It contains no authorization header, API key, database credential, filesystem path, or audio content. Nothing was written to production.

The held-out [500-track capture](jev-benchmark-runs.json.gz) completed with all 3,000 logical requests successful (500 tracks × two arms × three repeats). All resolved to `jev-1.13.0`. The response/checkpoint file is gzip JSON; the [frozen manifest](phaze-eswzd.1-benchmark.json.gz) and the runner's digest/contract checks establish its input provenance. The first sandboxed attempt recorded three terminal `network gaierror` events for B001 Jev-only (nine transport attempts including six internal retries), without an API response or charge. An explicit DNS-only resume retained those records in `attempt_history`, then completed all three keys and the rest of the run under network permission. Thus there are 3,003 logical request events and 3,009 transport attempts in the entire chronology, but zero active failures or internal retries in the final 3,000 successful records. This distinction is retained in the evaluator's operational summary.

| Arm | Successful requests | Input tokens | Output tokens | Public-rate cost | p50 latency | p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Jev-only | 1,500 | 1,493,634 | 184,500 | $0.062732628 | 167.260 ms | 232.206 ms |
| Hybrid | 1,500 | 1,832,010 | 184,500 | $0.076944420 | 167.730 ms | 237.771 ms |
| Total | 3,000 | 3,325,644 | 369,000 | $0.139677048 | 167.355 ms | 235.570 ms |

The latency percentiles describe successful requests under this local sequential run, not production concurrency. The public-rate charge is calculated from captured input usage, not an account invoice; historical DNS attempts had no response usage.

## Verdict

The two arms remain separable without classifier leakage, and the live benchmark completed at the expected ~$0.14 public-rate cost. Jev has not heard audio; Essentia agreement is operational fidelity to current Phaze behavior, not independent human or semantic truth. The [evaluation](phaze-eswzd.3-jev-evaluation.md) and [decision](phaze-eswzd.4-jev-verdict.md) use the completed capture.
