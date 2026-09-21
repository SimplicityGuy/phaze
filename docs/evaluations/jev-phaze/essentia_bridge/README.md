# Essentia classifier evidence with TypeSafe Jev

This proof of concept tests a narrow integration boundary:

1. Essentia and its TensorFlow models analyze audio locally.
2. Code labels and aggregates the per-frame outputs.
3. A compact JSON summary, optional catalog context, and an application goal
   become TypeSafe `state`.
4. Jev answers three independent questions in one request: a `Choice` catalog
   bucket, a `Score` for goal fit, and a `Noul` review flag.

It intentionally does **not** send audio or embedding vectors to Jev. It also
does not place an HTTP request in Essentia's real-time streaming graph.

## Inspect the request offline

From this directory:

```bash
python3 bridge.py sample_input.json --dry-run
```

The sample predictions are illustrative. For a real experiment, replace them
with the labeled frame outputs from an Essentia classification head and include
the exact model name, version, activation, and relevant catalog context.

## Make a live request

Export the key in the shell rather than putting it in a file or command-line
argument:

```bash
export TYPESAFE_API_KEY='...'
python3 bridge.py sample_input.json --output response.json
```

The bridge uses the documented `POST /v1/systemone` API directly so the proof
of concept does not add a mandatory dependency to the Essentia package. The
official TypeSafe SDK would be preferable in a production service because it
provides maintained request types and retry behavior.

## Run the focused tests

```bash
python3 -m unittest -v test_bridge.py
```

The tests are offline and use an injected HTTP opener. No API key is required.

## What to measure

Use a labeled set of tracks and compare the application outcome, not just Jev's
answer in isolation:

- Baseline: deterministic policy over the Essentia scores.
- Candidate: the same evidence plus Jev's semantic judgment.
- Record accuracy or ranking quality, abstention/review rate, request tokens,
  latency, and failures.
- Keep Essentia scores and Jev probabilities as distinct signals. Tune any
  thresholds from observed data rather than treating example values as policy.
- Cache by audio identity, Essentia model/version, aggregation settings, goal,
  taxonomy, and question version.

The first useful experiment is goal-conditioned playlist placement or mapping
the Discogs400 output into a smaller product taxonomy. Replacing an audio model
head with Jev is outside this proof of concept and is not supported by the
documented TypeSafe input modality.

## Initial live observations

Three synthetic smoke tests returned from `jev-1.13.0`:

| Probe | Selected bucket (confidence) | Goal fit / 4 (confidence) | Review probability |
| --- | --- | --- | --- |
| Dinner goal and separated evidence | `dinner_background` (0.95) | 2.64 (0.45) | 0.41 |
| Dance-floor goal, same evidence | `late_night_dance` (0.84) | 2.33 (0.34) | 0.51 |
| Flat 0.5 evidence | `manual_review` (0.56) | 2.07 (0.76) | 0.81 |

The requests used 1,053–1,122 input tokens and 89–92 output tokens each. These
are smoke tests, not accuracy results. They show that the decision responds to
the application goal, the review probability rises when evidence loses
separation, and a strong preference among available buckets does not imply a
confident overall fit. Production policy should not substitute Choice
confidence for fit confidence or for the review probability.
