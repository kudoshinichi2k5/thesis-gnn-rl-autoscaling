#!/bin/bash
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
OB_DIR="k8s-manifests/online-boutique"
mkdir -p "$OB_DIR/chart"

echo "[1/4] Clone repository và Vendor Helm Chart..."
rm -rf /tmp/microservices-demo
git clone --depth 1 https://github.com/GoogleCloudPlatform/microservices-demo.git /tmp/microservices-demo
CURRENT_HASH=$(cd /tmp/microservices-demo && git rev-parse HEAD)
rsync -av --delete /tmp/microservices-demo/helm-chart/ "$OB_DIR/chart/"

echo "[2/4] Xóa cứng loadgenerator..."
rm -f "$OB_DIR/chart/templates/loadgenerator.yaml"

echo "[3/4] Cấu hình values-override.yaml..."
cat << 'YAML_EOF' > "$OB_DIR/values-override.yaml"
frontend:
  # Chart chính thức của Online Boutique KHÔNG có key frontend.type/nodePort
  # — Helm âm thầm bỏ qua nếu khai báo (đã xác minh trực tiếp trong
  # templates/frontend.yaml). frontend luôn là ClusterIP (hardcode); expose
  # ra ngoài do service riêng "frontend-external", type LoadBalancer, cũng
  # hardcode, bật/tắt bằng frontend.externalService (mặc định true).
  # Cần K3s servicelb bật (xem 01-install-server.sh) để LoadBalancer nhận
  # được EXTERNAL-IP trên bare-metal.
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }

# Cấu hình tài nguyên chung
adservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
cartservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
checkoutservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
currencyservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
paymentservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
productcatalogservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
shippingservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }

# Cấu hình Đặc biệt cho Python Services: Nới lỏng CPU Limits và Probes
emailservice:
  resources: { requests: { cpu: 100m, memory: 128Mi }, limits: { cpu: 1000m, memory: 512Mi } }
  livenessProbe:
    initialDelaySeconds: 60
    timeoutSeconds: 5
    periodSeconds: 5
  readinessProbe:
    initialDelaySeconds: 60
    timeoutSeconds: 5
    periodSeconds: 5

recommendationservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 1000m, memory: 512Mi } }
  livenessProbe:
    initialDelaySeconds: 60
    timeoutSeconds: 5
    periodSeconds: 5
  readinessProbe:
    initialDelaySeconds: 60
    timeoutSeconds: 5
    periodSeconds: 5
YAML_EOF

echo "[4/4] Deploy Online Boutique..."
helm upgrade --install online-boutique "$OB_DIR/chart/" \
  -n online-boutique \
  -f "$OB_DIR/values-override.yaml" \
  --wait --timeout 5m

echo "Tạo file README..."
cat << MD_EOF > "$OB_DIR/README.md"
# Online Boutique - Microservices Benchmark
- **Vendor từ:** \`GoogleCloudPlatform/microservices-demo\`
- **Commit hash:** \`${CURRENT_HASH}\`
- **Thay đổi chính:** Xóa LoadGenerator, cấu hình Limits nới lỏng cho Python services, ghi đè Probes timeout (60s delay, 5s timeout) trực tiếp qua values. Frontend expose qua service \`frontend-external\` (LoadBalancer, do K3s servicelb cấp EXTERNAL-IP) — chart không hỗ trợ NodePort qua values.
MD_EOF

rm -rf /tmp/microservices-demo
echo "✅ Hoàn tất cài đặt Online Boutique!"