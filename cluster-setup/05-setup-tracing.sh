#!/bin/bash
set -euo pipefail

OBS_IP="192.168.120.80"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

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
kubectl rollout status deployment/frontend -n online-boutique --timeout=120s

echo "✅ HOÀN TẤT! Hãy truy cập http://$OBS_IP:16686 để xem Jaeger UI."