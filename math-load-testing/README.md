# math-load-testing: dataset sinh tải theo mô hình toán học và pipeline GAT-GRU

Dataset của đề tài. Tải được sinh **đúng theo các mô hình luồng đến chuẩn** trong lý thuyết hàng đợi và mô hình hóa traffic: **NHPP, MMPP, Bounded Pareto, ON/OFF**. Mỗi run thu telemetry đầy đủ (Prometheus + Jaeger, có cả cạnh), dùng chung cho GAT-GRU (notebook trong thư mục này), baseline LSTM ([`../lstm-load-testing/`](../lstm-load-testing/README.md)) và môi trường PPO offline. Code mô hình dùng chung nằm trong [`../modeling-common/`](../modeling-common/README.md).

Lý do đằng sau các lựa chọn (run là gì, vì sao 8 run, 30 phút, chia tập thế nào) được giải thích trong [DATASET_GUIDE.md](DATASET_GUIDE.md).

```text
 node-loadgen (1 vCPU)                                         node-observability
 ─────────────────────                                         ─────────────────────
 run-scenario.sh ── driver mở (Locust) ──HTTP──▶ frontend-external (K3s, 2 worker, Istio sidecar, HPA)
        │                                              │ Envoy spans (Zipkin :9411) ──▶ Jaeger :16686
        │                                              │ cAdvisor :10250 / KSM   ◀──── Prometheus :9090
        └─ sau mỗi run: collect_metrics.py ── query_range / api/traces ─────────────────┘
 results/<scenario>/run_XX/ ──▶ preprocess_gatgru ──▶ feature_selection_gatgru ──▶ train_gatgru
```

Lý do thiết kế:
- **Driver chạy trên `node-loadgen` riêng**, nên bộ sinh tải không chiếm CPU của cụm và không làm nhiễu dữ liệu.
- **Telemetry được xuất *sau* mỗi run** (Prometheus `query_range`, Jaeger `api/traces` cho đúng khoảng `[start, end]`), không lấy mẫu trực tiếp trong lúc chạy. Cách này không thêm tải lên hệ thống đang đo, và mọi metric nằm trên cùng một lưới thời gian 10s.
- **Không cần sửa hạ tầng.** Prometheus scrape cAdvisor của cả 3 node và kube-state-metrics. Metric Istio (`istio_requests_total`) **không** được scrape, nên tải và quan hệ gọi giữa service được lấy từ **span Envoy trong Jaeger** (sampling 100%).
- **Luôn thu khi HPA bật.** Replica thay đổi theo tải, nên PPO thấy được ảnh hưởng của việc scale (xem mục 7 của DATASET_GUIDE).

## Vì sao dùng mô hình luồng đến mở

Locust thông thường dùng **mô hình đóng**: có N(t) user cố định, mỗi user gửi request, **chờ phản hồi**, nghỉ rồi gửi tiếp. Mô hình này có ba hạn chế:

| Hạn chế của mô hình đóng | Hệ quả |
|---|---|
| Thông lượng $X \approx N/(Z + R)$ phụ thuộc thời gian phản hồi $R$ | Khi cụm nghẽn, tải **tự giảm** (220 user: ~105 rps lúc nhàn rỗi nhưng chỉ ~55 rps khi R = 2s). Vi phạm SLO bị đánh giá thấp; `rps_in` không còn là "nhu cầu" thuần túy |
| Thời gian nghỉ phân phối đều | Luồng request **đều hơn Poisson** (index of dispersion < 1), không có tính dồn cục tự nhiên |
| Kịch bản điều khiển số user, không điều khiển λ(t) | Không mô tả được bằng một mô hình toán, khó viết phần "System Model" của báo cáo |

Ở đây, một **driver luồng đến mở** phát lại một trace sinh trước từ mô hình toán. Mỗi arrival được khởi chạy đúng thời điểm, **bất kể** các request trước đã xong hay chưa. Tải thiết kế (`designed_rate.csv`) được lưu cùng run làm "ground truth".

## 4 kịch bản

| Kịch bản | Mô hình | Công thức / tham số | Mức tải | Mục đích |
|---|---|---|---|---|
| `nhpp` | **Non-Homogeneous Poisson Process** | $\lambda(t) = 7 + 5\sin(2\pi t/900 + \varphi) + 1{,}5\sin(4\pi t/900 + 2\varphi)$ phiên/s (≥ 0,5). Sinh bằng **thinning** (Lewis–Shedler). φ ngẫu nhiên theo seed | 3–80 rps, trung bình 42 | Tải có chu kỳ ("ngày" nén vào 15 phút): kiểm tra autoscaler **dự báo** |
| `mmpp` | **Markov Modulated Poisson Process** (3 trạng thái) | Xích Markov thời gian liên tục: Idle (λ = 1,5 phiên/s, lưu trú ~Exp(120s)), Normal (3,5; Exp(180s)), High (15; Exp(60s)). Chuyển trạng thái: Idle→{Normal 0,8; High 0,2}, Normal→{Idle 0,4; High 0,6}, High→{Normal 0,9; Idle 0,1} | Idle ~9, Normal ~21, High ~90 rps; trung bình dài hạn 29 | Flash sale / spike **ngẫu nhiên** về thời điểm và độ dài: kiểm tra tốc độ phát hiện chuyển trạng thái |
| `pareto` | **Bounded Pareto** (khối lượng công việc) | Phiên đến Poisson (λ = 3/s). Số sản phẩm mỗi đơn $S = \lceil X\rceil$, $X \sim \mathrm{BP}(\alpha = 1{,}2, L = 1, H = 40)$. Phiên: home → S × thêm vào giỏ → xem giỏ → checkout | ~20 rps, nhưng CPU dao động mạnh | Yêu cầu không đồng nhất: vài đơn rất lớn làm bão hòa checkoutservice / productcatalog / currency dù số request ít ("80/20") |
| `onoff` | **ON/OFF** (đuôi nặng) | 60 client giữ phiên. ON ~ BP(1,5; 5s; 300s), gửi request Poisson 1,5/s; OFF ~ BP(1,3; 10s; 600s), im lặng | ~27 rps, dồn cục ở mọi thang | Traffic tự tương tự (Hurst lý thuyết $H = (3-\alpha_{\min})/2 = 0{,}85$): kiểm tra dao động replica của autoscaler |

**Phiên người dùng** (nhpp, mmpp): trang chủ + K thao tác, K ~ Geometric (trung bình 5), thời gian nghỉ ~ Exp (trung bình 2s). Thao tác chủ yếu là xem trang/sản phẩm, thỉnh thoảng thêm giỏ và checkout. Mỗi phiên/client có cookie riêng, nên giỏ hàng và checkout hoạt động đúng.

**Hiệu chỉnh:** tham số được chọn để mức tải nằm trong khoảng 10–110 rps: vượt năng lực 1 replica ở đỉnh (để thu mẫu vi phạm SLO) nhưng không vượt năng lực loadgen 1 vCPU.

**Ánh xạ Bounded Pareto:** Online Boutique không có kiểu "request nặng tùy ý" (như sinh báo cáo). Khối lượng được biểu diễn bằng số sản phẩm mỗi đơn. Đây là chiều tốn tài nguyên nhất mà ứng dụng cung cấp, vì checkout gọi productcatalog/currency/cart theo từng sản phẩm.

## Thành phần

| File | Vai trò |
|---|---|
| `arrival_models.py` | Bộ sinh thuần Python có seed: Poisson, NHPP (thinning), CTMC/MMPP, Bounded Pareto (nghịch đảo CDF), ON/OFF; `build_trace`, tải kỳ vọng, Hurst lý thuyết |
| `scenarios/{nhpp,mmpp,pareto,onoff}.py` | Tham số từng mô hình (`CONFIG`) |
| `locustfile.py` | **Driver mở**: 1 user Locust phát lại trace, mỗi arrival chạy trong greenlet riêng với `HttpSession` riêng (cookie). Thống kê vẫn được ghi vào Locust. Giới hạn số phiên đồng thời `MAX_CONCURRENT` |
| `run-scenario.sh` | Chạy N run (seed cố định) trên node-loadgen: chờ HPA về 1 replica, phát tải, xuất telemetry **đầy đủ** bằng `collect_metrics.py` |
| `collect_metrics.py` | Xuất metric của một run từ Prometheus + Jaeger thành CSV |
| `requirements.txt` | `locust==2.31.8`, `requests` (venv `.venv/` trên node-loadgen, `run-scenario.sh` tự tạo nếu chưa có) |
| `node-ips.env` | IP các node và `FRONTEND_URL` (điền tay, gitignored) |
| `analyze_workload.ipynb` | Kiểm chứng thống kê tải sinh ra (offline) và so sánh tải thiết kế với tải thực nhận (dữ liệu thật) |
| `preprocess_gatgru.ipynb` | Tiền xử lý thành dữ liệu đồ thị cho GAT-GRU; giải thích từng feature |
| `feature_selection_gatgru.ipynb` | Chọn feature nút và feature cạnh: lọc thống kê + permutation importance |
| `train_gatgru.ipynb` | Tìm siêu tham số (Optuna), huấn luyện lại 5 seed, đánh giá test, xem attention |
| `DATASET_GUIDE.md` | Kiến thức về thu dataset: run, số run, chia tập, rò rỉ dữ liệu, dữ liệu cho PPO |

## Chạy

Yêu cầu: cụm, Istio, Online Boutique, monitoring và tracing đã sẵn sàng (`cluster-setup/`).

### Bước 1. Kiểm tra HPA (máy có kubectl)

```bash
bash cluster-setup/07-setup-hpa.sh status
#   -> 10 HPA, cột TARGETS có số % (không phải <unknown>), mọi deployment đang ở 1 replica
```

### Bước 2. Đồng bộ thư mục lên node-loadgen (máy điều khiển, từ repo root)

```bash
rsync -av --exclude='.venv/' --exclude='results/' --exclude='processed_math*/' --exclude='__pycache__/' \
  -e "ssh -i ~/.ssh/kltn_autoscaling" math-load-testing/ ubuntu@<NODE_LOADGEN_FLOATING_IP>:~/math-load-testing/
```

Chạy lại lệnh này **mỗi khi sửa** code trong thư mục. Lệnh đồng bộ cả `node-ips.env`, nên file này phải có `NODE_OBSERVABILITY_FIXED_IP` và `FRONTEND_URL` đúng.

### Bước 3. Chạy thử 2 phút (node-loadgen)

```bash
ssh -i ~/.ssh/kltn_autoscaling ubuntu@<NODE_LOADGEN_FLOATING_IP>
cd ~
RUN_DURATION_SEC=120 COOLDOWN_SEC=0 bash math-load-testing/run-scenario.sh mmpp 1
```

Lần đầu, script tự tạo `.venv/` và cài Locust. Kiểm tra kết quả:

```bash
ls math-load-testing/results/mmpp/run_01/                              # đủ file như mục "Kết quả mỗi run"
cat math-load-testing/results/mmpp/run_01/driver_report.json           # started > 0, dropped = 0
cat math-load-testing/results/mmpp/run_01/collect_report.json          # prometheus_series > 0, đủ 10 jaeger_services
cat math-load-testing/results/mmpp/run_01/locust_failures.csv          # không có lỗi POST /cart/checkout
```

Locust luôn báo **1 user**: đó là driver, mỗi arrival chạy trong greenlet riêng. Tải thật xem ở `driver_report.json` (`started`, `max_in_flight`) và `Requests/s` trong `locust_stats.csv`.

Xóa run thử trước khi thu thật (nếu không, đợt chính bắt đầu từ `run_02` và lệch seed):

```bash
rm -rf math-load-testing/results/mmpp/run_01
```

### Bước 4. Thu từng kịch bản: mỗi lần 1 kịch bản × 8 run

**Không chạy cả 4 kịch bản liền một mạch.** Jaeger (`all-in-one`, Badger lưu bền, sampling 100%) không tự xóa span cũ: TTL mặc định là 72 giờ. Mỗi run 30 phút sinh vài triệu span, rồi `collect_metrics.py` lại gửi khoảng 1.800 truy vấn `api/traces` ngay sau run. Dữ liệu dồn qua nhiều run làm node-observability quá tải dần: với 4 vCPU / 4 GB, đợt chạy liền chỉ thu được 4 run rồi dừng.

Telemetry đã được xuất ra CSV sau mỗi run, nên Jaeger chỉ cần giữ span của kịch bản đang chạy. Vì vậy mỗi kịch bản là một chu trình riêng:

```text
4a. chạy 1 kịch bản × 8 run  ──▶  4b. theo dõi  ──▶  4c. kéo kết quả + kiểm tra  ──▶  4d. reset Jaeger  ──▶  kịch bản tiếp theo
```

Thứ tự kịch bản: `nhpp` → `mmpp` → `pareto` → `onoff`. Mỗi kịch bản (8 run × 30 phút, cộng thời gian xuất telemetry, cooldown và chờ HPA) mất khoảng **5–7 giờ**.

#### 4a. Chạy 1 kịch bản ở chế độ nền (node-loadgen)

Đặt tên kịch bản vào `S`, rồi chạy:

```bash
cd ~
S=nhpp      # lần lượt: nhpp, mmpp, pareto, onoff
setsid nohup bash math-load-testing/run-scenario.sh "$S" 8 \
  > "math-load-testing/results/${S}.log" 2>&1 < /dev/null &
echo $! > "math-load-testing/results/${S}.pid"
```

Sau lệnh này có thể thoát SSH. Cách khác: chạy trong `tmux new -s mathload` rồi gõ trực tiếp `bash math-load-testing/run-scenario.sh nhpp 8`.

Trước khi chạy kịch bản đầu tiên, nên reset Jaeger như bước 4d để bắt đầu với Badger trống.

#### 4b. Theo dõi, dừng (node-loadgen)

```bash
S=nhpp
tail -f ~/math-load-testing/results/${S}.log                                     # log (Ctrl+C chỉ thoát tail)
grep -E "^=== \[math\]|Saved|⚠️|complete" ~/math-load-testing/results/${S}.log   # tiến độ
ps -o pid,etime,cmd -g "$(cat ~/math-load-testing/results/${S}.pid)"             # còn chạy không
kill -- -"$(cat ~/math-load-testing/results/${S}.pid)"                           # dừng tất cả, kể cả locust
```

Kịch bản xong khi log có dòng `[math] Scenario <S> complete: 8 run(s)`.

Theo dõi node-observability trong lúc chạy, nhất là ở 2–3 run cuối:

```bash
sudo docker stats --no-stream                                  # CPU/RAM của jaeger, prometheus
free -h; df -h
sudo du -sh /var/lib/docker/volumes/*jaeger*                   # dung lượng Badger
sudo docker logs --tail 50 jaeger                              # lỗi queue full, OOM
```

#### 4c. Kéo kết quả về và kiểm tra (máy điều khiển, từ repo root)

```bash
rsync -av -e "ssh -i ~/.ssh/kltn_autoscaling" \
  ubuntu@<NODE_LOADGEN_FLOATING_IP>:~/math-load-testing/results/ math-load-testing/results/
```

Kiểm tra cả 8 run theo mục [Kiểm tra chất lượng từng run](#kiểm-tra-chất-lượng-từng-run): đủ `collect_report.json`, `jaeger_services` đủ 10 service, `jaeger_truncated_windows = 0`, `dropped = 0`. Run nào lỗi telemetry thì **xuất lại ngay lúc này**, trước khi reset Jaeger (xem "Xuất lại telemetry" ở mục đó).

#### 4d. Reset Jaeger (node-observability)

Chỉ reset sau khi bước 4c đạt. **Reset xong thì không xuất lại telemetry của kịch bản cũ được nữa.** Prometheus giữ nguyên.

```bash
ssh -i ~/.ssh/kltn_autoscaling ubuntu@<NODE_OBSERVABILITY_FLOATING_IP>
cd ~/monitoring-stack
sudo docker compose stop jaeger
sudo docker compose rm -f jaeger jaeger-data-init
sudo docker volume ls | grep jaeger                  # thường là monitoring-stack_jaeger_data
sudo docker volume rm monitoring-stack_jaeger_data
sudo docker compose up -d jaeger-data-init jaeger
curl -s localhost:16686/api/services                 # Jaeger đã lên lại
```

Đợi khoảng 1 phút để Envoy gửi span trở lại (`api/services` liệt kê lại các service), rồi quay lại bước 4a với kịch bản tiếp theo.

### Bước 5. Chạy tiếp sau gián đoạn (node-loadgen)

Run hoàn chỉnh có `collect_report.json`. Tìm và xóa run dở, rồi chạy **số run còn thiếu** của kịch bản đó bằng lệnh `nohup` ở bước 4a:

```bash
cd ~/math-load-testing/results
for d in */run_*; do [[ -f "$d/collect_report.json" ]] || echo "chưa xong: $d"; done
rm -rf <scenario>/run_XX
# ví dụ nhpp đã xong 4 run: kéo kết quả về + kiểm tra (4c), reset Jaeger (4d),
# rồi chạy run-scenario.sh nhpp 4 thay cho nhpp 8 ở bước 4a
```

Số thứ tự run được đánh tiếp sau các run đã có, nên seed vẫn đúng.

### Bước 6. Sau khi đủ 4 kịch bản

Kéo kết quả lần cuối (bước 4c), kiểm tra chất lượng (mục bên dưới), mở mục 8 của `analyze_workload.ipynb` để so tải thiết kế với tải thực nhận, rồi chạy các notebook mô hình.

### Chờ HPA giữa các run

Trước mỗi run, script đọc `max(kube_deployment_spec_replicas)` từ Prometheus (loadgen không có kubectl) và chờ tới khi mọi deployment về 1 replica. HPA chỉ scale down sau 5 phút ổn định rồi bớt 1 pod mỗi 60s, nên từ 10 replica về 1 mất khoảng 14 phút. Quá `REPLICA_WAIT_MAX_SEC` thì script in cảnh báo và vẫn chạy tiếp; số replica lúc bắt đầu được ghi vào `meta.json` (`replicas_at_start`).

### Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `RUN_DURATION_SEC` | `1800` | Độ dài run (cũng là độ dài trace) |
| `COOLDOWN_SEC` | `120` | Nghỉ sau mỗi run để request tồn đọng xử lý xong (trước bước chờ replica) |
| `REPLICA_WAIT_MAX_SEC` | `1200` | Thời gian chờ tối đa để mọi deployment về 1 replica trước mỗi run |
| `SEED_BASE` | `1000` | Seed của `run_k` = `SEED_BASE + k`. Run cùng số thứ tự sinh **cùng trace**, nên các phương pháp autoscaling so sánh được theo cặp |
| `MAX_CONCURRENT` | `400` | Số phiên/request đồng thời tối đa trên loadgen 1 vCPU. Arrival vượt mức bị **bỏ và đếm** trong `driver_report.json` |
| `RESULTS_DIR` | `math-load-testing/results` | Thư mục kết quả. Đặt thư mục riêng cho mỗi phương pháp trong thực nghiệm cuối, khi đó run thứ k của mọi phương pháp có cùng seed |
| `STEP_SEC` | `10` | Bước lưới khi xuất telemetry |

Số thứ tự run được đánh tiếp sau các run đã có trong `results/<scenario>/`, nên có thể chạy dồn nhiều đợt. Run dở dang (không có `collect_report.json`) phải xóa trước khi chạy tiếp, để seed không bị lệch.

## Kết quả mỗi run (`results/<scenario>/run_XX/`)

| File | Nội dung |
|---|---|
| `meta.json` | `scenario`, `run_id`, `pipeline: math`, `arrival_model: open`, `seed`, `start`, `end` (unix s), `duration_sec`, `step_sec`, `frontend_url`, `max_concurrent`, `autoscaler: hpa`, `replicas_at_start`, `locust_exit_code` |
| `node_metrics.csv` | `timestamp, service, cpu_cores, cpu_sidecar_cores, cpu_throttle_ratio, mem_bytes, net_rx_bps, net_tx_bps, cpu_request_cores, cpu_limit_cores, restarts_total, replicas, replicas_desired` |
| `service_metrics.csv` | `timestamp, service, request_count, error_count, latency_p50_ms, latency_p95_ms` (span `server` của Envoy) |
| `edge_metrics.csv` | `timestamp, source, target, call_count, error_count, latency_p50_ms, latency_p95_ms` (span `client` của Envoy) |
| `locust_*.csv`, `locust.html` | Báo cáo Locust phía client (RPS, p95, lỗi theo endpoint) |
| `collect_report.json` | Số series mỗi query, service tìm thấy trong Jaeger, số cửa sổ trace bị cắt, thời gian xuất |
| `arrival_trace.csv` | Mọi arrival đã lập lịch: `t`, `kind` (session/request), `source`, `size` (số thao tác hoặc số sản phẩm), `state` (trạng thái MMPP...) |
| `designed_rate.csv` | Tải thiết kế mỗi giây: `lambda_sessions`, `expected_rps`, `state`. Dùng làm ground truth của nhu cầu |
| `trace_meta.json` | Mô hình, seed, pha NHPP / đường đi MMPP, tải kỳ vọng |
| `driver_report.json` | Arrival đã lập lịch / đã chạy / **bị bỏ** / lỗi, số phiên đồng thời tối đa |

Ghi chú về nguồn dữ liệu:
- CPU, memory và network lấy theo pod rồi **gộp theo service** (tên pod `<deployment>-<hash>-<id>`). CPU ứng dụng không tính container `istio-proxy`; CPU của sidecar được tách riêng thành `cpu_sidecar_cores`.
- Cạnh `A → B` được lấy từ span `client` của sidecar A. Đích xác định qua tag `upstream_cluster`, hoặc qua span `server` con. Online Boutique chưa truyền tiếp trace header, nên mỗi trace chỉ gồm một cặp client–server. Điều này **đủ** để đếm cạnh, nhưng Jaeger UI sẽ không hiện một chuỗi trace đầy đủ.
- `cartservice → redis-cart` là TCP, không có span, nên `call_rate = 0`. Cạnh vẫn được giữ trong topology tĩnh; tải của `redis-cart` thể hiện qua network và `cpu_sidecar_cores`.

## Kiểm tra chất lượng từng run

```bash
cat math-load-testing/results/mmpp/run_03/collect_report.json
cat math-load-testing/results/mmpp/run_03/driver_report.json
```

| Dấu hiệu | Nguyên nhân thường gặp | Xử lý |
|---|---|---|
| `prometheus_series.<x> = 0` | Target kubelet/KSM down, sai namespace | Xem Prometheus → Status → Targets |
| `jaeger_services` thiếu service | Pod thiếu sidecar, hoặc tracing chưa bật | Báo người phụ trách `cluster-setup/` |
| `jaeger_truncated_windows > 0` | Quá nhiều trace trong 1–2s | Tăng `TRACE_LIMIT` (mặc định 1500) khi gọi `collect_metrics.py` |
| Notebook báo `jaeger_coverage < 0.9` | Jaeger (node-observability 2 vCPU) rơi span khi tải cao | Loại run đó, hoặc đặt `CORRECT_COVERAGE = True` trong notebook |
| `driver_report.json`: `dropped > 0` | Loadgen 1 vCPU quá tải, tải thực thấp hơn thiết kế | Loại run, hoặc tăng `MAX_CONCURRENT` nếu CPU loadgen còn dư |
| `meta.json`: `replicas_at_start > 1` | HPA chưa kịp scale về 1 trong `REPLICA_WAIT_MAX_SEC` | Kiểm tra HPA; loại run nếu cần trạng thái đầu chuẩn |

`locust_exit_code = 1` trong `meta.json` là bình thường khi có request lỗi, ví dụ lúc trạng thái High làm hệ thống quá tải. Run vẫn được thu.

**Xuất lại telemetry** cho một run (trên node-loadgen), lấy `<start>`/`<end>` từ `meta.json`:

```bash
cd ~/math-load-testing && .venv/bin/python collect_metrics.py --start <start> --end <end> \
  --out results/mmpp/run_03 --prometheus http://<obs-fixed-ip>:9090 --jaeger http://<obs-fixed-ip>:16686
```

Chỉ xuất lại được khi dữ liệu còn được lưu: Prometheus mặc định 15 ngày, còn Jaeger chỉ giữ span tới **lần reset tiếp theo** (bước 4d), tối đa 72 giờ. Vì vậy hãy kiểm tra `collect_report.json` ngay sau mỗi kịch bản, trước khi reset.

## Kiểm chứng bộ sinh tải

Các bộ sinh đã được kiểm định thống kê (offline, xem `analyze_workload.ipynb`):

| Kiểm tra | Kết quả |
|---|---|
| Poisson: thời gian giữa hai lần đến ~ Exp | CV = 1,005; KS = 0,004 < ngưỡng 5% 0,0096 |
| NHPP: số phiên mỗi phút so với $\int\lambda$ | sai lệch tối đa 3,8%; tổng 12.602 so với 12.600 |
| MMPP: thời gian lưu trú | 119,5 / 179,8 / 59,8 s (cấu hình 120 / 180 / 60); tỉ lệ thời gian khớp phân phối dừng (0,20 / 0,65 / 0,15) |
| Bounded Pareto | nằm trong [1, 40]; E[X] = 3,17 (lý thuyết 3,17); KS đạt |
| Tải kỳ vọng so với thực nghiệm (20 trace) | nhpp 42,0 / 42,1; pareto 20,3 / 20,2; onoff 26,8 / 27,0; mmpp 28,9 (dài hạn) / 30,8 ± 7 (run 30 phút) |
| ON/OFF: Hurst | 0,70–0,71 (Poisson cùng trung bình: 0,51) |

Hurst ước lượng (0,7) thấp hơn lý thuyết (0,85) vì thời gian ON/OFF **bị chặn trên** (300s và 600s). Tính tự tương tự vì vậy chỉ giữ được tới khoảng vài phút, đủ cho cửa sổ dự báo 2 phút của mô hình.

Driver được kiểm tra luồng điều khiển bằng stub (không có Locust thật): mọi arrival được lập lịch đều được thực thi, mỗi phiên Pareto checkout đúng một lần, tốc độ request khớp kỳ vọng. Sau run đầu tiên, hãy mở mục 8 của `analyze_workload.ipynb` để so tải thiết kế với tải frontend thực nhận.

## Tiền xử lý, chọn feature, huấn luyện GAT-GRU

```bash
pip install -r modeling-common/requirements-ml.txt     # numpy, pandas, matplotlib, torch, optuna, jupyter
cd math-load-testing
jupyter notebook
```

Chạy lần lượt (mỗi notebook đọc đầu ra của notebook trước trong `processed_math/`):

| Thứ tự | Notebook | Đầu ra trong `processed_math/` |
|---|---|---|
| 1 | `preprocess_gatgru.ipynb` | `runs_raw.npz` (mảng thô từng run + split), `gatgru_dataset.npz` (cửa sổ mặc định w = 12), `metadata.json`, `timeseries_raw.csv` (cho PPO). Tạo `modeling-common/splits_math.json` nếu chưa có |
| 2 | `feature_selection_gatgru.ipynb` | `selected_features.json`: feature nút và cạnh giữ lại, kèm báo cáo từng bước |
| 3 | `train_gatgru.ipynb` | `gatgru_predictions_test.npz`, `gatgru_model.pt`, `gatgru_best_config.json` |

Sau đó chạy phần LSTM và notebook so sánh trong [`../lstm-load-testing/`](../lstm-load-testing/README.md). Hai mô hình đọc **cùng các run** trong `results/` và cùng split.

- Khi `results/` chưa có run thật, các notebook tự chuyển sang **chế độ demo** (dữ liệu giả lập đúng định dạng file, ghi vào `processed_math_demo/`, ngân sách tune rất nhỏ) để kiểm tra pipeline.
- `h = 6` bước (dự báo 60s tới, đủ bao thời gian một pod mới sẵn sàng) được cố định. Window `w` ∈ {6, 12, 18} được tune.
- Target là `rps_in` và `cpu_cores` của từng service.
- Quy trình chọn feature và tune được mô tả trong [`../modeling-common/README.md`](../modeling-common/README.md).

### Feature đưa vào mô hình (tóm tắt)

Nguyên tắc: **chỉ dùng những gì controller quan sát được khi chạy thật** từ Prometheus/Jaeger. Thông tin của driver (trace, số phiên) **không** là feature, chỉ dùng để kiểm tra độ phủ và làm ground truth.

| Nhóm | Feature | Mục tiêu |
|---|---|---|
| Tải (nút) | `rps_in`, `rps_in_delta`, `rps_per_replica` | Nhu cầu tải, xu hướng, tải trên mỗi pod |
| Hiệu năng (nút) | `latency_p50_ms`, `latency_p95_ms`, `error_rate` | Tín hiệu SLO; độ trễ/lỗi lan truyền dọc chuỗi gọi |
| Tài nguyên (nút) | `cpu_cores`, `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`, `mem_mib`, `net_rx_kBps`, `net_tx_kBps` | Nhu cầu tài nguyên, mức sử dụng giống HPA, bão hòa ở limit, tải dự phòng qua Envoy và network |
| Năng lực (nút) | `replicas`, `restarts_delta` | Năng lực hiện tại (biến PPO điều khiển), sự kiện bất ổn |
| Cạnh | `call_rate`, `call_ratio`, `edge_error_rate`, `edge_latency_p95_ms` | Trọng số phụ thuộc động cho attention của GAT; hệ số fan-out để lan truyền tải xuống downstream |
| Toàn đồ thị | `e2e_rps`, `e2e_latency_p95_ms`, `e2e_error_rate`, `slo_violation`, `total_replicas` | Trạng thái SLO đầu-cuối và chi phí, dùng cho state/reward của PPO |

## Troubleshooting

1. **`frontend unreachable`**: `FRONTEND_URL` trong `node-ips.env` sai (NodePort của `frontend-external` có thể đổi khi service được tạo lại). Lấy lại NodePort trên máy có kubectl: `kubectl get svc frontend-external -n online-boutique`.
2. **`Prometheus unreachable`** hoặc **không đọc được `kube_deployment_spec_replicas`**: kiểm tra Prometheus và target kube-state-metrics trên `node-observability`.
3. **`Locust venv missing`**: script tự tạo `.venv/` (cần `python3-venv` trên node-loadgen).
4. **Mất SSH giữa run**: chạy trong `tmux` (hoặc `nohup`). Run dở dang phải xóa thủ công trong `results/`.
5. **Xuất telemetry chậm**: Jaeger trả trace theo từng cửa sổ 10s cho mỗi service; với run 30 phút ở trạng thái High, bước này mất vài phút.
6. **node-observability quá tải giữa chừng** (Jaeger không phản hồi, `collect_metrics.py` báo `WARN jaeger ... failed`, `jaeger_services` rỗng): Badger đã tích quá nhiều span. Dừng kịch bản (bước 4b), kéo kết quả về và giữ lại các run đạt (4c), xóa run dở, reset Jaeger (4d), rồi chạy số run còn thiếu (bước 5). Muốn Jaeger tự xóa span cũ mà không cần reset tay thì cần đặt `BADGER_SPAN_STORE_TTL` ngắn (ví dụ `3h`) trong `cluster-setup/04-setup-monitoring.sh`; báo người phụ trách `cluster-setup/`.
