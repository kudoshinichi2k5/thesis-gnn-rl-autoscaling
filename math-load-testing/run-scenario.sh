#!/usr/bin/env bash
# Thu dataset theo mô hình toán học (NHPP, MMPP, Bounded Pareto, ON/OFF) bằng driver
# luồng đến MỞ (locustfile.py), rồi xuất telemetry ĐẦY ĐỦ (có edge_metrics) bằng
# load-testing/collect_metrics.py để dùng được cho cả GAT-GRU và LSTM.
#
# Chạy TRỰC TIẾP trên node-loadgen (~ có load-testing/ lstm-load-testing/ math-load-testing/):
#   bash math-load-testing/run-scenario.sh {nhpp|mmpp|pareto|onoff} [RUNS]
# Run dài: chạy trong tmux/screen để mất SSH không làm dừng run.
# Env:   RUN_DURATION_SEC (1800)  COOLDOWN_SEC (120)  STEP_SEC (10)
#        SEED_BASE (1000): seed của run_k = SEED_BASE + k (tái lập được, so sánh theo cặp)
#        MAX_CONCURRENT (400)  AUTOSCALER (nhãn, mặc định none)
# Dùng chung từ ../load-testing/: node-ips.env, .venv, collect_metrics.py.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOAD_DIR="${LOAD_DIR:-$(cd -- "${SCRIPT_DIR}/../load-testing" && pwd)}"
ENV_FILE="${LOAD_DIR}/node-ips.env"
RESULTS_DIR="${SCRIPT_DIR}/results"
VENV="${LOAD_DIR}/.venv"

if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^(nhpp|mmpp|pareto|onoff)$ ]]; then
  echo "Usage: bash math-load-testing/run-scenario.sh {nhpp|mmpp|pareto|onoff} [RUNS]" >&2
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
# node-loadgen không có kubectl, nên FRONTEND_URL phải có sẵn trong node-ips.env.
: "${FRONTEND_URL:?FRONTEND_URL is missing from node-ips.env (http://<NODE_APP_FLOATING_IP>:<frontend-external NodePort>)}"

RUN_DURATION_SEC="${RUN_DURATION_SEC:-1800}"
COOLDOWN_SEC="${COOLDOWN_SEC:-120}"
STEP_SEC="${STEP_SEC:-10}"
SEED_BASE="${SEED_BASE:-1000}"
MAX_CONCURRENT="${MAX_CONCURRENT:-400}"
AUTOSCALER="${AUTOSCALER:-none}"
PROMETHEUS_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:9090"
JAEGER_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:16686"

if [[ ! -f "${LOAD_DIR}/collect_metrics.py" ]]; then
  echo "${LOAD_DIR} thiếu collect_metrics.py" >&2
  exit 1
fi

# Tạo venv dùng chung nếu chưa có (cần python3-venv).
if [[ ! -x "${VENV}/bin/locust" ]]; then
  echo "Locust venv missing; creating ${VENV}..."
  python3 -m venv "$VENV"
  "${VENV}/bin/python" -m pip install --disable-pip-version-check -q -r "${LOAD_DIR}/requirements.txt"
fi

cd "$SCRIPT_DIR"

echo "Preflight: frontend, Prometheus, Jaeger, trace thử..."
curl -fsS --connect-timeout 5 --max-time 10 "$FRONTEND_URL" -o /dev/null \
  || { echo "frontend unreachable: ${FRONTEND_URL}" >&2; exit 1; }
curl -fsS --connect-timeout 5 --max-time 10 "${PROMETHEUS_URL}/-/ready" -o /dev/null \
  || { echo "Prometheus unreachable: ${PROMETHEUS_URL}" >&2; exit 1; }
curl -fsS --connect-timeout 5 --max-time 10 "${JAEGER_URL}/api/services" -o /dev/null \
  || { echo "Jaeger unreachable: ${JAEGER_URL}" >&2; exit 1; }
"${VENV}/bin/python" -c "
import importlib, arrival_models as am
cfg = importlib.import_module('scenarios.${SCENARIO}').CONFIG
tr = am.build_trace(cfg, ${RUN_DURATION_SEC}, 1)
print('trace ok: %d events, expected mean %.1f rps' % (len(tr['events']), tr['expected_mean_rps']))"

mkdir -p "${RESULTS_DIR}/${SCENARIO}"
LAST_RUN="$(find "${RESULTS_DIR}/${SCENARIO}" -maxdepth 1 -type d -name 'run_*' -printf '%f\n' 2>/dev/null \
  | sed 's/run_//' | sort -n | tail -1)"
FIRST_RUN=$(( 10#${LAST_RUN:-0} + 1 ))

for (( i = 0; i < RUNS; i++ )); do
  RUN_NUM=$(( FIRST_RUN + i ))
  RUN_ID="$(printf 'run_%02d' "$RUN_NUM")"
  SEED=$(( SEED_BASE + RUN_NUM ))
  RUN_PATH="${RESULTS_DIR}/${SCENARIO}/${RUN_ID}"
  echo
  echo "=== [math] ${SCENARIO} ${RUN_ID} seed=${SEED} ($(( i + 1 ))/${RUNS}, ${RUN_DURATION_SEC}s) ==="
  mkdir -p "$RUN_PATH"

  START=$(date +%s)
  set +e
  SCENARIO="$SCENARIO" SEED="$SEED" RUN_DURATION_SEC="$RUN_DURATION_SEC" TRACE_DIR="$RUN_PATH" \
    MAX_CONCURRENT="$MAX_CONCURRENT" \
    "${VENV}/bin/locust" -f locustfile.py --headless --only-summary \
    --users 1 --spawn-rate 1 --run-time "${RUN_DURATION_SEC}s" --stop-timeout 5 \
    --host "$FRONTEND_URL" --csv "${RUN_PATH}/locust" --html "${RUN_PATH}/locust.html"
  LOCUST_EXIT=$?
  set -e
  END=$(date +%s)

  cat > "${RUN_PATH}/meta.json" <<META
{"scenario": "${SCENARIO}", "run_id": "${RUN_ID}", "pipeline": "math", "arrival_model": "open",
 "seed": ${SEED}, "start": ${START}, "end": ${END}, "duration_sec": ${RUN_DURATION_SEC},
 "step_sec": ${STEP_SEC}, "frontend_url": "${FRONTEND_URL}", "max_concurrent": ${MAX_CONCURRENT},
 "autoscaler": "${AUTOSCALER}", "locust_exit_code": ${LOCUST_EXIT}}
META

  # Let Prometheus scrape the final samples before exporting the window.
  sleep 20
  "${VENV}/bin/python" "${LOAD_DIR}/collect_metrics.py" --start "$START" --end "$END" \
    --out "$RUN_PATH" --step "$STEP_SEC" --prometheus "$PROMETHEUS_URL" --jaeger "$JAEGER_URL"

  if grep -q '"dropped": [1-9]' "${RUN_PATH}/driver_report.json" 2>/dev/null; then
    echo "⚠️ ${RUN_ID}: driver đã bỏ một số arrival (xem driver_report.json) - loadgen quá tải"
  fi
  echo "Saved ${RUN_PATH}"

  if (( i < RUNS - 1 && COOLDOWN_SEC > 0 )); then
    echo "Cooldown ${COOLDOWN_SEC}s..."
    sleep "$COOLDOWN_SEC"
  fi
done

echo
echo "[math] Scenario ${SCENARIO} complete: ${RUNS} run(s) in ${RESULTS_DIR}/${SCENARIO}/"
