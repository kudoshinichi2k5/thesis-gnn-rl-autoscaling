#!/bin/bash
# Script cài đặt Standalone Observability (Prometheus, Grafana, KSM)

OBS_IP="192.168.120.80"
NODE_APP_PRIVATE_IP="10.42.0.7"
SSH_KEY="~/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"
KUBECONFIG_PATH="~/.kube/config"

echo "[1/4] Tạo RBAC và Kubeconfig (Read-Only) cho kube-state-metrics..."
export KUBECONFIG=$KUBECONFIG_PATH
cat << 'RBAC_EOF' | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ksm-reader
  namespace: kube-system
---
apiVersion: v1
kind: Secret
metadata:
  name: ksm-reader-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: ksm-reader
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ksm-reader-role
rules:
- apiGroups: ["", "apps"]
  resources: ["pods", "deployments", "nodes", "replicasets"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ksm-reader-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ksm-reader-role
subjects:
- kind: ServiceAccount
  name: ksm-reader
  namespace: kube-system
RBAC_EOF

mkdir -p monitoring-stack

# Lấy token và tạo kubeconfig cho KSM (Giao tiếp qua mạng nội bộ 10.42.0.7)
KSM_TOKEN=$(kubectl get secret ksm-reader-token -n kube-system -o jsonpath='{.data.token}' | base64 -d)
cat << KCONF_EOF > monitoring-stack/ksm-kubeconfig
apiVersion: v1
kind: Config
clusters:
- name: k3s
  cluster:
    server: https://${NODE_APP_PRIVATE_IP}:6443
    insecure-skip-tls-verify: true
users:
- name: ksm-reader
  user:
    token: ${KSM_TOKEN}
contexts:
- name: default
  context:
    cluster: k3s
    user: ksm-reader
current-context: default
KCONF_EOF

# Sao chép Token của Prometheus cAdvisor (đã tạo ở script 01)
cp cluster-setup/prometheus-remote-token.txt monitoring-stack/prometheus-remote-token.txt

# Cập nhật .gitignore để bảo vệ credentials
grep -qxF "monitoring-stack/ksm-kubeconfig" .gitignore || echo "monitoring-stack/ksm-kubeconfig" >> .gitignore
grep -qxF "monitoring-stack/prometheus-remote-token.txt" .gitignore || echo "monitoring-stack/prometheus-remote-token.txt" >> .gitignore

echo "[2/4] Tạo cấu hình Prometheus & Docker Compose..."
cat << 'PROM_EOF' > monitoring-stack/prometheus.yml
global:
  scrape_interval: 10s

scrape_configs:
  - job_name: 'kubelet-cadvisor'
    scheme: https
    tls_config:
      insecure_skip_verify: true
    bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token
    metrics_path: /metrics/cadvisor
    static_configs:
      - targets: ['10.42.0.7:10250']

  - job_name: 'kube-state-metrics'
    static_configs:
      - targets: ['kube-state-metrics:8080']
PROM_EOF

cat << 'COMPOSE_EOF' > monitoring-stack/docker-compose.yml
version: '3.8'
services:
  prometheus:
    image: prom/prometheus:v2.54.1
    container_name: prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - ./prometheus-remote-token.txt:/var/run/secrets/kubernetes.io/serviceaccount/token
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.2.0
    container_name: grafana
    ports:
      - "3000:3000"
    volumes:
      - ./grafana-provisioning:/etc/grafana/provisioning
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    restart: unless-stopped
    depends_on:
      - prometheus

  kube-state-metrics:
    image: registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0
    container_name: kube-state-metrics
    volumes:
      - ./ksm-kubeconfig:/kubeconfig
    command:
      - '--kubeconfig=/kubeconfig'
    restart: unless-stopped
COMPOSE_EOF

# Auto-provisioning Grafana Datasource
mkdir -p monitoring-stack/grafana-provisioning/datasources
cat << 'DS_EOF' > monitoring-stack/grafana-provisioning/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
DS_EOF

cat << 'MD_EOF' > monitoring-stack/README.md
# Giám sát Ngoại vi (Standalone Observability)
Hệ thống giám sát được cài đặt độc lập để không tranh chấp CPU/RAM với `node-app`.

## Cấu trúc Thành phần (Phiên bản Stable)
- **Prometheus (v2.54.1):** Thu thập metrics từ cAdvisor (`node-app:10250`) và kube-state-metrics.
- **Grafana (11.2.0):** Dashboard UI (Port 3000, Pass: admin/admin).
- **Kube-state-metrics (v2.13.0):** Đồng bộ trạng thái Cluster/Deployments qua Kubeconfig (Read-only).

## LƯU Ý QUAN TRỌNG VỀ ĐỘ TRỄ (LATENCY) & RPS
Trong thiết kế của KLTN này, **RPS, P99 Latency và Error Rate theo từng Service SẼ KHÔNG CÓ TRONG PROMETHEUS NÀY**.
Nguyên nhân: Các chỉ số này do Envoy sidecar phát ra. Nhưng vì Envoy sidecar nằm sâu bên trong mạng overlay (`10.244.x.x`), một Prometheus đặt NGOÀI cụm (Standalone) không thể định tuyến để thu thập (scrape) trực tiếp. Quyết định kiến trúc là chúng ta sẽ trích xuất các chỉ số này từ **Jaeger traces** (sẽ cài đặt ở bước sau) để cấp cho RL Agent. Dashboard Grafana hiện tại chỉ phản ánh Tài nguyên vật lý (CPU/RAM cAdvisor) và Trạng thái lập lịch (Replica KSM).
MD_EOF

echo "[3/4] Đẩy cấu hình lên node-observability ($OBS_IP)..."
rsync -avz --exclude='grafana-data' --exclude='prometheus-data' -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" monitoring-stack/ $SSH_USER@$OBS_IP:~/monitoring-stack/

echo "[4/4] Cài đặt Docker và Chạy hệ thống qua SSH..."
ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_USER@$OBS_IP << 'REMOTE_EOF'
  if ! command -v docker &> /dev/null; then
    echo "Đang cài đặt Docker Engine..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker $USER
  fi
  DOCKER_VER=$(docker --version)
  echo "Đã cài đặt: $DOCKER_VER"
  
  echo "Ghi nhận version vào README..."
  # Tránh ghi đè file nhiều lần khi chạy lại
  grep -qxF "- **Docker Engine:** $DOCKER_VER" ~/monitoring-stack/README.md || echo "- **Docker Engine:** $DOCKER_VER" >> ~/monitoring-stack/README.md
  
  echo "Khởi động Monitoring Stack..."
  cd ~/monitoring-stack
  sudo docker compose up -d
REMOTE_EOF

echo "============================================="
echo "✅ HỆ THỐNG GIÁM SÁT ĐÃ SẴN SÀNG!"
echo "📍 Kiểm tra Prometheus Targets: http://$OBS_IP:9090/targets (Nên có 2 mục UP màu xanh)"
echo "📍 Truy cập Grafana: http://$OBS_IP:3000 (User/Pass: admin / admin)"
echo "============================================="
