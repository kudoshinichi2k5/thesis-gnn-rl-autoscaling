#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <worker-private-ip>" >&2
  exit 2
fi

source "${SCRIPT_DIR}/node-ips.env"
WORKER_IP="$1"
case "$WORKER_IP" in
  "$NODE_WORKER1_FIXED_IP") WORKER_NAME="node-app-worker-1" ;;
  "$NODE_WORKER2_FIXED_IP") WORKER_NAME="node-app-worker-2" ;;
  *)
    echo "Worker IP must match NODE_WORKER1_FIXED_IP or NODE_WORKER2_FIXED_IP in node-ips.env." >&2
    exit 2
    ;;
esac

SSH_KEY="$HOME/.ssh/kltn_autoscaling"
SSH_USER="ubuntu"
K3S_VERSION="v1.34.9+k3s1"
TOKEN_FILE="${SCRIPT_DIR}/k3s-node-token.txt"
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

if [[ ! -r "$SSH_KEY" ]]; then
  echo "SSH private key not found: $SSH_KEY" >&2
  exit 1
fi
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "K3s node token is missing: $TOKEN_FILE. Run 01-install-server.sh first." >&2
  exit 1
fi
if [[ ! -r "$KUBECONFIG" ]]; then
  echo "Kubeconfig not found: $KUBECONFIG. Run 01-install-server.sh first." >&2
  exit 1
fi

K3S_TOKEN="$(< "$TOKEN_FILE")"
printf -v REMOTE_TOKEN '%q' "$K3S_TOKEN"
REMOTE_SCRIPT=$(cat <<REMOTE_EOF
set -euo pipefail
curl -sfL https://get.k3s.io | sudo env INSTALL_K3S_VERSION='${K3S_VERSION}' K3S_URL='https://${NODE_APP_FIXED_IP}:6443' K3S_TOKEN=${REMOTE_TOKEN} K3S_NODE_NAME='${WORKER_NAME}' sh -
REMOTE_EOF
)
unset K3S_TOKEN REMOTE_TOKEN

SSH_OPTIONS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o BatchMode=yes)
PROXY_JUMP="${SSH_USER}@${NODE_APP_FLOATING_IP}"

echo "Joining ${WORKER_NAME} (${WORKER_IP}) to the K3s cluster through ${PROXY_JUMP}..."
ssh "${SSH_OPTIONS[@]}" -o "ProxyJump=${PROXY_JUMP}" \
  "${SSH_USER}@${WORKER_IP}" bash -s <<< "$REMOTE_SCRIPT"
unset REMOTE_SCRIPT

export KUBECONFIG
if ! kubectl wait --for=condition=Ready "node/${WORKER_NAME}" --timeout=60s; then
  echo "${WORKER_NAME} is not Ready after 60 seconds; collecting k3s-agent journal." >&2
  ssh "${SSH_OPTIONS[@]}" -o "ProxyJump=${PROXY_JUMP}" \
    "${SSH_USER}@${WORKER_IP}" \
    'sudo journalctl -u k3s-agent --no-pager -n 200' || true
  exit 1
fi

kubectl label node "$WORKER_NAME" node-role=app-worker --overwrite
kubectl get nodes -o wide
