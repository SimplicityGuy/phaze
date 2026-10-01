#!/usr/bin/env bash
# One-command detached repeat of the staged candidate on the UpCloud host.
set -euo pipefail

root=/home/datum/phaze-iq64v
log=$root/results/candidate-2-launcher.log
context=$root/code/docs/candidate-run-context.json

if [[ ${1:-} != --internal ]]; then
  [[ ! -e $root/results/candidate-2 && ! -e $log ]] || {
    echo 'Candidate-2 output or log already exists; refusing to overwrite it.' >&2
    exit 2
  }
  nohup "$root/code/repeat-analysis.sh" --internal >"$log" 2>&1 </dev/null &
  echo "Started candidate repeat as PID $!. Follow it with: tail -f $log"
  exit 0
fi

exec 8>"$root/results/repeat-analysis.lock"
flock -n 8 || {
  echo 'A candidate repeat is already running.' >&2
  exit 1
}

image=$(
  /usr/bin/python3 - "$context" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1]))['image_digest'])
PY
)

"$root/code/run-candidate-v2.sh" \
  candidate-2 candidate-command.json "$image" \
  "$root/candidate-models" candidate-run-context.json

/usr/bin/python3 - "$root/results/candidate-2-report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
print(f"Candidate repeat complete: {report['batch_speedup_vs_fastest_baseline']:.3f}x versus fastest baseline; 2x gate={report['two_x_batch_gate']}", flush=True)
PY
