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

echo "Patch workload anti-affinity for the two application workers..."
cat << 'HELPER_EOF' > "$OB_DIR/chart/templates/_pod-anti-affinity.tpl"
{{- define "onlineboutique.podAntiAffinity" -}}
{{- $policy := .policy -}}
{{- if $policy.enabled }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: {{ $policy.weight }}
      podAffinityTerm:
        topologyKey: {{ $policy.topologyKey | quote }}
        labelSelector:
          matchLabels:
            app: {{ .app | quote }}
{{- end }}
{{- end -}}
HELPER_EOF

cat << 'SCHEDULING_EOF' >> "$OB_DIR/chart/values.yaml"

# Repository-specific scheduling policy consumed by _pod-anti-affinity.tpl.
workloadScheduling:
  podAntiAffinity:
    enabled: true
    weight: 100
    topologyKey: kubernetes.io/hostname
SCHEDULING_EOF

patch_anti_affinity() {
  local template="$1" app_value="$2" occurrence="${3:-1}"
  local temp_file="${template}.tmp"
  awk -v app_value="$app_value" -v occurrence="$occurrence" '
    /^      containers:$/ {
      container_count++
      if (container_count == occurrence) {
        print "      {{- include `onlineboutique.podAntiAffinity` (dict `policy` .Values.workloadScheduling.podAntiAffinity `app` " app_value ") | nindent 6 }}"
      }
    }
    { print }
    END { if (container_count < occurrence) exit 1 }
  ' "$template" > "$temp_file"
  mv "$temp_file" "$template"
}

patch_anti_affinity "$OB_DIR/chart/templates/frontend.yaml" '.Values.frontend.name'
patch_anti_affinity "$OB_DIR/chart/templates/adservice.yaml" '.Values.adService.name'
patch_anti_affinity "$OB_DIR/chart/templates/cartservice.yaml" '.Values.cartService.name' 1
patch_anti_affinity "$OB_DIR/chart/templates/cartservice.yaml" '.Values.cartDatabase.inClusterRedis.name' 2
patch_anti_affinity "$OB_DIR/chart/templates/checkoutservice.yaml" '.Values.checkoutService.name'
patch_anti_affinity "$OB_DIR/chart/templates/currencyservice.yaml" '.Values.currencyService.name'
patch_anti_affinity "$OB_DIR/chart/templates/emailservice.yaml" '.Values.emailService.name'
patch_anti_affinity "$OB_DIR/chart/templates/paymentservice.yaml" '.Values.paymentService.name'
patch_anti_affinity "$OB_DIR/chart/templates/productcatalogservice.yaml" '.Values.productCatalogService.name'
patch_anti_affinity "$OB_DIR/chart/templates/recommendationservice.yaml" '.Values.recommendationService.name'
patch_anti_affinity "$OB_DIR/chart/templates/shippingservice.yaml" '.Values.shippingService.name'
patch_anti_affinity "$OB_DIR/chart/templates/opentelemetry-collector.yaml" '.Values.opentelemetryCollector.name'

echo "[2/4] Xóa cứng loadgenerator..."
rm -f "$OB_DIR/chart/templates/loadgenerator.yaml"

# ▼▼▼ THÊM ĐOẠN NÀY ▼▼▼
echo "Patch probe timing cho emailservice/recommendationservice..."
# Chart KHÔNG expose initialDelaySeconds qua values.yaml cho 2 service này
# (probe hardcode: periodSeconds=5, initialDelaySeconds mặc định=0). Trên
# tài nguyên giới hạn, service khởi động chậm hơn 15s (3 x 5s) sẽ bị kubelet
# kill/restart trước khi kịp sẵn sàng. Patch trực tiếp vì đây là cách duy
# nhất chỉnh được giá trị này — chạy lại mỗi lần vendor nên không mất khi
# rsync --delete ghi đè chart ở bước [1/4].
for svc in emailservice recommendationservice; do
  f="$OB_DIR/chart/templates/${svc}.yaml"
  sed -i '/^        readinessProbe:$/a\          initialDelaySeconds: 60' "$f"
  sed -i '/^        readinessProbe:$/a\          timeoutSeconds: 5' "$f"
  sed -i '/^        livenessProbe:$/a\          initialDelaySeconds: 60' "$f"
  sed -i '/^        livenessProbe:$/a\          timeoutSeconds: 5' "$f"
done
# ▲▲▲ HẾT ĐOẠN THÊM ▲▲▲

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
adService:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
cartService:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
checkoutService:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
currencyService:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
paymentService:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
productCatalogService:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
shippingService:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }

# Chart không expose cấu hình probe qua values; script patch trực tiếp template.
emailService:
  resources: { requests: { cpu: 100m, memory: 128Mi }, limits: { cpu: 1000m, memory: 512Mi } }

recommendationService:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 1000m, memory: 512Mi } }

workloadScheduling:
  podAntiAffinity:
    enabled: true
    weight: 100
    topologyKey: kubernetes.io/hostname
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
- **Thay đổi chính:** Xóa LoadGenerator, chỉnh resource overrides theo key camelCase, patch probes của email/recommendation trực tiếp trong template (60s delay, 5s timeout), và thêm preferred pod anti-affinity để trải workload trên hai worker. Frontend expose qua `frontend-external` (LoadBalancer).
MD_EOF

rm -rf /tmp/microservices-demo
echo "✅ Hoàn tất cài đặt Online Boutique!"