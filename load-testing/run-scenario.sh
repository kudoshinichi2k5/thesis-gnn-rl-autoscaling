if [[ ! -r "$SSH_KEY" ]]; then
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/cluster-setup/node-ips.env"
RESULTS_DIR="${SCRIPT_DIR}/results"

if [[ $# -ne 1 || ! "$1" =~ ^(normal|spike|bursty)$ ]]; then
  echo "Usage: bash load-testing/run-scenario.sh {normal|spike|bursty}" >&2
  exit 2
fi
SCENARIO="$1"

if [[ ! -r "$ENV_FILE" ]]; then
  echo "Missing ${ENV_FILE}; run cluster-setup/00-generate-node-ips.sh first." >&2
  exit 1
fi
source "$ENV_FILE"
: "${NODE_LOADGEN_FLOATING_IP:?NODE_LOADGEN_FLOATING_IP is missing from node-ips.env}"
: "${FRONTEND_URL:?FRONTEND_URL is missing; regenerate node-ips.env after frontend-external exists}"

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kltn_autoscaling}"
REMOTE_DIR="${REMOTE_DIR:-/home/${SSH_USER}/load-testing}"
CONTROL_PATH="$HOME/.ssh/locust-loadgen-%C"
SSH_OPTIONS=(-i "$SSH_KEY" -o ControlMaster=auto -o ControlPersist=10m -o "ControlPath=${CONTROL_PATH}")
RSYNC_SSH="ssh -i ${SSH_KEY} -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=${CONTROL_PATH}"
RESULT_NAME="${SCENARIO}_test1"
CSV_BASE="results/${RESULT_NAME}"
HTML_FILE="results/${RESULT_NAME}.html"

if [[ ! -r "$SSH_KEY" ]]; then
  echo "SSH key is not readable: $SSH_KEY" >&2
  exit 1
fi

ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}" \
  "test -x '${REMOTE_DIR}/.venv/bin/locust' && curl -fsS --connect-timeout 5 --max-time 10 '${FRONTEND_URL}' -o /dev/null"

REMOTE_COMMAND="cd '${REMOTE_DIR}' && mkdir -p results && .venv/bin/locust -f 'locustfile.py,scenarios/${SCENARIO}.py' --headless --host '${FRONTEND_URL}' --csv='${CSV_BASE}' --html='${HTML_FILE}'"
ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}" "$REMOTE_COMMAND"

mkdir -p "$RESULTS_DIR"
rsync -av -e "$RSYNC_SSH" \
  "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}:${REMOTE_DIR}/results/" \
  "${RESULTS_DIR}/"

echo "Scenario ${SCENARIO} complete. Results: ${RESULTS_DIR}/${RESULT_NAME}_stats.csv and ${RESULTS_DIR}/${RESULT_NAME}.html"
