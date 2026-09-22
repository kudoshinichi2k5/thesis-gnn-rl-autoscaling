#!/bin/bash
set -euo pipefail

NODE_IP="192.168.120.175"
SSH_KEY="$HOME/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"

mkdir -p cluster-setup

# Đảm bảo secret không bao giờ bị commit vào git
touch .gitignore
grep -qxF "cluster-setup/prometheus-remote-token.txt" .gitignore || echo "cluster-setup/prometheus-remote-token.txt" >> .gitignore

echo "[1/3] Cài đặt K3s server (Mạng Pod 10.244.x.x)..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER@$NODE_IP" << 'REMOTE_EOF'
  set -euo pipefail

  # Ghi write-kubeconfig-mode vào config.yaml thay vì chỉ export biến môi
  # trường: script cài đặt của get.k3s.io tự gọi "sudo", và sudo mặc định
  # reset môi trường nên K3S_KUBECONFIG_MODE có thể không được áp dụng.
  # Ghi vào config.yaml đảm bảo quyền 644 luôn đúng, kể cả sau khi k3s
  # service restart (idempotent, không phụ thuộc lần cài đặt đầu tiên).
  sudo mkdir -p /etc/rancher/k3s
  echo 'write-kubeconfig-mode: "644"' | sudo tee /etc/rancher/k3s/config.yaml > /dev/null

  if command -v k3s >/dev/null 2>&1; then
    echo "K3s đã được cài đặt, bỏ qua bước cài mới."
  else
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.34.9+k3s1 INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --tls-san 192.168.120.175 --node-external-ip 192.168.120.175 --cluster-cidr 10.244.0.0/16 --service-cidr 10.96.0.0/16" sh -
  fi

  echo "Đợi K3s API sẵn sàng..."
  for i in $(seq 1 30); do
    if sudo k3s kubectl get --raw='/readyz' >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  # Set biến môi trường vĩnh viễn cho user ubuntu (idempotent, không thêm trùng dòng)
  grep -qxF 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' ~/.bashrc || echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

  cat << 'RBAC_EOF' > /tmp/prometheus-rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: prometheus-remote
  namespace: kube-system
---
apiVersion: v1
kind: Secret
metadata:
  name: prometheus-remote-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: prometheus-remote
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prometheus-remote-kubelet-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:kubelet-api-admin
subjects:
- kind: ServiceAccount
  name: prometheus-remote
  namespace: kube-system
RBAC_EOF

  sudo k3s kubectl apply -f /tmp/prometheus-rbac.yaml

  # Đợi controller điền token vào Secret trước khi đọc
  TOKEN=""
  for i in $(seq 1 15); do
    TOKEN=$(sudo k3s kubectl get secret prometheus-remote-token -n kube-system -o jsonpath='{.data.token}' 2>/dev/null || true)
    [ -n "$TOKEN" ] && break
    sleep 2
  done
  if [ -z "$TOKEN" ]; then
    echo "❌ Không lấy được token cho prometheus-remote sau 30s." >&2
    exit 1
  fi
  echo "$TOKEN" | base64 -d > /tmp/prometheus-remote-token.txt
  chmod 600 /tmp/prometheus-remote-token.txt
REMOTE_EOF

echo "[2/3] Kéo Token của Prometheus về local..."
scp -i "$SSH_KEY" "$SSH_USER@$NODE_IP:/tmp/prometheus-remote-token.txt" cluster-setup/prometheus-remote-token.txt
chmod 600 cluster-setup/prometheus-remote-token.txt

echo "Dọn dẹp token tạm trên remote..."
ssh -i "$SSH_KEY" "$SSH_USER@$NODE_IP" "rm -f /tmp/prometheus-remote-token.txt /tmp/prometheus-rbac.yaml"

echo "[3/3] Kéo Kubeconfig (Giữ nguyên 127.0.0.1 để dùng qua SSH Tunnel)..."
mkdir -p ~/.kube
ssh -i "$SSH_KEY" "$SSH_USER@$NODE_IP" "cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
chmod 600 ~/.kube/config

echo "✅ Hoàn tất cài đặt K3s!"