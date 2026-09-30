#!/usr/bin/env bash
# On the UpCloud host: run a staged neutral-protocol candidate and finish reports.
set -euo pipefail

run_id=${1:?usage: run-candidate-v2.sh RUN_ID COMMAND_JSON IMAGE_DIGEST MODEL_DIR CONTEXT_JSON [smoke]}
command_name=${2:?missing command JSON basename}
image=${3:?missing digest-pinned candidate image}
model_dir=${4:?missing absolute host model directory}
context_name=${5:?missing run-context JSON basename}
mode=${6:-full}
root=/home/datum/phaze-iq64v
baseline_image=ghcr.io/simplicityguy/phaze@sha256:7a98190739c83bfc224d6bc4b8688d34907eae1db6dea8052c752093d10be8fe

[[ $run_id =~ ^[A-Za-z0-9_-]+$ ]] || exit 2
[[ $mode == full || $mode == smoke ]] || {
  echo 'mode must be full or smoke' >&2
  exit 2
}
[[ $command_name =~ ^[A-Za-z0-9_.-]+\.json$ && $context_name =~ ^[A-Za-z0-9_.-]+\.json$ ]] || exit 2
[[ $image =~ ^[^[:space:]]+@sha256:[0-9a-f]{64}$ ]] || {
  echo 'image must be pinned by sha256 digest' >&2
  exit 2
}
[[ $model_dir == "$root/"* && -d $model_dir && ! -L $model_dir ]] || {
  echo 'model directory must exist under the evaluation root' >&2
  exit 2
}
[[ -s $root/code/docs/$command_name && -s $root/code/docs/$context_name && -d $root/candidate ]] || {
  echo 'candidate inputs are not staged' >&2
  exit 2
}
[[ ! -e $root/results/$run_id && ! -e $root/results/$run_id-reference-comparison.json && ! -e $root/results/$run_id-tag-score.json && ! -e $root/results/$run_id-report.json ]] || {
  echo 'run ID already used' >&2
  exit 2
}
/usr/bin/python3 - "$root/code/docs/$context_name" "$image" <<'PY'
import json, sys
context = json.load(open(sys.argv[1]))
assert context['image_digest'] == sys.argv[2], 'candidate image/context digest mismatch'
assert context['node'] == 'upcloud-us-sjo1-0068802d-f350-4a44-9ca9-b79c960b749d'
assert context['cpu_limit'] == '4 vCPU Docker --cpus=4'
assert context['memory_limit'] == '7 GiB Docker --memory=7g'
assert context.get('source_commit') and context['source_commit'] != 'FILL_AT_RUN_TIME'
PY

exec 9>"$root/results/finish-evaluation.lock"
flock -n 9 || {
  echo 'another evaluation is running' >&2
  exit 1
}

manifest=/app/docs/evaluation-phaze-iq64v-corpus.json
if [[ $mode == smoke ]]; then
  manifest=/app/docs/evaluation-phaze-iq64v-smoke.json
fi

sudo -n docker run --rm --network none --cpus=4 --memory=7g --pids-limit=512 \
  --security-opt=no-new-privileges --cap-drop=ALL \
  -e PYTHONPATH=/app/src:/app:/candidate -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  -v "$root/code/src:/app/src:ro" -v "$root/code/scripts:/app/scripts:ro" \
  -v "$root/code/docs:/app/docs:ro" -v "$root/candidate:/candidate:ro" \
  -v "$root/samples:/samples:ro" -v "$model_dir:/models:ro" -v "$root/results:/results" \
  -w /app "$image" /app/.venv/bin/python /app/scripts/analysis_eval.py \
  --manifest "$manifest" \
  --audio-dir /samples --models-dir /models --output-dir "/results/$run_id" \
  --workers 1 --phase cold --protocol neutral \
  --command-json "/app/docs/$command_name" --run-context-json "/app/docs/$context_name"

if [[ $mode == smoke ]]; then
  echo "Smoke run complete; inspect $root/results/$run_id/summary.json before the full run"
  exit 0
fi

sudo -n docker run --rm --network none --cpus=1 --memory=1g \
  -e PYTHONPATH=/app/src:/app \
  -v "$root/code/src:/app/src:ro" -v "$root/code/scripts:/app/scripts:ro" -v "$root/results:/results" \
  -w /app "$baseline_image" /app/.venv/bin/python /app/scripts/analysis_eval_compare_reference.py \
  --reference /results/homelab-reference.json --run-dir "/results/$run_id" \
  --output "/results/$run_id-reference-comparison.json"

sudo -n docker run --rm --network none --cpus=1 --memory=1g \
  -e PYTHONPATH=/app/src:/app \
  -v "$root/code/scripts:/app/scripts:ro" -v "$root/code/docs:/app/docs:ro" -v "$root/results:/results" \
  -w /app "$baseline_image" /app/.venv/bin/python /app/scripts/analysis_eval_score_tags.py \
  --reference /results/homelab-reference.json --mapping /app/docs/analysis-evaluation-tag-map.json \
  --run-dir "/results/$run_id" --output "/results/$run_id-tag-score.json"

sudo -n docker run --rm --network none --cpus=1 --memory=1g \
  -e PYTHONPATH=/app/src:/app \
  -v "$root/code/src:/app/src:ro" -v "$root/code/scripts:/app/scripts:ro" -v "$root/results:/results" \
  -w /app "$baseline_image" /app/.venv/bin/python /app/scripts/analysis_eval_finalize.py \
  --first /results/baseline-1/summary.json --repeat /results/baseline-2/summary.json \
  --candidate "/results/$run_id/summary.json" \
  --reference-comparison "/results/$run_id-reference-comparison.json" \
  --first-tag-score /results/baseline-1-tag-score.json \
  --candidate-tag-score "/results/$run_id-tag-score.json" \
  --output "/results/$run_id-report.json"
