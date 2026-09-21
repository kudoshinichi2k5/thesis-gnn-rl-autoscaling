#!/bin/bash
# Chạy từ WSL2/Windows. Lệnh cấu hình sẽ được đẩy qua SSH vào node-app.

NODE_IP="192.168.120.175"
SSH_KEY="~/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"

echo "[1/5] Kết nối SSH vào node-app để cài đặt Istio..."
ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_USER@$NODE_IP << 'REMOTE_EOF'
  # SỬA LỖI: Copy kubeconfig và cấp quyền cho user ubuntu thay vì đọc file của root
  mkdir -p ~/.kube
  sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
  sudo chown $(id -u):$(id -g) ~/.kube/config
  export KUBECONFIG=~/.kube/config

  echo "--> Tải istioctl v1.31.0..."
  curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=1.31.0 TARGET_ARCH=x86_64 sh -
  export PATH=$PWD/istio-1.31.0/bin:$PATH

  echo "--> Cài đặt Istio (profile=minimal) với Resource limits..."
  istioctl install --set profile=minimal \
    --set values.pilot.resources.requests.cpu=200m \
    --set values.pilot.resources.requests.memory=256Mi \
    --set values.pilot.resources.limits.cpu=500m \
    --set values.pilot.resources.limits.memory=512Mi -y

  echo "--> Tạo namespace online-boutique và bật Istio Sidecar Injection..."
  kubectl create namespace online-boutique --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace online-boutique istio-injection=enabled --overwrite

  echo "--> Thu thập Baseline Tài nguyên Node..."
  kubectl describe node | grep -A 7 "Allocated resources:" > /tmp/baseline_resources.txt
REMOTE_EOF

echo "[2/5] Tạo file cấu hình Tracing (Chưa áp dụng)..."
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

echo "[3/5] Kéo thông số Baseline về local và cập nhật README..."
scp -i $SSH_KEY $SSH_USER@$NODE_IP:/tmp/baseline_resources.txt /tmp/baseline_resources.txt

# Xóa text baseline cũ trong README nếu có
sed -i '/### Baseline Tài nguyên/,$d' cluster-setup/README.md 2>/dev/null

echo "### Baseline Tài nguyên (Sau khi cài K3s + Istio)" >> cluster-setup/README.md
echo '```text' >> cluster-setup/README.md
cat /tmp/baseline_resources.txt >> cluster-setup/README.md
echo '```' >> cluster-setup/README.md
echo "*Ghi chú: Traffic tracing từ Envoy sidecar sẽ định tuyến ra ngoài cụm (outbound) thông qua cơ chế NAT của node-app để đến Jaeger (10.42.0.93:9411) trên node-observability, hoàn toàn cô lập khỏi K8s network.*" >> cluster-setup/README.md

echo "[4/5] Dọn dẹp..."
rm -rf istio-1.31.0 2>/dev/null

echo "[5/5] Cập nhật lại Git Commit (Fix permission)..."
git add cluster-setup/02-install-istio.sh cluster-setup/istio-tracing.yaml cluster-setup/README.md
git commit --amend --no-edit

echo "✅ Hoàn tất cài đặt Istio từ xa!"
