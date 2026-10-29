# Online Boutique - Microservices Benchmark

- **Vendor từ:** `GoogleCloudPlatform/microservices-demo`
- **Commit hash:** `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`
- **Điều chỉnh trong repository:** bỏ LoadGenerator của chart, cấu hình resource override theo key camelCase, patch probe của `emailservice` và `recommendationservice` (initial delay 60 giây, timeout 5 giây), và thêm preferred pod anti-affinity để phân bố workload trên hai worker.
- **Frontend:** expose qua service `frontend-external` loại `LoadBalancer`; K3s ServiceLB cung cấp địa chỉ truy cập.
