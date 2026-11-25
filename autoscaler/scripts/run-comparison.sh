#!/usr/bin/env bash
# Kiểm tra NGẮN: chạy cùng một trace tải (bộ sinh tải của dataset) trên nhiều ngữ cảnh điều khiển:
#   hpa         : HPA baseline, áp từ manifest đã chép lên node-app (--hpa-manifest)
#   autoscaler  : controller GAT-GRU + PPO (HPA tắt)
# Kết thúc: cụm về đúng trạng thái trong snapshot (--snap), kể cả khi trạng thái ban đầu là HPA tắt.
# Chạy trên MÁY BUILD, điều khiển qua SSH:
#   node-app     -> set-context.sh (đổi ngữ cảnh), collect-evidence.sh (bằng chứng triển khai)
#   node-loadgen -> run-eval.sh (phát tải bằng load-testing/run-scenario.sh)
#
# Preset (đã chọn seed để trace 15 phút có hình dạng rõ):
#   --preset short-mmpp : mmpp, seed 2030, 900s. Normal/Idle -> High 254-360s -> 9 phút tải thấp
#   --preset short-nhpp : nhpp, seed 2016, 900s. Tải thấp -> đỉnh 76 rps tại ~400s -> giảm
#
# So sánh theo cặp: mọi ngữ cảnh dùng cùng seed nên cùng trace. Mỗi (kịch bản, seed, độ dài) ghi vào
# một thư mục riêng <results-base>/<kịch_bản>-seed<seed>-<độ_dài>s/<ngữ_cảnh>/..., nên run_01 luôn
# có seed = SEED_BASE + 1. Thứ tự ngữ cảnh xoay vòng giữa các lần lặp.
#
# Usage:
#   bash run-comparison.sh --node-app <user@host> --loadgen <user@host> --snap <SNAP_DIR_trên_node-app> \
#        --hpa-manifest <FILE_trên_node-app (bắt buộc khi có ngữ cảnh hpa)> \
#        [--preset short-mmpp|short-nhpp] [--contexts "hpa autoscaler"] [--scenarios "mmpp"] \
#        [--reps 1] [--seed-base 2029] [--duration 900] [--results-base ~/dataset/raw-eval] \
#        [--key ~/.ssh/kltn_autoscaling] [--allow-dummy] [--no-restore] [--dry-run] --yes
#
# CẢNH BÁO: thay đổi cụm dùng chung (xóa HPA, đổi replica). Cần sự đồng ý của người phụ trách cụm.
# Không có --yes script chỉ in kế hoạch rồi thoát.
set -euo pipefail

NODE_APP=""; LOADGEN=""; SNAP=""; HPA_MANIFEST=""; KEY="$HOME/.ssh/kltn_autoscaling"
SCENARIOS="mmpp"; REPS=1; SEED_BASE=2029; DURATION=900
CONTEXTS="hpa autoscaler"; RESULTS_BASE='~/dataset/raw-eval'; EVIDENCE_BASE='~/evidence'
ALLOW_DUMMY=0; RESTORE=1; DRY=0; YES=0
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preset)
      case "$2" in
        short-mmpp) SCENARIOS="mmpp"; SEED_BASE=2029; DURATION=900; REPS=1 ;;
        short-nhpp) SCENARIOS="nhpp"; SEED_BASE=2015; DURATION=900; REPS=1 ;;
        *) echo "Preset không hợp lệ: $2" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --node-app) NODE_APP="$2"; shift 2 ;;
    --loadgen) LOADGEN="$2"; shift 2 ;;
    --snap) SNAP="$2"; shift 2 ;;
    --hpa-manifest) HPA_MANIFEST="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --contexts) CONTEXTS="$2"; shift 2 ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --reps) REPS="$2"; shift 2 ;;
    --seed-base) SEED_BASE="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --results-base) RESULTS_BASE="$2"; shift 2 ;;
    --allow-dummy) ALLOW_DUMMY=1; shift ;;
    --no-restore) RESTORE=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$NODE_APP" && -n "$LOADGEN" && -n "$SNAP" ]] || { echo "Thiếu --node-app, --loadgen hoặc --snap (xem --help)" >&2; exit 2; }
[[ "$REPS" =~ ^[1-9][0-9]*$ ]] || { echo "--reps phải là số nguyên dương" >&2; exit 2; }
[[ "$DURATION" =~ ^[1-9][0-9]*$ ]] || { echo "--duration phải là số giây" >&2; exit 2; }
for s in $SCENARIOS; do [[ "$s" =~ ^(nhpp|mmpp|pareto|onoff)$ ]] || { echo "Kịch bản không hợp lệ: $s" >&2; exit 2; }; done
for c in $CONTEXTS; do [[ "$c" =~ ^(hpa|autoscaler)$ ]] || { echo "Ngữ cảnh không hợp lệ: $c" >&2; exit 2; }; done
[[ "$CONTEXTS" == *hpa* ]] || echo "LƯU Ý: không có ngữ cảnh hpa, sẽ không có mốc so sánh." >&2
if [[ "$CONTEXTS" == *hpa* && -z "$HPA_MANIFEST" ]]; then
  echo "Ngữ cảnh hpa cần --hpa-manifest <FILE trên node-app> (snapshot chỉ dùng để khôi phục trạng thái ban đầu)" >&2; exit 2
fi

SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
rssh() { local host="$1"; shift; ssh "${SSH_OPTS[@]}" "$host" "$@"; }

FIRST_SEED=$(( SEED_BASE + 1 ))
root_of() { echo "${RESULTS_BASE}/$1-seed${FIRST_SEED}-${DURATION}s"; }   # $1 = kịch bản
read -r -a CTX_ARR <<< "$CONTEXTS"
NCTX=${#CTX_ARR[@]}
NSCEN=$(wc -w <<< "$SCENARIOS")
TOTAL_RUNS=$(( NSCEN * REPS * NCTX ))
EST_MIN=$(( TOTAL_RUNS * (DURATION / 60 + 9) ))     # + xuất telemetry, reset, warm-up controller
if [[ "$ALLOW_DUMMY" == "1" ]]; then PPO_LABEL="gatgru-ppo-dummy"; else PPO_LABEL="gatgru-ppo"; fi

echo "=== Kế hoạch kiểm tra ==="
echo "Ngữ cảnh      : $CONTEXTS"
echo "Kịch bản      : $SCENARIOS"
echo "Seed          : run_k = $SEED_BASE + k (run_01 = seed $FIRST_SEED), cùng trace cho mọi ngữ cảnh"
echo "Độ dài run    : ${DURATION}s, số lần lặp: $REPS"
echo "Tổng số run   : $TOTAL_RUNS, ước tính khoảng $(( EST_MIN / 60 )) giờ $(( EST_MIN % 60 )) phút"
for s in $SCENARIOS; do echo "Kết quả $s   : loadgen:$(root_of "$s")/<ngữ_cảnh>/$s/run_XX"; done
echo "Bằng chứng    : node-app:${EVIDENCE_BASE}/<kịch_bản>-seed${FIRST_SEED}-<ngữ_cảnh>-rep<k>/"
[[ -n "$HPA_MANIFEST" ]] && echo "HPA baseline  : node-app:${HPA_MANIFEST}"
echo "Khôi phục     : node-app:${SNAP} (trạng thái ban đầu)"
if [[ "$ALLOW_DUMMY" == "1" ]]; then echo "LƯU Ý: PPO MẪU (dummy): chỉ chứng minh đường ống chạy, không quy cải thiện cho PPO."; fi
if [[ "$DRY" == "1" ]]; then echo "(dry-run: không thực thi gì)"; exit 0; fi
if [[ "$YES" != "1" ]]; then echo; echo "Chưa chạy. Thêm --yes sau khi đã được người phụ trách cụm đồng ý."; exit 1; fi

# ---------- Preflight ----------
echo "[preflight] SSH tới node-app và loadgen..."
rssh "$NODE_APP" 'sudo -n true' || { echo "node-app: không có sudo không mật khẩu" >&2; exit 1; }
rssh "$LOADGEN" 'test -f ~/load-testing/run-scenario.sh' || { echo "loadgen: chưa rsync load-testing/" >&2; exit 1; }
rssh "$NODE_APP" "test -f $SNAP/hpa.restore.json -a -f $SNAP/replicas.txt" \
  || { echo "node-app: snapshot $SNAP thiếu hpa.restore.json hoặc replicas.txt" >&2; exit 1; }
if [[ "$CONTEXTS" == *hpa* ]]; then
  rssh "$NODE_APP" "grep -q 'kind: *HorizontalPodAutoscaler' $HPA_MANIFEST" \
    || { echo "node-app: không thấy $HPA_MANIFEST hoặc file không chứa HorizontalPodAutoscaler" >&2; exit 1; }
  hpa_max="$(rssh "$NODE_APP" "grep -E '^ *maxReplicas:' $HPA_MANIFEST | awk '{print \$2}' | sort -u | tr '\n' ' '")"
  echo "[preflight] HPA baseline: $(rssh "$NODE_APP" "grep -c 'kind: *HorizontalPodAutoscaler' $HPA_MANIFEST") HPA, maxReplicas = ${hpa_max}"
  [[ "$hpa_max" == "15 " ]] || echo "LƯU Ý: maxReplicas của HPA baseline khác 15 (autoscaler dùng 1..15): so sánh không cùng phạm vi" >&2
fi
if [[ "$CONTEXTS" == *autoscaler* ]]; then
  rssh "$NODE_APP" 'sudo k3s kubectl -n autoscaler get deploy gnn-rl-autoscaler >/dev/null' \
    || { echo "node-app: controller chưa được triển khai (Bước 8 của DEPLOY_AUTOSCALER_MODELS.md)" >&2; exit 1; }
fi

echo "[preflight] đồng bộ script lên node-app và loadgen..."
for host in "$NODE_APP" "$LOADGEN"; do
  # Xóa thư mục tạm trước: nếu còn sót, scp -r sẽ chép lồng vào trong và node chạy bản script cũ.
  rssh "$host" 'rm -rf ~/autoscaler-scripts-new'
  scp -q -i "$KEY" -r "$SCRIPT_DIR" "$host:~/autoscaler-scripts-new"
  rssh "$host" 'rm -rf ~/autoscaler-scripts && mv ~/autoscaler-scripts-new ~/autoscaler-scripts'
done

count_runs() {   # $1=ngữ cảnh $2=kịch bản
  rssh "$LOADGEN" "find $(root_of "$2")/$1/$2 -maxdepth 1 -type d -name 'run_*' 2>/dev/null | wc -l"
}
for s in $SCENARIOS; do
  ref=""
  for c in "${CTX_ARR[@]}"; do
    n="$(count_runs "$c" "$s")"
    if [[ -z "$ref" ]]; then ref="$n"; fi
    if [[ "$n" != "$ref" ]]; then
      echo "Số run đã có trong $(root_of "$s") lệch giữa các ngữ cảnh: seed sẽ không khớp cặp." >&2
      echo "Xóa run dở/thừa trên loadgen rồi chạy lại." >&2
      exit 1
    fi
  done
  if [[ "$ref" != "0" ]]; then echo "[preflight] $s đã có $ref run mỗi ngữ cảnh trong $(root_of "$s"); run mới dùng seed tiếp theo."; fi
done

# Chỉ khôi phục trạng thái ban đầu (snapshot) khi MỌI run đã xong. Khi có lỗi hoặc bị ngắt,
# KHÔNG tự bật HPA: HPA không phải cơ chế dự phòng của autoscaler chủ động. Cụm giữ nguyên ngữ
# cảnh đang chạy để kiểm tra lỗi; người vận hành tự quyết định bước tiếp theo.
COMPLETED=0
on_exit() {
  if [[ "$COMPLETED" != "1" ]]; then
    echo "[cleanup] dừng giữa chừng: KHÔNG khôi phục, KHÔNG bật HPA. Cụm giữ nguyên ngữ cảnh hiện tại." >&2
    echo "[cleanup] Sau khi xử lý lỗi, khôi phục trạng thái ban đầu bằng tay trên node-app:" >&2
    echo "          bash ~/autoscaler-scripts/set-context.sh restore --yes --snap $SNAP" >&2
  elif [[ "$RESTORE" == "1" ]]; then
    echo "[cleanup] hoàn tất: khôi phục trạng thái ban đầu trong snapshot..."
    rssh "$NODE_APP" "bash ~/autoscaler-scripts/set-context.sh restore --yes --snap $SNAP" || \
      echo "[cleanup] KHÔNG khôi phục được tự động; chạy tay: set-context.sh restore --yes --snap $SNAP" >&2
  else
    echo "[cleanup] bỏ qua khôi phục (--no-restore). Cụm đang ở ngữ cảnh cuối cùng."
  fi
}
trap on_exit EXIT

run_context() {  # $1=ngữ cảnh $2=kịch bản $3=lần lặp
  local ctx="$1" scen="$2" rep="$3" tag rc label ctx_args root
  tag="${scen}-seed${FIRST_SEED}-${ctx}-rep${rep}"
  root="$(root_of "$scen")"
  case "$ctx" in
    hpa)        label="hpa";        ctx_args="--hpa-manifest $HPA_MANIFEST" ;;
    autoscaler) label="$PPO_LABEL"; ctx_args=""; if [[ "$ALLOW_DUMMY" == "1" ]]; then ctx_args="--allow-dummy"; fi ;;
  esac

  echo; echo "---- [$scen] lần $rep: ngữ cảnh $ctx ----"
  rssh "$NODE_APP" "bash ~/autoscaler-scripts/set-context.sh $ctx --yes $ctx_args"

  local tagrun="${tag}-$(date +%s)"
  rssh "$LOADGEN" "EVAL_BG=1 TAG='$tagrun' SEED_BASE='$SEED_BASE' RUN_DURATION_SEC='$DURATION' \
      RESULTS_ROOT=$root AUTOSCALER_LABEL='$label' bash ~/autoscaler-scripts/run-eval.sh '$ctx' '$scen' 1"

  local deadline=$(( $(date +%s) + DURATION + 3600 ))
  until rssh "$LOADGEN" "test -f ~/eval-logs/$tagrun.rc"; do
    (( $(date +%s) < deadline )) || { echo "Quá thời gian chờ run $tagrun" >&2; exit 1; }
    sleep 30
  done
  rc="$(rssh "$LOADGEN" "cat ~/eval-logs/$tagrun.rc")"
  if [[ "$rc" != "0" ]]; then
    echo "Run $tagrun thất bại (mã $rc). Xem loadgen:~/eval-logs/$tagrun.log" >&2
    echo "Xóa run dở (thiếu collect_report.json) trong $root/$ctx/$scen/ trước khi chạy lại." >&2
    exit 1
  fi

  if [[ "$ctx" != "hpa" ]]; then
    rssh "$NODE_APP" "bash ~/autoscaler-scripts/collect-evidence.sh ${EVIDENCE_BASE}/${tag} --since $(( DURATION + 900 ))" \
      || echo "Cảnh báo: thu bằng chứng lỗi cho $tag" >&2
  fi
  echo "Run $tagrun xong."
}

for s in $SCENARIOS; do
  for (( rep = 1; rep <= REPS; rep++ )); do
    for (( i = 0; i < NCTX; i++ )); do
      ctx="${CTX_ARR[$(( (i + rep - 1) % NCTX ))]}"      # xoay vòng thứ tự giữa các lần lặp
      run_context "$ctx" "$s" "$rep"
    done
  done
done
COMPLETED=1

echo
echo "=== Hoàn tất ==="
echo "Chuyển (move) kết quả và bằng chứng về repo trên máy build (bản trên node bị xóa sau khi chép):"
echo "  rsync -av --remove-source-files -e 'ssh -i $KEY' $LOADGEN:${RESULTS_BASE}/ dataset/raw-eval/"
echo "  rsync -av --remove-source-files -e 'ssh -i $KEY' $NODE_APP:${EVIDENCE_BASE}/ evidence/"
echo "  ssh -i $KEY $LOADGEN 'find ${RESULTS_BASE} -mindepth 1 -type d -empty -delete'"
echo "  ssh -i $KEY $NODE_APP 'find ${EVIDENCE_BASE} -mindepth 1 -type d -empty -delete'"
echo "Snapshot ($SNAP) ở lại node-app tới khi gỡ xong (Bước 11)."
echo "Tính chỉ số: chạy notebooks/deployment_evaluation/evaluate_short_test.ipynb với ROOTS gồm:"
for s in $SCENARIOS; do
  echo "  dataset/raw-eval/$(basename "$(root_of "$s")")"
done
