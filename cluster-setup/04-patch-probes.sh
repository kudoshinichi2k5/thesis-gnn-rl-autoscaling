#!/bin/bash
export KUBECONFIG=~/.kube/config
echo "Đang vá cấu hình Health Check (Probes) cho emailservice và recommendationservice..."

for svc in emailservice recommendationservice; do
  kubectl patch deployment $svc -n online-boutique -p '{
    "spec": {
      "template": {
        "spec": {
          "containers": [
            {
              "name": "server",
              "livenessProbe": {
                "initialDelaySeconds": 20,
                "timeoutSeconds": 5
              },
              "readinessProbe": {
                "initialDelaySeconds": 20,
                "timeoutSeconds": 5
              }
            }
          ]
        }
      }
    }
  }'
done

echo "Patch hoàn tất! K8s sẽ tự động khởi động lại 2 service này với cấu hình mới."
