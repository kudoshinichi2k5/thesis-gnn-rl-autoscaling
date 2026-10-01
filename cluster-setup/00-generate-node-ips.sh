#!/bin/bash
set -euo pipefail

# Chạy được từ bất kỳ đâu — tự tính đường dẫn tuyệt đối tới repo root, không
# phụ thuộc thư mục hiện tại của người gọi (kể cả khi được gọi tự động từ
# terraform-openstack/scripts/replace-floating-ips.sh).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${REPO_ROOT}/terraform-openstack/environments/dev"
OUT_FILE="${REPO_ROOT}/cluster-setup/node-ips.env"

if ! command -v terraform >/dev/null 2>&1; then
  echo "❌ terraform không có trong PATH." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "❌ jq không có trong PATH." >&2
  exit 1
fi

NODE_JSON="$(terraform "-chdir=${TF_DIR}" output -json node_fixed_ips)"

get() {
  local node="$1" field="$2"
  echo "$NODE_JSON" | jq -er --arg n "$node" --arg f "$field" '.[$n][$f]'
}

cat << EOF > "$OUT_FILE"
# File này được sinh TỰ ĐỘNG bởi cluster-setup/00-generate-node-ips.sh từ
# terraform output. KHÔNG sửa tay — mọi thay đổi sẽ mất ở lần chạy sau.
# Chạy lại script này (hoặc replace-floating-ips.sh, script này đã được gọi
# tự động ở cuối) bất cứ khi nào terraform apply thay đổi IP của node nào.
NODE_APP_FLOATING_IP="$(get node-app floating_ip)"
NODE_APP_FIXED_IP="$(get node-app fixed_ip)"
NODE_WORKER1_FIXED_IP="$(get node-app-worker-1 fixed_ip)"
NODE_WORKER2_FIXED_IP="$(get node-app-worker-2 fixed_ip)"
NODE_OBSERVABILITY_FLOATING_IP="$(get node-observability floating_ip)"
NODE_OBSERVABILITY_FIXED_IP="$(get node-observability fixed_ip)"
NODE_LOADGEN_FLOATING_IP="$(get node-loadgen floating_ip)"
NODE_LOADGEN_FIXED_IP="$(get node-loadgen fixed_ip)"
EOF

echo "✅ Đã cập nhật $OUT_FILE:"
cat "$OUT_FILE"