#!/usr/bin/env bash
# Kiểm tra trạng thái hệ thống TRƯỚC khi làm từng bước. Chỉ ĐỌC, không thay đổi gì.
#
#   bash preflight.sh build        # trên máy build (Ubuntu Server), từ thư mục gốc repo
#   bash preflight.sh node-app     # trên node-app (cần sudo)
#   bash preflight.sh loadgen      # trên node-loadgen
#
# Biến môi trường tùy chọn: OBS_IP (fixed IP node-observability, kiểm tra Prometheus/Jaeger),
#                           FRONTEND_URL (loadgen), MIN_FREE_GB (mặc định 4).
# Thoát mã 0 nếu không có mục FAIL (WARN không làm thất bại).
set -uo pipefail

MODE="${1:-}"
MIN_FREE_GB="${MIN_FREE_GB:-4}"
NS_APP="${NS_APP:-online-boutique}"
FAILS=0

ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }
k()    { sudo -n k3s kubectl "$@"; }

free_gb() { df -Pk "$1" | awk 'NR==2 {printf "%d", $4/1024/1024}'; }

check_disk() {
  local gb; gb="$(free_gb "$1")"
  if (( gb >= MIN_FREE_GB )); then ok "ổ đĩa $1 còn ${gb} GB (cần >= ${MIN_FREE_GB})"; else fail "ổ đĩa $1 chỉ còn ${gb} GB (cần >= ${MIN_FREE_GB})"; fi
}

check_http() {  # $1=tên $2=url
  local code; code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$2" 2>/dev/null || true)"
  if [[ "$code" =~ ^(200|301|302)$ ]]; then ok "$1 phản hồi ($code): $2"; else fail "$1 không truy cập được ($2, mã '${code:-none}')"; fi
}

case "$MODE" in
build)
  echo "== Kiểm tra máy build =="
  have docker && ok "docker: $(docker --version)" || fail "chưa cài docker"
  if have docker; then
    docker info >/dev/null 2>&1 && ok "docker chạy được không cần sudo" || fail "docker không chạy được (thêm user vào nhóm docker rồi đăng nhập lại)"
  fi
  arch="$(uname -m)"; [[ "$arch" == "x86_64" ]] && ok "kiến trúc x86_64" || fail "kiến trúc $arch (node OpenStack là x86_64; dùng buildx --platform linux/amd64)"
  have python3 && ok "python3: $(python3 --version 2>&1)" || fail "chưa cài python3"
  python3 -c 'import venv' 2>/dev/null && ok "python3-venv có sẵn" || fail "thiếu python3-venv (sudo apt-get install python3-venv)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)' 2>/dev/null \
    && ok "Python 3.12 (khớp handoff_manifest)" || warn "Python không phải 3.12; golden test có thể lệch, hoặc dùng container python:3.12-slim"
  have git && ok "git có sẵn" || fail "chưa cài git"
  have ssh && have scp && ok "ssh/scp có sẵn" || fail "thiếu ssh/scp"
  KEY="${SSH_KEY:-$HOME/.ssh/kltn_autoscaling}"
  if [[ -f "$KEY" ]]; then
    perm="$(stat -c %a "$KEY")"; [[ "$perm" == "600" ]] && ok "SSH key $KEY (quyền 600)" || fail "SSH key $KEY quyền $perm (chmod 600)"
  else
    fail "không thấy SSH key $KEY (hỏi team cách cấp key)"
  fi
  check_disk "."
  [[ -f artifacts/gatgru/v05_selected_features_final/gatgru_selected_model_seed_0.pt ]] \
    && ok "có checkpoint GAT-GRU seed_0" || fail "thiếu artifacts/gatgru/v05_selected_features_final/gatgru_selected_model_seed_0.pt (sai nhánh, hoặc đang đứng sai thư mục)"
  [[ -f configs/gatgru/graph_metadata.json && -f configs/gatgru/feature_contract_v01.json ]] \
    && ok "có configs/gatgru/*" || fail "thiếu configs/gatgru/*"
  [[ -f autoscaler/rl/observation_contract.json ]] && ok "có autoscaler/rl/observation_contract.json" || fail "thiếu autoscaler/rl/observation_contract.json"
  if [[ -f autoscaler/controller/main.py ]]; then ok "có autoscaler/controller/main.py"; else fail "thiếu autoscaler/controller/main.py (sai nhánh hoặc sai thư mục)"; fi
  [[ -d .venv ]] && ok "có .venv" || warn "chưa có .venv (python3 -m venv .venv)"
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -c 'import torch, stable_baselines3, gymnasium' 2>/dev/null \
      && ok "venv có torch, stable-baselines3, gymnasium" || fail "venv thiếu thư viện (pip install -r modeling-common/requirements-ml.txt stable-baselines3 gymnasium prometheus-client)"
  fi
  ;;

node-app)
  echo "== Kiểm tra node-app =="
  if sudo -n true 2>/dev/null; then ok "sudo không cần mật khẩu"; else fail "không có sudo không mật khẩu (kubectl cần root để đọc k3s.yaml)"; exit 1; fi
  have k3s && ok "k3s có sẵn" || { fail "không có lệnh k3s (đang ở sai node?)"; exit 1; }
  n_ready="$(k get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l)"
  [[ "$n_ready" == "3" ]] && ok "3 node Ready" || fail "chỉ $n_ready/3 node Ready"
  k get nodes -o wide 2>/dev/null | sed 's/^/        /'
  k get ns "$NS_APP" >/dev/null 2>&1 && ok "namespace $NS_APP tồn tại" || fail "không có namespace $NS_APP"
  n_dep="$(k -n "$NS_APP" get deploy --no-headers 2>/dev/null | wc -l)"
  n_rdy="$(k -n "$NS_APP" get deploy --no-headers 2>/dev/null | awk '{split($2,a,"/"); if (a[1]==a[2] && a[2]>0) c++} END{print c+0}')"
  [[ "$n_dep" -ge 11 && "$n_rdy" == "$n_dep" ]] && ok "$n_rdy/$n_dep Deployment sẵn sàng" || fail "Deployment sẵn sàng $n_rdy/$n_dep (kỳ vọng 11/11)"
  pend="$(k -n "$NS_APP" get pods --no-headers 2>/dev/null | awk '$3=="Pending"' | wc -l)"
  [[ "$pend" == "0" ]] && ok "không có pod Pending trong $NS_APP" || warn "$pend pod Pending trong $NS_APP (cụm có thể đã hết chỗ)"
  n_hpa="$(k -n "$NS_APP" get hpa --no-headers 2>/dev/null | wc -l)"
  ok "HPA hiện có: $n_hpa (ghi lại trước khi thay đổi)"
  if (( n_hpa > 0 )); then
    k -n "$NS_APP" get hpa -o custom-columns=NAME:.metadata.name,MIN:.spec.minReplicas,MAX:.spec.maxReplicas --no-headers 2>/dev/null \
      | sed 's/^/        /'
    hmax="$(k -n "$NS_APP" get hpa -o jsonpath='{.items[*].spec.maxReplicas}' 2>/dev/null | tr ' ' '\n' | sort -u | tr '\n' ' ')"
    [[ "$hmax" == "15 " ]] && ok "mọi HPA có maxReplicas = 15 (khớp phạm vi autoscaler)" \
      || warn "maxReplicas của HPA là: ${hmax:-?} (autoscaler dùng 1..15; ghi nhận khi so sánh)"
  fi
  k get apiservice v1beta1.metrics.k8s.io -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null | grep -q True \
    && ok "metrics-server Available (HPA có dữ liệu CPU)" || warn "metrics-server chưa Available (HPA sẽ không hoạt động)"
  k get storageclass 2>/dev/null | grep -q local-path && ok "StorageClass local-path có sẵn (cho PVC)" || fail "không có StorageClass local-path"
  k get node node-app -o jsonpath='{.metadata.labels.node-role}' 2>/dev/null | grep -q control-plane \
    && ok "node-app có nhãn node-role=control-plane" || fail "node-app thiếu nhãn node-role=control-plane (nodeSelector sẽ không khớp)"
  k get node node-app -o jsonpath='{.spec.taints[*].key}' 2>/dev/null | grep -q dedicated \
    && ok "node-app có taint dedicated (Deployment đã có toleration)" || warn "node-app không có taint dedicated"
  k get ns autoscaler >/dev/null 2>&1 && warn "namespace autoscaler ĐÃ tồn tại (đã triển khai trước đó?)" || ok "namespace autoscaler chưa có (sạch)"
  sudo -n k3s ctr images ls -q >/dev/null 2>&1 && ok "containerd (k3s ctr) truy cập được" || fail "không chạy được 'sudo k3s ctr images ls'"
  sudo -n k3s ctr images ls -q 2>/dev/null | grep -q gnn-rl-autoscaler && ok "image gnn-rl-autoscaler đã có trên node" || warn "chưa có image gnn-rl-autoscaler (cần import ở Bước 4)"
  check_disk "/"
  have rsync && ok "rsync có sẵn (chuyển evidence về repo)" || warn "chưa cài rsync (sudo apt-get install -y rsync): cần để chuyển evidence về repo"
  echo "  -- Tài nguyên worker (request đã dùng / allocatable):"
  k describe nodes 2>/dev/null | awk '/^Name:/{n=$2} /^Allocated resources:/{f=1} f&&/cpu |memory /{print "        " n ": " $0} /^Events:/{f=0}'
  if [[ -n "${OBS_IP:-}" ]]; then
    check_http "Prometheus" "http://${OBS_IP}:9090/-/ready"
    check_http "Jaeger" "http://${OBS_IP}:16686/api/services"
  else
    warn "chưa đặt OBS_IP: bỏ qua kiểm tra Prometheus/Jaeger (OBS_IP=<fixed IP node-observability>)"
  fi
  ;;

loadgen)
  echo "== Kiểm tra node-loadgen =="
  [[ -f "$HOME/load-testing/run-scenario.sh" ]] && ok "có ~/load-testing/run-scenario.sh" || fail "chưa rsync load-testing/ lên loadgen"
  [[ -f "$HOME/load-testing/node-ips.env" ]] && ok "có node-ips.env" || fail "thiếu ~/load-testing/node-ips.env"
  if [[ -f "$HOME/load-testing/node-ips.env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/load-testing/node-ips.env"
    [[ -n "${NODE_OBSERVABILITY_FIXED_IP:-}" ]] && ok "NODE_OBSERVABILITY_FIXED_IP có giá trị" || fail "thiếu NODE_OBSERVABILITY_FIXED_IP"
    [[ -n "${FRONTEND_URL:-}" ]] && ok "FRONTEND_URL có giá trị" || fail "thiếu FRONTEND_URL"
    [[ -n "${FRONTEND_URL:-}" ]] && check_http "frontend" "$FRONTEND_URL"
    if [[ -n "${NODE_OBSERVABILITY_FIXED_IP:-}" ]]; then
      check_http "Prometheus" "http://${NODE_OBSERVABILITY_FIXED_IP}:9090/-/ready"
      check_http "Jaeger" "http://${NODE_OBSERVABILITY_FIXED_IP}:16686/api/services"
    fi
  fi
  have python3 && ok "python3: $(python3 --version 2>&1)" || fail "chưa cài python3"
  python3 -c 'import venv' 2>/dev/null && ok "python3-venv có sẵn" || fail "thiếu python3-venv"
  [[ -x "$HOME/load-testing/.venv/bin/locust" ]] && ok "Locust venv sẵn sàng" || warn "chưa có Locust venv (run-scenario.sh sẽ tự tạo ở lần đầu)"
  check_disk "$HOME"
  have rsync && ok "rsync có sẵn (chuyển kết quả về repo)" || warn "chưa cài rsync (sudo apt-get install -y rsync): cần để chuyển kết quả về repo"
  ;;

*)
  sed -n '2,10p' "$0" >&2
  exit 2
  ;;
esac

echo
if (( FAILS == 0 )); then echo "KẾT QUẢ: đạt (không có mục FAIL)."; else echo "KẾT QUẢ: $FAILS mục FAIL. Sửa trước khi tiếp tục."; fi
exit $(( FAILS > 0 ))
