# math-load-testing: dataset sinh tải theo mô hình toán học

Bộ dataset thứ hai của đề tài. Tải được sinh **đúng theo các mô hình luồng đến chuẩn** trong lý thuyết hàng đợi và mô hình hóa traffic: **NHPP, MMPP, Bounded Pareto, ON/OFF**. Telemetry được thu giống hệt [`load-testing/`](../load-testing/README.md) (Prometheus + Jaeger, có cả cạnh), nên dùng thẳng được cho pipeline GAT-GRU / LSTM trong [`modeling-common/`](../modeling-common/README.md).

## Vì sao cần bộ dataset này

`load-testing/` dùng **mô hình đóng** của Locust: có N(t) user cố định, mỗi user gửi request, **chờ phản hồi**, nghỉ `U(1, 3)` giây rồi gửi tiếp. Mô hình này có hai hạn chế:

| Hạn chế của mô hình đóng | Hệ quả |
|---|---|
| Thông lượng $X \approx N/(Z + R)$ phụ thuộc thời gian phản hồi $R$ | Khi cụm nghẽn, tải **tự giảm** (spike 220 user: ~105 rps lúc nhàn rỗi nhưng chỉ ~55 rps khi R = 2s). Vi phạm SLO bị đánh giá thấp; `rps_in` không còn là "nhu cầu" thuần túy |
| Thời gian nghỉ phân phối đều | Luồng request **đều hơn Poisson** (index of dispersion < 1), không có tính dồn cục tự nhiên |
| Kịch bản điều khiển số user, không điều khiển λ(t) | Không mô tả được bằng một mô hình toán, khó viết phần "System Model" của báo cáo |

Ở bộ dataset này, một **driver luồng đến mở** phát lại một trace sinh trước từ mô hình toán. Mỗi arrival được khởi chạy đúng thời điểm, **bất kể** các request trước đã xong hay chưa. Tải thiết kế (`designed_rate.csv`) được lưu cùng run làm "ground truth".

## 4 kịch bản

| Kịch bản | Mô hình | Công thức / tham số | Mức tải | Mục đích |
|---|---|---|---|---|
| `nhpp` | **Non-Homogeneous Poisson Process** | $\lambda(t) = 7 + 5\sin(2\pi t/900 + \varphi) + 1{,}5\sin(4\pi t/900 + 2\varphi)$ phiên/s (≥ 0,5). Sinh bằng **thinning** (Lewis–Shedler). φ ngẫu nhiên theo seed | 3–80 rps, trung bình 42 | Tải có chu kỳ ("ngày" nén vào 15 phút): kiểm tra autoscaler **dự báo** |
| `mmpp` | **Markov Modulated Poisson Process** (3 trạng thái) | Xích Markov thời gian liên tục: Idle (λ = 1,5 phiên/s, lưu trú ~Exp(120s)), Normal (3,5; Exp(180s)), High (15; Exp(60s)). Chuyển trạng thái: Idle→{Normal 0,8; High 0,2}, Normal→{Idle 0,4; High 0,6}, High→{Normal 0,9; Idle 0,1} | Idle ~9, Normal ~21, High ~90 rps; trung bình dài hạn 29 | Flash sale / spike **ngẫu nhiên** (khác `spike` cố định 6 phút): kiểm tra tốc độ phát hiện chuyển trạng thái |
| `pareto` | **Bounded Pareto** (khối lượng công việc) | Phiên đến Poisson (λ = 3/s). Số sản phẩm mỗi đơn $S = \lceil X\rceil$, $X \sim \mathrm{BP}(\alpha = 1{,}2, L = 1, H = 40)$. Phiên: home → S × thêm vào giỏ → xem giỏ → checkout | ~20 rps, nhưng CPU dao động mạnh | Yêu cầu không đồng nhất: vài đơn rất lớn làm bão hòa checkoutservice / productcatalog / currency dù số request ít ("80/20") |
| `onoff` | **ON/OFF** (đuôi nặng) | 60 client giữ phiên. ON ~ BP(1,5; 5s; 300s), gửi request Poisson 1,5/s; OFF ~ BP(1,3; 10s; 600s), im lặng | ~27 rps, dồn cục ở mọi thang | Traffic tự tương tự (Hurst lý thuyết $H = (3-\alpha_{\min})/2 = 0{,}85$): kiểm tra dao động replica của autoscaler |

**Phiên người dùng** (nhpp, mmpp): trang chủ + K thao tác, K ~ Geometric (trung bình 5), thời gian nghỉ ~ Exp (trung bình 2s). Tỉ lệ thao tác giống `load-testing/locustfile.py`. Mỗi phiên/client có cookie riêng, nên giỏ hàng và checkout hoạt động đúng.

**Hiệu chỉnh:** tham số được chọn để mức tải nằm cùng dải với `load-testing/` (khoảng 10–110 rps), nên cụm chịu mức tải tương đương giữa hai bộ dataset.

**Ánh xạ Bounded Pareto:** Online Boutique không có kiểu "request nặng tùy ý" (như sinh báo cáo). Khối lượng được biểu diễn bằng số sản phẩm mỗi đơn. Đây là chiều tốn tài nguyên nhất mà ứng dụng cung cấp, vì checkout gọi productcatalog/currency/cart theo từng sản phẩm.

## Thành phần

| File | Vai trò |
|---|---|
| `arrival_models.py` | Bộ sinh thuần Python có seed: Poisson, NHPP (thinning), CTMC/MMPP, Bounded Pareto (nghịch đảo CDF), ON/OFF; `build_trace`, tải kỳ vọng, Hurst lý thuyết |
| `scenarios/{nhpp,mmpp,pareto,onoff}.py` | Tham số từng mô hình (`CONFIG`) |
| `locustfile.py` | **Driver mở**: 1 user Locust phát lại trace, mỗi arrival chạy trong greenlet riêng với `HttpSession` riêng (cookie). Thống kê vẫn được ghi vào Locust. Giới hạn số phiên đồng thời `MAX_CONCURRENT` |
| `run-scenario.sh` | Đồng bộ thư mục lên node-loadgen, chạy N run (seed cố định), xuất telemetry **đầy đủ** bằng `load-testing/collect_metrics.py`, kéo kết quả về |
| `analyze_workload.ipynb` | Kiểm chứng thống kê tải sinh ra (offline) và so sánh tải thiết kế với tải thực nhận (dữ liệu thật) |

## Chạy

Yêu cầu: đã chạy `cluster-setup/00` → `06` (venv Locust và `load-testing/` đã có trên `node-loadgen`; driver dùng chung venv này).

```bash
RUN_DURATION_SEC=120 COOLDOWN_SEC=0 bash math-load-testing/run-scenario.sh mmpp 1     # chạy thử
rm -rf math-load-testing/results/mmpp/run_01                                          # xóa run thử

tmux new -s mathload
for s in nhpp mmpp pareto onoff; do bash math-load-testing/run-scenario.sh "$s" 8; done   # ~19 giờ
```

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `RUN_DURATION_SEC` | `1800` | Độ dài run (cũng là độ dài trace) |
| `COOLDOWN_SEC` | `120` | Nghỉ giữa các run |
| `SEED_BASE` | `1000` | Seed của `run_k` = `SEED_BASE + k`. Run cùng số thứ tự sinh **cùng trace**, nên các phương pháp autoscaling so sánh được theo cặp |
| `MAX_CONCURRENT` | `400` | Số phiên/request đồng thời tối đa trên loadgen 1 vCPU. Arrival vượt mức bị **bỏ và đếm** trong `driver_report.json` |
| `AUTOSCALER` | `none` | Nhãn ghi vào `meta.json` (ví dụ `hpa` khi chạy với `cluster-setup/07-setup-hpa.sh apply`) |
| `STEP_SEC` | `10` | Bước lưới khi xuất telemetry |

## Kết quả mỗi run (`results/<scenario>/run_XX/`)

Ngoài các file giống `load-testing` (`meta.json`, `node_metrics.csv`, `service_metrics.csv`, `edge_metrics.csv`, `locust_*.csv`, `collect_report.json`), mỗi run có thêm:

| File | Nội dung |
|---|---|
| `arrival_trace.csv` | Mọi arrival đã lập lịch: `t`, `kind` (session/request), `source`, `size` (số thao tác hoặc số sản phẩm), `state` (trạng thái MMPP...) |
| `designed_rate.csv` | Tải thiết kế mỗi giây: `lambda_sessions`, `expected_rps`, `state`. Dùng làm ground truth của nhu cầu |
| `trace_meta.json` | Mô hình, seed, pha NHPP / đường đi MMPP, tải kỳ vọng |
| `driver_report.json` | Arrival đã lập lịch / đã chạy / **bị bỏ** / lỗi, số phiên đồng thời tối đa |

`meta.json` có thêm `pipeline: math`, `arrival_model: open` và `seed`.

## Kiểm chứng

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

Driver được kiểm tra luồng điều khiển bằng stub (không có Locust thật): mọi arrival được lập lịch đều được thực thi, mỗi phiên Pareto checkout đúng một lần, tốc độ request khớp kỳ vọng. **Chưa chạy trên cụm thật.** Sau run đầu tiên, hãy mở mục 8 của `analyze_workload.ipynb` để so tải thiết kế với tải frontend thực nhận, và kiểm tra `driver_report.json` không có arrival bị bỏ.

## Dùng với mô hình dự báo

Trong mọi notebook của `load-testing/` và `lstm-load-testing/`, đặt:

```python
DATASET = 'math'
```

Notebook sẽ đọc `math-load-testing/results/`, dùng split riêng `modeling-common/splits_math.json` và ghi ra `processed_math/`. Bộ dataset Locust (`splits.json`, `processed/`) không bị ảnh hưởng. Khi chưa có run thật, các notebook chạy được bằng dữ liệu demo cho 4 kịch bản này.

Một số cách dùng:
- **Huấn luyện và đánh giá trên bộ toán học:** tải "đúng chuẩn" hơn, kết quả dễ mô tả trong báo cáo.
- **So sánh LSTM và GAT-GRU trên cả hai bộ:** kiểm tra kết luận có phụ thuộc cách sinh tải hay không.
- **Thực nghiệm cuối với autoscaler:** cùng `SEED_BASE` cho mọi phương pháp, nên mọi phương pháp nhận **cùng một trace tải**.
