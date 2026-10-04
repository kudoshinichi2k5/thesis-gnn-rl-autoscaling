# Load testing & thu thập dữ liệu cho GAT-GRU / PPO

Thư mục này thực hiện bước **"Hạ tầng và dữ liệu"** của đề tài: sinh tải có kiểm soát lên Online Boutique, thu telemetry (Prometheus + Jaeger) của từng lần chạy, và tiền xử lý thành tập dữ liệu đồ thị theo thời gian cho mô hình **GAT-GRU**. Bảng dữ liệu raw đã căn chỉnh cũng được dùng để xây môi trường **PPO offline**.

Lý do đằng sau các lựa chọn (run là gì, vì sao 8 run, 30 phút, chia tập thế nào) được giải thích trong [DATASET_GUIDE.md](DATASET_GUIDE.md).

```text
 WSL (máy điều khiển)                node-loadgen (1 vCPU)              node-observability
 ─────────────────────               ─────────────────────              ─────────────────────
 run-scenario.sh ──SSH──▶  Locust ──HTTP──▶ frontend-external (K3s, 2 worker, Istio sidecar)
        ▲                     │                       │ Envoy spans (Zipkin :9411) ──▶ Jaeger :16686
        │                     │                       │ cAdvisor :10250 / KSM   ◀──── Prometheus :9090
        │                     └─ sau mỗi run: collect_metrics.py ── query_range / api/traces ──┘
        └──────── rsync results/<scenario>/run_XX/ ◀──┘
 preprocess_gatgru ──▶ feature_selection_gatgru ──▶ train_gatgru ──▶ processed/gatgru_predictions_test.npz
```

Lý do thiết kế:
- **Locust chạy trên `node-loadgen` riêng**, nên bộ sinh tải không chiếm CPU của cụm và không làm nhiễu dữ liệu.
- **Telemetry được xuất *sau* mỗi run** (Prometheus `query_range`, Jaeger `api/traces` cho đúng khoảng `[start, end]`), không lấy mẫu trực tiếp trong lúc chạy. Cách này không thêm tải lên hệ thống đang đo, và mọi metric nằm trên cùng một lưới thời gian 10s.
- **Không cần sửa hạ tầng.** Prometheus hiện scrape cAdvisor của cả 3 node và kube-state-metrics. Metric Istio (`istio_requests_total`) **không** được scrape, nên tải và quan hệ gọi giữa service được lấy từ **span Envoy trong Jaeger** (sampling 100%).

## Thành phần

| File | Vai trò |
|---|---|
| `locustfile.py` | Hành vi người dùng: xem trang chủ, xem sản phẩm, thêm vào giỏ, checkout (chỉ khi giỏ có hàng), đổi tiền tệ |
| `scenarios/normal.py` | Kịch bản Normal |
| `scenarios/spike.py` | Kịch bản Spike |
| `scenarios/bursty.py` | Kịch bản Bursty |
| `run-scenario.sh` | Chạy N run của một kịch bản trên `node-loadgen`, xuất telemetry, kéo kết quả về `results/` |
| `collect_metrics.py` | Xuất metric của một run từ Prometheus + Jaeger thành CSV (chạy trên `node-loadgen`) |
| `preprocess_gatgru.ipynb` | Tiền xử lý thành dữ liệu đồ thị cho GAT-GRU; giải thích từng feature |
| `feature_selection_gatgru.ipynb` | Chọn feature nút và feature cạnh: lọc thống kê + permutation importance |
| `train_gatgru.ipynb` | Tìm siêu tham số (Optuna), huấn luyện lại 5 seed, đánh giá test, xem attention |
| `requirements.txt` | `locust==2.31.8`, `requests` (cài vào venv trên `node-loadgen`) |

Code mô hình và xử lý dữ liệu nằm trong [`../modeling-common/`](../modeling-common/README.md), dùng chung với baseline LSTM ([`../lstm-load-testing/`](../lstm-load-testing/README.md)).

## Kịch bản tải

Mỗi run mặc định dài **30 phút** (`RUN_DURATION_SEC`). Người dùng nghỉ `between(1, 3)` giây giữa các thao tác, nên RPS vào frontend xấp xỉ `users / 2`.

| Kịch bản | Hình dạng | Mục đích trong đề tài |
|---|---|---|
| `normal` | Warm-up tuyến tính 60s lên 40 user, sau đó dao động ±10% mỗi giây (spawn 5/s) | Giai đoạn khởi tạo: GAT-GRU học cấu trúc đồ thị và xu hướng ở trạng thái ổn định; PPO học giữ ổn định ở tải thấp |
| `spike` | Nền 25 user. Mỗi 6 phút có spike 220 user kéo dài 90s, tăng 40 user/s | Độ bền: dự báo bước nhảy ngoài phân phối; PPO ưu tiên SLO hơn chi phí. So sánh rõ proactive với độ trễ của HPA |
| `bursty` | Nền 20 user (±30%). Mỗi cửa sổ 30s có 35% khả năng nổ burst 80–180 user (spawn 15/s) | Tinh chỉnh: học quan hệ dịch vụ thay đổi liên tục; phân biệt nhiễu với tải thật để tránh scale dao động (thrashing) |

Mỗi run `bursty` mặc định có chuỗi burst khác nhau. Đặt `BURSTY_SEED=<số>` nếu cần lặp lại đúng một run.


## Điều kiện tiên quyết

Đã chạy xong `cluster-setup/00` → `06` (xem `cluster-setup/README.md`). Kiểm tra nhanh trước khi thu dữ liệu:

```bash
export KUBECONFIG="$HOME/.kube/config"
source cluster-setup/node-ips.env

kubectl get nodes                                   # node-app + 2 worker đều Ready
kubectl get pods -n online-boutique                 # mọi pod READY 2/2 (có istio-proxy)
bash cluster-setup/verify-istio-injection.sh online-boutique --check-existing
echo "$FRONTEND_URL"                                # phải có giá trị (NodePort của frontend-external)
curl -s "http://$NODE_OBSERVABILITY_FLOATING_IP:9090/api/v1/targets" | grep -o '"health":"[a-z]*"' | sort | uniq -c
#   -> 3 target kubelet-cadvisor + 1 kube-state-metrics đều "up"
curl -s "http://$NODE_OBSERVABILITY_FLOATING_IP:16686/api/services"
#   -> có frontend.online-boutique, productcatalogservice.online-boutique, ...
```

Nếu Jaeger chưa có service nào, chạy lại `cluster-setup/05-setup-tracing.sh`, gửi vài request tới frontend rồi kiểm tra lại.

## Các bước chạy load test và thu thập dữ liệu

Chạy mọi lệnh từ **repo root** trong WSL.

### Bước 1. Cài và đồng bộ code lên node-loadgen

```bash
bash cluster-setup/06-setup-loadgen.sh
```

Script cài Python/venv, Locust, `requests` và **rsync toàn bộ thư mục `load-testing/`** sang `node-loadgen`. `run-scenario.sh` không tự đồng bộ code, nên **chạy lại bước này mỗi khi sửa** `locustfile.py`, `scenarios/` hay `collect_metrics.py`.

### Bước 2. Chạy thử ngắn (smoke test)

```bash
RUN_DURATION_SEC=120 COOLDOWN_SEC=0 bash load-testing/run-scenario.sh spike 1
```

Run này dài 2 phút (spike đầu tiên bắt đầu ngay ở giây 0). Kiểm tra `load-testing/results/spike/run_01/`:
- Có đủ `meta.json`, `locust_stats.csv`, `locust_stats_history.csv`, `locust.html`, `node_metrics.csv`, `service_metrics.csv`, `edge_metrics.csv`, `collect_report.json`.
- Trong `collect_report.json`, mọi giá trị ở `prometheus_series` > 0, `jaeger_services` có 10 service, và `jaeger_truncated_windows` = 0.
- `edge_metrics.csv` có các cặp như `frontend,productcatalogservice`.

Xóa run thử trước khi thu thật, để không lẫn vào dataset: `rm -rf load-testing/results/spike/run_01`.

### Bước 3. Thu thập dữ liệu đầy đủ

```bash
tmux new -s loadtest          # giữ phiên chạy khi mất kết nối
bash load-testing/run-scenario.sh normal 8
bash load-testing/run-scenario.sh spike  8
bash load-testing/run-scenario.sh bursty 8
```

- Mỗi lệnh chạy 8 run × 30 phút, cộng 2 phút cooldown giữa các run, tổng khoảng **4.3 giờ cho mỗi kịch bản**. Với 8 run, notebook chia train/val/test theo run được 6/1/1 cho mỗi kịch bản. Cần **ít nhất 3 run cho mỗi kịch bản** để chia theo run; ít hơn thì notebook chia theo thời gian bên trong run.
- Số thứ tự run được đánh tiếp sau các run đã có trong `results/<scenario>/`, nên có thể chạy dồn nhiều đợt.
- Mỗi run tạo ra 180 bước × 11 service. 8 run × 3 kịch bản tương đương 4.320 snapshot đồ thị.

Biến môi trường tùy chọn:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `RUN_DURATION_SEC` | `1800` | Độ dài một run (các shape đọc biến này) |
| `COOLDOWN_SEC` | `120` | Thời gian nghỉ giữa các run để cụm về trạng thái nhàn rỗi, giúp các run độc lập với nhau |
| `STEP_SEC` | `10` | Bước lưới thời gian khi xuất metric (bằng `scrape_interval`) |
| `BURSTY_SEED` | trống | Seed cho bursty; để trống thì mỗi run một chuỗi burst khác |
| `AUTOSCALER` | `none` | Nhãn ghi vào `meta.json` (`none`, `hpa`, ...), để phân biệt điều kiện thu |
| `SKIP_COLLECT` | `0` | `1` = chỉ chạy Locust, không xuất telemetry |

#### Khuyến nghị: thu thêm dữ liệu có autoscaler

Nếu không có autoscaler thì `replicas` luôn bằng 1. Khi đó bước feature selection sẽ thấy `replicas` là hằng số (feature này vẫn được giữ vì là biến PPO điều khiển, nhưng không mang thông tin), và PPO không thấy được **ảnh hưởng của việc thay đổi replica** (CPU trên mỗi pod giảm, latency hồi phục). Nên thu thêm một đợt với HPA baseline làm "chính sách hành vi":

```bash
bash cluster-setup/07-setup-hpa.sh apply       # HPA: CPU 70% container 'server', scale down sau 5 phút
for s in normal spike bursty; do AUTOSCALER=hpa bash load-testing/run-scenario.sh "$s" 4; done
bash cluster-setup/07-setup-hpa.sh delete      # xóa HPA và scale về 1 replica
```

Cấu hình HPA nằm ở `k8s-manifests/hpa/hpa-online-boutique.yaml`. Nó dùng `ContainerResource`, chỉ tính CPU của container ứng dụng `server`, vì nếu tính cả pod thì sidecar `istio-proxy` làm sai lệch ngưỡng 70%. Cùng file này cũng là baseline HPA trong thực nghiệm so sánh cuối.

### Bước 4. Kiểm tra chất lượng từng run

```bash
cat load-testing/results/spike/run_03/collect_report.json
```

| Dấu hiệu | Nguyên nhân thường gặp | Xử lý |
|---|---|---|
| `prometheus_series.<x> = 0` | Target kubelet/KSM down, sai namespace | Xem Prometheus → Status → Targets |
| `jaeger_services` thiếu service | Pod thiếu sidecar, hoặc tracing chưa bật | `verify-istio-injection.sh --check-existing`, chạy lại `05-setup-tracing.sh` |
| `jaeger_truncated_windows > 0` | Quá nhiều trace trong 1–2s | Tăng `TRACE_LIMIT` (mặc định 1500) trong lệnh gọi `collect_metrics.py` |
| Notebook báo `jaeger_coverage < 0.9` | Jaeger (1 vCPU) rơi span khi tải cao | Loại run đó, hoặc đặt `CORRECT_COVERAGE = True` trong notebook |
| Locust in `CPU usage above 90%` | `node-loadgen` 1 vCPU quá tải ở 220 user | Kết quả phía client kém tin cậy; giảm `spike_users` hoặc nâng flavor |

`locust_exit_code = 1` trong `meta.json` là bình thường khi có request lỗi, ví dụ lúc spike làm hệ thống quá tải. Run vẫn được thu.

### Bước 5. Tiền xử lý, chọn feature, huấn luyện GAT-GRU

```bash
pip install -r modeling-common/requirements-ml.txt     # numpy, pandas, matplotlib, torch, optuna, jupyter
cd load-testing
jupyter notebook
```

Chạy lần lượt (mỗi notebook đọc đầu ra của notebook trước trong `processed/`):

| Thứ tự | Notebook | Đầu ra trong `processed/` |
|---|---|---|
| 1 | `preprocess_gatgru.ipynb` | `runs_raw.npz` (mảng thô từng run + split), `gatgru_dataset.npz` (cửa sổ mặc định w = 12), `metadata.json`, `timeseries_raw.csv` (cho PPO). Tạo `modeling-common/splits.json` nếu chưa có |
| 2 | `feature_selection_gatgru.ipynb` | `selected_features.json`: feature nút và cạnh giữ lại, kèm báo cáo từng bước |
| 3 | `train_gatgru.ipynb` | `gatgru_predictions_test.npz`, `gatgru_model.pt`, `gatgru_best_config.json` |

Sau đó chạy phần LSTM và notebook so sánh trong [`../lstm-load-testing/`](../lstm-load-testing/README.md).

- Biến `DATASET` ở đầu mỗi notebook: `'load-testing'` (mặc định, dữ liệu thư mục này) hoặc `'math'` (dataset theo mô hình toán học, xem [`../math-load-testing/`](../math-load-testing/README.md)). Mỗi bộ có split và thư mục `processed*/` riêng.
- Khi `results/` chưa có run thật, các notebook tự chuyển sang **chế độ demo** (dữ liệu giả lập đúng định dạng file, ghi vào `processed_demo/`, ngân sách tune rất nhỏ) để kiểm tra pipeline.
- `h = 6` bước (dự báo 60s tới, đủ bao thời gian một pod mới sẵn sàng) được cố định. Window `w` ∈ {6, 12, 18} được tune.
- Target là `rps_in` và `cpu_cores` của từng service.
- Quy trình chọn feature và tune được mô tả trong [`../modeling-common/README.md`](../modeling-common/README.md).

## Feature đưa vào mô hình (tóm tắt)

Notebook `preprocess_gatgru.ipynb` giải thích chi tiết từng feature; `feature_selection_gatgru.ipynb` loại các feature không cần thiết. Nguyên tắc chung: **chỉ dùng những gì controller quan sát được khi chạy thật** từ Prometheus/Jaeger. Số user của Locust **không** là feature, chỉ dùng để kiểm tra độ phủ.

| Nhóm | Feature | Mục tiêu |
|---|---|---|
| Tải (nút) | `rps_in`, `rps_in_delta`, `rps_per_replica` | Nhu cầu tải, xu hướng, tải trên mỗi pod |
| Hiệu năng (nút) | `latency_p50_ms`, `latency_p95_ms`, `error_rate` | Tín hiệu SLO; độ trễ/lỗi lan truyền dọc chuỗi gọi |
| Tài nguyên (nút) | `cpu_cores`, `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`, `mem_mib`, `net_rx_kBps`, `net_tx_kBps` | Nhu cầu tài nguyên, mức sử dụng giống HPA, bão hòa ở limit, tải dự phòng qua Envoy và network |
| Năng lực (nút) | `replicas`, `restarts_delta` | Năng lực hiện tại (biến PPO điều khiển), sự kiện bất ổn |
| Cạnh | `call_rate`, `call_ratio`, `edge_error_rate`, `edge_latency_p95_ms` | Trọng số phụ thuộc động cho attention của GAT; hệ số fan-out để lan truyền tải xuống downstream |
| Toàn đồ thị | `e2e_rps`, `e2e_latency_p95_ms`, `e2e_error_rate`, `slo_violation`, `total_replicas` | Trạng thái SLO đầu-cuối và chi phí, dùng cho state/reward của PPO |

## Định dạng dữ liệu thô (`results/<scenario>/run_XX/`)

| File | Cột |
|---|---|
| `meta.json` | `scenario`, `run_id`, `start`, `end` (unix s), `duration_sec`, `step_sec`, `frontend_url`, `bursty_seed`, `autoscaler`, `locust_exit_code` |
| `node_metrics.csv` | `timestamp, service, cpu_cores, cpu_sidecar_cores, cpu_throttle_ratio, mem_bytes, net_rx_bps, net_tx_bps, cpu_request_cores, cpu_limit_cores, restarts_total, replicas, replicas_desired` |
| `service_metrics.csv` | `timestamp, service, request_count, error_count, latency_p50_ms, latency_p95_ms` (span `server` của Envoy) |
| `edge_metrics.csv` | `timestamp, source, target, call_count, error_count, latency_p50_ms, latency_p95_ms` (span `client` của Envoy) |
| `locust_*.csv`, `locust.html` | Báo cáo Locust; `locust_stats_history.csv` có user count, RPS, p95 mỗi giây |
| `collect_report.json` | Số series mỗi query, service tìm thấy trong Jaeger, số cửa sổ trace bị cắt, thời gian xuất |

Ghi chú về nguồn dữ liệu:
- CPU, memory và network lấy theo pod rồi **gộp theo service** (tên pod `<deployment>-<hash>-<id>`). CPU ứng dụng không tính container `istio-proxy`; CPU của sidecar được tách riêng thành `cpu_sidecar_cores`.
- Cạnh `A → B` được lấy từ span `client` của sidecar A. Đích xác định qua tag `upstream_cluster`, hoặc qua span `server` con. Online Boutique chưa truyền tiếp trace header, nên mỗi trace chỉ gồm một cặp client–server. Điều này **đủ** để đếm cạnh, nhưng Jaeger UI sẽ không hiện một chuỗi trace đầy đủ.
- `cartservice → redis-cart` là TCP, không có span, nên `call_rate = 0`. Cạnh vẫn được giữ trong topology tĩnh; tải của `redis-cart` thể hiện qua network và `cpu_sidecar_cores`.

## Troubleshooting

1. **`frontend unreachable`**: kiểm tra `kubectl get svc frontend-external -n online-boutique`, sau đó chạy lại `cluster-setup/00-generate-node-ips.sh` để cập nhật `FRONTEND_URL` (NodePort có thể đổi khi service được tạo lại). Security group đã mở `30000-32767`.
2. **`Prometheus unreachable`** từ `node-loadgen`: kiểm tra `docker compose ps` trên `node-observability`, và rule `prometheus-ui` (9090) trong security group.
3. **`Locust venv missing`**: chạy `bash cluster-setup/06-setup-loadgen.sh`.
4. **Mất SSH giữa run**: Locust chạy trong phiên SSH nên sẽ dừng theo. Hãy chạy trong `tmux`; run dở dang được xóa thủ công ở cả local (`results/...`) và `node-loadgen` (`~/load-testing/results/...`).
5. **Xuất telemetry chậm**: Jaeger trả trace theo từng cửa sổ 10s cho mỗi service. Với run 30 phút ở tải spike, bước này mất vài phút. Có thể chạy lại riêng cho một run trên `node-loadgen`:
   ```bash
   cd ~/load-testing && .venv/bin/python collect_metrics.py --start <start> --end <end> \
     --out results/spike/run_03 --prometheus http://<obs-fixed-ip>:9090 --jaeger http://<obs-fixed-ip>:16686
   ```
   Lấy `<start>`/`<end>` từ `meta.json`. Chỉ xuất lại được trong thời gian dữ liệu còn được lưu: Prometheus mặc định 15 ngày, còn Jaeger Badger chỉ giữ span **72 giờ** (`BADGER_SPAN_STORE_TTL`). Vì vậy hãy kiểm tra `collect_report.json` ngay sau mỗi đợt chạy.
