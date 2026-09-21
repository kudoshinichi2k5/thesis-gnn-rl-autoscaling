#!/bin/bash
# Chạy trực tiếp từ WSL2/Windows (thư mục gốc của repo)

NODE_IP="192.168.120.175"
SSH_KEY="~/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"

echo "[1/4] Đang cài đặt K3s server trên node-app ($NODE_IP)..."
ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_USER@$NODE_IP << 'REMOTE_EOF'
  # Cài đặt k3s bản Server (single-node)
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.34.9+k3s1 INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --tls-san 192.168.120.175 --node-external-ip 192.168.120.175" sh -

  echo "Đợi K3s API sẵn sàng (khoảng 20s)..."
  sleep 20

  # Tạo file RBAC cho Prometheus
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

  # Apply RBAC
  sudo k3s kubectl apply -f /tmp/prometheus-rbac.yaml

  # Trích xuất Token ra file tạm
  sudo k3s kubectl get secret prometheus-remote-token -n kube-system -o jsonpath='{.data.token}' | base64 -d > /tmp/prometheus-remote-token.txt
REMOTE_EOF

echo "[2/4] Đang kéo Token của Prometheus về local..."
scp -i $SSH_KEY $SSH_USER@$NODE_IP:/tmp/prometheus-remote-token.txt cluster-setup/prometheus-remote-token.txt

echo "[3/4] Đang kéo kubeconfig về máy WSL2 và cấu hình IP..."
mkdir -p ~/.kube
ssh -i $SSH_KEY $SSH_USER@$NODE_IP "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
sed -i "s/127.0.0.1/$NODE_IP/g" ~/.kube/config
chmod 600 ~/.kube/config

echo "[4/4] Cập nhật .gitignore để không lộ Token..."
if ! grep -q "prometheus-remote-token.txt" .gitignore 2>/dev/null; then
    echo "cluster-setup/prometheus-remote-token.txt" >> .gitignore
fi

echo "✅ Hoàn tất! Bạn đã có thể dùng kubectl từ máy WSL2."
