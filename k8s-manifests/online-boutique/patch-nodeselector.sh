#!/bin/bash
export KUBECONFIG=~/.kube/config
echo "Patching nodeSelector cho namespace online-boutique..."
DEPLOYS=$(kubectl get deployment -n online-boutique -o custom-columns=":metadata.name" --no-headers)
for dep in $DEPLOYS; do
  kubectl patch deployment $dep -n online-boutique -p '{"spec":{"template":{"spec":{"nodeSelector":{"role":"app"}}}}}'
done
echo "Patch hoàn tất!"
