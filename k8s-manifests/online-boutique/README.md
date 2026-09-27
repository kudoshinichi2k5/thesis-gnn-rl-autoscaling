# Online Boutique - Microservices Benchmark
- **Vendor từ:** `GoogleCloudPlatform/microservices-demo`
- **Commit hash:** `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`
- **Thay đổi chính:** Xóa LoadGenerator, cấu hình Limits nới lỏng cho Python services, ghi đè Probes timeout (60s delay, 5s timeout) trực tiếp qua values. Frontend expose qua service `frontend-external` (LoadBalancer, do K3s servicelb cấp EXTERNAL-IP) — chart không hỗ trợ NodePort qua values.
