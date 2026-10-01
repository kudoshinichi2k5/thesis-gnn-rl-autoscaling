# Kiến trúc triển khai K3s multi-node

Thư mục này chứa automation dựng lại môi trường benchmark trên OpenStack. K3s chạy trên ba máy trong cùng một cluster: một control-plane và hai worker giống nhau. Prometheus/Grafana/Jaeger và load generator tiếp tục chạy trên hai VM standalone để hạn chế nhiễu tài nguyên lên workload.

## Sơ đồ tổng thể

```text
             K3s cluster (private network 10.42.0.0/24)
     +------------------+       +--------------------+
     | node-app         |       | node-app-worker-1  |
     | control-plane    |------>| app workloads      |
     | label + taint    |       | label app-worker   |
     +--------+---------+       +--------------------+
           |                 +--------------------+
           +---------------->| node-app-worker-2  |
                       | app workloads      |
                       | label app-worker   |
                       +----------+---------+
                              |
[node-loadgen] -- HTTP/80 -- ServiceLB/K3s -+- Online Boutique
                              |
                cAdvisor :10250 <-----+---- Prometheus
                Istio/Envoy traces --------> Jaeger :9411
                                   Grafana :3000
                                   Prometheus :9090
                                   Jaeger UI :16686
```

## Năm node

| Node | Vai trò | Flavor | vCPU | Boot volume | Floating IP | Kubernetes placement |
|---|---|---|---:|---:|---|---|
| `node-app` | K3s control-plane | `gp.small-2c` (`00-0000-0002-02`) | 2 | 20 GB | Có | `node-role=control-plane`; taint `dedicated=control-plane:NoSchedule` |
| `node-app-worker-1` | K3s application worker | `gp.large-4c` (`00-0000-0008-04`) | 4 | 40 GB | Không | `node-role=app-worker`; nhận Online Boutique pods |
| `node-app-worker-2` | K3s application worker | `gp.large-4c` (`00-0000-0008-04`) | 4 | 40 GB | Không | `node-role=app-worker`; nhận Online Boutique pods |
| `node-observability` | Standalone monitoring/tracing | `gp.small-1c` (`00-0000-0002-01`) | 1 | 50 GB | Có | Không thuộc cluster; Docker Compose |
| `node-loadgen` | Standalone load generator | `gp.small-1c` (`00-0000-0002-01`) | 1 | 30 GB | Có | Không thuộc cluster; Locust/load patterns |

Tổng theo flavor CPU và cấu hình volume là 12 vCPU và 180 GB. Với quota 16 vCPU/200 GB, còn 4 vCPU/20 GB. Hai worker dùng 8 vCPU tổng; theo thông số worker đã nêu, mỗi worker có 8 GB RAM và khoảng 7 GB/3.5 vCPU có thể dùng cho workload sau overhead hệ thống. RAM của control-plane, observability và loadgen phải đối chiếu catalog flavor OpenStack; tổng RAM cần không vượt 22 GB để giữ reserve yêu cầu 2 GB. Không tự suy ra RAM từ tên flavor.

## Labels, taint và scheduling

- Server node được đặt tên Kubernetes cố định là `node-app`, gắn label `node-role=control-plane` và taint `dedicated=control-plane:NoSchedule` ngay trong `01-install-server.sh`, trước khi join worker. Điều này ngăn khoảng thời gian workload ứng dụng bị schedule nhầm lên control-plane.
- Hai agent được đặt tên `node-app-worker-1` và `node-app-worker-2`, mỗi node được gắn label `node-role=app-worker` sau khi đạt `Ready`.
- Istiod có `nodeSelector: node-role=control-plane` và toleration tương ứng với taint `dedicated=control-plane:NoSchedule`.
- Helm chart Online Boutique dùng preferred pod anti-affinity theo app label, topology `kubernetes.io/hostname`, weight `100`. Replicas cùng service được khuyến khích đặt trên hai worker khác nhau; đây là preference, không phải hard constraint. Taint control-plane là lớp chặn workload trên node-app.
- Các daemon/system pods có thể tiếp tục chạy trên control-plane nếu chúng toleration taint; mục tiêu là workload ứng dụng không chiếm tài nguyên control-plane.

## Luồng cài đặt

Thực hiện tuần tự; mỗi bước cần được xác nhận trước khi chuyển bước tiếp:

1. Từ repo root, nạp OpenStack credentials trong WSL/Bash, sau đó chạy Terraform `init`, `plan`, xem đủ 5 instance và đúng 3 Floating IP (control-plane, observability, loadgen), rồi mới `apply`.
2. Chạy `bash cluster-setup/00-generate-node-ips.sh`. File `node-ips.env` được sinh tự động, gồm floating IP cho ba node public và fixed IP cho cả hai worker.
3. Chạy `bash cluster-setup/01-install-server.sh`. Script cài K3s `v1.34.9+k3s1`, tải kubeconfig có endpoint public của control-plane, lưu `k3s-node-token.txt` với quyền `0600`, rồi label/taint node-app.
4. Export kubeconfig và join từng worker qua control-plane bằng ProxyJump:

  ```bash
  export KUBECONFIG="$HOME/.kube/config"
  source cluster-setup/node-ips.env
  bash cluster-setup/01b-install-worker.sh "$NODE_WORKER1_FIXED_IP"
  bash cluster-setup/01b-install-worker.sh "$NODE_WORKER2_FIXED_IP"
  ```

  Script đợi mỗi worker tối đa 60 giây. Nếu chưa `Ready`, nó lấy `journalctl -u k3s-agent` qua ProxyJump và dừng.
5. Chạy `bash cluster-setup/02-install-istio.sh`. Istio `1.31.0` và Istiod được pin vào control-plane; tracing provider dùng Jaeger fixed/private IP cổng `9411`.
6. Chạy `bash cluster-setup/03-deploy-online-boutique.sh`. Script vendor chart, áp dụng lại probe patch và anti-affinity, rồi deploy trong namespace `online-boutique`.
7. Xem pod/service và thử scale frontend lên 2 replicas để xác nhận preferred anti-affinity phân tán chúng giữa hai worker. Scale frontend về 1 sau phép thử.
8. Service `frontend-external` là LoadBalancer. K3s ServiceLB dùng host port của Service (mặc định HTTP port `80`); security group Terraform có rule `frontend-http` TCP/80. Truy cập Floating IP của control-plane cần ServiceLB pod/listener hoạt động trên node đó.
9. Chạy `bash cluster-setup/04-setup-monitoring.sh`. Script sinh file-based discovery từ fixed IP mới của control-plane và hai worker; Prometheus scrape cả ba kubelet tại TCP/10250.
10. Chạy `bash cluster-setup/05-setup-tracing.sh` để áp Telemetry sampling 100% và restart deployment để Envoy nhận cấu hình.

## Script và file

- `00-generate-node-ips.sh`: lấy Terraform output; không sửa tay `node-ips.env`.
- `01-install-server.sh`: cài K3s server, cấp kubeconfig/token, và đặt label/taint trước worker.
- `01b-install-worker.sh`: nhận một worker private IP, xác thực IP thuộc một trong hai worker, SSH qua control-plane, cài K3s agent, chờ Ready, label node và in danh sách node.
- `02-install-istio.sh`: cài Istio, cấu hình tài nguyên, scheduling Istiod và Jaeger extension provider.
- `03-deploy-online-boutique.sh`: vendor/install Helm chart và áp patch có thể tái tạo sau `rsync --delete`.
- `04-setup-monitoring.sh`: tạo RBAC, kubeconfig KSM, Prometheus config và file SD target list rồi triển khai Docker Compose ở node-observability.
- `05-setup-tracing.sh`: bật Telemetry sampling 100%, restart ứng dụng và kiểm tra rollout frontend.
- `istio-tracing.yaml`: Istio Telemetry resource dùng provider `external-jaeger`.
- `node-ips.env`: file runtime được sinh từ Terraform và bị gitignore.
- `k3s-node-token.txt`: token join worker, secret bị gitignore; không commit/in nội dung.

## Quota và dữ liệu benchmark

- Quota: tối đa 16 vCPU, 24 GB RAM, 200 GB storage; mục tiêu cấu hình không vượt 15 vCPU, 22 GB RAM, 180 GB storage.
- CPU đã biết từ flavor IDs: 12 vCPU tổng. Boot volume: 180 GB tổng.
- Cần xác nhận RAM flavor từ OpenStack trước `apply`. Hai worker chiếm 16 GB theo thông tin flavor; ba node còn lại phải nằm trong 6 GB để đáp ứng giới hạn mục tiêu 22 GB.
- Giữ cố định K3s `v1.34.9+k3s1`, Istio `1.31.0`, requests/limits, replica count, scheduling policy và sampling rate giữa các lượt benchmark nếu cần so sánh dataset.
- `preferred` anti-affinity không đảm bảo rải replica tuyệt đối khi node thiếu tài nguyên hoặc scheduler có ràng buộc khác; kiểm tra node placement thực tế.

## Luồng metrics và traces

- Prometheus scrape HTTPS `/metrics/cadvisor` trên ba fixed IP cổng `10250`, xác thực bằng token kubelet; discovery list được `04-setup-monitoring.sh` sinh trong `monitoring-stack/file_sd/kubelet-targets.json`.
- Kube-state-metrics chạy standalone trong Docker, đọc Kubernetes API bằng ServiceAccount `ksm-reader` và scrape tại `kube-state-metrics:8080`.
- Istio/Envoy gửi Zipkin spans tới Jaeger trên node-observability cổng `9411`; Jaeger UI ở `16686`, Prometheus ở `9090`, Grafana ở `3000`.
- Locust chạy trên node-loadgen riêng; không dùng loadgenerator template upstream vì script vendor xóa template đó.

## Bảo mật

- Không commit hoặc chia sẻ `k3s-node-token.txt`, kubeconfig, Prometheus token, `ksm-kubeconfig`, OpenRC credentials, SSH private key hoặc Terraform state.
- Token kubelet hiện gắn quyền `system:kubelet-api-admin`; bảo vệ/rotate nếu có dấu hiệu bị lộ.
- Các UI và một số security-group rules có thể đang mở rộng cho lab. Giới hạn ingress tới IP quản trị trước khi dùng lâu dài.
- Không in token ra terminal khi tạo worker; script truyền token qua stdin của SSH.
