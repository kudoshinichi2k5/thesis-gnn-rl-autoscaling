#!/bin/bash
# Bật / tắt baseline HPA (k8s-manifests/hpa/hpa-online-boutique.yaml).
#   apply  : kiểm tra metrics-server rồi tạo HPA cho 10 service (trừ redis-cart)
#   delete : xóa HPA và scale các Deployment về 1 replica (trạng thái chuẩn trước mỗi run)
#   status : xem HPA, mức CPU hiện tại và số replica
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
HPA_FILE="${REPO_ROOT}/k8s-manifests/hpa/hpa-online-boutique.yaml"
NAMESPACE="online-boutique"
# Mọi service trừ redis-cart (stateful, giữ 1 replica).
SCALED=(frontend cartservice checkoutservice currencyservice productcatalogservice recommendationservice
        adservice emailservice paymentservice shippingservice)
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

usage() { echo "Usage: bash cluster-setup/07-setup-hpa.sh {apply|delete|status}" >&2; exit 2; }
[[ $# -eq 1 ]] || usage

check_metrics_server() {
  # K3s cài sẵn metrics-server; HPA không có dữ liệu CPU nếu API này không Available.
  local available
  available="$(kubectl get apiservice v1beta1.metrics.k8s.io \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || true)"
  if [[ "$available" != "True" ]]; then
    echo "❌ metrics.k8s.io chưa Available. Kiểm tra: kubectl -n kube-system get pods -l k8s-app=metrics-server" >&2
    exit 1
  fi
  kubectl top pods -n "$NAMESPACE" --containers >/dev/null
}

check_requests() {
  # Utilization = usage / request, nên container 'server' phải có CPU request.
  local d req
  for d in "${SCALED[@]}"; do
    req="$(kubectl get deployment "$d" -n "$NAMESPACE" \
      -o jsonpath='{.spec.template.spec.containers[?(@.name=="server")].resources.requests.cpu}')"
    if [[ -z "$req" ]]; then
      echo "❌ ${d}: container 'server' không có resources.requests.cpu" >&2
      exit 1
    fi
    echo "  ${d}: server cpu request = ${req}"
  done
}

reset_replicas() {
  local d
  for d in "${SCALED[@]}"; do
    kubectl scale deployment "$d" -n "$NAMESPACE" --replicas=1
  done
  for d in "${SCALED[@]}"; do
    kubectl rollout status deployment "$d" -n "$NAMESPACE" --timeout=180s
  done
}

case "$1" in
  apply)
    echo "Kiểm tra metrics-server..."
    check_metrics_server
    echo "Kiểm tra CPU request của container ứng dụng..."
    check_requests
    kubectl apply -f "$HPA_FILE"
    kubectl get hpa -n "$NAMESPACE"
    echo "✅ HPA đã bật (CPU 70% của container 'server', scale down sau 5 phút)."
    echo "   Khi thu dữ liệu, gắn nhãn: AUTOSCALER=hpa bash load-testing/run-scenario.sh <scenario> <runs>"
    ;;
  delete)
    kubectl delete -f "$HPA_FILE" --ignore-not-found
    echo "Scale các Deployment về 1 replica..."
    reset_replicas
    echo "✅ HPA đã tắt, cụm về trạng thái chuẩn (1 replica)."
    ;;
  status)
    kubectl get hpa -n "$NAMESPACE" -o wide || true
    echo
    kubectl top pods -n "$NAMESPACE" --containers 2>/dev/null | awk 'NR==1 || $2=="server"' || true
    echo
    kubectl get deployments -n "$NAMESPACE" -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas
    ;;
  *) usage ;;
esac
