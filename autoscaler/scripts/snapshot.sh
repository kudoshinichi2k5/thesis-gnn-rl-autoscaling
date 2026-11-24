#!/usr/bin/env bash
# Chụp snapshot trạng thái cụm TRƯỚC khi apply autoscaler. Chạy trên node-app (cần sudo).
# Usage: bash snapshot.sh [thư_mục_snapshot]
# In ra đường dẫn snapshot ở dòng cuối.
set -euo pipefail

k() { sudo k3s kubectl "$@"; }
NS=online-boutique
SNAP="${1:-$HOME/snapshots/pre-autoscaler-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$SNAP"

k -n "$NS" get hpa -o yaml    > "$SNAP/hpa.yaml"          # bản đầy đủ, để tham khảo
k -n "$NS" get deploy -o yaml > "$SNAP/deployments.yaml"

# Bản HPA đã bỏ trường runtime (resourceVersion, uid, status...) để khôi phục được bằng "kubectl apply".
k -n "$NS" get hpa -o json | python3 -c '
import json, sys
data = json.load(sys.stdin)
for item in data.get("items", []):
    item.pop("status", None)
    meta = item.get("metadata", {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
        meta.pop(key, None)
out = {"apiVersion": "v1", "kind": "List", "items": data.get("items", [])}
json.dump(out, sys.stdout, indent=2)
' > "$SNAP/hpa.restore.json"

k -n "$NS" get deploy \
  -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas \
  > "$SNAP/replicas.txt"
k -n "$NS" get role,rolebinding,sa,cm -o name | sort > "$SNAP/online-boutique-objects.txt"
k get ns -o name | sort                              > "$SNAP/namespaces.txt"
k get nodes -o wide                                  > "$SNAP/nodes.txt"
k get pv,pvc -A                                      > "$SNAP/pv-pvc.txt"
k get clusterrole,clusterrolebinding -o name | sort  > "$SNAP/cluster-rbac.txt"
k describe nodes                                     > "$SNAP/nodes-describe.txt"
sudo k3s ctr images ls -q | sort                     > "$SNAP/images-node-app.txt"

echo "HPA đang có: $(k -n "$NS" get hpa --no-headers 2>/dev/null | wc -l)"
echo "Snapshot: $SNAP"
