#!/usr/bin/env bash
# Run the Robot Framework suites against the lab.   tests/run.sh [robot options...]   e.g. tests/run.sh suites/05_incast.robot
# Each run gets tests/results_YYYY-MM-DD_HH-MM-SS/ (tests/results_latest links the newest): log.html, report.html, output.xml, and state/ — the hosts' algorithms and
# the WAN emulator's settings, before the run (the suites change the WAN and put the defaults back).
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
ROBOT="$PWD/.venv/bin/robot"
[[ -x "$ROBOT" ]] || { echo "error: run tests/setup.sh first" >&2; exit 1; }
exec 9>/tmp/tcp-cc-lab-test.lock          # one run at a time: the suites change the WAN emulator
flock -n 9 || { echo "==> another test run holds /tmp/tcp-cc-lab-test.lock — waiting for it"; flock 9; }
unset FORCE_COLOR

ts="$(date +%Y-%m-%d_%H-%M-%S)"; out="$PWD/results_$ts"; mkdir -p "$out/state"
echo "==> results: $out"
../lab.sh inventory > "$out/state/inventory.json"
../lab.sh cc > "$out/state/hosts.txt" 2>&1
../lab.sh wan show > "$out/state/wanem.txt" 2>&1

args=("$@"); explicit=0; for x in "$@"; do [[ "$x" == *.robot ]] && explicit=1; done; [[ $explicit -eq 1 ]] || args+=(suites/)
"$ROBOT" --outputdir "$out" --name "tcp-cc lab" --loglevel INFO --pythonpath resources "${args[@]}"
rc=$?
ln -sfn "$(basename "$out")" results_latest
mkdir -p ../results; ln -sfn "../tests/$(basename "$out")" ../results/latest   # where Lab Hub reads a lab's last run
echo "==> report: $out/report.html  (rc=$rc)"
exit $rc
