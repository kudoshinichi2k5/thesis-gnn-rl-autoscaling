#!/usr/bin/env bash
# Chuyển cụm sang một NGỮ CẢNH điều khiển để chạy kiểm tra so sánh. Chạy trên node-app (cần sudo).
#
#   bash set-context.sh hpa         --yes --snap <SNAP_DIR>  # HPA truyền thống (khôi phục từ snapshot)
#   bash set-context.sh autoscaler  --yes [--allow-dummy]    # controller GAT-GRU + PPO, HPA tắt
#   bash set-context.sh off         --yes                    # không bộ điều khiển nào, mọi service 1 replica
#
# Mọi ngữ cảnh đều bắt đầu từ CÙNG một trạng thái chuẩn: controller dừng, HPA bị xóa, mọi
# Deployment về 1 replica. Với ngữ cảnh có controller, script chờ CONTROLLER_WARMUP_SEC (200s)
# để controller có đủ 18 bước (180s) lịch sử trước khi phát tải.
#
# CẢNH BÁO: script THAY ĐỔI cụm dùng chung (xóa HPA, đổi replica). Chỉ chạy khi đã được
# người phụ trách cụm đồng ý và đã có snapshot (autoscaler/scripts/snapshot.sh).
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
  sed -n '2,14p' "$0" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
CTX="$1"; shift
YES=0; SNAP=""; ALLOW_DUMMY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --snap) SNAP="${2:?thiếu đường dẫn snapshot}"; shift 2 ;;
    --allow-dummy) ALLOW_DUMMY=1; shift ;;
    *) echo "Tham số không hợp lệ: $1" >&2; usage ;;
  esac
done
[[ "$CTX" =~ ^(hpa|autoscaler|off)$ ]] || usage

if [[ "$YES" != "1" ]]; then
  echo "Script này xóa HPA và đặt mọi Deployment trong $NS_APP về 1 replica." >&2
  echo "Chạy lại với --yes sau khi đã được người phụ trách cụm đồng ý." >&2
  exit 1
fi
if [[ "$CTX" == "hpa" ]]; then
  [[ -n "$SNAP" && -f "$SNAP/hpa.restore.json" ]] \
    || { echo "Ngữ cảnh hpa cần --snap <SNAP_DIR> có hpa.restore.json" >&2; exit 1; }
fi
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

# 2. Xóa HPA, đặt mọi Deployment về 1 replica, chờ ổn định
k -n "$NS_APP" delete hpa --all --ignore-not-found
for d in $(k -n "$NS_APP" get deploy -o jsonpath='{.items[*].metadata.name}'); do
  k -n "$NS_APP" scale deploy/"$d" --replicas=1
done
for d in $(k -n "$NS_APP" get deploy -o jsonpath='{.items[*].metadata.name}'); do
  k -n "$NS_APP" rollout status deploy/"$d" --timeout=180s
done

# 3. Bật đúng một bộ điều khiển
wait_sec="$SETTLE_SEC"
case "$CTX" in
  hpa)
    k apply -f "$SNAP/hpa.restore.json"
    echo "HPA đã bật:"; k -n "$NS_APP" get hpa
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
