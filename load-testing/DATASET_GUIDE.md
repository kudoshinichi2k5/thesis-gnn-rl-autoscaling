# Kiến thức về thu thập dataset cho GAT-GRU và PPO

Tài liệu này giải thích **vì sao** quy trình thu dữ liệu trong `load-testing/` được thiết kế như hiện tại. Các câu hỏi chính: run là gì, vì sao 8 run, vì sao 30 phút, vì sao bước 10 giây, chia tập thế nào cho đúng. Các bước chạy cụ thể nằm trong [README.md](README.md); cách tạo feature nằm trong [preprocess_gatgru.ipynb](preprocess_gatgru.ipynb).

---

## 1. Dataset này phục vụ việc gì

Theo đề cương, dataset là đầu vào cho hai thành phần:

| Thành phần | Dùng dataset để | Yêu cầu với dữ liệu |
|---|---|---|
| **GAT-GRU** (dự báo) | Học $\hat{y}_{t+1:t+h} = f(\mathcal{G}_{t-w+1:t})$: từ chuỗi đồ thị trạng thái quá khứ, dự báo nhu cầu tải/tài nguyên của từng service | Nhiều mẫu tải đa dạng (ổn định, spike, burst); các metric đồng bộ thời gian giữa các service; có quan hệ phụ thuộc (cạnh) |
| **PPO** (ra quyết định, huấn luyện offline) | Xây môi trường mô phỏng: khi tải là X và có R replica thì CPU, latency, SLO ra sao | Thấy được **tác động của số replica** lên hiệu năng (nên có cả dữ liệu khi autoscaler hoạt động) |

Cả hai đều cần dữ liệu **lặp lại được**, **đủ nhiều** và **không rò rỉ** giữa tập huấn luyện và tập đánh giá. Phần lớn quyết định thiết kế bên dưới xuất phát từ ba yêu cầu này.

---

## 2. Thuật ngữ

| Thuật ngữ | Định nghĩa trong dự án | Ví dụ |
|---|---|---|
| **Scenario (kịch bản)** | Một *hình dạng tải*: hàm số user theo thời gian, định nghĩa trong `scenarios/*.py` | `normal`, `spike`, `bursty` |
| **Run (lượt chạy)** | **Một lần thực thi trọn vẹn** một scenario từ đầu đến cuối, có thời điểm bắt đầu/kết thúc riêng và thư mục kết quả riêng | `results/spike/run_03/` = lần chạy thứ 3 của spike, dài 30 phút |
| **Step (bước)** | Một ô trên lưới thời gian, rộng `STEP_SEC = 10s`. Mọi metric được quy về các bước này | Run 30 phút có 180 step |
| **Snapshot đồ thị** | Trạng thái toàn hệ thống tại 1 step: 11 nút (service) có feature, cùng các cạnh có feature | $\mathcal{G}_t$ |
| **Window (cửa sổ, w)** | Số step quá khứ mô hình nhìn vào để dự báo | `w = 12` → 2 phút |
| **Horizon (h)** | Số step tương lai cần dự báo | `h = 6` → 60 giây |
| **Sample (mẫu)** | Một cặp (đầu vào w step, target h step) cắt từ một run | Một run tạo ra 157 mẫu |
| **Warm-up** | Đoạn đầu run bị loại vì số liệu chưa ổn định | 6 step đầu (60s) |
| **Cooldown** | Thời gian nghỉ giữa hai run để cụm trở về trạng thái nhàn rỗi | 120s |
| **Split** | Cách chia dữ liệu thành train / val / test | Chia theo run: 6 / 1 / 1 |
| **Telemetry** | Dữ liệu giám sát: metric (Prometheus) và trace (Jaeger) | CPU, số request, latency |
| **Span** | Một đoạn xử lý được Envoy ghi lại. `server` = request đi vào service, `client` = lời gọi đi ra | Span client của frontend gửi tới productcatalogservice |
| **Coverage (độ phủ)** | Tỉ lệ dữ liệu thu được so với thực tế | Request frontend đếm từ span / request Locust gửi |
| **Behavior policy** | Chính sách scaling đang chạy trong lúc thu dữ liệu | `none` (replica cố định) hoặc `hpa` |

Quan hệ giữa các khái niệm:

```text
Dataset
 └── Scenario (normal | spike | bursty)
      └── Run 01 … Run 08          ← đơn vị độc lập, đơn vị để chia train/val/test
           └── Step 0 … 179        ← 10s một step
                └── Snapshot đồ thị: 11 nút × 15 feature, 15 cạnh × 4 feature
      Cắt cửa sổ trượt trong mỗi run:
           Sample = [step s … s+11] → dự báo [step s+12 … s+17]
```

---

## 3. Run là gì và vì sao phải chạy nhiều run

### 3.1 Một run = một "thí nghiệm" độc lập

Mỗi run gồm: khởi động Locust, phát tải theo scenario trong 30 phút, dừng, xuất telemetry của đúng khoảng thời gian đó, rồi nghỉ 2 phút. Hai run của cùng scenario có **cùng hình dạng tải** nhưng **không giống hệt nhau**, vì có nhiều nguồn ngẫu nhiên:

| Nguồn biến thiên | Ví dụ |
|---|---|
| Hành vi user | Mỗi user chọn ngẫu nhiên thao tác (xem sản phẩm, thêm giỏ, checkout) và thời gian nghỉ 1–3s |
| Kịch bản ngẫu nhiên | `bursty` chọn lại vị trí và độ cao burst ở mỗi run; `normal` có jitter ±10% |
| Hệ thống | Garbage collection (JVM của adservice, Python), cache, vị trí pod trên worker, nhiễu mạng, I/O đĩa |
| Hạ tầng đo | Thời điểm scrape lệch pha với tải, Jaeger rơi span khi tải cao |

Lặp lại nhiều run giúp mô hình học **quy luật chung** của hệ thống thay vì học thuộc một chuỗi thời gian duy nhất.

### 3.2 Vì sao không chạy một run thật dài thay cho nhiều run ngắn

Ví dụ: một run 4 giờ so với 8 run 30 phút. Tổng thời lượng như nhau, nhưng nhiều run ngắn có lợi hơn:

1. **Chia tập không rò rỉ.** Các sample cắt bằng cửa sổ trượt **chồng lấn nhau** (sample *s* và *s+1* chung 11/12 step). Nếu một chuỗi dài bị cắt ngẫu nhiên thành train/test, test sẽ gần như trùng với train, cho kết quả đẹp giả. Chia **theo run** loại bỏ vấn đề này: test là các run mô hình chưa từng thấy (xem mục 6).
2. **Đo được độ biến thiên.** Có nhiều run thì báo cáo được MAE/RMSE dạng *trung bình ± độ lệch chuẩn* qua các run, thay vì một con số duy nhất.
3. **Trạng thái đầu giống nhau.** Mỗi run bắt đầu từ cụm nhàn rỗi (nhờ cooldown), giống điều kiện khi controller thật bắt đầu hoạt động. Một run dài thì trạng thái giữa chừng phụ thuộc vào lịch sử trước đó.
4. **Chịu lỗi tốt hơn.** Mất SSH, pod crash hay Jaeger quá tải chỉ hỏng một run 30 phút; loại run đó và chạy lại, không mất cả đợt.
5. **Phù hợp với cách đánh giá RL.** PPO huấn luyện theo *episode*, mỗi run tự nhiên tương ứng với một episode tải.

---

## 4. Vì sao 8 run cho mỗi scenario

Con số 8 là **lựa chọn thực dụng** cân bằng giữa 5 ràng buộc, không phải định luật. Lập luận cụ thể:

### 4.1 Ràng buộc 1: chia train/val/test theo run

Notebook chia mỗi scenario theo tỉ lệ 70/15/15 **tính bằng số run**, với mỗi tập ít nhất 1 run.

| Số run / scenario | train / val / test | Nhận xét |
|---:|---|---|
| 1–2 | không chia theo run được | Notebook phải chia theo thời gian bên trong run. Chấp nhận được để thử nghiệm, không đủ cho báo cáo |
| 3 | 1 / 1 / 1 | Tối thiểu về kỹ thuật; train chỉ có 1 run, mô hình dễ học thuộc |
| 5 | 3 / 1 / 1 | Dùng được nhưng train còn ít |
| **8** | **6 / 1 / 1** | **Train có 6 run đa dạng; vẫn giữ riêng 1 run cho val và 1 run cho test** |
| 16 | 12 / 2 / 2 | Tốt hơn, nhưng tốn gấp đôi thời gian |

Ngoài ra, 8 run chia đều được thành **4 fold × 2 run** cho kiểm định chéo theo run (mục 6.3). Nhờ đó cả 8 run đều lần lượt được dùng làm test, khắc phục nhược điểm "test chỉ có 1 run".

### 4.2 Ràng buộc 2: đủ số lượng mẫu

Với cấu hình mặc định (`RUN_DURATION_SEC = 1800`, `STEP_SEC = 10`, `WARMUP_DROP = 6`, `w = 12`, `h = 6`):

```text
Số step mỗi run        T     = 1800 / 10        = 180
Sau khi bỏ warm-up     T'    = 180 − 6          = 174
Số sample mỗi run      S_run = T' − w − h + 1   = 174 − 12 − 6 + 1 = 157
```

| | Run | Sample | Quan sát (step × service) |
|---|---:|---:|---:|
| Mỗi scenario (8 run) | 8 | 1.256 | 15.312 |
| Toàn dataset (3 scenario) | 24 | 3.768 | 45.936 |
| Train (6 run × 3 scenario) | 18 | 2.826 | 34.452 |

Một mô hình GAT-GRU cỡ nhỏ (2 lớp GAT, hidden 32–64, 1 lớp GRU) có khoảng vài chục nghìn tham số, và **trọng số được chia sẻ giữa 11 nút**. Mỗi sample vì vậy cung cấp 11 chuỗi để học. Khoảng 2.800 sample train (hơn 30.000 chuỗi nút) là mức hợp lý cho mô hình cỡ này.

Lưu ý: do các sample chồng lấn, **số mẫu độc lập thực sự ít hơn nhiều**. Độ đa dạng thật đến từ số run, không đến từ số sample. Đây cũng là lý do tăng số run có giá trị hơn tăng độ dài run.

### 4.3 Ràng buộc 3: đủ số sự kiện quan trọng

Mô hình cần thấy **đủ nhiều lần** các tình huống khó (đầu spike, cuối spike, burst ngẫu nhiên) thì mới dự báo được chúng:

| Scenario | Sự kiện / run | Sự kiện / 8 run | Trong train (6 run) |
|---|---|---:|---:|
| `spike` | 5 spike (bắt đầu ở 0, 6, 12, 18, 24 phút), tức 10 lần chuyển trạng thái lên/xuống | 40 spike | 30 spike, 60 lần chuyển |
| `bursty` | 60 cửa sổ 30s × 35% ≈ 21 burst | ≈ 168 burst | ≈ 126 burst |
| `normal` | 1 warm-up (bị loại) + 29 phút ổn định | 8 run ổn định | 6 run |

Khoảng 30 spike trong train là đủ để mô hình học được mẫu "trước spike, đầu spike, đỉnh, hồi phục". Với 3 run (1 train), mô hình chỉ thấy 5 spike, rất dễ học thuộc vị trí thay vì học cơ chế.

### 4.4 Ràng buộc 4: thời gian và thời hạn lưu dữ liệu

Thời gian thực tế cho mỗi run:

```text
30 phút tải + 20s chờ scrape cuối + 2–5 phút xuất telemetry + 2 phút cooldown ≈ 35–37 phút
```

| Phương án | Mỗi scenario | 3 scenario | Có kịp trong tuần 3 của kế hoạch? |
|---|---:|---:|---|
| 3 run | ≈ 1,8 giờ | ≈ 5,5 giờ | Có, nhưng dữ liệu yếu |
| **8 run** | **≈ 4,8 giờ** | **≈ 14,5 giờ** | **Có: một đêm + nửa ngày** |
| 16 run | ≈ 9,6 giờ | ≈ 29 giờ | Sát, và rủi ro hỏng giữa chừng cao hơn |

Jaeger Badger chỉ giữ span **72 giờ**. Toàn bộ đợt thu phải được xuất telemetry trong khoảng này, mà script đã xuất ngay sau mỗi run. 8 run vẫn nằm trong cùng một "phiên vận hành" của cụm (cùng phiên bản, cùng IP, cùng trạng thái), giảm khả năng dữ liệu bị lệch phân phối do hạ tầng thay đổi giữa các đợt.

### 4.5 Ràng buộc 5: lợi ích giảm dần

Độ chính xác dự báo thường tăng nhanh khi đi từ 1 → 5 run, rồi chậm lại. Cách kiểm chứng trên chính dữ liệu của dự án (**learning curve theo số run**):

1. Huấn luyện mô hình với 2, 4 và 6 run train của mỗi scenario, giữ nguyên val/test.
2. Vẽ MAE trên val theo số run train.
3. Nếu đường cong **vẫn đang giảm rõ** ở 6 run thì cần thêm run (ví dụ chạy thêm 4 run cho mỗi scenario; script tự đánh số tiếp). Nếu đường cong **đã phẳng** thì 8 là đủ.

Đây là cách trả lời câu hỏi "vì sao 8" bằng số liệu thay vì bằng cảm tính, và có thể đưa vào báo cáo.

### 4.6 Tóm tắt

> **8 run / scenario** là mức nhỏ nhất đáp ứng đồng thời: chia train/val/test theo run (6/1/1) và 4-fold theo run; khoảng 2.800 sample train và khoảng 30 spike / 126 burst để học; tổng thời gian khoảng 14 giờ, nằm gọn trong lịch và trong thời hạn lưu span 72 giờ của Jaeger. Nên xác nhận lại bằng learning curve sau khi có dữ liệu.

---

## 5. Các tham số khác và lý do chọn

| Tham số | Giá trị | Lý do |
|---|---|---|
| Độ dài run | 30 phút | Đủ chứa 5 chu kỳ spike (chu kỳ 6 phút) và khoảng 60 cửa sổ bursty. Cũng đủ dài để thấy các hiệu ứng chậm (rò rỉ bộ nhớ, HPA scale up/down nhiều lần). Ngắn hơn thì phần warm-up chiếm tỉ lệ lớn; dài hơn thì ít run hơn trong cùng thời gian |
| `STEP_SEC` | 10s | Bằng `scrape_interval` của Prometheus, nên mỗi step có ít nhất 1 mẫu thật. Nhỏ hơn 10s chỉ tạo ra số liệu nội suy |
| `RATE_WINDOW` | 40s | `rate()` cần ít nhất 2 mẫu trong cửa sổ; 40s ≈ 4 lần scrape cho kết quả ổn định mà vẫn nhạy với spike |
| Warm-up bỏ đi | 60s (6 step) | Locust đang spawn user và `rate()[40s]` chưa đủ dữ liệu. Giữ lại sẽ dạy mô hình một đoạn tăng tải "giả" ở đầu mỗi run |
| Cooldown | 120s | Đủ để request tồn đọng xử lý xong, CPU về nền và (nếu có HPA) bắt đầu scale-down. Nhờ đó run sau không thừa hưởng trạng thái của run trước |
| Window `w` | 12 step = 2 phút | Đủ thấy xu hướng tăng/giảm và trọn một cửa sổ burst 30s. Không quá dài để dự báo vẫn chạy nhanh trong vòng điều khiển |
| Horizon `h` | 6 step = 60s | Pod mới cần khoảng 30–60s mới sẵn sàng (schedule, khởi động container + sidecar, readiness 60s của service Python). Muốn scale **chủ động** thì phải biết nhu cầu trước ít nhất bằng thời gian này |
| Sampling Jaeger | 100% | Edge RPS được đếm từ span. Nếu sampling thấp hơn thì số đếm bị thu nhỏ theo tỉ lệ và nhiễu hơn khi tải thấp |

Mức tải ước tính (user nghỉ trung bình 2s, nên RPS ≈ users / 2):

| Scenario | User | RPS vào frontend (ước tính) |
|---|---|---|
| normal | 36–44 | ≈ 18–22 |
| spike | 25 → 220 | ≈ 12 → 110 |
| bursty | 14–26, burst 80–180 | ≈ 7–13, burst 40–90 |

Mức spike (220 user) được chọn để **vượt năng lực** của cấu hình 1 replica, nhằm thu các mẫu SLO bị vi phạm. Thiếu các mẫu này thì PPO không học được cách tránh vi phạm.

---

## 6. Chia tập dữ liệu và rò rỉ dữ liệu (data leakage)

### 6.1 Vì sao không trộn ngẫu nhiên các sample

```text
Run 05:  step  0 ─────────────────────────────── 173
Sample 40: [40 … 51] → [52 … 57]
Sample 41: [41 … 52] → [53 … 58]   ← trùng 11/12 step với sample 40
```

Nếu sample 40 vào train và sample 41 vào test, mô hình gần như **đã thấy đáp án**. MAE trên test sẽ thấp bất thường và không phản ánh khả năng dự báo thật.

### 6.2 Ba cách chia trong notebook

| Chế độ | Cách chia | Trả lời câu hỏi |
|---|---|---|
| `by_run` (mặc định) | Mỗi scenario: 70% run train, 15% val, 15% test | Mô hình dự báo tốt trên **run mới** của các mẫu tải đã biết không? |
| Theo thời gian (khi < 3 run) | Đầu run cho train, giữa cho val, cuối cho test; bỏ cửa sổ vắt qua ranh giới | Dùng tạm khi thiếu dữ liệu |
| `by_scenario` | Toàn bộ một scenario (ví dụ spike) làm test | Mô hình **tổng quát hóa** sang mẫu tải chưa từng thấy không? (thí nghiệm robustness) |

### 6.3 Kiểm định chéo theo run (khuyến nghị cho báo cáo)

Với 8 run mỗi scenario, chia thành 4 fold, mỗi fold 2 run:

```text
Fold 1: test = run 1–2,  train/val = run 3–8
Fold 2: test = run 3–4,  train/val = run 1–2, 5–8
Fold 3: test = run 5–6,  ...
Fold 4: test = run 7–8,  ...
→ Báo cáo MAE/RMSE = trung bình ± độ lệch chuẩn qua 4 fold
```

Cách này dùng hết dữ liệu để đánh giá và cho biết kết quả có ổn định hay không.

### 6.4 Những rò rỉ khác đã được chặn

| Rò rỉ | Cách xử lý |
|---|---|
| Thống kê chuẩn hóa (mean/std, ngưỡng clip) tính trên cả val/test | Scaler chỉ fit trên **train** |
| Dùng số user Locust làm feature | **Không dùng.** Hệ thống thật không biết số user, nên đưa vào sẽ giúp mô hình "gian lận" trong lúc đánh giá nhưng thất bại khi triển khai. Locust chỉ dùng để kiểm tra độ phủ |
| Cửa sổ vắt qua hai run | Cửa sổ chỉ cắt **bên trong** từng run |
| Feature tính từ tương lai | Mọi feature tại step *t* chỉ dùng dữ liệu đến *t* (`rate()` nhìn lùi, `rps_in_delta` = t − (t−1)) |

---

## 7. Dữ liệu cho PPO: vai trò của autoscaler khi thu

PPO được huấn luyện **offline** (theo giới hạn của đề cương), tức là học từ dữ liệu đã thu thay vì thử trên cụm thật. Điều này đặt thêm một yêu cầu:

- **Thu khi không có autoscaler** (`AUTOSCALER=none`): replica luôn bằng 1. Dữ liệu này cho biết *với 1 replica*, tải X gây ra CPU, latency và SLO như thế nào. Đủ cho GAT-GRU, nhưng PPO **không thấy được** tác dụng của việc thêm replica.
- **Thu khi có HPA** (`AUTOSCALER=hpa`): replica thay đổi theo CPU. Dữ liệu cho thấy *sau khi scale lên, CPU trên mỗi pod và latency thay đổi ra sao, sau bao lâu*. Đây là thông tin bắt buộc để mô phỏng transition trong môi trường RL.

Trong RL offline, chính sách dùng khi thu dữ liệu được gọi là **behavior policy**. PPO chỉ học tin cậy được trong vùng trạng thái mà behavior policy đã đi qua. Ví dụ, nếu HPA không bao giờ scale quá 4 replica thì môi trường mô phỏng không biết chuyện gì xảy ra ở 6 replica. Khuyến nghị: thu **cả hai loại**, ví dụ 8 run `none` + 4 run `hpa` cho mỗi scenario, và có thể chạy HPA với ngưỡng khác nhau (`--cpu-percent=50`, `70`) để mở rộng vùng trạng thái.

---

## 8. Kiểm soát nhiễu để các run so sánh được

| Yếu tố | Cách kiểm soát |
|---|---|
| Phiên bản phần mềm | Giữ cố định K3s `v1.34.9+k3s1`, Istio `1.31.0`, commit Online Boutique, Locust `2.31.8` trong suốt đợt thu |
| Cấu hình tài nguyên | Không đổi requests/limits, anti-affinity, sampling giữa các run |
| Trạng thái đầu | Mỗi run bắt đầu với số replica như nhau (cooldown; khi tắt HPA thì scale tất cả về 1) |
| Bộ sinh tải | Locust ở VM riêng. Theo dõi cảnh báo `CPU usage above 90%`: nếu loadgen quá tải thì tải thực tế thấp hơn tải thiết kế |
| Hạ tầng đo | Kiểm tra `jaeger_coverage` (Jaeger 1 vCPU có thể rơi span khi spike) và `prometheus_coverage` của mỗi run |
| Hoạt động khác trên cụm | Không deploy, không chạy job khác trong lúc thu |
| Ghi chép | `meta.json` lưu thời điểm, scenario, seed, autoscaler. Nên ghi thêm sự cố (nếu có) vào nhật ký thí nghiệm |

---

## 9. Checklist chất lượng cho mỗi run

Sau mỗi run (hoặc mỗi đợt), kiểm tra:

- [ ] `meta.json` có `start`, `end`, và `end − start` ≈ `RUN_DURATION_SEC`.
- [ ] `collect_report.json` → mọi giá trị trong `prometheus_series` > 0.
- [ ] `collect_report.json` → `jaeger_services` có đủ 10 service (trừ `redis-cart`).
- [ ] `collect_report.json` → `jaeger_truncated_windows` = 0.
- [ ] Notebook mục 4: `prometheus_coverage` ≥ 0,9 và `jaeger_coverage` ≥ 0,9.
- [ ] Biểu đồ `rps_in` của frontend có đúng hình dạng scenario (5 spike, các burst rời rạc, đường gần phẳng).
- [ ] Không có pod restart bất thường (`restarts_delta` = 0), trừ khi đó là hiện tượng quá tải cần ghi nhận.

Run không đạt thì **loại và chạy lại**, không cố "sửa" số liệu. Ghi lại lý do loại để mô tả trong báo cáo.

---

## 10. Dung lượng ước tính

| Thành phần | Mỗi run | 24 run |
|---|---:|---:|
| `node_metrics.csv` (180 step × 11 service) | ≈ 0,2 MB | ≈ 5 MB |
| `service_metrics.csv`, `edge_metrics.csv` | ≈ 0,3 MB | ≈ 7 MB |
| Báo cáo Locust (CSV + HTML) | ≈ 1 MB | ≈ 25 MB |
| `processed/gatgru_dataset.npz` | | vài chục MB |

Phần tốn tài nguyên là **Jaeger** trên node-observability: mỗi request vào frontend sinh ra khoảng 10 span (span server ở frontend, cùng một cặp client/server cho mỗi lời gọi downstream). Ở spike 110 RPS, con số này vào khoảng 1.000 span/giây. Nên theo dõi dung lượng đĩa (boot volume 50 GB) và CPU của node này trong lúc thu.

---

## 11. Từ dữ liệu thô đến tensor huấn luyện

```text
results/<scenario>/run_XX/*.csv
   │  (1) căn lưới 10s, gộp pod → service
   ▼
Bảng [T step × 11 service × feature]  ── lưu thành processed/timeseries_raw.csv (cho PPO)
   │  (2) bỏ warm-up, điền thiếu, tạo feature (rps_per_replica, call_ratio, ...)
   │  (3) chia train/val/test theo run
   │  (4) clip p99,9 → log1p → z-score (fit trên train)
   │  (5) cửa sổ trượt w = 12, h = 6 trong từng run
   ▼
X [S, 12, 11, 15]   E [S, 12, M, 4]   G [S, 12, 5]   Y [S, 6, 11, 2]   edge_index [2, M]
```

| Ký hiệu | Ý nghĩa |
|---|---|
| S | Số sample (khoảng 157 × số run) |
| 12 | `w`: số step quá khứ |
| 11 | N: số service (nút) |
| 15 / 4 / 5 | Số feature nút / cạnh / toàn đồ thị |
| M | Số cạnh (15 cạnh tĩnh, cộng cạnh mới nếu trace phát hiện) |
| 6 | `h`: số step dự báo |
| 2 | Số target: `rps_in`, `cpu_cores` |

---

## 12. Câu hỏi thường gặp

**Có thể dùng 5 run thay vì 8 không?**
Có thể (chia được 3/1/1), nhưng train chỉ còn 3 run (khoảng 15 spike) và không chia đều 4-fold được. Hãy dùng learning curve (mục 4.5) để chứng minh 5 run là đủ trước khi chọn.

**Có nên giảm run xuống 10 phút để chạy được nhiều run hơn không?**
Không khuyến khích. Mỗi run 10 phút mất tỉ lệ warm-up lớn hơn, chỉ có khoảng 1–2 spike và 20 cửa sổ bursty, nên số sample hữu ích trên mỗi giờ chạy thấp hơn. Run 10 phút hợp với kiểm thử nhanh hơn là với dataset huấn luyện.

**Vì sao các run của `spike` gần giống nhau, có lãng phí không?**
Hình dạng tải giống nhau là có chủ đích: đó là biến được kiểm soát. Khác biệt giữa các run nằm ở **phản ứng của hệ thống** (latency, CPU, lỗi), và đó chính là phần mô hình cần học. Độ đa dạng về hình dạng tải đến từ `bursty`.

**Có cần chạy các scenario xen kẽ (normal, spike, bursty, normal, …) không?**
Không bắt buộc, vì cooldown đã tách các run. Tuy nhiên chạy xen kẽ giúp tránh trường hợp toàn bộ một scenario rơi vào một khoảng thời gian hạ tầng bất thường (ví dụ Jaeger chậm). Nếu có thời gian, chia thành nhiều đợt nhỏ: `normal 4 → spike 4 → bursty 4`, rồi lặp lại.

**Dữ liệu demo trong notebook có dùng cho báo cáo được không?**
Không. Dữ liệu demo do notebook tự sinh để kiểm tra pipeline, không phản ánh hệ thống thật. Notebook tự tắt chế độ demo khi trong `results/` có ít nhất một run thật.

**Khi nào cần thu lại toàn bộ dataset?**
Khi thay đổi bất kỳ yếu tố nào trong mục 8: phiên bản, requests/limits, số node, cấu hình sampling, hay định nghĩa scenario. Dữ liệu trước và sau thay đổi thuộc hai phân phối khác nhau và không nên trộn.
