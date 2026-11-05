#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/node-ips.env"
VENV="${SCRIPT_DIR}/.venv"
NAMESPACE="${NAMESPACE:-online-boutique}"
TRACE_SAMPLING_PERCENTAGE="${TRACE_SAMPLING_PERCENTAGE:-unknown}"
export TRACE_SAMPLING_PERCENTAGE

if [[ $# -ne 6 ]]; then
  echo "Expected: SCENARIO SEED DURATION STEP MAX_CONCURRENT RUN_DIR" >&2
  exit 2
fi
SCENARIO="$1"
SEED="$2"
RUN_DURATION_SEC="$3"
STEP_SEC="$4"
MAX_CONCURRENT="$5"
RUN_PATH="$6"
: "${PROFILE_CELL_JSON:?Run through run-profiling.py with a profiling plan}"

[[ "$SCENARIO" =~ ^(nhpp|mmpp|onoff|pareto)$ ]] || { echo "Invalid scenario: $SCENARIO" >&2; exit 2; }
for value in "$SEED" "$RUN_DURATION_SEC" "$STEP_SEC" "$MAX_CONCURRENT"; do
  [[ "$value" =~ ^[0-9]+$ ]] || { echo "Expected integer argument, got: $value" >&2; exit 2; }
done
(( RUN_DURATION_SEC > 0 && STEP_SEC > 0 && MAX_CONCURRENT > 0 )) \
  || { echo "Duration, step, and concurrency must be positive." >&2; exit 2; }
[[ "$TRACE_SAMPLING_PERCENTAGE" == "10" ]] \
  || { echo "Profiling requires fixed 10% sampling." >&2; exit 2; }
[[ -r "$ENV_FILE" ]] || { echo "Missing ${ENV_FILE}; sync load-testing with cluster-setup/06-setup-loadgen.sh." >&2; exit 1; }
source "$ENV_FILE"
: "${NODE_OBSERVABILITY_FIXED_IP:?NODE_OBSERVABILITY_FIXED_IP is missing from node-ips.env}"
: "${FRONTEND_URL:?FRONTEND_URL is missing from node-ips.env}"
[[ -x "${VENV}/bin/locust" ]] || { echo "Locust virtual environment is missing; run cluster-setup/06-setup-loadgen.sh." >&2; exit 1; }
[[ ! -e "$RUN_PATH" ]] || { echo "Run output already exists; refusing to overwrite: ${RUN_PATH}" >&2; exit 1; }
cd "$SCRIPT_DIR"

PROMETHEUS_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:9090"
JAEGER_URL="http://${NODE_OBSERVABILITY_FIXED_IP}:16686"
mkdir -p "$RUN_PATH"

echo "Starting open-load intervention: ${SCENARIO} seed=${SEED}, ${RUN_DURATION_SEC}s"
START="$(date +%s)"
set +e
SCENARIO="$SCENARIO" SEED="$SEED" RUN_DURATION_SEC="$RUN_DURATION_SEC" TRACE_DIR="$RUN_PATH" \
  MAX_CONCURRENT="$MAX_CONCURRENT" \
  "${VENV}/bin/locust" -f "${SCRIPT_DIR}/locustfile.py" --headless --only-summary \
  --users 1 --spawn-rate 1 --run-time "${RUN_DURATION_SEC}s" --stop-timeout 5 \
  --host "$FRONTEND_URL" --csv "${RUN_PATH}/locust" --html "${RUN_PATH}/locust.html"
LOCUST_EXIT=$?
set -e
END="$(date +%s)"

SCENARIO="$SCENARIO" SEED="$SEED" START="$START" END="$END" \
  RUN_DURATION_SEC="$RUN_DURATION_SEC" STEP_SEC="$STEP_SEC" \
  FRONTEND_URL="$FRONTEND_URL" MAX_CONCURRENT="$MAX_CONCURRENT" \
  LOCUST_EXIT="$LOCUST_EXIT" NAMESPACE="$NAMESPACE" RUN_PATH="$RUN_PATH" LOAD_TEST_DIR="$SCRIPT_DIR" \
  "${VENV}/bin/python" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

run_path = Path(os.environ["RUN_PATH"])
trace_path = run_path / "arrival_trace.csv"
trace_sha256 = hashlib.sha256(trace_path.read_bytes()).hexdigest() if trace_path.exists() else None
profile = json.loads(os.environ["PROFILE_CELL_JSON"])
code_files = [
    Path(os.environ["LOAD_TEST_DIR"]) / "locustfile.py",
    Path(os.environ["LOAD_TEST_DIR"]) / "arrival_models.py",
    Path(os.environ["LOAD_TEST_DIR"]) / "collect_metrics.py",
    Path(os.environ["LOAD_TEST_DIR"]) / "run-profile-loadgen.sh",
    Path(os.environ["LOAD_TEST_DIR"]) / "scenarios" / f"{os.environ['SCENARIO']}.py",
]
metadata = {
    "scenario": os.environ["SCENARIO"],
    "run_id": run_path.name,
    "pipeline": profile["protocol"],
    "arrival_model": "open",
    "seed": int(os.environ["SEED"]),
    "repeat": profile["repeat"],
    "start": int(os.environ["START"]),
    "end": int(os.environ["END"]),
    "duration_sec": int(os.environ["RUN_DURATION_SEC"]),
    "step_sec": int(os.environ["STEP_SEC"]),
    "frontend_url": os.environ["FRONTEND_URL"],
    "namespace": os.environ["NAMESPACE"],
    "max_concurrent": int(os.environ["MAX_CONCURRENT"]),
    "autoscaler": "disabled",
    "hpa_disabled": True,
    "trace_sampling_percentage": os.environ.get("TRACE_SAMPLING_PERCENTAGE", "unknown"),
    "trace_sampling_note": "Sampling fixed at 10%; Jaeger provides sampled latency, Prometheus provides resources/capacity.",
    "baseline_replica_policy": "initial full K in profiling.replicas; redis-cart=1",
    "replica_schedule": profile.get("schedule", []),
    "trace_sha256": trace_sha256,
    "code_sha256": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in code_files
    },
    "locust_exit_code": int(os.environ["LOCUST_EXIT"]),
    "load_multiplier": float(os.environ.get("LOAD_MULTIPLIER", "1")),
    "profiling": profile,
}
(run_path / "meta.json").write_text(json.dumps(metadata, indent=2) + "\n")
PY

echo "Waiting 20s for final Prometheus scrapes..."
sleep 20
"${VENV}/bin/python" "${SCRIPT_DIR}/collect_metrics.py" \
  --start "$START" --end "$END" --out "$RUN_PATH" --step "$STEP_SEC" \
  --prometheus "$PROMETHEUS_URL" --jaeger "$JAEGER_URL" \
  --namespace "$NAMESPACE" \
  --raw-spans-out "${RUN_PATH}/jaeger_server_spans.csv"

echo "Saved run ${RUN_PATH}"
