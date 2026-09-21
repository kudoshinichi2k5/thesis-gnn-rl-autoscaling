#!/bin/bash
NODE_IP="192.168.120.175"
SSH_KEY="~/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"

echo "[1/3] Cài đặt K3s server (Mạng Pod 10.244.x.x)..."
ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_USER@$NODE_IP << 'REMOTE_EOF'
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.34.9+k3s1 INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --tls-san 192.168.120.175 --node-external-ip 192.168.120.175 --cluster-cidr 10.244.0.0/16 --service-cidr 10.96.0.0/16" sh -
  
  echo "Đợi K3s API sẵn sàng (20s)..."
  sleep 20
  
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
  sudo k3s kubectl get secret prometheus-remote-token -n kube-system -o jsonpath='{.data.token}' | base64 -d > /tmp/prometheus-remote-token.txt
REMOTE_EOF

echo "[2/3] Kéo Token của Prometheus về local..."
scp -i $SSH_KEY $SSH_USER@$NODE_IP:/tmp/prometheus-remote-token.txt cluster-setup/prometheus-remote-token.txt

echo "[3/3] Kéo Kubeconfig (Giữ nguyên 127.0.0.1 để dùng qua SSH Tunnel)..."
mkdir -p ~/.kube
ssh -i $SSH_KEY $SSH_USER@$NODE_IP "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
chmod 600 ~/.kube/config

echo "✅ Hoàn tất cài đặt K3s!"