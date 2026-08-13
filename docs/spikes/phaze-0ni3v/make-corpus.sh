#!/usr/bin/env bash
# Synthetic audio for the EasyLoader seek verification (phaze-0ni3v).
# Same signal definition as the phaze-han03 spike corpus, so results are comparable.
set -euo pipefail

OUT="${1:?usage: make-corpus.sh <output-dir>}"
mkdir -p "$OUT"

DECAY='exp(-6*mod(t\,0.5))'
EXPR="0.34*sin(2*PI*220*t)*${DECAY}+0.28*sin(2*PI*261.626*t)*${DECAY}+0.22*sin(2*PI*329.628*t)*${DECAY}+0.30*exp(-40*mod(t\,0.5))+0.15*sin(2*PI*(300+0.05*t)*t)"

gen_wav() { # gen_wav <seconds> <rate> <path>
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "aevalsrc=exprs=${EXPR}:s=$2:d=$1" \
    -c:a pcm_s16le "$3"
}

# --- accuracy corpus -------------------------------------------------------------------
gen_wav 600 44100 "$OUT/synth-600s.wav"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-600s.wav" -c:a libmp3lame -b:a 192k "$OUT/synth-600s.mp3"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-600s.wav" -c:a aac -b:a 192k "$OUT/synth-600s.m4a"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-600s.wav" -c:a aac -aac_pns 0 -b:a 192k "$OUT/synth-600s-nopns.m4a"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-600s.wav" -c:a libopus -b:a 192k "$OUT/synth-600s.opus"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-600s.wav" -c:a flac "$OUT/synth-600s.flac"

# --- short file, for boundary cases ----------------------------------------------------
gen_wav 3 44100 "$OUT/synth-3s.wav"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-3s.wav" -c:a libmp3lame -b:a 192k "$OUT/synth-3s.mp3"

# --- timing corpus ---------------------------------------------------------------------
gen_wav 3600 44100 "$OUT/synth-3600s.wav"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-3600s.wav" -c:a libmp3lame -b:a 192k "$OUT/synth-3600s.mp3"
rm -f "$OUT/synth-3600s.wav"
gen_wav 3600 48000 "$OUT/synth-3600s-48k.wav"
ffmpeg -hide_banner -loglevel error -y -i "$OUT/synth-3600s-48k.wav" -c:a libmp3lame -b:a 192k "$OUT/synth-3600s-48k.mp3"
rm -f "$OUT/synth-3600s-48k.wav"

ls -l "$OUT"
