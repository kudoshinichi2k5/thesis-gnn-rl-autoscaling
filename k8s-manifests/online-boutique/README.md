# Online Boutique - Microservices Benchmark
- **Vendor từ:** `GoogleCloudPlatform/microservices-demo`
- **Commit hash:** `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`
- **Thay đổi chính:** Xóa LoadGenerator; resource overrides dùng đúng key camelCase; patch probes email/recommendation trực tiếp trong template (60s delay, 5s timeout); preferred pod anti-affinity trải workload giữa hai worker. Frontend expose qua `frontend-external` (LoadBalancer).
