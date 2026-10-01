# Online Boutique - Microservices Benchmark
- **Vendor từ:** `GoogleCloudPlatform/microservices-demo`
- **Commit hash:** `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`
- **Thay đổi chính:** Xóa LoadGenerator, cấu hình Limits nới lỏng cho Python services, ghi đè Probes timeout (60s delay, 5s timeout) trực tiếp qua values. Frontend expose qua service `frontend-external` (LoadBalancer, do K3s servicelb cấp EXTERNAL-IP) — chart không hỗ trợ NodePort qua values.
- **Thay đổi chính:** Xóa LoadGenerator, chỉnh resource overrides theo key camelCase, patch probes của email/recommendation trực tiếp trong template (60s delay, 5s timeout), và thêm preferred pod anti-affinity để trải workload trên hai worker. Frontend expose qua  (LoadBalancer).
