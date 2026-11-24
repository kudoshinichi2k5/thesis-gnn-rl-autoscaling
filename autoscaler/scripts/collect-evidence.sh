#!/usr/bin/env bash
# Thu BẰNG CHỨNG controller đã chạy trong cụm và đã ra lệnh scale. Chạy trên node-app (cần sudo).
# Chỉ ĐỌC, không thay đổi cụm.
#
#   bash collect-evidence.sh <OUT_DIR> [--since SECONDS]
#
# Kết quả trong OUT_DIR:
#   pods.txt, controller-deployment.yaml, policy-configmap.yaml   controller có chạy, cấu hình gì
#   controller.log                                                 log controller trong khoảng --since
#   decision-log/                                                  Decision Log (forecast, action, action sau lớp an toàn)
#   model-files.txt                                                checksum + training_meta của model đang mount
#   deployments.json, replica-managers.tsv                         AI đã đặt spec.replicas của từng Deployment (managedFields)
#   events-online-boutique.txt, events-autoscaler.txt              sự kiện scale của Kubernetes
#   hpa.txt, replicas-now.txt, image.txt, top-nodes.txt            trạng thái tại thời điểm thu
set -uo pipefail

k() { sudo k3s kubectl "$@"; }
NS_APP="${NS_APP:-online-boutique}"
NS_AS="${NS_AS:-autoscaler}"
CTRL="gnn-rl-autoscaler"

OUT="${1:?Usage: collect-evidence.sh <OUT_DIR> [--since SECONDS]}"; shift
SINCE=3600
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="${2:?}"; shift 2 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT"
note() { echo "[evidence] $*"; }

date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/collected_at.txt"
k -n "$NS_AS" get pods -o wide                         > "$OUT/pods.txt" 2>&1
k -n "$NS_AS" get deploy "$CTRL" -o yaml               > "$OUT/controller-deployment.yaml" 2>&1
k -n "$NS_AS" get configmap autoscaler-policy -o yaml  > "$OUT/policy-configmap.yaml" 2>&1
k -n "$NS_AS" logs deploy/"$CTRL" --since="${SINCE}s" --timestamps > "$OUT/controller.log" 2>&1 \
  || note "không lấy được log controller (pod đã dừng?)"

pod="$(k -n "$NS_AS" get pod -l app="$CTRL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -n "$pod" ]]; then
  k -n "$NS_AS" cp "$pod:/decision-log" "$OUT/decision-log" >/dev/null 2>&1 \
    && note "đã sao chép Decision Log" || note "không sao chép được /decision-log (container cần có tar)"
  k -n "$NS_AS" exec "$pod" -- sh -c \
    'for d in /models/*/*; do echo "== $d"; cat "$d/SHA256SUMS" 2>/dev/null; cat "$d/training_meta.json" 2>/dev/null; echo; done' \
    > "$OUT/model-files.txt" 2>&1 || note "không đọc được thư mục model"
else
  note "không có pod controller đang chạy: bỏ qua Decision Log và model"
fi

# Ai đã đặt spec.replicas: managedFields ghi manager/thao tác/subresource cuối cùng sở hữu trường.
# Controller PATCH với field_manager=gnn-rl-autoscaler; HPA là kube-controller-manager; kubectl scale là kubectl.
k -n "$NS_APP" get deploy -o json > "$OUT/deployments.json" 2>/dev/null
python3 - "$OUT/deployments.json" > "$OUT/replica-managers.tsv" <<'EOF' || note "không phân tích được managedFields"
import json, sys
data = json.load(open(sys.argv[1]))
print("deployment\tspec.replicas\tmanager\toperation\tsubresource\ttime")
for d in data.get("items", []):
    name = d["metadata"]["name"]
    reps = d.get("spec", {}).get("replicas")
    owners = [m for m in d["metadata"].get("managedFields", [])
              if "f:replicas" in ((m.get("fieldsV1") or {}).get("f:spec") or {})]
    if not owners:
        print("\t".join([name, str(reps), "-", "-", "-", "-"]))
    for m in owners:
        print("\t".join([name, str(reps), str(m.get("manager")), str(m.get("operation")),
                         str(m.get("subresource", "")), str(m.get("time"))]))
EOF

k -n "$NS_APP" get events --sort-by=.lastTimestamp > "$OUT/events-online-boutique.txt" 2>&1
k -n "$NS_AS"  get events --sort-by=.lastTimestamp > "$OUT/events-autoscaler.txt" 2>&1
k -n "$NS_APP" get hpa                             > "$OUT/hpa.txt" 2>&1
k -n "$NS_APP" get deploy -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas \
  > "$OUT/replicas-now.txt" 2>&1
sudo k3s ctr images ls 2>/dev/null | grep gnn-rl-autoscaler > "$OUT/image.txt" || true
k top nodes > "$OUT/top-nodes.txt" 2>&1 || true

n_ctrl="$(grep -c "gnn-rl-autoscaler" "$OUT/replica-managers.tsv" 2>/dev/null)"; n_ctrl="${n_ctrl:-0}"
note "Deployment có spec.replicas do controller đặt: $n_ctrl (xem $OUT/replica-managers.tsv)"
note "xong: $OUT"
