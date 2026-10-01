#!/usr/bin/env bash
# One-command UpCloud entrypoint: detached smoke run, full run, and reports.
set -euo pipefail

root=/home/datum/phaze-iq64v
context=$root/code/docs/candidate-run-context.json
command_file=$root/code/docs/candidate-command.json
log=$root/results/candidate-launcher.log

[[ -s $context && -s $command_file ]] || {
  echo 'Candidate has not been staged; candidate-command.json and candidate-run-context.json are required.' >&2
  exit 2
}
[[ -n $(find "$root/candidate" -type f -print -quit) ]] || {
  echo 'Candidate code directory is empty.' >&2
  exit 2
}

if [[ ${1:-} == --check-only ]]; then
  /usr/bin/python3 - "$context" "$root/results/candidate-smoke-1/summary.json" <<'PY'
import json, sys
context=json.load(open(sys.argv[1]))
smoke=json.load(open(sys.argv[2]))
assert context['image_digest'].startswith('ghcr.io/simplicityguy/phaze@sha256:')
assert smoke['runs'] == 1 and smoke['failed_runs'] == 0 and smoke['protocol'] == 'neutral'
print('candidate image, code, context, and smoke result are ready')
PY
  [[ ! -e $root/results/candidate-1 ]] || {
    echo 'candidate-1 already exists' >&2
    exit 2
  }
  exit 0
fi

if [[ ${1:-} != --internal ]]; then
  nohup "$root/code/start-analysis.sh" --internal >>"$log" 2>&1 </dev/null &
  echo "Started candidate evaluation as PID $!. Follow it with: tail -f $log"
  exit 0
fi

exec 8>"$root/results/start-analysis.lock"
flock -n 8 || {
  echo 'A candidate workflow is already running.' >&2
  exit 1
}

image=$(
  /usr/bin/python3 - "$context" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['image_digest'])
PY
)
launcher=$root/code/run-candidate-v2.sh

if [[ ! -e $root/results/candidate-smoke-1/summary.json ]]; then
  "$launcher" candidate-smoke-1 candidate-command.json "$image" "$root/candidate-models" candidate-run-context.json smoke
fi

/usr/bin/python3 - "$root/results/candidate-smoke-1/summary.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1]))
assert report['runs'] == 1 and report['failed_runs'] == 0 and report['protocol'] == 'neutral', 'candidate smoke contract failed'
print('Candidate smoke check passed: 1 file, zero failures', flush=True)
PY

if [[ -e $root/results/candidate-1 ]]; then
  echo 'Candidate-1 output already exists; refusing to overwrite it.' >&2
  exit 2
fi
"$launcher" candidate-1 candidate-command.json "$image" "$root/candidate-models" candidate-run-context.json

/usr/bin/python3 - "$root/results/candidate-1-report.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1]))
print(f"Candidate complete: {report['batch_speedup_vs_fastest_baseline']:.3f}x versus fastest baseline; 2x gate={report['two_x_batch_gate']}", flush=True)
PY
