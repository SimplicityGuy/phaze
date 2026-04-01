#!/usr/bin/env bash
# Download essentia ML models for audio analysis.
# Usage: bash scripts/download-models.sh [output_dir]
#   output_dir defaults to ./models

set -euo pipefail

OUTPUT_DIR="${1:-./models}"
mkdir -p "$OUTPUT_DIR"

CLASSIFIER_BASE="https://essentia.upf.edu/models/classifiers"
GENRE_BASE="https://essentia.upf.edu/models/music-style-classification/discogs-effnet"

# 11 classifier model sets, each with 3 variants (34 models = 34 .pb + 34 .json = 68 files)
# voice_instrumental uses musicnn-msd-1 instead of -2
CLASSIFIER_MODELS=(
    "mood_acoustic/mood_acoustic-musicnn-msd-2"
    "mood_acoustic/mood_acoustic-musicnn-mtt-2"
    "mood_acoustic/mood_acoustic-vggish-audioset-1"
    "mood_electronic/mood_electronic-musicnn-msd-2"
    "mood_electronic/mood_electronic-musicnn-mtt-2"
    "mood_electronic/mood_electronic-vggish-audioset-1"
    "mood_aggressive/mood_aggressive-musicnn-msd-2"
    "mood_aggressive/mood_aggressive-musicnn-mtt-2"
    "mood_aggressive/mood_aggressive-vggish-audioset-1"
    "mood_relaxed/mood_relaxed-musicnn-msd-2"
    "mood_relaxed/mood_relaxed-musicnn-mtt-2"
    "mood_relaxed/mood_relaxed-vggish-audioset-1"
    "mood_happy/mood_happy-musicnn-msd-2"
    "mood_happy/mood_happy-musicnn-mtt-2"
    "mood_happy/mood_happy-vggish-audioset-1"
    "mood_sad/mood_sad-musicnn-msd-2"
    "mood_sad/mood_sad-musicnn-mtt-2"
    "mood_sad/mood_sad-vggish-audioset-1"
    "mood_party/mood_party-musicnn-msd-2"
    "mood_party/mood_party-musicnn-mtt-2"
    "mood_party/mood_party-vggish-audioset-1"
    "danceability/danceability-musicnn-msd-2"
    "danceability/danceability-musicnn-mtt-2"
    "danceability/danceability-vggish-audioset-1"
    "gender/gender-musicnn-msd-2"
    "gender/gender-musicnn-mtt-2"
    "gender/gender-vggish-audioset-1"
    "tonal_atonal/tonal_atonal-musicnn-msd-2"
    "tonal_atonal/tonal_atonal-musicnn-mtt-2"
    "tonal_atonal/tonal_atonal-vggish-audioset-1"
    "voice_instrumental/voice_instrumental-musicnn-msd-1"
    "voice_instrumental/voice_instrumental-musicnn-mtt-2"
    "voice_instrumental/voice_instrumental-vggish-audioset-1"
)

# Genre model (1 set = 2 files)
GENRE_MODELS=(
    "discogs-effnet-bs64-1"
)

TOTAL_FILES=$(( (${#CLASSIFIER_MODELS[@]} * 2) + (${#GENRE_MODELS[@]} * 2) ))
CURRENT=0
FAILED=0

download_file() {
    local url="$1"
    local dest="$2"
    CURRENT=$((CURRENT + 1))

    if [ -f "$dest" ]; then
        echo "Exists: $(basename "$dest") [$CURRENT/$TOTAL_FILES]"
        return 0
    fi

    echo "Downloading $(basename "$dest")... [$CURRENT/$TOTAL_FILES]"
    if ! curl -fSL --retry 3 --create-dirs -o "$dest" "$url"; then
        echo "ERROR: Failed to download $(basename "$dest")" >&2
        FAILED=$((FAILED + 1))
        return 1
    fi
}

echo "Downloading $TOTAL_FILES model files to $OUTPUT_DIR..."
echo ""

# Download classifier models (flat directory -- filename only, no subdirs)
for model_path in "${CLASSIFIER_MODELS[@]}"; do
    filename=$(basename "$model_path")
    download_file "${CLASSIFIER_BASE}/${model_path}.pb" "${OUTPUT_DIR}/${filename}.pb" || true
    download_file "${CLASSIFIER_BASE}/${model_path}.json" "${OUTPUT_DIR}/${filename}.json" || true
done

# Download genre models
for model in "${GENRE_MODELS[@]}"; do
    download_file "${GENRE_BASE}/${model}.pb" "${OUTPUT_DIR}/${model}.pb" || true
    download_file "${GENRE_BASE}/${model}.json" "${OUTPUT_DIR}/${model}.json" || true
done

echo ""
if [ "$FAILED" -gt 0 ]; then
    echo "ERROR: $FAILED file(s) failed to download." >&2
    exit 1
fi

echo "All $TOTAL_FILES model files downloaded to $OUTPUT_DIR"
