#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/node-ips.env"
OBS_IP="$NODE_OBSERVABILITY_FLOATING_IP"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

echo "Kiểm tra Istio admission webhook trước khi restart workload..."
bash "${SCRIPT_DIR}/verify-istio-injection.sh" online-boutique

echo "[1/2] Kích hoạt Istio Telemetry (Jaeger tracing) trên k3s..."
kubectl apply -f cluster-setup/istio-tracing.yaml

echo "Cập nhật monitoring-stack/README.md cho thông số Data Collection..."
mkdir -p monitoring-stack
touch monitoring-stack/README.md
if ! grep -qxF "## Jaeger Tracing & Service Graph Data" monitoring-stack/README.md; then
  cat << 'MD_EOF' >> monitoring-stack/README.md

## Jaeger Tracing & Service Graph Data
- **Version:** `jaegertracing/all-in-one:1.60.0`
- **Storage:** Badger DB (Docker named volume `jaeger_data`, xem `docker-compose.yml`)
- **Sampling Rate:** 100% (Thu thập toàn bộ truy vết phục vụ huấn luyện GNN).
MD_EOF
fi

echo "[2/2] Khởi động lại các pod Online Boutique để Envoy nhận cấu hình mới..."
kubectl rollout restart deployment -n online-boutique
while IFS= read -r deployment; do
  [[ -n "$deployment" ]] || continue
  kubectl rollout status "$deployment" -n online-boutique --timeout=180s
done < <(kubectl get deployments -n online-boutique -o name)

echo "Xác nhận sidecar đã được inject sau rollout..."
bash "${SCRIPT_DIR}/verify-istio-injection.sh" online-boutique --check-existing

echo "✅ HOÀN TẤT! Hãy truy cập http://$OBS_IP:16686 để xem Jaeger UI."