#!/bin/bash
# Chạy trực tiếp từ WSL2/Windows (thư mục gốc của repo)

echo "[1/5] Đang tải istioctl v1.31.0..."
curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.31.0 TARGET_ARCH=x86_64 sh -
export PATH=$PWD/istio-1.31.0/bin:$PATH

echo "[2/5] Đang cài đặt Istio (profile=minimal) với Resource limits..."
istioctl install --set profile=minimal \
  --set values.pilot.resources.requests.cpu=200m \
  --set values.pilot.resources.requests.memory=256Mi \
  --set values.pilot.resources.limits.cpu=500m \
  --set values.pilot.resources.limits.memory=512Mi -y

echo "[3/5] Tạo namespace online-boutique và bật Istio Sidecar Injection..."
kubectl create namespace online-boutique --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace online-boutique istio-injection=enabled --overwrite

echo "[4/5] Tạo file cấu hình Tracing (Chưa áp dụng)..."
cat << 'YAML_EOF' > cluster-setup/istio-tracing.yaml
# CHÚ Ý: CHỈ APPLY SAU KHI JAEGER ĐÃ CHẠY Ở TUẦN 3
# Traffic từ Envoy sidecar sẽ đi ra ngoài qua NAT của k3s host (node-app) 
# tới trực tiếp IP vật lý của node-observability.
apiVersion: install.istio.io/v1alpha1
kind: IstioOperator
metadata:
  namespace: istio-system
  name: tracing-config
spec:
  meshConfig:
    extensionProviders:
    - name: external-jaeger
      zipkin:
        service: "10.42.0.93"
        port: 9411
---
apiVersion: telemetry.istio.io/v1alpha1
kind: Telemetry
metadata:
  name: mesh-default
  namespace: istio-system
spec:
  tracing:
  - providers:
    - name: external-jaeger
    randomSamplingPercentage: 100.0
YAML_EOF

echo "[5/5] Thu thập số liệu Baseline Tài nguyên Node..."
echo "" >> cluster-setup/README.md
echo "### Baseline Tài nguyên (Sau khi cài K3s + Istio)" >> cluster-setup/README.md
echo '```text' >> cluster-setup/README.md
kubectl describe node | grep -A 7 "Allocated resources:" >> cluster-setup/README.md
echo '```' >> cluster-setup/README.md
echo "*Ghi chú: Traffic tracing từ Envoy sidecar sẽ định tuyến ra ngoài cụm (outbound) thông qua cơ chế NAT của node-app để đến Jaeger (10.42.0.93:9411) trên node-observability, hoàn toàn cô lập khỏi K8s network.*" >> cluster-setup/README.md

echo "[6/6] Commit thay đổi vào Git..."
git add cluster-setup/02-install-istio.sh cluster-setup/istio-tracing.yaml cluster-setup/README.md
git commit -m "chore(infra): install istio minimal with strict limits & prep external tracing"

echo "✅ Hoàn tất cài đặt Istio!"
