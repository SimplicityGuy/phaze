#!/usr/bin/env bash
# Drive bench_seek.py over the phaze-han03 measurement matrix, ONE FRESH PROCESS PER DATA POINT
# (phaze-b2qs9 section 1). Emits newline-delimited JSON on stdout.
#
# The two arms of a pair are run ADJACENTLY IN TIME and the whole matrix is repeated, because the
# measurement host is a workstation with other load on it: absolute wall clock is not defensible,
# but a baseline/seek ratio measured seconds apart is.
#
# Usage: run_bench.sh <python> <audio-dir> <bench_seek.py> [repeats]
set -euo pipefail

PY="${1:?usage: run_bench.sh <python> <audio-dir> <bench_seek.py> [repeats]}"
AUDIO="${2:?}"
BENCH="${3:?}"
REPEATS="${4:-2}"

run() { # run <file> <rate> <window_sec> <chunk_windows> <chunk_index>...
  local file="$1" rate="$2" win="$3" cw="$4"
  shift 4
  for k in "$@"; do
    for arm in baseline seek; do
      "$PY" "$BENCH" "$AUDIO/$file" "$arm" "$rate" "$win" "$k" "$cw"
    done
  done
}

for _rep in $(seq 1 "$REPEATS"); do
  # Fine tier: 30 s windows, 60 per chunk, at the tier rate 44100 -- phaze's own geometry.
  # A 44.1 kHz source hits Resample's ratio-1.0 fastcopy path, so decode is cheap here and the
  # per-window fan-out dominates. This is the LEAST favourable case for the patch, on purpose.
  run synth-3600s.mp3 44100 30 60 0 1
  # A 48 kHz source resamples for real (ratio 0.91875). phaze-b2qs9 section 4d found source rate
  # to be a first-order term in decode cost, so this is the case that matters most.
  run synth-3600s-48k.mp3 44100 30 60 0 1
  # Coarse tier: 180 s windows, 30 per chunk, at 16000 -- always resamples.
  run synth-7200s.mp3 16000 180 30 0 1
done
