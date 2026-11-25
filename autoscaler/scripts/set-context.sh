#!/usr/bin/env bash
# Chuyển cụm sang một NGỮ CẢNH điều khiển để chạy kiểm tra so sánh. Chạy trên node-app (cần sudo).
#
#   bash set-context.sh hpa        --yes --hpa-manifest <FILE>  # HPA baseline từ manifest (để so sánh)
#   bash set-context.sh autoscaler --yes [--allow-dummy]        # controller GAT-GRU + PPO, HPA tắt
#   bash set-context.sh off        --yes                        # không bộ điều khiển nào, mọi service 1 replica
#   bash set-context.sh restore    --yes --snap <SNAP_DIR>      # về đúng trạng thái trong snapshot
#
# hpa / autoscaler / off bắt đầu từ CÙNG một trạng thái chuẩn: controller dừng, HPA bị xóa, mọi
# Deployment về 1 replica. Ngữ cảnh autoscaler chờ CONTROLLER_WARMUP_SEC (200s) để controller có đủ
# 18 bước (180s) lịch sử trước khi phát tải.
# restore: dừng controller, xóa HPA, áp lại HPA của snapshot (nếu snapshot có HPA), rồi đặt số replica
# mỗi Deployment như trong snapshot. HPA baseline (manifest) và trạng thái ban đầu (snapshot) là hai
# thứ khác nhau: cụm có thể đang tắt HPA (snapshot không có HPA) nhưng vẫn cần HPA để so sánh.
#
# CẢNH BÁO: script THAY ĐỔI cụm dùng chung (xóa HPA, đổi replica). Chỉ chạy khi đã được
# người phụ trách cụm đồng ý và đã có snapshot (snapshot.sh).
set -euo pipefail

k() { sudo k3s kubectl "$@"; }

NS_APP="${NS_APP:-online-boutique}"
NS_AS="${NS_AS:-autoscaler}"
CTRL="gnn-rl-autoscaler"
POLICY_CM="autoscaler-policy"
SETTLE_SEC="${SETTLE_SEC:-60}"
CONTROLLER_WARMUP_SEC="${CONTROLLER_WARMUP_SEC:-200}"
SCALED_SERVICES="${SCALED_SERVICES:-frontend,cartservice,checkoutservice,currencyservice,productcatalogservice,recommendationservice,adservice,emailservice,paymentservice,shippingservice}"

usage() {
  sed -n '2,17p' "$0" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
CTX="$1"; shift
YES=0; SNAP=""; HPA_MANIFEST=""; ALLOW_DUMMY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --snap) SNAP="${2:?thiếu đường dẫn snapshot}"; shift 2 ;;
    --hpa-manifest) HPA_MANIFEST="${2:?thiếu đường dẫn manifest HPA}"; shift 2 ;;
    --allow-dummy) ALLOW_DUMMY=1; shift ;;
    *) echo "Tham số không hợp lệ: $1" >&2; usage ;;
  esac
done
[[ "$CTX" =~ ^(hpa|autoscaler|off|restore)$ ]] || usage
# Đường dẫn truyền qua SSH trong nháy đơn giữ nguyên "~": tự mở rộng ở đây.
SNAP="${SNAP/#\~/$HOME}"
HPA_MANIFEST="${HPA_MANIFEST/#\~/$HOME}"

if [[ "$YES" != "1" ]]; then
  echo "Script này xóa HPA và đổi số replica của các Deployment trong $NS_APP." >&2
  echo "Chạy lại với --yes sau khi đã được người phụ trách cụm đồng ý." >&2
  exit 1
fi

hpa_items() {  # số HPA trong một file List JSON (snapshot)
  python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1])).get("items", [])))' "$1"
}

case "$CTX" in
  hpa)
    [[ -n "$HPA_MANIFEST" && -f "$HPA_MANIFEST" ]] \
      || { echo "Ngữ cảnh hpa cần --hpa-manifest <FILE> (manifest HPA baseline đã chép lên node-app)" >&2; exit 1; }
    grep -q 'kind: *HorizontalPodAutoscaler' "$HPA_MANIFEST" \
      || { echo "$HPA_MANIFEST không chứa HorizontalPodAutoscaler nào" >&2; exit 1; }
    ;;
  restore)
    [[ -n "$SNAP" && -f "$SNAP/hpa.restore.json" && -f "$SNAP/replicas.txt" ]] \
      || { echo "Ngữ cảnh restore cần --snap <SNAP_DIR> có hpa.restore.json và replicas.txt" >&2; exit 1; }
    ;;
esac
USES_CONTROLLER=0
[[ "$CTX" == "autoscaler" ]] && USES_CONTROLLER=1

echo "== set-context: $CTX =="

# 1. Dừng controller (nếu đã cài)
if k get deploy "$CTRL" -n "$NS_AS" >/dev/null 2>&1; then
  k -n "$NS_AS" scale deploy/"$CTRL" --replicas=0
  k -n "$NS_AS" wait --for=delete pod -l app="$CTRL" --timeout=120s 2>/dev/null || true
else
  echo "(controller chưa được cài trong namespace $NS_AS)"
  if [[ "$USES_CONTROLLER" == "1" ]]; then echo "Ngữ cảnh $CTX cần controller đã triển khai" >&2; exit 1; fi
fi

# 2. Xóa HPA
k -n "$NS_APP" delete hpa --all --ignore-not-found

# 3a. restore: về đúng trạng thái trong snapshot rồi kết thúc (không chờ ổn định)
if [[ "$CTX" == "restore" ]]; then
  n="$(hpa_items "$SNAP/hpa.restore.json")"
  if (( n > 0 )); then
    k apply -f "$SNAP/hpa.restore.json"
    echo "Đã khôi phục $n HPA từ snapshot."
  else
    echo "Snapshot không có HPA: để HPA tắt như trạng thái ban đầu."
  fi
  # replicas.txt: NAME DESIRED READY (dòng đầu là tiêu đề)
  while read -r d want _; do
    [[ "$want" =~ ^[0-9]+$ ]] || continue
    if k -n "$NS_APP" get deploy "$d" >/dev/null 2>&1; then
      k -n "$NS_APP" scale deploy/"$d" --replicas="$want"
    fi
  done < <(tail -n +2 "$SNAP/replicas.txt")
  for d in $(k -n "$NS_APP" get deploy -o jsonpath='{.items[*].metadata.name}'); do
    k -n "$NS_APP" rollout status deploy/"$d" --timeout=180s
  done
  k -n "$NS_APP" get hpa 2>/dev/null || true
  k -n "$NS_APP" get deploy -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas
  echo "== xong: đã khôi phục trạng thái trong $SNAP =="
  exit 0
fi

# 3b. hpa / autoscaler / off: đặt mọi Deployment về 1 replica, chờ ổn định
for d in $(k -n "$NS_APP" get deploy -o jsonpath='{.items[*].metadata.name}'); do
  k -n "$NS_APP" scale deploy/"$d" --replicas=1
done
for d in $(k -n "$NS_APP" get deploy -o jsonpath='{.items[*].metadata.name}'); do
  k -n "$NS_APP" rollout status deploy/"$d" --timeout=180s
done

# 4. Bật đúng một bộ điều khiển
wait_sec="$SETTLE_SEC"
case "$CTX" in
  hpa)
    k apply -f "$HPA_MANIFEST"
    n_hpa="$(k -n "$NS_APP" get hpa --no-headers 2>/dev/null | wc -l)"
    (( n_hpa > 0 )) || { echo "Không có HPA nào trong $NS_APP sau khi apply $HPA_MANIFEST" >&2; exit 1; }
    echo "HPA baseline đã bật ($n_hpa HPA):"
    k -n "$NS_APP" get hpa -o custom-columns=NAME:.metadata.name,MIN:.spec.minReplicas,MAX:.spec.maxReplicas
    ;;
  autoscaler)
    mode="gatgru-ppo"
    allow="false"; [[ "$ALLOW_DUMMY" == "1" ]] && allow="true"
    k -n "$NS_AS" patch configmap "$POLICY_CM" --type merge \
      -p "{\"data\":{\"mode\":\"${mode}\",\"scaled_services\":\"${SCALED_SERVICES}\",\"allow_active_with_dummy\":\"${allow}\"}}"
    k -n "$NS_AS" scale deploy/"$CTRL" --replicas=1
    k -n "$NS_AS" rollout status deploy/"$CTRL" --timeout=180s
    echo "Controller đang chạy (mode=${mode}, allow_active_with_dummy=${allow})"
    (( CONTROLLER_WARMUP_SEC > wait_sec )) && wait_sec="$CONTROLLER_WARMUP_SEC"
    ;;
  off)
    echo "Không có bộ điều khiển nào đang bật."
    ;;
esac

echo "Chờ ${wait_sec}s cho cụm ổn định (controller cần 180s lịch sử)..."
sleep "$wait_sec"
k -n "$NS_APP" get deploy -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas
echo "== xong: ngữ cảnh $CTX =="
