# Giám sát Ngoại vi (Standalone Observability)
Hệ thống giám sát được cài đặt độc lập để không tranh chấp CPU/RAM với `node-app`.

## Cấu trúc Thành phần (Phiên bản Stable)
- **Prometheus (v2.54.1):** Thu thập metrics từ cAdvisor (`node-app:10250`) và kube-state-metrics.
- **Grafana (11.2.0):** Dashboard UI (Port 3000, Pass: admin/admin).
- **Kube-state-metrics (v2.13.0):** Đồng bộ trạng thái Cluster/Deployments qua Kubeconfig (Read-only).

## LƯU Ý QUAN TRỌNG VỀ ĐỘ TRỄ (LATENCY) & RPS
Trong thiết kế của KLTN này, **RPS, P99 Latency và Error Rate theo từng Service SẼ KHÔNG CÓ TRONG PROMETHEUS NÀY**.
Nguyên nhân: Các chỉ số này do Envoy sidecar phát ra. Nhưng vì Envoy sidecar nằm sâu bên trong mạng overlay (`10.244.x.x`), một Prometheus đặt NGOÀI cụm (Standalone) không thể định tuyến để thu thập (scrape) trực tiếp. Quyết định kiến trúc là chúng ta sẽ trích xuất các chỉ số này từ **Jaeger traces** (sẽ cài đặt ở bước sau) để cấp cho RL Agent. Dashboard Grafana hiện tại chỉ phản ánh Tài nguyên vật lý (CPU/RAM cAdvisor) và Trạng thái lập lịch (Replica KSM).
