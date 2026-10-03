#!/usr/bin/env bash
# Run one load scenario N times on node-loadgen, export Prometheus/Jaeger
# telemetry of every run window, and pull results back to load-testing/results/.
#
# Usage: bash load-testing/run-scenario.sh {normal|spike|bursty} [RUNS]
# Env:   RUN_DURATION_SEC (1800)  COOLDOWN_SEC (120)  STEP_SEC (10)
#        BURSTY_SEED (unset = random)  AUTOSCALER (label, default none)
#        SKIP_COLLECT=1 (Locust only)
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/cluster-setup/node-ips.env"
RESULTS_DIR="${SCRIPT_DIR}/results"

if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^(normal|spike|bursty)$ ]]; then
  echo "Usage: bash load-testing/run-scenario.sh {normal|spike|bursty} [RUNS]" >&2
  exit 2
fi
SCENARIO="$1"
RUNS="${2:-1}"
if [[ ! "$RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "RUNS must be a positive integer" >&2
  exit 2
fi

if [[ ! -r "$ENV_FILE" ]]; then
  echo "Missing ${ENV_FILE}; run cluster-setup/00-generate-node-ips.sh first." >&2
  exit 1
fi
source "$ENV_FILE"
: "${NODE_LOADGEN_FLOATING_IP:?NODE_LOADGEN_FLOATING_IP is missing from node-ips.env}"
: "${NODE_OBSERVABILITY_FIXED_IP:?NODE_OBSERVABILITY_FIXED_IP is missing from node-ips.env}"
: "${FRONTEND_URL:?FRONTEND_URL is missing; regenerate node-ips.env after frontend-external exists}"

RUN_DURATION_SEC="${RUN_DURATION_SEC:-1800}"
COOLDOWN_SEC="${COOLDOWN_SEC:-120}"
STEP_SEC="${STEP_SEC:-10}"
BURSTY_SEED="${BURSTY_SEED:-}"
AUTOSCALER="${AUTOSCALER:-none}"   # label only: none | hpa | ... (recorded in meta.json)
SKIP_COLLECT="${SKIP_COLLECT:-0}"
# Private network: node-loadgen reaches node-observability directly.
PROMETHEUS_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:9090"
JAEGER_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:16686"

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kltn_autoscaling}"
REMOTE_DIR="${REMOTE_DIR:-/home/${SSH_USER}/load-testing}"
CONTROL_PATH="$HOME/.ssh/locust-loadgen-%C"
SSH_OPTIONS=(-i "$SSH_KEY" -o ControlMaster=auto -o ControlPersist=10m -o "ControlPath=${CONTROL_PATH}"
             -o ServerAliveInterval=60 -o ServerAliveCountMax=5)
RSYNC_SSH="ssh -i ${SSH_KEY} -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=${CONTROL_PATH}"
REMOTE="${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}"

if [[ ! -r "$SSH_KEY" ]]; then
  echo "SSH key is not readable: $SSH_KEY" >&2
  exit 1
fi

echo "Preflight from node-loadgen: venv, frontend, Prometheus, Jaeger..."
ssh "${SSH_OPTIONS[@]}" "$REMOTE" "set -e
  test -x '${REMOTE_DIR}/.venv/bin/locust' || { echo 'Locust venv missing: run cluster-setup/06-setup-loadgen.sh' >&2; exit 1; }
  curl -fsS --connect-timeout 5 --max-time 10 '${FRONTEND_URL}' -o /dev/null || { echo 'frontend unreachable: ${FRONTEND_URL}' >&2; exit 1; }
  curl -fsS --connect-timeout 5 --max-time 10 '${PROMETHEUS_URL}/-/ready' -o /dev/null || { echo 'Prometheus unreachable: ${PROMETHEUS_URL}' >&2; exit 1; }
  curl -fsS --connect-timeout 5 --max-time 10 '${JAEGER_URL}/api/services' -o /dev/null || echo 'WARN Jaeger unreachable: ${JAEGER_URL}' >&2"

# Continue numbering after runs already pulled back locally.
mkdir -p "${RESULTS_DIR}/${SCENARIO}"
LAST_RUN="$(find "${RESULTS_DIR}/${SCENARIO}" -maxdepth 1 -type d -name 'run_*' -printf '%f\n' 2>/dev/null \
  | sed 's/run_//' | sort -n | tail -1)"
FIRST_RUN=$(( 10#${LAST_RUN:-0} + 1 ))

for (( i = 0; i < RUNS; i++ )); do
  RUN_ID="$(printf 'run_%02d' $(( FIRST_RUN + i )))"
  RUN_PATH="results/${SCENARIO}/${RUN_ID}"
  echo
  echo "=== ${SCENARIO} ${RUN_ID} ($(( i + 1 ))/${RUNS}, ${RUN_DURATION_SEC}s) ==="

  ssh "${SSH_OPTIONS[@]}" "$REMOTE" "set -euo pipefail
    cd '${REMOTE_DIR}'
    mkdir -p '${RUN_PATH}'
    START=\$(date +%s)
    set +e
    RUN_DURATION_SEC='${RUN_DURATION_SEC}' BURSTY_SEED='${BURSTY_SEED}' \
      .venv/bin/locust -f 'locustfile.py,scenarios/${SCENARIO}.py' --headless --only-summary \
      --host '${FRONTEND_URL}' --csv '${RUN_PATH}/locust' --html '${RUN_PATH}/locust.html'
    LOCUST_EXIT=\$?
    set -e
    END=\$(date +%s)
    cat > '${RUN_PATH}/meta.json' <<META
{\"scenario\": \"${SCENARIO}\", \"run_id\": \"${RUN_ID}\", \"start\": \$START, \"end\": \$END,
 \"duration_sec\": ${RUN_DURATION_SEC}, \"step_sec\": ${STEP_SEC}, \"frontend_url\": \"${FRONTEND_URL}\",
 \"bursty_seed\": \"${BURSTY_SEED}\", \"autoscaler\": \"${AUTOSCALER}\", \"locust_exit_code\": \$LOCUST_EXIT}
META
    if [ '${SKIP_COLLECT}' != '1' ]; then
      # Let Prometheus scrape the final samples before exporting the window.
      sleep 20
      .venv/bin/python collect_metrics.py --start \$START --end \$END --out '${RUN_PATH}' \
        --step '${STEP_SEC}' --prometheus '${PROMETHEUS_URL}' --jaeger '${JAEGER_URL}'
    fi"

  mkdir -p "${RESULTS_DIR}/${SCENARIO}/${RUN_ID}"
  rsync -a -e "$RSYNC_SSH" "${REMOTE}:${REMOTE_DIR}/${RUN_PATH}/" "${RESULTS_DIR}/${SCENARIO}/${RUN_ID}/"
  echo "Saved ${RESULTS_DIR}/${SCENARIO}/${RUN_ID}"

  if (( i < RUNS - 1 && COOLDOWN_SEC > 0 )); then
    echo "Cooldown ${COOLDOWN_SEC}s so the next run starts from an idle cluster..."
    sleep "$COOLDOWN_SEC"
  fi
done

echo
echo "Scenario ${SCENARIO} complete: ${RUNS} run(s) in ${RESULTS_DIR}/${SCENARIO}/"
