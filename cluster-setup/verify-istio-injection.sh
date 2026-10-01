#!/bin/bash
set -euo pipefail

NAMESPACE="${1:-online-boutique}"
MODE="${2:-}"
if [[ "$MODE" != "" && "$MODE" != "--check-existing" ]]; then
  echo "Usage: $0 [namespace] [--check-existing]" >&2
  exit 2
fi

INJECTION_LABEL="$(kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.labels.istio-injection}')"
if [[ "$INJECTION_LABEL" != "enabled" ]]; then
  echo "Istio injection label is missing on namespace '$NAMESPACE'." >&2
  echo "Expected: istio-injection=enabled" >&2
  exit 1
fi

if ! PROBE_CONTAINERS="$(kubectl run istio-injection-probe \
  --namespace="$NAMESPACE" \
  --image=busybox:1.36 \
  --restart=Never \
  --dry-run=server \
  -o jsonpath='{.spec.containers[*].name} {.spec.initContainers[*].name}')"; then
  echo "Server-side admission probe failed for namespace '$NAMESPACE'." >&2
  exit 1
fi
if ! grep -qw istio-proxy <<< "$PROBE_CONTAINERS"; then
  echo "Istio injector did not add istio-proxy to a server-side dry-run pod." >&2
  echo "Returned containers: ${PROBE_CONTAINERS:-<none>}" >&2
  exit 1
fi

echo "Istio admission injection is active in namespace '$NAMESPACE'."

if [[ "$MODE" == "--check-existing" ]]; then
  POD_CONTAINERS="$(kubectl get pods -n "$NAMESPACE" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.containers[*]}{.name}{" "}{end}{range .spec.initContainers[*]}{.name}{" "}{end}{"\n"}{end}')"
  if [[ -z "$POD_CONTAINERS" ]]; then
    echo "No pods found in namespace '$NAMESPACE'." >&2
    exit 1
  fi

  MISSING_PROXY="$(awk 'index($0, "istio-proxy") == 0 { print $1 }' <<< "$POD_CONTAINERS")"
  if [[ -n "$MISSING_PROXY" ]]; then
    echo "These pods are missing istio-proxy:" >&2
    printf '%s\n' "$MISSING_PROXY" >&2
    exit 1
  fi
  echo "All pods in namespace '$NAMESPACE' have an istio-proxy sidecar."
fi
