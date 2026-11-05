# Load testing và thu raw dataset

## Thu dữ liệu hiệu chỉnh Digital Twin

Thiết kế và nguồn khoa học: [RL_DESIGN_PLAN.md](../docs/RL_DESIGN_PLAN.md).

Chỉ có một entry point: **`run-profiling.py`**; cấu hình nằm trong
**`profiling-plan.json`**. Script lập lịch replica, ghi command/desired/ready/available,
thu raw server spans, kiểm tra dữ liệu và xuất manifest có hash. Helper
`run-profile-loadgen.sh` chỉ được controller gọi nội bộ.

| Profile | Mục đích | Ngân sách đầy đủ |
|---|---|---|
| `smoke` | ON/OFF tải 0.5, `recommendationservice` 1→2→3→1 | 1 run đầu tiên khoảng 10 phút |
| `main` | 4 scenario × 3 mức tải × train/validation/test | 36 run × 35 phút = 21 giờ tải, chưa tính overhead |

Mỗi run quét tuần tự 1→2→3→1 cho năm service được PPO điều khiển, sau đó đo một
cấu hình joint hai service. Thứ tự service được xáo trộn theo seed nhưng tái lập được.
Pair joint còn được salt theo split/scenario/mức tải để 12 run train không lặp duy nhất
một cặp; surrogate nhận thêm tổng replica của cluster để nhận biết nhiễu tương tác.
Đây không phải Cartesian lookup table và không dùng MLP sinh toàn bộ next-state.
Sampling luôn **10%**, Redis cố định một replica và HPA phải tắt. Validator yêu cầu
target thật sự Available trước action kế tiếp; một lệnh scale thành công chưa đủ.

### Contract dữ liệu bắt buộc cho Digital Twin

Campaign chỉ được chấp nhận khi mỗi run giữ đủ các nhóm sau; không cần tạo lookup table
hay thêm nhiều file dẫn xuất:

| Nhóm | Trường bắt buộc | Vai trò |
|---|---|---|
| Workload ngoại sinh | `timestamp/t`, scenario, seed, `load_multiplier`, `expected_rps`, loại request/session, `size/work_units`, planned/started/completed/dropped/failed | Tách tải được phát khỏi tải hệ thống xử lý được |
| Replica lifecycle | thời điểm command, service, target, desired, total, ready, available ở mỗi poll; startup delay suy ra từ command→available | Học pending/warm-up và xác nhận action đã có hiệu lực |
| Tài nguyên | service, CPU app, CPU sidecar, throttling, RAM working set, RX/TX, CPU request/limit, restart, replicas desired/available | Hiệu chỉnh capacity, CPU/RAM và phát hiện nhiễu hạ tầng |
| Service performance | timestamp, service, sampled request/error count, P50/P95; raw server-span timestamp, duration, error, operation, trace/span ID | Fit và calibration phân phối latency ngẫu nhiên ở sampling 10% |
| Graph edge | timestamp, caller `source`, callee `target`, sampled call/error count, P50/P95 | Học fan-out và thuộc tính cạnh động để lan truyền tải |
| Provenance | protocol/study/cell ID, code/trace hash, sampling 10%, namespace, cluster-context hash, split | Chống lẫn campaign, sửa dữ liệu và rò rỉ split |

`capacity_events.jsonl` chứa lifecycle; các file CSV/JSON còn lại đã bao phủ contract trên.
Pod UID và node placement chỉ là trường mở rộng nếu nghiên cứu thêm restart/scheduling;
Digital Twin hiện tại không tuyên bố mô hình hóa scheduler hay contention theo node.

Chạy từ repo root trên máy Linux/WSL có kubectl/SSH/rsync. Sync helper mới bằng
`bash cluster-setup/06-setup-loadgen.sh`, rồi tắt HPA bằng
`bash cluster-setup/07-setup-hpa.sh delete`. Giữ máy điều khiển hoạt động.
Kubeconfig dùng `127.0.0.1:6443`, vì vậy phải giữ SSH tunnel Kubernetes API theo
`cluster-setup/README.md` trong suốt quá trình thu. Private key có passphrase phải được
nạp bằng `ssh-add` trước khi chạy controller vì controller dùng SSH `BatchMode`.

```bash
# 1. Preview smoke, không tác động cluster.
python3 load-testing/run-profiling.py --profile smoke

# 2. Chạy đúng một smoke run; thay host/key.
python3 load-testing/run-profiling.py --profile smoke --confirm --max-new 1 \
  --ssh-target ubuntu@LOADGEN_IP --ssh-key /path/to/kltn_autoscaling

# 3. Khi đủ ba split smoke mới có thể export; smoke chỉ kiểm tra pipeline.
python3 load-testing/run-profiling.py --profile smoke \
  --export dataset/digital-twin-smoke-v1/digital_twin_manifest.json

# 4. Sau khi smoke đạt, preview campaign chính: phải có đúng 36 cell.
python3 load-testing/run-profiling.py --profile main

# 5. Chạy đúng một campaign dài. Validator kiểm tra từng run và dừng khi có lỗi.
mkdir -p dataset/digital-twin-calibration-v1
set -o pipefail
python3 load-testing/run-profiling.py --profile main --confirm --max-new 36 \
  --ssh-target ubuntu@LOADGEN_IP --ssh-key /path/to/kltn_autoscaling \
  2>&1 | tee dataset/digital-twin-calibration-v1/controller-campaign.log

# 6. Xuất manifest hoàn chỉnh; missing_cells phải rỗng.
python3 load-testing/run-profiling.py \
  --export dataset/digital-twin-calibration-v1/digital_twin_manifest.json
```

Thư mục mặc định là `dataset/digital-twin-smoke-v1` và
`dataset/digital-twin-calibration-v1`. Có thể override bằng `--output` và
`--remote-results`. Chạy lại đúng lệnh sẽ xác minh rồi bỏ qua run hoàn chỉnh. Không resume
study cũ bằng protocol mới. Test được thu cùng campaign nhưng notebook khóa test cho tới khi
Digital Twin, reward và quy tắc chọn checkpoint đã đóng băng.

Notebook [train_ppo_kaggle.ipynb](../notebooks/rl/train_ppo_kaggle.ipynb) nạp manifest bằng
`KAGGLE_TWIN_MANIFEST`: train/validation dùng hiệu chỉnh và gate; test đóng.

Kiểm thử local: `python3 -B -m unittest discover -s load-testing -p test_profiling.py -v`.

Thư mục này chứa code sinh tải và thu telemetry cho dataset duy nhất của đề tài. Tải được sinh **đúng theo các mô hình luồng đến chuẩn** trong lý thuyết hàng đợi và mô hình hóa traffic: **NHPP, MMPP, Bounded Pareto, ON/OFF**. Raw run được ghi vào [`../dataset/raw/`](../dataset/) và dùng chung cho GAT-GRU, GAT-LSTM, LSTM, GRU và RL. Notebook nằm riêng trong [`../notebooks/`](../notebooks/); code mô hình dùng chung nằm trong [`../modeling-common/`](../modeling-common/README.md).

Lý do đằng sau các lựa chọn (run là gì, vì sao 8 run, 30 phút, chia tập thế nào) được giải thích trong [`../dataset/DATASET_GUIDE.md`](../dataset/DATASET_GUIDE.md).

```text
 node-loadgen (1 vCPU)                                         node-observability
 ─────────────────────                                         ─────────────────────
 run-scenario.sh ── driver mở (Locust) ──HTTP──▶ frontend-external (K3s, 2 worker, Istio sidecar, HPA)
        │                                              │ Envoy spans (Zipkin :9411) ──▶ Jaeger :16686
        │                                              │ cAdvisor :10250 / KSM   ◀──── Prometheus :9090
        └─ sau mỗi run: collect_metrics.py ── query_range / api/traces ─────────────────┘
 dataset/raw/<scenario>/run_XX/ ──▶ notebooks/<model>/ ──▶ artifacts/<model>/
```

Lý do thiết kế:
- **Driver chạy trên `node-loadgen` riêng**, nên bộ sinh tải không chiếm CPU của cụm và không làm nhiễu dữ liệu.
- **Telemetry được xuất *sau* mỗi run** (Prometheus `query_range`, Jaeger `api/traces` cho đúng khoảng `[start, end]`), không lấy mẫu trực tiếp trong lúc chạy. Cách này không thêm tải lên hệ thống đang đo, và mọi metric nằm trên cùng một lưới thời gian 10s.
- **Không cần sửa hạ tầng.** Prometheus scrape cAdvisor của cả 3 node và kube-state-metrics. Metric Istio (`istio_requests_total`) **không** được scrape, nên tải và quan hệ gọi giữa service được lấy từ **span Envoy quan sát được trong Jaeger**. Sampling deployment giữ ở 10%; capture coverage của span vẫn phải kiểm tra riêng.
- **Raw dataset lịch sử thu khi HPA bật; calibration mới thu khi HPA tắt.** Dataset cũ vẫn cung cấp workload/graph/forecast. Campaign mới nhận dạng startup, capacity, CPU/request và phân phối service time trong miền replica 1–3.

## Vì sao dùng mô hình luồng đến mở

Locust thông thường dùng **mô hình đóng**: có N(t) user cố định, mỗi user gửi request, **chờ phản hồi**, nghỉ rồi gửi tiếp. Mô hình này có ba hạn chế:

| Hạn chế của mô hình đóng | Hệ quả |
|---|---|
| Thông lượng $X \approx N/(Z + R)$ phụ thuộc thời gian phản hồi $R$ | Khi cụm nghẽn, tải **tự giảm** (220 user: ~105 rps lúc nhàn rỗi nhưng chỉ ~55 rps khi R = 2s). Vi phạm SLO bị đánh giá thấp; `rps_in` không còn là "nhu cầu" thuần túy |
| Thời gian nghỉ phân phối đều | Luồng request **đều hơn Poisson** (index of dispersion < 1), không có tính dồn cục tự nhiên |
| Kịch bản điều khiển số user, không điều khiển λ(t) | Không mô tả được bằng một mô hình toán, khó viết phần "System Model" của báo cáo |

Ở đây, một **driver luồng đến mở** phát lại một trace sinh trước từ mô hình toán. Mỗi arrival được khởi chạy đúng thời điểm, **bất kể** các request trước đã xong hay chưa. Tải thiết kế (`designed_rate.csv`) được lưu cùng run làm tham chiếu workload đầu vào, không thay thế RPS thực thi hoặc RPS quan sát tại từng service.

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
| `run-scenario.sh` | Chạy N run (seed cố định) trên node-loadgen: chờ HPA về 1 replica, phát tải và xuất node/service/edge telemetry bằng `collect_metrics.py` |
| `run-profiling.py` | Pipeline hiệu chỉnh Digital Twin: preview, lịch 1→2→3→1, journal capacity, validation/export và khôi phục replica |
| `run-profile-loadgen.sh` | Helper nội bộ phát tải/thu telemetry; được controller gọi, không chạy riêng |
| `collect_metrics.py` | Xuất metric của một run từ Prometheus + Jaeger thành CSV |
| `requirements.txt` | `locust==2.31.8`, `requests` (venv `.venv/` trên node-loadgen, `run-scenario.sh` tự tạo nếu chưa có) |
| `cluster-setup/node-ips.env` | Inventory runtime được sinh bởi `00-generate-node-ips.sh`; `06-setup-loadgen.sh` chép bản này sang node-loadgen |
| `notebooks/dataset/analyze_workload.ipynb` | Kiểm chứng thống kê tải sinh ra và so sánh tải thiết kế với tải quan sát được |
| `notebooks/gatgru/preprocess_gatgru.ipynb` | Tiền xử lý raw telemetry thành dữ liệu đồ thị |
| `notebooks/gatgru/feature_selection_gatgru.ipynb` | Phân tích feature nút và feature cạnh trên train/validation |
| `notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb` | Tune, huấn luyện năm seed và đánh giá mô hình GAT-GRU cuối |
| `dataset/DATASET_GUIDE.md` | Quy ước về run, split, chống rò rỉ dữ liệu và phạm vi sử dụng dataset |

## Chạy

Yêu cầu: cụm, Istio, Online Boutique, monitoring và tracing đã sẵn sàng (`cluster-setup/`).

### Bước 1. Kiểm tra HPA (máy có kubectl)

```bash
bash cluster-setup/07-setup-hpa.sh status
#   -> 10 HPA, cột TARGETS có số % (không phải <unknown>), mọi deployment đang ở 1 replica
```

### Bước 2. Đồng bộ thư mục lên node-loadgen (máy điều khiển, từ repo root)

```bash
bash cluster-setup/06-setup-loadgen.sh
```

Chạy lại lệnh này **mỗi khi sửa** code trong thư mục. Script đồng bộ code và chép
`cluster-setup/node-ips.env` sang node-loadgen; inventory phải có
`NODE_OBSERVABILITY_FIXED_IP` và `FRONTEND_URL` đúng.

### Bước 3. Chạy thử 2 phút (node-loadgen)

```bash
ssh -i ~/.ssh/kltn_autoscaling ubuntu@<NODE_LOADGEN_FLOATING_IP>
cd ~
RUN_DURATION_SEC=120 COOLDOWN_SEC=0 bash load-testing/run-scenario.sh mmpp 1
```

Lần đầu, script tự tạo `.venv/` và cài Locust. Kiểm tra kết quả:

```bash
ls dataset/raw/mmpp/run_01/                              # đủ file như mục "Kết quả mỗi run"
cat dataset/raw/mmpp/run_01/driver_report.json           # started > 0, dropped = 0
cat dataset/raw/mmpp/run_01/collect_report.json          # prometheus_series > 0, đủ 10 jaeger_services
cat dataset/raw/mmpp/run_01/locust_failures.csv          # không có lỗi POST /cart/checkout
```

Locust luôn báo **1 user**: đó là driver, mỗi arrival chạy trong greenlet riêng. Tải thật xem ở `driver_report.json` (`started`, `max_in_flight`) và `Requests/s` trong `locust_stats.csv`.

Xóa run thử trước khi thu thật (nếu không, đợt chính bắt đầu từ `run_02` và lệch seed):

```bash
rm -rf dataset/raw/mmpp/run_01
```

### Bước 4. Thu từng kịch bản: mỗi lần 1 kịch bản × 8 run

**Không chạy cả 4 kịch bản liền một mạch.** Jaeger (`all-in-one`, Badger lưu bền, sampling 10%) không tự xóa span cũ: TTL mặc định là 72 giờ. Mỗi run 30 phút sinh nhiều span, rồi `collect_metrics.py` lại gửi khoảng 1.800 truy vấn `api/traces` ngay sau run. Dữ liệu dồn qua nhiều run làm node-observability quá tải dần: với 4 vCPU / 4 GB, đợt chạy liền chỉ thu được 4 run rồi dừng.

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
setsid nohup bash load-testing/run-scenario.sh "$S" 8 \
  > "dataset/raw/${S}.log" 2>&1 < /dev/null &
echo $! > "dataset/raw/${S}.pid"
```

Sau lệnh này có thể thoát SSH. Cách khác: chạy trong `tmux new -s mathload` rồi gõ trực tiếp `bash load-testing/run-scenario.sh nhpp 8`.

Trước khi chạy kịch bản đầu tiên, nên reset Jaeger như bước 4d để bắt đầu với Badger trống.

#### 4b. Theo dõi, dừng (node-loadgen)

```bash
S=nhpp
tail -f ~/dataset/raw/${S}.log                                     # log (Ctrl+C chỉ thoát tail)
grep -E "^=== \[math\]|Saved|⚠️|complete" ~/dataset/raw/${S}.log   # tiến độ
ps -o pid,etime,cmd -g "$(cat ~/dataset/raw/${S}.pid)"             # còn chạy không
kill -- -"$(cat ~/dataset/raw/${S}.pid)"                           # dừng tất cả, kể cả locust
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
  ubuntu@<NODE_LOADGEN_FLOATING_IP>:~/dataset/raw/ dataset/raw/
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
cd ~/dataset/raw
for d in */run_*; do [[ -f "$d/collect_report.json" ]] || echo "chưa xong: $d"; done
rm -rf <scenario>/run_XX
# ví dụ nhpp đã xong 4 run: kéo kết quả về + kiểm tra (4c), reset Jaeger (4d),
# rồi chạy run-scenario.sh nhpp 4 thay cho nhpp 8 ở bước 4a
```

Số thứ tự run được đánh tiếp sau các run đã có, nên seed vẫn đúng.

### Bước 6. Sau khi đủ 4 kịch bản

Kéo kết quả lần cuối (bước 4c), kiểm tra chất lượng (mục bên dưới), mở mục 8 của `notebooks/dataset/analyze_workload.ipynb` để so tải thiết kế với tải quan sát được, rồi chạy các notebook mô hình.

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
| `RESULTS_DIR` | `dataset/raw` | Thư mục kết quả. Đặt thư mục riêng cho mỗi phương pháp trong thực nghiệm cuối, khi đó run thứ k của mọi phương pháp có cùng seed |
| `STEP_SEC` | `10` | Bước lưới khi xuất telemetry |

Số thứ tự run được đánh tiếp sau các run đã có trong `dataset/raw/<scenario>/`, nên có thể chạy dồn nhiều đợt. Run dở dang (không có `collect_report.json`) phải xóa trước khi chạy tiếp, để seed không bị lệch.

## Kết quả mỗi run (`dataset/raw/<scenario>/run_XX/`)

| File | Nội dung |
|---|---|
| `meta.json` | `scenario`, `run_id`, `pipeline: math`, `arrival_model: open`, `seed`, `start`, `end` (unix s), `duration_sec`, `step_sec`, `frontend_url`, `max_concurrent`, `autoscaler: hpa`, `replicas_at_start`, `locust_exit_code` |
| `node_metrics.csv` | `timestamp, service, cpu_cores, cpu_sidecar_cores, cpu_throttle_ratio, mem_bytes, net_rx_bps, net_tx_bps, cpu_request_cores, cpu_limit_cores, restarts_total, replicas, replicas_desired` |
| `service_metrics.csv` | `timestamp, service, request_count, error_count, latency_p50_ms, latency_p95_ms` (span `server` của Envoy) |
| `edge_metrics.csv` | `timestamp, source, target, call_count, error_count, latency_p50_ms, latency_p95_ms` (span `client` của Envoy) |
| `locust_*.csv`, `locust.html` | Báo cáo Locust phía client (RPS, p95, lỗi theo endpoint) |
| `collect_report.json` | Số series mỗi query, service tìm thấy trong Jaeger, số cửa sổ trace bị cắt, thời gian xuất |
| `arrival_trace.csv` | Mọi arrival đã lập lịch: `t`, `kind` (session/request), `source`, `size` (số thao tác hoặc số sản phẩm), `state` (trạng thái MMPP...) |
| `designed_rate.csv` | Tải thiết kế mỗi giây: `lambda_sessions`, `expected_rps`, `state`. Dùng làm tham chiếu workload đầu vào |
| `trace_meta.json` | Mô hình, seed, pha NHPP / đường đi MMPP, tải kỳ vọng |
| `driver_report.json` | Arrival đã lập lịch / đã chạy / **bị bỏ** / lỗi, số phiên đồng thời tối đa |

Ghi chú về nguồn dữ liệu:
- CPU, memory và network lấy theo pod rồi **gộp theo service** (tên pod `<deployment>-<hash>-<id>`). CPU ứng dụng không tính container `istio-proxy`; CPU của sidecar được tách riêng thành `cpu_sidecar_cores`.
- Cạnh `A → B` được lấy từ span `client` của sidecar A. Đích xác định qua tag `upstream_cluster`, hoặc qua span `server` con. Online Boutique chưa truyền tiếp trace header, nên mỗi trace chỉ gồm một cặp client–server. Cấu trúc này cho phép đếm các cạnh quan sát được, nhưng Jaeger UI không hiện một chuỗi trace đầy đủ.
- `cartservice → redis-cart` là TCP, không có span, nên `call_rate = 0`. Cạnh vẫn được giữ trong topology tĩnh; tải của `redis-cart` thể hiện qua network và `cpu_sidecar_cores`.
- Với intervention, `jaeger_server_spans.csv` là file tùy chọn do `--raw-spans-out` tạo; file chứa timestamp, service, latency, error, operation, trace ID và span ID để phân tích phân phối latency ở cấp span.

## Kiểm tra chất lượng từng run

```bash
cat dataset/raw/mmpp/run_03/collect_report.json
cat dataset/raw/mmpp/run_03/driver_report.json
```

| Dấu hiệu | Nguyên nhân thường gặp | Xử lý |
|---|---|---|
| `prometheus_series.<x> = 0` | Target kubelet/KSM down, sai namespace | Xem Prometheus → Status → Targets |
| `jaeger_services` thiếu service | Pod thiếu sidecar, hoặc tracing chưa bật | Báo người phụ trách `cluster-setup/` |
| `jaeger_truncated_windows > 0` | Quá nhiều trace trong 1–2s | Tăng `TRACE_LIMIT` (mặc định 1500) khi gọi `collect_metrics.py` |
| Jaeger coverage thấp | Trace capture không quan sát đủ request | Ghi nhận giới hạn coverage; không diễn giải Jaeger RPS là toàn bộ tải thực. Thu lại dữ liệu nếu nghiên cứu cần kết luận theo tải tuyệt đối |
| `driver_report.json`: `dropped > 0` | Loadgen 1 vCPU quá tải, tải thực thấp hơn thiết kế | Loại run, hoặc tăng `MAX_CONCURRENT` nếu CPU loadgen còn dư |
| `meta.json`: `replicas_at_start > 1` | HPA chưa kịp scale về 1 trong `REPLICA_WAIT_MAX_SEC` | Kiểm tra HPA; loại run nếu cần trạng thái đầu chuẩn |

`locust_exit_code = 1` trong `meta.json` là bình thường khi có request lỗi, ví dụ lúc trạng thái High làm hệ thống quá tải. Run vẫn được thu.

**Xuất lại telemetry** cho một run (trên node-loadgen), lấy `<start>`/`<end>` từ `meta.json`:

```bash
cd ~/thesis/load-testing && .venv/bin/python collect_metrics.py --start <start> --end <end> \
  --out ../dataset/raw/mmpp/run_03 --prometheus http://<obs-fixed-ip>:9090 --jaeger http://<obs-fixed-ip>:16686
```

Chỉ xuất lại được khi dữ liệu còn được lưu: Prometheus mặc định 15 ngày, còn Jaeger chỉ giữ span tới **lần reset tiếp theo** (bước 4d), tối đa 72 giờ. Vì vậy hãy kiểm tra `collect_report.json` ngay sau mỗi kịch bản, trước khi reset.

## Kiểm chứng bộ sinh tải

Các bộ sinh đã được kiểm định thống kê (offline, xem `notebooks/dataset/analyze_workload.ipynb`):

| Kiểm tra | Kết quả |
|---|---|
| Poisson: thời gian giữa hai lần đến ~ Exp | CV = 1,005; KS = 0,004 < ngưỡng 5% 0,0096 |
| NHPP: số phiên mỗi phút so với $\int\lambda$ | sai lệch tối đa 3,8%; tổng 12.602 so với 12.600 |
| MMPP: thời gian lưu trú | 119,5 / 179,8 / 59,8 s (cấu hình 120 / 180 / 60); tỉ lệ thời gian khớp phân phối dừng (0,20 / 0,65 / 0,15) |
| Bounded Pareto | nằm trong [1, 40]; E[X] = 3,17 (lý thuyết 3,17); KS đạt |
| Tải kỳ vọng so với thực nghiệm (20 trace) | nhpp 42,0 / 42,1; pareto 20,3 / 20,2; onoff 26,8 / 27,0; mmpp 28,9 (dài hạn) / 30,8 ± 7 (run 30 phút) |
| ON/OFF: Hurst | 0,70–0,71 (Poisson cùng trung bình: 0,51) |

Hurst ước lượng (0,7) thấp hơn lý thuyết (0,85) vì thời gian ON/OFF **bị chặn trên** (300s và 600s). Tính tự tương tự vì vậy chỉ giữ được tới khoảng vài phút, đủ cho cửa sổ dự báo 2 phút của mô hình.

Driver được kiểm tra luồng điều khiển bằng stub (không có Locust thật): mọi arrival được lập lịch đều được thực thi, mỗi phiên Pareto checkout đúng một lần, tốc độ request khớp kỳ vọng. Sau run đầu tiên, hãy mở `notebooks/dataset/analyze_workload.ipynb` để so tải thiết kế với tải frontend thực nhận.

## Tiền xử lý và huấn luyện GAT-GRU

```bash
pip install -r modeling-common/requirements-ml.txt     # numpy, pandas, matplotlib, torch, optuna, jupyter
cd notebooks/gatgru
jupyter notebook
```

Pipeline chính dùng cache trong `dataset/processed/gatgru/`, contract trong `configs/gatgru/` và kết quả huấn luyện trong `artifacts/gatgru/`:

| Thứ tự | Notebook | Đầu ra chính |
|---|---|---|
| 1 | `notebooks/gatgru/preprocess_gatgru.ipynb` | `dataset/processed/gatgru/runs_raw.npz`, `gatgru_dataset.npz`, `metadata.json`, `timeseries_raw.csv` và `dataset/splits.json` |
| 2 | `notebooks/gatgru/feature_selection_gatgru.ipynb` và các notebook ablation | Báo cáo validation trong `artifacts/gatgru/` và feature contract trong `configs/gatgru/` |
| 3 | `notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb` | Checkpoint, prediction và metrics trong `artifacts/gatgru/v05_selected_features_final/` |

Sau đó chạy phần LSTM trong [`../notebooks/lstm/`](../notebooks/lstm/README.md) và notebook đánh giá trong `notebooks/comparison/`. Mọi mô hình đọc **cùng các run** trong `dataset/raw/` và cùng `dataset/splits.json`.

- `dataset/processed/` là cache dẫn xuất và bị Git ignore; raw dataset trong `dataset/raw/` được Git track và phải push.
- `h = 6` bước (dự báo 60s tới, đủ bao thời gian một pod mới sẵn sàng) được cố định. Window `w` ∈ {6, 12, 18} được tune.
- Target là `rps_in` và `cpu_cores` của từng service.
- Quy trình chọn feature và tune được mô tả trong [`../modeling-common/README.md`](../modeling-common/README.md).

### Telemetry ứng viên và contract cuối

Nguyên tắc: **chỉ dùng những gì controller quan sát được khi chạy thật** từ Prometheus/Jaeger. Thông tin của driver (trace, số phiên) **không** là feature; chỉ dùng để kiểm tra coverage và đối chiếu workload đầu vào.

Các feature dưới đây được tạo trong preprocessing để phục vụ phân tích. Mô hình GAT-GRU v05 chỉ nhận 10 node features và 3 edge features được liệt kê trong `configs/gatgru/feature_contract_v01.json`.

| Nhóm | Feature ứng viên | Mục tiêu |
|---|---|---|
| Tải (nút) | `rps_in`, `rps_in_delta`, `rps_per_replica` | Nhu cầu tải, xu hướng, tải trên mỗi pod |
| Hiệu năng (nút) | `latency_p50_ms`, `latency_p95_ms`, `error_rate` | Tín hiệu SLO; độ trễ/lỗi lan truyền dọc chuỗi gọi |
| Tài nguyên (nút) | `cpu_cores`, `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`, `mem_mib`, `net_rx_kBps`, `net_tx_kBps` | Nhu cầu tài nguyên, mức sử dụng giống HPA, bão hòa ở limit, tải dự phòng qua Envoy và network |
| Năng lực (nút) | `replicas`, `restarts_delta` | Năng lực hiện tại (biến PPO điều khiển), sự kiện bất ổn |
| Cạnh | `call_rate`, `call_ratio`, `edge_error_rate`, `edge_latency_p95_ms` | Trọng số phụ thuộc động cho attention của GAT; hệ số fan-out để lan truyền tải xuống downstream |
| Toàn đồ thị | `e2e_rps`, `e2e_latency_p95_ms`, `e2e_error_rate`, `slo_violation`, `total_replicas` | Trạng thái SLO đầu-cuối và chi phí, dùng cho state/reward của PPO |

Final v05 không dùng `error_rate`, `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`, `restarts_delta` hoặc `edge_error_rate` làm input dự báo. RL vẫn có thể lấy error/SLO metrics trực tiếp từ telemetry hoặc environment state.

## Troubleshooting

1. **`frontend unreachable`**: `FRONTEND_URL` trong `node-ips.env` sai. Kiểm tra địa chỉ của service `frontend-external` bằng `kubectl get svc frontend-external -n online-boutique` rồi cập nhật URL.
2. **`Prometheus unreachable`** hoặc **không đọc được `kube_deployment_spec_replicas`**: kiểm tra Prometheus và target kube-state-metrics trên `node-observability`.
3. **`Locust venv missing`**: script tự tạo `.venv/` (cần `python3-venv` trên node-loadgen).
4. **Mất SSH giữa run**: chạy trong `tmux` (hoặc `nohup`). Run dở dang phải xóa thủ công trong `dataset/raw/`.
5. **Xuất telemetry chậm**: Jaeger trả trace theo từng cửa sổ 10s cho mỗi service; với run 30 phút ở trạng thái High, bước này mất vài phút.
6. **node-observability quá tải giữa chừng** (Jaeger không phản hồi, `collect_metrics.py` báo `WARN jaeger ... failed`, `jaeger_services` rỗng): kiểm tra container có flag `--badger.span-store-ttl=3h0m0s`. Campaign v4 đã cấu hình TTL này trong monitoring Compose; không tăng sampling quá 10%.
