#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOAD_TEST_DIR="${REPO_ROOT}/load-testing"
source "${SCRIPT_DIR}/node-ips.env"

: "${NODE_LOADGEN_FLOATING_IP:?NODE_LOADGEN_FLOATING_IP is missing from node-ips.env}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kltn_autoscaling}"
REMOTE_DIR="${REMOTE_DIR:-/home/${SSH_USER}/load-testing}"
CONTROL_PATH="$HOME/.ssh/locust-loadgen-%C"
SSH_OPTIONS=(-i "$SSH_KEY" -o ControlMaster=auto -o ControlPersist=10m -o "ControlPath=${CONTROL_PATH}")
RSYNC_SSH="ssh -i ${SSH_KEY} -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=${CONTROL_PATH}"

if [[ ! -r "$SSH_KEY" ]]; then
  echo "SSH key is not readable: $SSH_KEY" >&2
  exit 1
fi

ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}" \
  "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip rsync && mkdir -p '${REMOTE_DIR}'"

rsync -av \
  --exclude='.venv/' \
  --exclude='results/' \
  -e "$RSYNC_SSH" \
  "${LOAD_TEST_DIR}/" "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}:${REMOTE_DIR}/"

ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${NODE_LOADGEN_FLOATING_IP}" \
  "cd '${REMOTE_DIR}' && python3 -m venv .venv && .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt && .venv/bin/locust --version"

echo "Locust is installed on node-loadgen at ${REMOTE_DIR}/.venv/bin/locust"
