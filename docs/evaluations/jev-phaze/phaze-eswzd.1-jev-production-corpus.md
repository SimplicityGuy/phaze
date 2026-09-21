# phaze-eswzd.1 — production-derived Jev evaluation corpus

## Question

Can reproducible, privacy-conscious pilot and held-out benchmark corpora be frozen from analyses Phaze already computed, without copying audio or production paths?

## Method

On 2026-09-20 (America/Los_Angeles), read-only SSH and `docker exec postgres psql` queries inspected the production Phaze database on `the production host`. The running application image was `ghcr.io/simplicityguy/phaze:2026.9.2`; PostgreSQL reported 18.6. Production contained 5,583 completed `analysis` rows and 93,444 `analysis_window` rows with `tier = 'coarse'`.

The original twelve-track selection query is [phaze-eswzd.1-corpus.sql](phaze-eswzd.1-corpus.sql). It:

1. averages each stored coarse window's seven positive-class mood projections by file;
2. chooses the highest-scoring file for each dominant mood, breaking ties by subject code, to make seven clear exemplars;
3. chooses the five remaining files with the smallest top-two margin, requiring a margin no greater than 0.015; and
4. emits only subject code, pseudonymous display metadata, duration, BPM/key, redacted style summary, window count, and mood evidence.

The redacted machine-readable result is [phaze-eswzd.1-corpus.json](phaze-eswzd.1-corpus.json). The held-out benchmark was selected using [phaze-eswzd.1-benchmark.sql](phaze-eswzd.1-benchmark.sql) and [phaze-eswzd.1-benchmark.json.gz](phaze-eswzd.1-benchmark.json.gz), a standard gzip-compressed JSON object to keep generated evidence below the repository's per-file size gate. The mood keys map the stored projection names as follows:

| Stored key | Study name | Ordinal |
| --- | --- | ---: |
| `mood_acoustic` | acoustic | 0 |
| `mood_electronic` | electronic | 1 |
| `mood_aggressive` | aggressive | 2 |
| `mood_relaxed` | relaxed | 3 |
| `mood_happy` | happy | 4 |
| `mood_sad` | sad | 5 |
| `mood_party` | party | 6 |

These are the first seven entries of Phaze's canonical `MOOD_ORDER`; danceability, gender, tonality, and voice/instrumental are outside both samples.

## Evidence

| Group | Subject code | Artist | Dominant | Top-two margin | Coarse windows |
| --- | --- | --- | --- | ---: | ---: |
| clear | `P01` | Artist 001 | acoustic | 0.069614 | 1 |
| clear | `P02` | Artist 002 | aggressive | 0.363401 | 1 |
| clear | `P03` | Artist 003 | electronic | 0.014940 | 3 |
| clear | `P04` | Artist 004 | happy | 0.116048 | 1 |
| clear | `P05` | Artist 005 | party | 0.128890 | 20 |
| clear | `P06` | Artist 006 | relaxed | 0.154751 | 2 |
| clear | `P07` | Artist 007 | sad | 0.027398 | 2 |
| ambiguous | `P08` | Artist 008 | aggressive | 0.000019 | 2 |
| ambiguous | `P09` | Artist 009 | aggressive | 0.000155 | 23 |
| ambiguous | `P10` | Artist 010 | electronic | 0.000186 | 21 |
| ambiguous | `P11` | Artist 011 | party | 0.000184 | 20 |
| ambiguous | `P12` | Artist 012 | party | 0.000276 | 30 |

The JSON contains all seven probabilities and a redacted style summary for every row. The electronic clear exemplar is the strongest electronic score in the corpus even though its party score is close; “clear” describes deterministic exemplar selection, not a claim that humans will find it unambiguous.

Production was not mutated. No database credential, API credential, original filesystem path, or audio content was read into the artifact. The one missing metadata title uses the basename already stored as a display label, never a path.

### Held-out 500-track benchmark

On 2026-09-21 UTC, the second exact SELECT-only query excluded all twelve pilot subject codes and their twelve normalized artist/release groups. Its leakage-group key is lowercased, whitespace-collapsed artist plus album, using `unknown artist` and `unknown album` placeholders when missing. The query caps each group at two deterministic rows, ordered by `md5(file_id || ':phaze-eswzd-500-v1')`. This conservative fallback means unknown artist/album rows share a group rather than silently becoming leakage-free singletons.

The query assigns a joint stratum from dominant mood, top-two margin (`ambiguous <= 0.05`, `close <= 0.20`, otherwise `clear`), duration (`short < 180s`, `medium 180–360s`, `long > 360s`, or `unknown`), and metadata completeness (artist, title, album, duration, BPM, and key present). It round-robins by within-stratum rank, breaks ties with the fixed hash and subject code, and takes 500. This is a deterministic coverage-oriented sample, not a random population estimate; it is held out from the twelve-track pilot, not from future model training or calibration. The benchmark report must not tune thresholds on these rows.

The frozen manifest records 5,451 eligible rows after pilot subject code/group exclusion, 4,647 after the two-per-group cap, 500 selected tracks from 437 groups across 64 joint strata, 63 two-track groups, and no pilot subject code or leakage-group overlap. Mood counts in canonical order are 27 acoustic, 102 electronic, 130 aggressive, 70 relaxed, 50 happy, 55 sad, and 66 party. Margin bands contain 199 ambiguous, 195 close, and 106 clear; durations are 87 short, 163 medium, 249 long, and one unknown; 400 have complete metadata and 100 are incomplete. Every track carries a pseudonymous subject code and metadata, seven original mood probabilities, window count, stratum, and pseudonymous leakage group. Exact duration, BPM, key, and style summary are removed from the published manifest. Neither query nor manifest contains source paths, audio, or credentials.

## Verdict

Both corpora were selected from stored analysis data; this public copy is de-identified and cannot reconstruct the original API prompts. The pilot spans every mood and five deliberately ambiguous cases; the larger held-out sample broadens coverage while limiting artist/release repetition. These are Essentia-output fidelity datasets, not independent labels of musical truth.

## Recommendation

Use the two frozen manifests separately for the unchanged two-arm, three-repeat Jev contract. Score Jev against the stored Essentia probabilities as the operator-designated operational reference, explicitly separating fidelity to Phaze's present behavior from independent semantic correctness.
