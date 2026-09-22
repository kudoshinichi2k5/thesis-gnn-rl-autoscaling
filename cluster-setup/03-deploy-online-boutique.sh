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
  type: NodePort
  nodePort: 30080
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
- **Thay đổi chính:** Cố định NodePort (30080), xóa LoadGenerator, cấu hình Limits nới lỏng cho Python services, ghi đè Probes timeout (60s delay, 5s timeout) trực tiếp qua values.
MD_EOF

rm -rf /tmp/microservices-demo
echo "✅ Hoàn tất cài đặt Online Boutique!"