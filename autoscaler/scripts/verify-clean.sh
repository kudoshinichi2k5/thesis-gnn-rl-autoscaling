#!/usr/bin/env bash
# Đối chiếu cụm hiện tại với snapshot trước khi apply. Chạy trên node-app (cần sudo).
# Usage: bash verify-clean.sh <SNAP_DIR>
set -euo pipefail

k() { sudo k3s kubectl "$@"; }
NS=online-boutique
SNAP="${1:?Usage: verify-clean.sh <SNAP_DIR>}"
POST="$(mktemp -d)"
trap 'rm -rf "$POST"' EXIT

k get ns -o name | sort                              > "$POST/namespaces.txt"
k get clusterrole,clusterrolebinding -o name | sort  > "$POST/cluster-rbac.txt"
k -n "$NS" get role,rolebinding,sa,cm -o name | sort > "$POST/online-boutique-objects.txt"
sudo k3s ctr images ls -q | sort                     > "$POST/images-node-app.txt"
k -n "$NS" get deploy \
  -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas \
  > "$POST/replicas.txt"

bad=0
# Các danh sách tên phải giống hệt snapshot. replicas.txt chỉ cảnh báo vì HPA điều chỉnh theo tải.
for f in namespaces.txt cluster-rbac.txt online-boutique-objects.txt images-node-app.txt; do
  if diff -u "$SNAP/$f" "$POST/$f" >/dev/null; then
    echo "OK     $f"
  else
    echo "KHÁC   $f"; diff -u "$SNAP/$f" "$POST/$f" || true; bad=1
  fi
done

if diff -u "$SNAP/replicas.txt" "$POST/replicas.txt" >/dev/null; then
  echo "OK     replicas.txt"
else
  echo "LƯU Ý  replicas.txt khác snapshot (có thể do HPA đang scale theo tải):"
  diff -u "$SNAP/replicas.txt" "$POST/replicas.txt" || true
fi

echo
echo "HPA hiện tại:"
k -n "$NS" get hpa || true
echo
echo "PV/PVC (so với $SNAP/pv-pvc.txt; PV local-path có thể cần vài chục giây để dọn):"
k get pv,pvc -A | diff -u "$SNAP/pv-pvc.txt" - || true

exit "$bad"
