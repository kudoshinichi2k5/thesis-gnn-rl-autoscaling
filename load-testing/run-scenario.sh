#!/usr/bin/env bash
# Run one load scenario N times DIRECTLY on node-loadgen and export the
# Prometheus/Jaeger telemetry of every run window to load-testing/results/.
#
# Run on node-loadgen (ubuntu@node-loadgen:~ has load-testing/ lstm-load-testing/ math-load-testing/):
#   bash load-testing/run-scenario.sh {normal|spike|bursty} [RUNS]
# Long runs: start inside tmux/screen so an SSH disconnect does not kill them.
# Env:   RUN_DURATION_SEC (1800)  COOLDOWN_SEC (120)  STEP_SEC (10)
#        BURSTY_SEED (unset = random)  AUTOSCALER (label, default none)
#        SKIP_COLLECT=1 (Locust only)
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/node-ips.env"
RESULTS_DIR="${SCRIPT_DIR}/results"
VENV="${SCRIPT_DIR}/.venv"

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
  echo "Missing ${ENV_FILE}." >&2
  exit 1
fi
source "$ENV_FILE"
: "${NODE_OBSERVABILITY_FIXED_IP:?NODE_OBSERVABILITY_FIXED_IP is missing from node-ips.env}"
# node-loadgen has no kubectl, so FRONTEND_URL must be set in node-ips.env.
: "${FRONTEND_URL:?FRONTEND_URL is missing from node-ips.env (http://<NODE_APP_FLOATING_IP>:<frontend-external NodePort>)}"

RUN_DURATION_SEC="${RUN_DURATION_SEC:-1800}"
COOLDOWN_SEC="${COOLDOWN_SEC:-120}"
STEP_SEC="${STEP_SEC:-10}"
BURSTY_SEED="${BURSTY_SEED:-}"
AUTOSCALER="${AUTOSCALER:-none}"   # label only: none | hpa | ... (recorded in meta.json)
SKIP_COLLECT="${SKIP_COLLECT:-0}"
# Private network: node-loadgen reaches node-observability directly.
PROMETHEUS_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:9090"
JAEGER_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:16686"

cd "$SCRIPT_DIR"

# Create the venv on first use (or after it was deleted); requires python3-venv.
if [[ ! -x "${VENV}/bin/locust" ]]; then
  echo "Locust venv missing; creating ${VENV}..."
  python3 -m venv "$VENV"
  "${VENV}/bin/python" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

echo "Preflight: frontend, Prometheus, Jaeger..."
curl -fsS --connect-timeout 5 --max-time 10 "$FRONTEND_URL" -o /dev/null \
  || { echo "frontend unreachable: ${FRONTEND_URL}" >&2; exit 1; }
if [[ "$SKIP_COLLECT" != "1" ]]; then
  curl -fsS --connect-timeout 5 --max-time 10 "${PROMETHEUS_URL}/-/ready" -o /dev/null \
    || { echo "Prometheus unreachable: ${PROMETHEUS_URL}" >&2; exit 1; }
  curl -fsS --connect-timeout 5 --max-time 10 "${JAEGER_URL}/api/services" -o /dev/null \
    || echo "WARN Jaeger unreachable: ${JAEGER_URL} (edge_metrics.csv will be empty)" >&2
fi

# Continue numbering after existing runs.
mkdir -p "${RESULTS_DIR}/${SCENARIO}"
LAST_RUN="$(find "${RESULTS_DIR}/${SCENARIO}" -maxdepth 1 -type d -name 'run_*' -printf '%f\n' 2>/dev/null \
  | sed 's/run_//' | sort -n | tail -1)"
FIRST_RUN=$(( 10#${LAST_RUN:-0} + 1 ))

for (( i = 0; i < RUNS; i++ )); do
  RUN_ID="$(printf 'run_%02d' $(( FIRST_RUN + i )))"
  RUN_PATH="${RESULTS_DIR}/${SCENARIO}/${RUN_ID}"
  echo
  echo "=== ${SCENARIO} ${RUN_ID} ($(( i + 1 ))/${RUNS}, ${RUN_DURATION_SEC}s) ==="
  mkdir -p "$RUN_PATH"

  START=$(date +%s)
  set +e
  RUN_DURATION_SEC="$RUN_DURATION_SEC" BURSTY_SEED="$BURSTY_SEED" \
    "${VENV}/bin/locust" -f "locustfile.py,scenarios/${SCENARIO}.py" --headless --only-summary \
    --host "$FRONTEND_URL" --csv "${RUN_PATH}/locust" --html "${RUN_PATH}/locust.html"
  LOCUST_EXIT=$?
  set -e
  END=$(date +%s)

  cat > "${RUN_PATH}/meta.json" <<META
{"scenario": "${SCENARIO}", "run_id": "${RUN_ID}", "start": ${START}, "end": ${END},
 "duration_sec": ${RUN_DURATION_SEC}, "step_sec": ${STEP_SEC}, "frontend_url": "${FRONTEND_URL}",
 "bursty_seed": "${BURSTY_SEED}", "autoscaler": "${AUTOSCALER}", "locust_exit_code": ${LOCUST_EXIT}}
META

  if [[ "$SKIP_COLLECT" != "1" ]]; then
    # Let Prometheus scrape the final samples before exporting the window.
    sleep 20
    "${VENV}/bin/python" collect_metrics.py --start "$START" --end "$END" --out "$RUN_PATH" \
      --step "$STEP_SEC" --prometheus "$PROMETHEUS_URL" --jaeger "$JAEGER_URL"
  fi
  echo "Saved ${RUN_PATH}"

  if (( i < RUNS - 1 && COOLDOWN_SEC > 0 )); then
    echo "Cooldown ${COOLDOWN_SEC}s so the next run starts from an idle cluster..."
    sleep "$COOLDOWN_SEC"
  fi
done

echo
echo "Scenario ${SCENARIO} complete: ${RUNS} run(s) in ${RESULTS_DIR}/${SCENARIO}/"
