# Load testing và thu raw dataset

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
- **Luôn thu khi HPA bật.** Replica thay đổi theo tải, nên PPO thấy được ảnh hưởng của việc scale (xem mục 7 của DATASET_GUIDE).

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
| `collect_metrics.py` | Xuất metric của một run từ Prometheus + Jaeger thành CSV |
| `requirements.txt` | `locust==2.31.8`, `requests` (venv `.venv/` trên node-loadgen, `run-scenario.sh` tự tạo nếu chưa có) |
| `node-ips.env` | IP các node và `FRONTEND_URL` (điền tay, gitignored) |
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
ssh -i ~/.ssh/kltn_autoscaling ubuntu@<NODE_LOADGEN_FLOATING_IP> 'mkdir -p ~/load-testing ~/dataset/raw'
rsync -av --exclude='.venv/' --exclude='__pycache__/' \
  -e "ssh -i ~/.ssh/kltn_autoscaling" load-testing/ ubuntu@<NODE_LOADGEN_FLOATING_IP>:~/load-testing/
rsync -av dataset/raw/ \
  -e "ssh -i ~/.ssh/kltn_autoscaling" ubuntu@<NODE_LOADGEN_FLOATING_IP>:~/dataset/raw/
```

Chạy lại lệnh này **mỗi khi sửa** code trong thư mục. Lệnh đồng bộ cả `node-ips.env`, nên file này phải có `NODE_OBSERVABILITY_FIXED_IP` và `FRONTEND_URL` đúng.

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

## Thu dữ liệu can thiệp replica (ON-OFF và Pareto)

Quy trình này bổ sung dữ liệu đối chứng cho surrogate; **không chạy PPO**. Script điều khiển chạy trên máy Linux/WSL có `kubectl` + kubeconfig và SSH key tới node-loadgen. HPA phải tắt để replica không bị controller khác thay đổi. Loadgen chỉ phát tải và thu telemetry; replica do script điều khiển từ máy có `kubectl`.

Mỗi cặp dùng cùng scenario/seed (trace open-load xác định), với cùng một baseline trong đó 10 deployment ứng dụng ở 1 replica. Mỗi điều kiện intervention chỉ tăng **một** service; thứ tự baseline và các mức replica được xáo trộn trong từng lần lặp để giảm ảnh hưởng của drift theo thời gian. Đây là phép đo tác động biên từng service, không đo tương tác giữa nhiều service cùng scale.

Default study gồm 20 run: hai scenario × hai lần lặp × (một baseline + hai service × hai mức replica). Mỗi run 30 phút, chưa tính rollout, collector và cooldown; dự kiến hơn 10 giờ. Jaeger có giới hạn dung lượng như mô tả ở bước 4, nên controller mặc định chỉ chạy tối đa 3 **run mới** mỗi invocation. Sau khi xác minh CSV đã lưu, reset Jaeger theo bước 4d rồi chạy lại đúng lệnh/configuration; các run hợp lệ đã có sẽ được xác minh và bỏ qua. Sáu batch đầu thu 18 run, batch cuối thu 2. Có thể đặt `MAX_RUNS_PER_INVOCATION=0` để bỏ giới hạn, nhưng không khuyến nghị trên node-observability hiện tại. Có thể bắt đầu giảm thời lượng bằng `RUN_DURATION_SEC=600`, nhưng run ngắn không tương đương run 30 phút và cần giữ cố định giữa các điều kiện được so sánh.

```bash
# 1. Tắt HPA trên máy có kubectl; status phải báo không còn HPA.
bash cluster-setup/07-setup-hpa.sh delete
kubectl get hpa -n online-boutique

# 2. Đồng bộ helper mới lên node-loadgen từ repo root.
bash cluster-setup/06-setup-loadgen.sh

# 3. Xem plan và kiểm tra cấu hình local; bước này không scale và không phát tải.
TRACE_SAMPLING_PERCENTAGE=10 bash load-testing/run-intervention.sh --dry-run

# 4. Smoke test 3 phút mỗi điều kiện, ghi vào thư mục riêng để không đụng study chính.
TRACE_SAMPLING_PERCENTAGE=10 SCENARIOS=onoff \
INTERVENTION_SERVICES=frontend \
REPLICA_LEVELS=2 \
REPEATS=1 \
RUN_DURATION_SEC=180 \
COOLDOWN_SEC=0 \
LOCAL_RESULTS_DIR=dataset/intervention_smoke \
REMOTE_RESULTS_DIR=/home/ubuntu/dataset/intervention_smoke \
bash load-testing/run-intervention.sh --dry-run

# Đọc plan, rồi chạy thật smoke test.
TRACE_SAMPLING_PERCENTAGE=10 SCENARIOS=onoff INTERVENTION_SERVICES=frontend REPLICA_LEVELS=2 REPEATS=1 \
RUN_DURATION_SEC=180 COOLDOWN_SEC=0 \
LOCAL_RESULTS_DIR=dataset/intervention_smoke \
REMOTE_RESULTS_DIR=/home/ubuntu/dataset/intervention_smoke \
bash load-testing/run-intervention.sh --confirm

# 5. Khi smoke test đạt quality checks, xem plan study chính rồi mới chạy dài.
TRACE_SAMPLING_PERCENTAGE=10 bash load-testing/run-intervention.sh --dry-run
TRACE_SAMPLING_PERCENTAGE=10 bash load-testing/run-intervention.sh --confirm
```

Smoke test chỉ kiểm tra orchestration/telemetry, **không đủ làm kết quả noise floor hay kết luận causal**. Xác nhận từng run có `raw_server_spans > 0`, `jaeger_truncated_windows = 0`, `dropped = 0`, đủ 10 Jaeger services, desired/ready replicas đúng, và hai điều kiện có cùng `trace_sha256`.

Với study chính, giữ nguyên mọi biến cấu hình qua các batch. Sau khi một batch dừng ở thông báo `Batch limit reached`, kiểm tra các run local; nếu batch đạt quality checks và còn run chưa thu, reset Jaeger theo mục **4d** rồi chạy lại cùng lệnh `--confirm`. Script giữ nguyên thứ tự ngẫu nhiên/seed, không ghi đè và chỉ bỏ qua run đã khớp metadata, hash code, trace hash và quality checks. Nếu quality check fail hoặc code/configuration thay đổi, dừng lại để phân loại run; không reset Jaeger trước khi CSV của các run cần cứu đã được lưu.

Default study chính chỉ khảo sát `frontend` và `checkoutservice` ở mức 2, 3 replica, với các deployment khác cố định ở 1. Mở rộng có chủ đích bằng biến môi trường, ví dụ:

```bash
TRACE_SAMPLING_PERCENTAGE=10 INTERVENTION_SERVICES=frontend,cartservice,currencyservice,productcatalogservice,recommendationservice,checkoutservice \
REPLICA_LEVELS=2,4 \
REPEATS=3 \
bash load-testing/run-intervention.sh --dry-run
```

Chỉ chạy thật sau khi xem dry-run và kiểm tra số run/thời lượng. Có thể đặt `RUN_DURATION_SEC`, `COOLDOWN_SEC`, `MAX_TARGET_REPLICAS` để điều chỉnh có kiểm soát. `MAX_TARGET_REPLICAS` mặc định là 4; script từ chối mức replica cao hơn. Dùng `tmux` trên một **máy điều khiển còn hoạt động** để SSH ngắt kết nối không dừng script; chạy trong WSL trên laptop không bảo đảm tiếp tục nếu laptop tắt.

Script từ chối HPA đang hoạt động, lưu replica ban đầu, kiểm tra rollout/ready replicas trước mỗi run, không ghi đè kết quả cũ và khôi phục replica ban đầu khi kết thúc/lỗi/nhận SIGINT. Nếu script bị kill cứng hoặc máy điều khiển mất điện, kiểm tra replica thủ công bằng `kubectl get deployments -n online-boutique` trước khi tiếp tục. Trong thời gian chạy không bật lại HPA, không chạy benchmark khác trên cùng cụm.

Mỗi đường dẫn kết quả chỉ dùng một study; nếu cần chạy lại hoặc đổi plan, đặt `LOCAL_RESULTS_DIR` và `REMOTE_RESULTS_DIR` mới thay vì trộn/ghi đè dữ liệu cũ.

Kết quả nằm trong `dataset/intervention/`, theo cấu trúc `scenario/repeat_NN/condition/`. Mỗi run gồm telemetry như pipeline hiện tại, `meta.json` (scenario, seed, cặp, cấu hình intervention và hash trace), cùng `jaeger_server_spans.csv` chứa latency từng server span để đo noise floor. Kiểm tra:

```bash
cat dataset/intervention/onoff/repeat_01/baseline/collect_report.json
cat dataset/intervention/onoff/repeat_01/baseline/driver_report.json
cat dataset/intervention/onoff/repeat_01/baseline/meta.json
```

Chỉ dùng run đạt đủ các điều kiện: `jaeger_truncated_windows = 0`, `raw_server_spans > 0`, `driver_report.json` có `dropped = 0`, đủ service trong `jaeger_services`, và `node_metrics.csv` xác nhận replica desired/ready giữ đúng cấu hình suốt run. So sánh `trace_sha256` giữa các điều kiện cùng scenario để xác nhận trace giống hệt. Nếu collector cảnh báo Jaeger truncation, loadgen drop, hoặc replica lệch, đánh dấu run không hợp lệ và thu lại trước khi reset Jaeger. Jaeger vẫn tích lũy span; theo dõi dung lượng observability và chia đợt/reset Jaeger chỉ sau khi CSV của đợt đã được lưu an toàn.

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

## Intervention study v2 (current collection protocol)

The intervention controller now defaults to ON-OFF/Pareto, the main RL-controlled services, replica levels 2 and 4, three paired repeats, and a 90-second post-rollout settle period. It records the intended trace sampling percentage in `meta.json` and validates `replicas` and `replicas_desired` for every service row across the complete `node_metrics.csv`.

The production mesh remains configured at 10% Jaeger sampling and is not changed for this study. Run with `TRACE_SAMPLING_PERCENTAGE=10` (the controller rejects other values). Prometheus is the primary source for resource/replica telemetry; Jaeger spans remain sampled diagnostics. Report span coverage and do not interpret sampled raw spans as a complete request-level noise-floor distribution.

The end-of-study validator checks that `planned`, `expected_mean_rps`, and `designed_mean_rps` in `driver_report.json` are identical across paired conditions. Different `completed` counts are possible when latency changes; report them instead of silently treating the realized work as identical. The study remains a single-service marginal intervention and does not identify multi-service action interactions.

## Troubleshooting

1. **`frontend unreachable`**: `FRONTEND_URL` trong `node-ips.env` sai. Kiểm tra địa chỉ của service `frontend-external` bằng `kubectl get svc frontend-external -n online-boutique` rồi cập nhật URL.
2. **`Prometheus unreachable`** hoặc **không đọc được `kube_deployment_spec_replicas`**: kiểm tra Prometheus và target kube-state-metrics trên `node-observability`.
3. **`Locust venv missing`**: script tự tạo `.venv/` (cần `python3-venv` trên node-loadgen).
4. **Mất SSH giữa run**: chạy trong `tmux` (hoặc `nohup`). Run dở dang phải xóa thủ công trong `dataset/raw/`.
5. **Xuất telemetry chậm**: Jaeger trả trace theo từng cửa sổ 10s cho mỗi service; với run 30 phút ở trạng thái High, bước này mất vài phút.
6. **node-observability quá tải giữa chừng** (Jaeger không phản hồi, `collect_metrics.py` báo `WARN jaeger ... failed`, `jaeger_services` rỗng): Badger đã tích quá nhiều span. Dừng kịch bản (bước 4b), kéo kết quả về và giữ lại các run đạt (4c), xóa run dở, reset Jaeger (4d), rồi chạy số run còn thiếu (bước 5). Muốn Jaeger tự xóa span cũ mà không cần reset tay thì cần đặt `BADGER_SPAN_STORE_TTL` ngắn (ví dụ `3h`) trong `cluster-setup/04-setup-monitoring.sh`; báo người phụ trách `cluster-setup/`.
