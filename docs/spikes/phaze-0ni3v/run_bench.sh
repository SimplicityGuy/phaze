#!/usr/bin/env bash
# Alternate pre-patch / post-patch EasyLoader reads, adjacently in time, in a fresh process
# each, following the phaze-b2qs9 / phaze-han03 process model.
#
# usage: run_bench.sh <audio-dir> <pre-patch-venv> <post-patch-venv>
#
# The two venvs must each have an essentia built from the SAME source tree except for the
# patch under test -- see docs/spikes/phaze-0ni3v-easyloader-seek.md section 2.
set -u

AUDIO="${1:?usage: run_bench.sh <audio-dir> <pre-patch-venv> <post-patch-venv>}"
BASE="${2:?}"
SEEK="${3:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"

row() { # row <label> <file> <sampleRate> <startTime> <endTime>
  label="$1"
  f="$AUDIO/$2"
  sr="$3"
  st="$4"
  en="$5"
  b=$("$BASE/bin/python" "$HERE/bench_easyloader.py" "$f" "$sr" "$st" "$en" 2>/dev/null)
  s=$("$SEEK/bin/python" "$HERE/bench_easyloader.py" "$f" "$sr" "$st" "$en" 2>/dev/null)
  printf "%-46s base=%9ss (%s samples)  seek=%9ss (%s samples)  speedup=%s\n" \
    "$label" "${b%% *}" "${b##* }" "${s%% *}" "${s##* }" \
    "$(python3 -c "print(f'{${b%% *}/${s%% *}:.1f}x')")"
}

echo "== ratio 1.0 (44.1 kHz source read at 44100) -- the seeking path"
row "3600 s file, full [0, 3600)" synth-3600s.mp3 44100 0 1000000
row "3600 s file, second half [1800, 3600)" synth-3600s.mp3 44100 1800 3600
row "3600 s file, one 30 s window at 1800 s" synth-3600s.mp3 44100 1800 1830
row "3600 s file, one 30 s window at 3540 s" synth-3600s.mp3 44100 3540 3570
row "600 s file, one 5 s window at 300 s" synth-600s.mp3 44100 300 305

echo
echo "== 48 kHz source read at 48000 (still ratio 1.0)"
row "3600 s file, one 30 s window at 1800 s" synth-3600s-48k.mp3 48000 1800 1830

echo
echo "== ratio != 1.0 -- deliberately NOT seeking, so no change is expected"
row "3600 s file at 16000, one 30 s window" synth-3600s.mp3 16000 1800 1830
row "48 kHz source at 44100, one 30 s window" synth-3600s-48k.mp3 44100 1800 1830
