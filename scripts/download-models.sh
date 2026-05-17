#!/usr/bin/env bash
# Download essentia ML models for audio analysis.
# Usage: bash scripts/download-models.sh [output_dir]
#   output_dir defaults to ./models
# Phase 29: delegates to phaze.scripts.download_models for single-source-of-truth URL list.
set -euo pipefail
exec uv run python -m phaze.scripts.download_models "${1:-./models}"
