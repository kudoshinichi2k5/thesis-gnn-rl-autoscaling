#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/cluster-setup/node-ips.env"
NAMESPACE="${NAMESPACE:-online-boutique}"

if [[ ! -r "$ENV_FILE" ]]; then
  echo "Missing ${ENV_FILE}; generate it with cluster-setup/00-generate-node-ips.sh." >&2
  exit 1
fi
source "$ENV_FILE"
: "${NODE_LOADGEN_FLOATING_IP:?NODE_LOADGEN_FLOATING_IP is missing from node-ips.env}"
: "${NODE_OBSERVABILITY_FIXED_IP:?NODE_OBSERVABILITY_FIXED_IP is missing from node-ips.env}"
: "${FRONTEND_URL:?FRONTEND_URL is missing from node-ips.env}"

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kltn_autoscaling}"
REMOTE_DIR="${REMOTE_DIR:-/home/${SSH_USER}/load-testing}"
REMOTE_RESULTS_DIR="${REMOTE_RESULTS_DIR:-/home/${SSH_USER}/dataset/intervention}"
LOCAL_RESULTS_DIR="${LOCAL_RESULTS_DIR:-${REPO_ROOT}/dataset/intervention}"
SCENARIOS="${SCENARIOS:-onoff,pareto}"
INTERVENTION_SERVICES="${INTERVENTION_SERVICES:-frontend,cartservice,currencyservice,productcatalogservice,recommendationservice,checkoutservice}"
REPLICA_LEVELS="${REPLICA_LEVELS:-2,4}"
REPEATS="${REPEATS:-3}"
MAX_RUNS_PER_INVOCATION="${MAX_RUNS_PER_INVOCATION:-3}"
RUN_DURATION_SEC="${RUN_DURATION_SEC:-1800}"
COOLDOWN_SEC="${COOLDOWN_SEC:-120}"
POST_ROLLOUT_SETTLE_SEC="${POST_ROLLOUT_SETTLE_SEC:-90}"
STEP_SEC="${STEP_SEC:-10}"
SEED_BASE="${SEED_BASE:-7600}"
MAX_CONCURRENT="${MAX_CONCURRENT:-400}"
ROLLOUT_WAIT_SEC="${ROLLOUT_WAIT_SEC:-300}"
MAX_TARGET_REPLICAS="${MAX_TARGET_REPLICAS:-4}"
TRACE_SAMPLING_PERCENTAGE="${TRACE_SAMPLING_PERCENTAGE:-10}"

APP_DEPLOYMENTS=(
  adservice cartservice checkoutservice currencyservice emailservice frontend
  paymentservice productcatalogservice recommendationservice shippingservice
)
VALID_SCENARIOS=(onoff pareto)
DRY_RUN=0
CONFIRMED=0

usage() {
  cat <<'USAGE'
Usage: bash load-testing/run-intervention.sh [--dry-run | --confirm]

Run on the management machine with kubectl access and SSH access to node-loadgen.
The default study tests ON-OFF and Pareto, varying the main RL-controlled
services one at a time against an all-services-at-1 baseline.

Options:
  --dry-run   Validate local settings and print the randomized plan; do not scale or load-test.
  --confirm   Required for a real run because it changes deployment replica counts.

Important environment variables:
  SCENARIOS=onoff,pareto
  INTERVENTION_SERVICES=frontend,cartservice,currencyservice,productcatalogservice,recommendationservice,checkoutservice
  REPLICA_LEVELS=2,4       Added replica levels; baseline is always 1.
  REPEATS=3                Repeats each paired workload trace.
  MAX_RUNS_PER_INVOCATION=3 Stop after this many new runs so Jaeger can be reset; 0 means no cap.
  RUN_DURATION_SEC=1800    Duration of each Locust run.
  COOLDOWN_SEC=120         Idle time between runs.
  POST_ROLLOUT_SETTLE_SEC=90  Wait after rollout before starting load.
  MAX_TARGET_REPLICAS=4    Safety ceiling for the target deployment.
  TRACE_SAMPLING_PERCENTAGE=10   Keep fixed at the deployed 10% sampling rate.
  SSH_USER, SSH_KEY, REMOTE_DIR, REMOTE_RESULTS_DIR, LOCAL_RESULTS_DIR
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --confirm) CONFIRMED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
if (( DRY_RUN && CONFIRMED )); then
  echo "Choose either --dry-run or --confirm, not both." >&2
  exit 2
fi
if (( ! DRY_RUN && ! CONFIRMED )); then
  echo "A real cluster run requires --confirm. Use --dry-run to inspect the plan first." >&2
  exit 2
fi
[[ "$TRACE_SAMPLING_PERCENTAGE" == "10" ]] || {
  echo "TRACE_SAMPLING_PERCENTAGE must remain 10; this study must not change the production Istio sampling rate." >&2
  exit 2
}

command -v python3 >/dev/null 2>&1 || { echo "Required command not found: python3" >&2; exit 1; }
if (( ! DRY_RUN )); then
  for cmd in kubectl ssh rsync; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "Required command not found: $cmd" >&2; exit 1; }
  done
  [[ -r "$SSH_KEY" ]] || { echo "SSH key is not readable: $SSH_KEY" >&2; exit 1; }
fi

IFS=',' read -r -a SCENARIO_LIST <<< "$SCENARIOS"
IFS=',' read -r -a SERVICE_LIST <<< "$INTERVENTION_SERVICES"
IFS=',' read -r -a LEVEL_LIST <<< "$REPLICA_LEVELS"

is_integer() { [[ "$1" =~ ^[0-9]+$ ]]; }
for value in "$REPEATS" "$MAX_RUNS_PER_INVOCATION" "$RUN_DURATION_SEC" "$COOLDOWN_SEC" "$POST_ROLLOUT_SETTLE_SEC" "$STEP_SEC" \
             "$SEED_BASE" "$MAX_CONCURRENT" "$ROLLOUT_WAIT_SEC" "$MAX_TARGET_REPLICAS" "$TRACE_SAMPLING_PERCENTAGE"; do
  is_integer "$value" || { echo "Expected a non-negative integer, got: $value" >&2; exit 2; }
done
(( REPEATS > 0 && RUN_DURATION_SEC > 0 && STEP_SEC > 0 && MAX_CONCURRENT > 0 )) \
  || { echo "REPEATS, RUN_DURATION_SEC, STEP_SEC, and MAX_CONCURRENT must be positive." >&2; exit 2; }
(( TRACE_SAMPLING_PERCENTAGE >= 0 && TRACE_SAMPLING_PERCENTAGE <= 100 )) \
  || { echo "TRACE_SAMPLING_PERCENTAGE must be in [0,100]." >&2; exit 2; }

declare -A UNIQUE
for scenario in "${SCENARIO_LIST[@]}"; do
  [[ " ${VALID_SCENARIOS[*]} " == *" ${scenario} "* ]] \
    || { echo "Unsupported intervention scenario: ${scenario}" >&2; exit 2; }
  [[ -z "${UNIQUE[scenario_${scenario}]:-}" ]] \
    || { echo "Duplicate scenario: ${scenario}" >&2; exit 2; }
  UNIQUE[scenario_${scenario}]=1
done
(( ${#SCENARIO_LIST[@]} > 0 )) || { echo "SCENARIOS cannot be empty." >&2; exit 2; }
for service in "${SERVICE_LIST[@]}"; do
  [[ " ${APP_DEPLOYMENTS[*]} " == *" ${service} "* ]] \
    || { echo "Unsupported or non-scalable service: ${service}" >&2; exit 2; }
  [[ -z "${UNIQUE[service_${service}]:-}" ]] \
    || { echo "Duplicate intervention service: ${service}" >&2; exit 2; }
  UNIQUE[service_${service}]=1
done
(( ${#SERVICE_LIST[@]} > 0 )) || { echo "INTERVENTION_SERVICES cannot be empty." >&2; exit 2; }
for level in "${LEVEL_LIST[@]}"; do
  is_integer "$level" && (( level > 1 && level <= MAX_TARGET_REPLICAS )) \
    || { echo "Replica levels must be integers in [2, ${MAX_TARGET_REPLICAS}], got: ${level}" >&2; exit 2; }
  [[ -z "${UNIQUE[level_${level}]:-}" ]] || { echo "Duplicate replica level: ${level}" >&2; exit 2; }
  UNIQUE[level_${level}]=1
done
(( ${#LEVEL_LIST[@]} > 0 )) || { echo "REPLICA_LEVELS cannot be empty." >&2; exit 2; }

mkdir -p "$LOCAL_RESULTS_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${LOCAL_RESULTS_DIR}/controller_${TIMESTAMP}.log"
exec > >(tee -a "$LOG_FILE") 2>&1

PLAN_TSV="$(mktemp)"
trap 'rm -f "$PLAN_TSV"' EXIT
python3 - "$PLAN_TSV" "$SCENARIOS" "$INTERVENTION_SERVICES" "$REPLICA_LEVELS" \
  "$REPEATS" "$SEED_BASE" <<'PY'
import csv
import random
import sys

path, scenarios, services, levels, repeats, seed_base = sys.argv[1:]
scenarios = scenarios.split(",")
services = services.split(",")
levels = [int(value) for value in levels.split(",")]
repeats = int(repeats)
seed_base = int(seed_base)

with open(path, "w", newline="") as output:
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    seed_offsets = {"onoff": 0, "pareto": 1000}
    for scenario in scenarios:
        seed_offset = seed_offsets[scenario]
        workload_seed = seed_base + seed_offset
        for repeat in range(1, repeats + 1):
            conditions = [("baseline", "baseline", 1)]
            conditions.extend(
                (f"{service}-r{replicas}", service, replicas)
                for service in services
                for replicas in levels
            )
            random.Random(seed_base + seed_offset * 100 + repeat).shuffle(conditions)
            pair_id = f"{scenario}_repeat_{repeat:02d}"
            for order, (condition, service, replicas) in enumerate(conditions, start=1):
                writer.writerow([
                    scenario, repeat, workload_seed, condition, service,
                    replicas, order, pair_id,
                ])
PY

PLAN_CSV="${LOCAL_RESULTS_DIR}/intervention_plan_${TIMESTAMP}.csv"
python3 - "$PLAN_TSV" "$PLAN_CSV" <<'PY'
import csv
import sys

source, destination = sys.argv[1:]
with open(source, newline="") as input_file, open(destination, "w", newline="") as output_file:
    writer = csv.writer(output_file)
    writer.writerow([
        "scenario", "repeat", "seed", "condition", "target_service",
        "target_replicas", "order", "pair_id",
    ])
    for row in csv.reader(input_file, delimiter="\t"):
        writer.writerow(row)
PY

TOTAL_RUNS="$(wc -l < "$PLAN_TSV" | tr -d ' ')"
BATCH_DISPLAY="$MAX_RUNS_PER_INVOCATION"
(( BATCH_DISPLAY > 0 )) || BATCH_DISPLAY="all"
echo "Plan: ${TOTAL_RUNS} runs; max ${BATCH_DISPLAY} new runs per invocation; ${RUN_DURATION_SEC}s per run; results: ${LOCAL_RESULTS_DIR}"
echo "Plan CSV: ${PLAN_CSV}"
echo "Jaeger sampling is fixed at 10%; use Prometheus as the primary telemetry source and treat Jaeger spans as sampled diagnostics."
cat "$PLAN_CSV"

if (( DRY_RUN )); then
  echo "Dry run complete: no cluster scaling or load generation was performed."
  exit 0
fi

SSH_TARGET="${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}"
SSH_OPTIONS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10)
remote_exec() {
  local command
  printf -v command '%q ' "$@"
  ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" "$command"
}
RSYNC_SSH="ssh -i '${SSH_KEY}' -o BatchMode=yes"

kubectl get deployments -n "$NAMESPACE" >/dev/null
assert_no_hpa() {
  local hpas
  hpas="$(kubectl get hpa -n "$NAMESPACE" -o name)"
  [[ -z "$hpas" ]] || {
    echo "HPA is active in ${NAMESPACE}; refusing to run fixed-replica interventions:" >&2
    echo "$hpas" >&2
    echo "Disable it first with: bash cluster-setup/07-setup-hpa.sh delete" >&2
    return 1
  }
}
assert_no_hpa

remote_exec bash -lc \
  "set -e; test -x '${REMOTE_DIR}/.venv/bin/locust'; test -x '${REMOTE_DIR}/.venv/bin/python'; '${REMOTE_DIR}/.venv/bin/python' -c 'import requests'; test -f '${REMOTE_DIR}/collect_metrics.py'; test -f '${REMOTE_DIR}/run-intervention-loadgen.sh'; curl -fsS --connect-timeout 5 --max-time 10 '${FRONTEND_URL}' -o /dev/null; curl -fsS --connect-timeout 5 --max-time 10 'http://${NODE_OBSERVABILITY_FIXED_IP}:9090/-/ready' -o /dev/null; curl -fsS --connect-timeout 5 --max-time 10 'http://${NODE_OBSERVABILITY_FIXED_IP}:16686/api/services' -o /dev/null"

declare -A ORIGINAL_REPLICAS
SNAPSHOT_READY=0
CLUSTER_CHANGED=0
for deployment in "${APP_DEPLOYMENTS[@]}"; do
  replicas="$(kubectl get deployment "$deployment" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')"
  is_integer "$replicas" || { echo "Cannot read replica count for deployment/${deployment}" >&2; exit 1; }
  ORIGINAL_REPLICAS[$deployment]="$replicas"
done
SNAPSHOT_READY=1

restore_cluster() {
  local status=$?
  trap - EXIT INT TERM
  if (( SNAPSHOT_READY && CLUSTER_CHANGED )); then
    echo "Restoring original deployment replica counts..."
    for deployment in "${APP_DEPLOYMENTS[@]}"; do
      kubectl scale "deployment/${deployment}" -n "$NAMESPACE" \
        --replicas="${ORIGINAL_REPLICAS[$deployment]}" || \
        echo "WARNING: failed to restore deployment/${deployment}" >&2
    done
    for deployment in "${APP_DEPLOYMENTS[@]}"; do
      kubectl rollout status "deployment/${deployment}" -n "$NAMESPACE" \
        --timeout="${ROLLOUT_WAIT_SEC}s" || \
        echo "WARNING: deployment/${deployment} did not finish restoring before timeout" >&2
    done
  fi
  exit "$status"
}
trap restore_cluster EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_deployments() {
  local deployment
  for deployment in "${APP_DEPLOYMENTS[@]}"; do
    kubectl rollout status "deployment/${deployment}" -n "$NAMESPACE" \
      --timeout="${ROLLOUT_WAIT_SEC}s"
  done
}

verify_replica_configuration() {
  local target_service="$1" target_replicas="$2" deployment expected actual
  for deployment in "${APP_DEPLOYMENTS[@]}"; do
    expected=1
    if [[ "$deployment" == "$target_service" ]]; then
      expected="$target_replicas"
    fi
    actual="$(kubectl get deployment "$deployment" -n "$NAMESPACE" \
      -o jsonpath='{.spec.replicas}:{.status.readyReplicas}')"
    [[ "$actual" == "${expected}:${expected}" ]] || {
      echo "Replica state mismatch for ${deployment}: expected ${expected}:${expected}, got ${actual}" >&2
      return 1
    }
  done
}

run_index=0
completed_count=0
new_run_count=0
batch_paused=0
while IFS=$'\t' read -r scenario repeat seed condition target_service target_replicas order pair_id; do
  run_index=$((run_index + 1))
  remote_run_dir="${REMOTE_RESULTS_DIR}/${scenario}/repeat_$(printf '%02d' "$repeat")/${condition}"
  local_run_dir="${LOCAL_RESULTS_DIR}/${scenario}/repeat_$(printf '%02d' "$repeat")/${condition}"
  if [[ -e "$local_run_dir" ]]; then
    if [[ -s "${local_run_dir}/meta.json" && -s "${local_run_dir}/collect_report.json" \
          && -s "${local_run_dir}/jaeger_server_spans.csv" && -s "${local_run_dir}/arrival_trace.csv" \
          && -s "${local_run_dir}/driver_report.json" ]] \
      && python3 - "$local_run_dir" "$scenario" "$seed" "$RUN_DURATION_SEC" "$STEP_SEC" \
        "$MAX_CONCURRENT" "$target_service" "$target_replicas" "$repeat" "$order" "$pair_id" \
        "$SCRIPT_DIR" <<'PY'
import hashlib
import csv
import json
import pathlib
import sys

run_dir = pathlib.Path(sys.argv[1])
scenario, seed, duration, step, max_concurrent, target, replicas, repeat, order, pair_id = sys.argv[2:12]
load_test_dir = pathlib.Path(sys.argv[12])
meta = json.loads((run_dir / "meta.json").read_text())
report = json.loads((run_dir / "collect_report.json").read_text())
driver = json.loads((run_dir / "driver_report.json").read_text())
expected_service = None if target == "baseline" else target
code_files = [
    load_test_dir / "locustfile.py",
    load_test_dir / "arrival_models.py",
    load_test_dir / "collect_metrics.py",
    load_test_dir / "run-intervention-loadgen.sh",
    load_test_dir / "scenarios" / f"{scenario}.py",
]
expected_code_hashes = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in code_files
}
valid = (
    meta.get("scenario") == scenario
    and meta.get("seed") == int(seed)
    and meta.get("duration_sec") == int(duration)
    and meta.get("step_sec") == int(step)
    and meta.get("max_concurrent") == int(max_concurrent)
    and meta.get("intervention_service") == expected_service
    and meta.get("intervention_replicas") == int(replicas)
    and meta.get("repeat") == int(repeat)
    and meta.get("randomized_order") == int(order)
    and meta.get("pair_id") == pair_id
    and str(meta.get("trace_sampling_percentage")) == str(__import__("os").environ.get("TRACE_SAMPLING_PERCENTAGE", "10"))
    and meta.get("trace_sha256")
    and meta.get("trace_sha256") == hashlib.sha256((run_dir / "arrival_trace.csv").read_bytes()).hexdigest()
    and meta.get("code_sha256") == expected_code_hashes
    and meta.get("controller_script_sha256")
    == hashlib.sha256((load_test_dir / "run-intervention.sh").read_bytes()).hexdigest()
    and report.get("jaeger_truncated_windows", 0) == 0
    and report.get("raw_server_spans", 0) > 0
    and len(report.get("jaeger_services", {})) == 10
    and driver.get("dropped", 0) == 0
)
if valid:
    expected_services = {
        "adservice", "cartservice", "checkoutservice", "currencyservice",
        "emailservice", "frontend", "paymentservice", "productcatalogservice",
        "recommendationservice", "shippingservice",
    }
    expected_replicas = int(replicas) if target != "baseline" else 1
    seen = set()
    with (run_dir / "node_metrics.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        service = row.get("service")
        if service not in expected_services:
            continue
        seen.add(service)
        try:
            actual = float(row["replicas"])
            desired = float(row["replicas_desired"])
        except (KeyError, TypeError, ValueError):
            valid = False
            break
        if abs(actual - expected_replicas) > 1e-6 or abs(desired - expected_replicas) > 1e-6:
            valid = False
            break
    valid = valid and bool(rows) and seen == expected_services
sys.exit(0 if valid else 1)
PY
    then
      echo "Skipping already-collected run ${scenario}/${condition}; metadata and quick quality checks match."
      completed_count=$((completed_count + 1))
      continue
    else
      echo "Existing output is incomplete, invalid, or belongs to a different plan: ${local_run_dir}" >&2
      echo "Preserve/review it, then use a new LOCAL_RESULTS_DIR and REMOTE_RESULTS_DIR for a fresh study." >&2
      exit 1
    fi
  fi

  if (( MAX_RUNS_PER_INVOCATION > 0 && new_run_count >= MAX_RUNS_PER_INVOCATION )); then
    batch_paused=1
    break
  fi
  assert_no_hpa
  echo
  echo "=== [${run_index}/${TOTAL_RUNS}] ${scenario} ${condition} repeat=${repeat} seed=${seed} ==="
  remote_exec test '!' -e "$remote_run_dir"

  CLUSTER_CHANGED=1
  for deployment in "${APP_DEPLOYMENTS[@]}"; do
    kubectl scale "deployment/${deployment}" -n "$NAMESPACE" --replicas=1
  done
  if [[ "$target_service" != "baseline" ]]; then
    kubectl scale "deployment/${target_service}" -n "$NAMESPACE" \
      --replicas="$target_replicas"
  fi
  wait_for_deployments
  verify_replica_configuration "$target_service" "$target_replicas"
  if (( POST_ROLLOUT_SETTLE_SEC > 0 )); then
    echo "Settling cluster for ${POST_ROLLOUT_SETTLE_SEC}s after rollout..."
    sleep "$POST_ROLLOUT_SETTLE_SEC"
    verify_replica_configuration "$target_service" "$target_replicas"
  fi

  remote_exec env "TRACE_SAMPLING_PERCENTAGE=${TRACE_SAMPLING_PERCENTAGE}" \
    bash "${REMOTE_DIR}/run-intervention-loadgen.sh" \
    "$scenario" "$seed" "$RUN_DURATION_SEC" "$STEP_SEC" "$MAX_CONCURRENT" \
    "$remote_run_dir" "$target_service" "$target_replicas" "$repeat" "$order" "$pair_id"
  mkdir -p "$(dirname "$local_run_dir")"
  rsync -a -e "$RSYNC_SSH" "${SSH_TARGET}:${remote_run_dir}/" "${local_run_dir}/"
  python3 - "$local_run_dir" "${SCRIPT_DIR}/run-intervention.sh" <<'PY'
import hashlib
import json
import pathlib
import sys

run_dir = pathlib.Path(sys.argv[1])
script = pathlib.Path(sys.argv[2])
meta_path = run_dir / "meta.json"
meta = json.loads(meta_path.read_text())
load_test_dir = script.parent
scenario = meta["scenario"]
code_files = [
    load_test_dir / "locustfile.py",
    load_test_dir / "arrival_models.py",
    load_test_dir / "collect_metrics.py",
    load_test_dir / "run-intervention-loadgen.sh",
    load_test_dir / "scenarios" / f"{scenario}.py",
]
expected_code_hashes = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in code_files
}
if meta.get("code_sha256") != expected_code_hashes:
    raise SystemExit("node-loadgen code is out of sync; rerun cluster-setup/06-setup-loadgen.sh")
meta["controller_script_sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
meta_path.write_text(json.dumps(meta, indent=2) + "\n")
PY
  [[ -s "${local_run_dir}/meta.json" && -s "${local_run_dir}/collect_report.json" ]] \
    || { echo "Missing metadata or collector report in ${local_run_dir}" >&2; exit 1; }

  python3 - "$local_run_dir" <<'PY'
import hashlib
import json
import pathlib
import sys

run_dir = pathlib.Path(sys.argv[1])
report = json.loads((run_dir / "collect_report.json").read_text())
driver_path = run_dir / "driver_report.json"
driver = json.loads(driver_path.read_text()) if driver_path.exists() else {}
meta = json.loads((run_dir / "meta.json").read_text())
issues = []
if len(report.get("jaeger_services", {})) != 10:
    issues.append(f"Jaeger services={len(report.get('jaeger_services', {}))}, expected 10")
if report.get("jaeger_truncated_windows", 0):
    issues.append(f"Jaeger truncated windows={report['jaeger_truncated_windows']}")
if not report.get("raw_server_spans", 0):
    issues.append("no raw server spans")
if driver.get("dropped", 0):
    issues.append(f"driver dropped={driver['dropped']}")
trace_path = run_dir / "arrival_trace.csv"
if not meta.get("trace_sha256") or not trace_path.is_file() \
   or meta["trace_sha256"] != hashlib.sha256(trace_path.read_bytes()).hexdigest():
    issues.append("arrival trace missing or hash mismatch")
if not report.get("node_rows", 0):
    issues.append("no Prometheus node rows")
expected_services = {
    "adservice", "cartservice", "checkoutservice", "currencyservice",
    "emailservice", "frontend", "paymentservice", "productcatalogservice",
    "recommendationservice", "shippingservice",
}
expected_replicas = int(meta.get("intervention_replicas", 1)) if meta.get("intervention_service") else 1
seen = set()
try:
    import csv
    with (run_dir / "node_metrics.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        service = row.get("service")
        if service not in expected_services:
            continue
        seen.add(service)
        replicas = float(row["replicas"])
        desired = float(row["replicas_desired"])
        if abs(replicas - expected_replicas) > 1e-6 or abs(desired - expected_replicas) > 1e-6:
            issues.append(f"replica drift for {service}: expected {expected_replicas}, got {replicas}/{desired}")
            break
    if not rows:
        issues.append("node_metrics.csv is empty")
    elif seen != expected_services:
        issues.append(f"node metrics missing services: {sorted(expected_services - seen)}")
except (OSError, KeyError, TypeError, ValueError) as exc:
    issues.append(f"cannot validate replica trajectory: {exc}")
if issues:
    print("QUALITY WARNING:", "; ".join(issues))
    raise SystemExit(1)
else:
    print(f"Quality quick-check: raw spans={report['raw_server_spans']}, "
          f"Jaeger truncation=0, dropped={driver.get('dropped', 0)}")
PY

  echo "Saved run: ${local_run_dir}"
  completed_count=$((completed_count + 1))
  new_run_count=$((new_run_count + 1))
  if (( run_index < TOTAL_RUNS && COOLDOWN_SEC > 0 )); then
    echo "Cooldown ${COOLDOWN_SEC}s..."
    sleep "$COOLDOWN_SEC"
  fi
done < "$PLAN_TSV"

if (( batch_paused )); then
  echo
  echo "Batch limit reached: ${completed_count}/${TOTAL_RUNS} runs are present and validated."
  echo "Reset Jaeger only after verifying the saved CSVs, then rerun the same command."
  echo "Completed runs are skipped; the remaining randomized plan and workload seeds stay unchanged."
  exit 0
fi

python3 - "$LOCAL_RESULTS_DIR" "$SCENARIOS" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
scenarios = sys.argv[2].split(",")
failed = False
for scenario in scenarios:
    metadata_files = sorted((root / scenario).glob("repeat_*/**/meta.json"))
    hashes = {
        json.loads(path.read_text()).get("trace_sha256")
        for path in metadata_files
    }
    if not metadata_files or None in hashes or len(hashes) != 1:
        print(f"QUALITY ERROR: {scenario} trace hashes differ or are missing: "
              f"{len(metadata_files)} runs, {len(hashes)} hashes")
        failed = True
    else:
        print(f"Trace pairing OK: {scenario}, {len(metadata_files)} runs, "
              f"sha256={next(iter(hashes))}")
        by_pair = {}
        sampling = set()
        for path in metadata_files:
            meta = json.loads(path.read_text())
            sampling.add(str(meta.get("trace_sampling_percentage", "unknown")))
            pair = meta.get("pair_id")
            driver_path = path.parent / "driver_report.json"
            driver = json.loads(driver_path.read_text()) if driver_path.is_file() else {}
            by_pair.setdefault(pair, set()).add((driver.get("planned"), driver.get("expected_mean_rps"), driver.get("designed_mean_rps")))
        for pair, values in by_pair.items():
            if len(values) != 1:
                print(f"QUALITY ERROR: {scenario}/{pair} driver workload differs across conditions: {values}")
                failed = True
        if len(sampling) != 1:
            print(f"QUALITY ERROR: {scenario} trace sampling metadata differs: {sampling}")
            failed = True
if failed:
    raise SystemExit(1)
PY

echo
echo "Intervention study complete. Replica counts will be restored by the exit handler."
echo "Results: ${LOCAL_RESULTS_DIR}"
