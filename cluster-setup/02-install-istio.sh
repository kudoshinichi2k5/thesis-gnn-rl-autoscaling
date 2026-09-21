#!/bin/bash
NODE_IP="192.168.120.175"
SSH_KEY="~/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"

echo "[1/4] Cài đặt Istio và chuẩn bị Namespace..."
ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_USER@$NODE_IP << 'REMOTE_EOF'
  mkdir -p ~/.kube
  sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
  sudo chown $(id -u):$(id -g) ~/.kube/config
  export KUBECONFIG=~/.kube/config

  curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=1.31.0 TARGET_ARCH=x86_64 sh -
  export PATH=$PWD/istio-1.31.0/bin:$PATH

  istioctl install --set profile=minimal \
    --set values.pilot.resources.requests.cpu=200m \
    --set values.pilot.resources.requests.memory=256Mi \
    --set values.pilot.resources.limits.cpu=500m \
    --set values.pilot.resources.limits.memory=512Mi -y

  kubectl create namespace online-boutique --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace online-boutique istio-injection=enabled --overwrite

  kubectl describe node | grep -A 7 "Allocated resources:" > /tmp/baseline_resources.txt
  rm -rf istio-1.31.0
REMOTE_EOF

echo "[2/4] Tạo file cấu hình Tracing (Chưa áp dụng)..."
cat << 'YAML_EOF' > cluster-setup/istio-tracing.yaml
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

echo "[3/4] Kéo Baseline Tài nguyên về README..."
scp -i $SSH_KEY $SSH_USER@$NODE_IP:/tmp/baseline_resources.txt /tmp/baseline_resources.txt
sed -i '/### Baseline Tài nguyên/,$d' cluster-setup/README.md 2>/dev/null
echo "### Baseline Tài nguyên (Sau khi cài K3s + Istio)" >> cluster-setup/README.md
echo '```text' >> cluster-setup/README.md
cat /tmp/baseline_resources.txt >> cluster-setup/README.md
echo '```' >> cluster-setup/README.md

echo "✅ Hoàn tất cài đặt Istio!"