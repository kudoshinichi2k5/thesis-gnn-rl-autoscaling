# Khởi tạo Hạ tầng KLTN (Phương án Dự phòng)

Do giới hạn về cấp phát tài nguyên trên OpenStack, hệ thống được chuyển đổi sang kiến trúc Single-Node Kubernetes phân lập hoàn toàn với các thành phần Observability và Load Generator. Kiến trúc này đảm bảo môi trường thu thập dữ liệu (Dataset) không bị nhiễu bởi các công cụ đo lường.

## Kiến trúc Mạng & Nodes

* **`node-app` (192.168.120.175 / 10.42.0.7 - 4 vCPU/8GB):** Chạy K3s Server-only (Single-node). Đóng vai trò Host duy nhất chạy ứng dụng benchmark (Online Boutique) và thu thập metrics nội bộ (kube-state-metrics, cAdvisor).
* **`node-observability` (10.42.0.93 - Độc lập):** Chạy Docker thuần. Chứa hệ sinh thái Prometheus, Grafana, và Jaeger.
* **`node-loadgen` (10.42.0.134 - Độc lập):** Chạy Docker thuần. Dùng Locust để giả lập tải người dùng (Spike, Bursty, Ramp, Normal).

## Sơ đồ luồng dữ liệu (Traffic Flow)

```text
[node-loadgen]                      [node-app (K3s)]                         [node-observability]
   Locust  -------- (HTTP) --------> Online Boutique (Istio Ingress)
                                            |
                                            +-- Spans -----------------------> Jaeger Collector
                                            |
                                            +-- Pod/App Metrics (Scrape) ----> Prometheus
                                            |
                                        Kubelet (10250) <--- (Scrape) -------- Prometheus