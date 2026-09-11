#!/usr/bin/env bash
# Provision the essentia ML models for audio analysis into a directory (explicit, never at runtime).
# Usage: bash scripts/download-models.sh <output_dir>
# The Python module owns the model manifest; this wrapper only forwards CLI arguments.
# phaze-ynv6w: the output dir is REQUIRED -- phaze never downloads models on its own, so
# nothing here may quietly pull ~3.1 GB into ./models by default.
set -euo pipefail
if [ $# -ne 1 ]; then
  echo "usage: $0 <output_dir>" >&2
  exit 2
fi
exec uv run python -m phaze.scripts.download_models "$1"
