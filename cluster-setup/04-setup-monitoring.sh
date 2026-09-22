#!/bin/bash
# Script cài đặt Standalone Observability (Prometheus, Grafana, KSM, Jaeger)
set -euo pipefail

OBS_IP="192.168.120.80"
NODE_APP_PRIVATE_IP="10.42.0.7"
SSH_KEY="$HOME/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

echo "[1/4] Tạo RBAC và Kubeconfig (Read-Only) cho kube-state-metrics..."
kubectl apply -f - << 'RBAC_EOF'
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

echo "Đợi token của ksm-reader được cấp phát..."
KSM_TOKEN=""
for i in $(seq 1 15); do
  KSM_TOKEN=$(kubectl get secret ksm-reader-token -n kube-system -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
  [ -n "$KSM_TOKEN" ] && break
  sleep 2
done
if [ -z "$KSM_TOKEN" ]; then
  echo "❌ Không lấy được token cho ksm-reader, kiểm tra lại kết nối tới cluster." >&2
  exit 1
fi

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
chmod 600 monitoring-stack/ksm-kubeconfig

if [ ! -f cluster-setup/prometheus-remote-token.txt ]; then
  echo "❌ Không tìm thấy cluster-setup/prometheus-remote-token.txt. Hãy chạy 01-install-server.sh trước." >&2
  exit 1
fi
cp cluster-setup/prometheus-remote-token.txt monitoring-stack/prometheus-remote-token.txt
chmod 600 monitoring-stack/prometheus-remote-token.txt

touch .gitignore
for entry in "monitoring-stack/ksm-kubeconfig" "monitoring-stack/prometheus-remote-token.txt"; do
  grep -qxF "$entry" .gitignore || echo "$entry" >> .gitignore
done

echo "[2/4] Tạo cấu hình Monitoring Stack (Prometheus, Grafana, KSM, Jaeger)..."
cat << PROM_EOF > monitoring-stack/prometheus.yml
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
      - targets: ['${NODE_APP_PRIVATE_IP}:10250']

  - job_name: 'kube-state-metrics'
    static_configs:
      - targets: ['kube-state-metrics:8080']
PROM_EOF

cat << 'COMPOSE_EOF' > monitoring-stack/docker-compose.yml
services:
  prometheus:
    image: prom/prometheus:v2.54.1
    container_name: prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./prometheus-remote-token.txt:/var/run/secrets/kubernetes.io/serviceaccount/token:ro
      - prom_data:/prometheus
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
      - ./grafana-provisioning:/etc/grafana/provisioning:ro
      - grafana_data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    restart: unless-stopped
    depends_on:
      - prometheus

  kube-state-metrics:
    image: registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0
    container_name: kube-state-metrics
    volumes:
      - ./ksm-kubeconfig:/kubeconfig:ro
    command:
      - '--kubeconfig=/kubeconfig'
    restart: unless-stopped

  jaeger-data-init:
    image: busybox:1.36
    container_name: jaeger-data-init
    # Fix bug đã biết từ Jaeger >=1.50.0: image chạy bằng non-root UID
    # 10001, nhưng Docker tạo named volume lần đầu với owner root:root ->
    # Jaeger không ghi được vào /badger ("mkdir /badger/key: permission
    # denied"). Chown 1 lần trước khi jaeger khởi động, chạy lại mỗi lần
    # `docker compose up` nên không phụ thuộc lần chạy đầu tiên.
    # https://github.com/orgs/jaegertracing/discussions/5097
    command: ["sh", "-c", "chown -R 10001:10001 /badger"]
    volumes:
      - jaeger_data:/badger

  jaeger:
    image: jaegertracing/all-in-one:1.60.0
    container_name: jaeger
    depends_on:
      jaeger-data-init:
        condition: service_completed_successfully
    ports:
      - "16686:16686" # UI
      - "9411:9411"   # Zipkin Collector (cho Envoy)
    environment:
      - SPAN_STORAGE_TYPE=badger
      - BADGER_EPHEMERAL=false
      - BADGER_DIRECTORY_VALUE=/badger/data
      - BADGER_DIRECTORY_KEY=/badger/key
    volumes:
      - jaeger_data:/badger
    restart: unless-stopped

volumes:
  prom_data:
  grafana_data:
  jaeger_data:
COMPOSE_EOF

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

echo "[3/4] Đẩy cấu hình lên node-observability ($OBS_IP)..."
rsync -avz --exclude='grafana-data' --exclude='prometheus-data' -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" monitoring-stack/ "$SSH_USER@$OBS_IP:~/monitoring-stack/"

echo "[4/4] Cài đặt Docker và Chạy hệ thống qua SSH..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER@$OBS_IP" << 'REMOTE_EOF'
  set -euo pipefail
  if ! command -v docker &> /dev/null; then
    sudo apt-get update && sudo apt-get install -y ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings