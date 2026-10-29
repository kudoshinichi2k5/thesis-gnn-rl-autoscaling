#!/usr/bin/env bash
# Thu dataset theo mô hình toán học (NHPP, MMPP, Bounded Pareto, ON/OFF) bằng driver
# luồng đến MỞ (locustfile.py), rồi xuất telemetry ĐẦY ĐỦ (có edge_metrics) bằng
# collect_metrics.py để dùng được cho cả GAT-GRU và LSTM. Dataset luôn được thu khi HPA bật.
#
# Chạy TRỰC TIẾP trên node-loadgen từ thư mục load-testing/:
#   bash load-testing/run-scenario.sh {nhpp|mmpp|pareto|onoff} [RUNS]
# Run dài: chạy bằng nohup/tmux để mất SSH không làm dừng run (xem README.md).
# Env:   RUN_DURATION_SEC (1800)  COOLDOWN_SEC (120)  STEP_SEC (10)
#        SEED_BASE (1000): seed của run_k = SEED_BASE + k (tái lập được, so sánh theo cặp)
#        MAX_CONCURRENT (400)
#        REPLICA_WAIT_MAX_SEC (1200): trước mỗi run, chờ HPA scale mọi deployment về 1 replica
#        RESULTS_DIR (mặc định: dataset/raw)
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/node-ips.env"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/../dataset/raw}"
VENV="${SCRIPT_DIR}/.venv"
NAMESPACE="online-boutique"

if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^(nhpp|mmpp|pareto|onoff)$ ]]; then
  echo "Usage: bash load-testing/run-scenario.sh {nhpp|mmpp|pareto|onoff} [RUNS]" >&2
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
REPLICA_WAIT_MAX_SEC="${REPLICA_WAIT_MAX_SEC:-1200}"
PROMETHEUS_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:9090"
JAEGER_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:16686"

# Tạo venv nếu chưa có (cần python3-venv).
if [[ ! -x "${VENV}/bin/locust" ]]; then
  echo "Locust venv missing; creating ${VENV}..."
  python3 -m venv "$VENV"
  "${VENV}/bin/python" -m pip install --disable-pip-version-check -q -r "${SCRIPT_DIR}/requirements.txt"
fi

# Số replica lớn nhất trong namespace, đọc từ kube-state-metrics qua Prometheus (loadgen không có kubectl).
max_replicas() {
  curl -fsS --connect-timeout 5 --max-time 10 "${PROMETHEUS_URL}/api/v1/query" \
    --data-urlencode "query=max(kube_deployment_spec_replicas{namespace=\"${NAMESPACE}\"})" 2>/dev/null \
    | "${VENV}/bin/python" -c 'import json, sys
r = json.load(sys.stdin)["data"]["result"]
print(int(float(r[0]["value"][1])) if r else "")' 2>/dev/null || true
}

# HPA chỉ scale down sau 5 phút ổn định, rồi bớt 1 pod mỗi 60s: từ 10 về 1 mất ~14 phút.
# Chờ tới khi mọi deployment về 1 replica để mọi run bắt đầu từ cùng một trạng thái.
wait_for_min_replicas() {
  local deadline=$(( $(date +%s) + REPLICA_WAIT_MAX_SEC )) max
  while :; do
    max="$(max_replicas)"
    if [[ "$max" == "1" ]]; then
      echo "Replica: mọi deployment ở 1 replica"
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      echo "⚠️ Sau ${REPLICA_WAIT_MAX_SEC}s vẫn còn deployment ở ${max:-?} replica - vẫn chạy tiếp (xem replicas_at_start trong meta.json)" >&2
      return 0
    fi
    echo "Chờ HPA scale về 1 replica (hiện max = ${max:-?})..."
    sleep 30
  done
}

cd "$SCRIPT_DIR"

echo "Preflight: frontend, Prometheus, Jaeger, trace thử..."
curl -fsS --connect-timeout 5 --max-time 10 "$FRONTEND_URL" -o /dev/null \
  || { echo "frontend unreachable: ${FRONTEND_URL}" >&2; exit 1; }
curl -fsS --connect-timeout 5 --max-time 10 "${PROMETHEUS_URL}/-/ready" -o /dev/null \
  || { echo "Prometheus unreachable: ${PROMETHEUS_URL}" >&2; exit 1; }
curl -fsS --connect-timeout 5 --max-time 10 "${JAEGER_URL}/api/services" -o /dev/null \
  || { echo "Jaeger unreachable: ${JAEGER_URL}" >&2; exit 1; }
[[ -n "$(max_replicas)" ]] \
  || { echo "Không đọc được kube_deployment_spec_replicas từ Prometheus (kube-state-metrics)" >&2; exit 1; }
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
  wait_for_min_replicas
  REPLICAS_AT_START="$(max_replicas)"
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
 "autoscaler": "hpa", "replicas_at_start": ${REPLICAS_AT_START:-null}, "locust_exit_code": ${LOCUST_EXIT}}
META

  # Let Prometheus scrape the final samples before exporting the window.
  sleep 20
  "${VENV}/bin/python" "${SCRIPT_DIR}/collect_metrics.py" --start "$START" --end "$END" \
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
