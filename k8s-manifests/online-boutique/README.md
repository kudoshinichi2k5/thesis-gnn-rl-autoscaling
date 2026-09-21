# Online Boutique - Microservices Benchmark

## Nguồn gốc
- **Vendor từ:** `GoogleCloudPlatform/microservices-demo`
- **Commit hash:** `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`
- **Thời điểm copy:** 2026-09-21

## Tùy chỉnh (Overrides)
Mọi tùy chỉnh được đặt tại `values-override.yaml`:
1. **Frontend:** Cố định NodePort `30080`.
2. **LoadGenerator:** Đã bị vô hiệu hóa (`replicas: 0`) để nhường quyền sinh tải cho hệ thống Locust (node-loadgen).
3. **Tài nguyên (Resources):** Đã phân chia cứng Requests/Limits cho 10 dịch vụ. Dành ra không gian đệm cho Envoy sidecar. Tổng requests ~2500m CPU, nằm trong giới hạn 4 vCPU của cụm K3s.
4. **NodeSelector:** Bản chart gốc không hỗ trợ global nodeSelector. Cần chạy script `patch-nodeselector.sh` sau mỗi lần `helm install/upgrade` để ép toàn bộ pod chạy trên node có label `role=app`.

## Quản trị Vòng đời (Lifecycle)
Chạy lại thực nghiệm hoặc reset môi trường:
```bash
helm upgrade --install online-boutique ./chart -n online-boutique -f values-override.yaml
./patch-nodeselector.sh
```
