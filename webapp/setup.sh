#!/usr/bin/env bash
# Build the portal's virtualenv. Idempotent. The shared run engine (labportal) is installed from the local
# checkout when there is one, so a change there is picked up without a release.
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
[[ -x "$here/.venv/bin/python" ]] || python3 -m venv "$here/.venv"
"$here/.venv/bin/pip" install -q --upgrade pip
"$here/.venv/bin/pip" install -q -r "$here/requirements.txt"
[[ -d "$HOME/lab-portal" ]] && "$here/.venv/bin/pip" install -q -e "$HOME/lab-portal"
echo "webapp/.venv ready: $("$here/.venv/bin/python" --version)"
