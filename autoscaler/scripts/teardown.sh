#!/usr/bin/env bash
# Gỡ toàn bộ autoscaler khỏi cụm. Chạy trên node-app (cần sudo).
# Usage:
#   bash teardown.sh                          # gỡ khi chỉ dùng shadow (cụm chưa bị thay đổi)
#   bash teardown.sh --restore-hpa <SNAP_DIR> # gỡ và khôi phục HPA từ snapshot (sau khi đã chạy active)
#
# Thứ tự: dừng controller -> (khôi phục HPA) -> xóa Role/RoleBinding -> xóa namespace -> gỡ image.
# Không xóa thư mục ~/autoscaler-bundle và ~/snapshots (xóa tay khi chắc chắn không cần).
set -euo pipefail

k() { sudo k3s kubectl "$@"; }
NS_APP=online-boutique
NS_AS=autoscaler
LABEL=part-of=gnn-rl-autoscaler
IMAGE_PATTERN="gnn-rl-autoscaler"
RESTORE_SNAP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore-hpa) RESTORE_SNAP="${2:?thiếu đường dẫn snapshot}"; shift 2 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$RESTORE_SNAP" && ! -f "$RESTORE_SNAP/hpa.restore.json" ]]; then
  echo "Không thấy $RESTORE_SNAP/hpa.restore.json" >&2; exit 1
fi

echo "[1/5] Dừng controller..."
if k get ns "$NS_AS" >/dev/null 2>&1; then
  k -n "$NS_AS" scale deploy/gnn-rl-autoscaler --replicas=0 2>/dev/null || true
  k -n "$NS_AS" wait --for=delete pod -l app=gnn-rl-autoscaler --timeout=120s 2>/dev/null || true
else
  echo "  namespace $NS_AS không tồn tại, bỏ qua"
fi

if [[ -n "$RESTORE_SNAP" ]]; then
  echo "[2/5] Khôi phục HPA từ $RESTORE_SNAP ..."
  k apply -f "$RESTORE_SNAP/hpa.restore.json"
  k -n "$NS_APP" get hpa
else
  echo "[2/5] Không khôi phục HPA (không truyền --restore-hpa)"
fi

echo "[3/5] Xóa Role/RoleBinding trong $NS_APP theo nhãn..."
k -n "$NS_APP" delete role,rolebinding -l "$LABEL" --ignore-not-found

echo "[4/5] Xóa namespace $NS_AS (kèm PVC và dữ liệu)..."
k delete namespace "$NS_AS" --ignore-not-found --wait=true --timeout=180s

echo "[5/5] Gỡ image khỏi containerd của node-app..."
imgs="$(sudo k3s ctr images ls -q | grep "$IMAGE_PATTERN" || true)"
if [[ -n "$imgs" ]]; then
  while read -r img; do sudo k3s ctr images rm "$img"; done <<< "$imgs"
else
  echo "  không có image khớp '$IMAGE_PATTERN'"
fi

echo "Xong. Chạy verify-clean.sh <SNAP_DIR> để đối chiếu với snapshot."
echo "Nếu đã chạy active, đối chiếu cột DESIRED trong replicas.txt của snapshot với: kubectl -n $NS_APP get deploy"
