#!/bin/bash
# Chạy trực tiếp từ WSL2/Windows
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/node-ips.env"
NODE_IP="$NODE_APP_FLOATING_IP"
SSH_KEY="$HOME/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"
JAEGER_IP="$NODE_OBSERVABILITY_FIXED_IP"
ISTIO_VERSION="1.31.0"

mkdir -p cluster-setup

echo "[1/4] Cài đặt Istio và cấu hình Tracing Provider..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER@$NODE_IP" << REMOTE_EOF
  set -euo pipefail
  mkdir -p ~/.kube
  sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
  sudo chown \$(id -u):\$(id -g) ~/.kube/config
  chmod 600 ~/.kube/config
  export KUBECONFIG=~/.kube/config

  if [ ! -d "istio-${ISTIO_VERSION}" ]; then
    curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=${ISTIO_VERSION} TARGET_ARCH=x86_64 sh -
  fi
  export PATH=\$PWD/istio-${ISTIO_VERSION}/bin:\$PATH

  istioctl install --set profile=minimal \
    --set values.pilot.resources.requests.cpu=200m \
    --set values.pilot.resources.requests.memory=256Mi \
    --set values.pilot.resources.limits.cpu=500m \
    --set values.pilot.resources.limits.memory=512Mi \
    --set values.pilot.nodeSelector.node-role=control-plane \
    --set 'values.pilot.tolerations[0].key=dedicated' \
    --set 'values.pilot.tolerations[0].operator=Equal' \
    --set 'values.pilot.tolerations[0].value=control-plane' \
    --set 'values.pilot.tolerations[0].effect=NoSchedule' \
    --set meshConfig.extensionProviders[0].name=external-jaeger \
    --set meshConfig.extensionProviders[0].zipkin.service=${JAEGER_IP} \
    --set meshConfig.extensionProviders[0].zipkin.port=9411 -y

  kubectl create namespace online-boutique --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace online-boutique istio-injection=enabled --overwrite

  kubectl describe node | grep -A 7 "Allocated resources:" > /tmp/baseline_resources.txt
REMOTE_EOF

echo "[2/4] Đảm bảo file Telemetry (Sampling 100%) tồn tại..."
# istio-tracing.yaml là manifest tĩnh, quản lý trong git tại cluster-setup/.
# Script chỉ tạo nếu CHƯA có — không ghi đè để tránh mất chỉnh sửa thủ công
# và để tránh hai nơi (script + file) cùng là "nguồn sự thật" cho manifest này.
if [ ! -f cluster-setup/istio-tracing.yaml ]; then
  cat << 'YAML_EOF' > cluster-setup/istio-tracing.yaml
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
fi

echo "[3/4] Kéo Baseline Tài nguyên về README..."
scp -i "$SSH_KEY" "$SSH_USER@$NODE_IP:/tmp/baseline_resources.txt" /tmp/baseline_resources.txt
if [ -f cluster-setup/README.md ]; then
  sed -i '/### Baseline Tài nguyên/,$d' cluster-setup/README.md
fi
{
  echo "### Baseline Tài nguyên (Sau khi cài K3s + Istio)"
  echo '```text'
  cat /tmp/baseline_resources.txt
  echo '```'
} >> cluster-setup/README.md

echo "✅ Hoàn tất cài đặt Istio!"