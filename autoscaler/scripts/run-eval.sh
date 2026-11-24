#!/usr/bin/env bash
# Chạy một kịch bản tải (cùng bộ sinh tải đã dùng để thu dataset) trong MỘT ngữ cảnh,
# ghi kết quả vào thư mục riêng của ngữ cảnh đó. Chạy trên node-loadgen.
#
#   bash run-eval.sh <ngữ_cảnh> <kịch_bản> [RUNS]
#       ngữ_cảnh : nhãn, ví dụ hpa | autoscaler (chỉ để đặt tên thư mục và gắn nhãn meta.json)
#       kịch_bản : nhpp | mmpp | pareto | onoff
#
# Script này KHÔNG đổi ngữ cảnh của cụm (loadgen không có kubectl). Dùng set-context.sh
# trên node-app trước, hoặc dùng run-comparison.sh để tự động hóa cả hai.
#
# Biến môi trường:
#   SEED_BASE      (2000)  seed của run_k = SEED_BASE + k; KHÁC dataset huấn luyện (1000)
#   RUN_DURATION_SEC (1800), STEP_SEC, MAX_CONCURRENT: chuyển thẳng cho run-scenario.sh
#   COOLDOWN_SEC   (0)     mỗi lần gọi chỉ chạy 1 run nên không cần nghỉ giữa run
#   RESULTS_ROOT   ($HOME/dataset/raw-eval)  gốc kết quả; mỗi ngữ cảnh một thư mục con
#   LT_DIR         ($HOME/load-testing)      nơi đã rsync thư mục load-testing/
#   AUTOSCALER_LABEL  nhãn ghi vào meta.json "autoscaler" (mặc định = ngữ cảnh)
#   EVAL_BG=1      chạy nền (nohup/setsid); trạng thái ở $LOG_DIR/$TAG.rc
#   TAG            tên lần chạy (bắt buộc khi EVAL_BG=1)
set -euo pipefail

CTX="${1:?Usage: run-eval.sh <ngữ_cảnh> <nhpp|mmpp|pareto|onoff> [RUNS]}"
SCENARIO="${2:?thiếu kịch bản}"
RUNS="${3:-1}"
[[ "$SCENARIO" =~ ^(nhpp|mmpp|pareto|onoff)$ ]] || { echo "Kịch bản không hợp lệ: $SCENARIO" >&2; exit 2; }
[[ "$CTX" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Ngữ cảnh chỉ gồm chữ, số, . _ -" >&2; exit 2; }

LT_DIR="${LT_DIR:-$HOME/load-testing}"
RESULTS_ROOT="${RESULTS_ROOT:-$HOME/dataset/raw-eval}"
LOG_DIR="${LOG_DIR:-$HOME/eval-logs}"
RESULTS_DIR="${RESULTS_ROOT}/${CTX}"
LABEL="${AUTOSCALER_LABEL:-$CTX}"
export SEED_BASE="${SEED_BASE:-2000}"
export COOLDOWN_SEC="${COOLDOWN_SEC:-0}"
export RESULTS_DIR

[[ -f "$LT_DIR/run-scenario.sh" ]] || { echo "Không thấy $LT_DIR/run-scenario.sh (đã rsync load-testing/ chưa?)" >&2; exit 1; }

# Chế độ nền: tự chạy lại trong nohup/setsid rồi thoát ngay; kết quả mã thoát ghi vào .rc
if [[ "${EVAL_BG:-0}" == "1" && -z "${EVAL_CHILD:-}" ]]; then
  : "${TAG:?EVAL_BG=1 cần TAG}"
  mkdir -p "$LOG_DIR"
  rm -f "$LOG_DIR/$TAG.rc"
  export RC_FILE="$LOG_DIR/$TAG.rc" EVAL_CHILD=1
  setsid nohup bash -c 'bash "$0" "$@"; echo $? > "$RC_FILE"' "$0" "$CTX" "$SCENARIO" "$RUNS" \
    > "$LOG_DIR/$TAG.log" 2>&1 < /dev/null &
  echo "started $TAG"
  exit 0
fi

mkdir -p "$RESULTS_DIR/$SCENARIO"
before="$(find "$RESULTS_DIR/$SCENARIO" -maxdepth 1 -type d -name 'run_*' -printf '%f\n' 2>/dev/null | sort || true)"

echo "[eval] ctx=$CTX scenario=$SCENARIO runs=$RUNS seed_base=$SEED_BASE results=$RESULTS_DIR"
bash "$LT_DIR/run-scenario.sh" "$SCENARIO" "$RUNS"

after="$(find "$RESULTS_DIR/$SCENARIO" -maxdepth 1 -type d -name 'run_*' -printf '%f\n' 2>/dev/null | sort || true)"
new_runs="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | grep . || true)"

# run-scenario.sh luôn ghi "autoscaler": "hpa" trong meta.json; sửa lại cho đúng ngữ cảnh.
for r in $new_runs; do
  meta="$RESULTS_DIR/$SCENARIO/$r/meta.json"
  [[ -f "$meta" ]] || continue
  python3 - "$meta" "$LABEL" "$CTX" <<'EOF'
import json, sys
path, label, ctx = sys.argv[1:4]
m = json.load(open(path))
m["autoscaler"] = label
m["eval_context"] = ctx
json.dump(m, open(path, "w"), indent=1)
EOF
  echo "[eval] đã gắn nhãn autoscaler=$LABEL cho $SCENARIO/$r"
done
echo "[eval] xong ctx=$CTX scenario=$SCENARIO"
