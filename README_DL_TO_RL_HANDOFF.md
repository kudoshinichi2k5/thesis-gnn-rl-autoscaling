# Bàn giao GAT-GRU cho pha phát triển Reinforcement Learning

> Trạng thái kiểm tra: 29/10/2026. Tài liệu này mô tả pipeline GAT-GRU `v05_selected_features_final` đã hoàn tất training, validation và testing. RL sẽ được phát triển mới; notebook DL-RL cũ chỉ được dùng để xác định loại thông tin mà một consumer RL cần nhận từ mô hình dự báo.

## Phạm vi sử dụng

Tài liệu này bàn giao mô hình GAT-GRU dưới dạng pretrained inference package. Thành viên phát triển RL sử dụng trực tiếp checkpoint và các contract sau, không cần chạy lại training DL:

- raw dataset: `dataset/raw/`;
- frozen split: `dataset/splits.json`;
- feature contract: `configs/gatgru/feature_contract_v01.json`;
- graph contract: `configs/gatgru/graph_metadata.json`;
- inference API: `modeling-common/gatgru_inference.py`;
- checkpoint và metadata: `artifacts/gatgru/v05_selected_features_final/`.

### Việc phía RL cần hoàn thiện

Các yêu cầu dưới đây không ngăn việc bắt đầu phát triển RL offline từ artifact hiện có. Chúng phải được hoàn thiện trước khi chạy RL online hoặc đưa ra kết luận cuối về autoscaling/SLA.

1. **Chọn checkpoint hoặc quy tắc ensemble.** Package có năm checkpoint tương ứng seed 0–4. RL phải dùng một checkpoint cố định hoặc quy định rõ cách tổng hợp dự báo của nhiều checkpoint, ví dụ lấy trung bình. Lựa chọn này phải được ghi vào handoff/deployment manifest để observation của RL ổn định và tái lập được. Seed 4 có validation MAE scaled thấp nhất nhưng hiện chỉ là ứng viên, chưa phải deployment checkpoint chính thức.
2. **Xây telemetry/window adapter nếu inference online.** GAT-GRU không nhận trực tiếp response thô từ Prometheus hoặc Jaeger. Adapter phải thu metric theo bước 10 giây, giữ buffer 18 bước (180 giây), tạo đúng `X_raw [B,18,11,10]` và `E_raw [B,18,15,3]`, giữ đúng feature/service/edge ordering, đồng thời xử lý missing value, stale telemetry, NaN và service bị thiếu. Nếu RL chỉ dùng prediction đã tính sẵn để thử nghiệm offline thì chưa cần adapter này.
3. **Định nghĩa evaluation closed-loop.** MAE/RMSE của GAT-GRU chỉ đánh giá forecast, không tự chứng minh hiệu quả autoscaling. Evaluation RL phải đo toàn bộ chuỗi `telemetry → forecast → action → thay đổi replica → phản ứng hệ thống`, sau đó so sánh latency, error/SLA, replica-hours, resource utilization, scaling frequency và oscillation với HPA hoặc baseline trên cùng workload.
4. **Tuân thủ giới hạn dữ liệu và test set tại mục 6.4.** Test set chỉ có bốn run độc lập; 611 sliding windows có chồng lấn và không phải 611 thực nghiệm độc lập. Jaeger RPS chỉ phản ánh tải quan sát được từ trace. Không đưa prediction test của GAT-GRU vào replay buffer để train/tune RL; đánh giá RL cuối nên sử dụng run hoặc workload held-out mới.

---

## 1. Project Overview

Mục tiêu của GAT-GRU là dự báo trực tiếp hai đại lượng cho 11 service:

1. `rps_in`: tốc độ request đến service, đơn vị requests/s sau inverse transform.
2. `cpu_cores`: CPU service sử dụng, đơn vị CPU core sau inverse transform.

Mỗi lần inference nhận 180 giây lịch sử và dự báo sáu bước tương lai, tương ứng 10, 20, 30, 40, 50 và 60 giây. RL mới có thể dùng forecast này như tín hiệu quan sát để ra quyết định autoscaling; GAT-GRU không trực tiếp sinh action replica.

```text
Raw telemetry của 32 run
        │
        ▼
10-second alignment + feature construction + causal missing handling
        │
        ├── full node/edge/graph arrays ──▶ dữ liệu trạng thái cho RL
        │
        ▼
Chọn 10 node features + 3 edge features
        │
        ▼
Train-only scaler + window 18 + horizon 6
        │
        ▼
2-layer edge-aware GAT ──▶ per-service GRU ──▶ RPS/CPU forecast
        │
        ├── pretrained checkpoint ──▶ online/offline inference cho RL
        └── test prediction NPZ ────▶ phân tích offline, không dùng train RL
```

Nguồn chính:

- Notebook điều phối: [`notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb`](notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb).
- Data pipeline: [`modeling-common/forecast_data.py`](modeling-common/forecast_data.py).
- Model/training: [`modeling-common/forecast_torch.py`](modeling-common/forecast_torch.py).
- Tuning/final run: [`modeling-common/forecast_ablation.py`](modeling-common/forecast_ablation.py).
- Metrics/artifact contract: [`modeling-common/forecast_metrics.py`](modeling-common/forecast_metrics.py).
- Artifact cuối: [`artifacts/gatgru/v05_selected_features_final/`](artifacts/gatgru/v05_selected_features_final/).

---

## 2. Current Repository Structure

```text
thesis-gnn-rl-autoscaling/
├── README.md
├── README_DL_TO_RL_HANDOFF.md                 # tài liệu này
├── GATGRU_FEATURE_TRAINING_REPORT_2026-10-22.md
├── dataset/
│   ├── raw/{mmpp,nhpp,onoff,pareto}/run_01..run_08/  # canonical raw dataset, tracked
│   ├── processed/gatgru/                     # ignored derived cache
│   │   ├── runs_raw.npz
│   │   ├── timeseries_raw.csv
│   │   └── metadata.json
│   ├── processed/lstm/                       # ignored model-specific cache
│   ├── splits.json                           # frozen split 24/4/4
│   └── DATASET.md, DATASET_GUIDE.md
├── notebooks/
│   ├── dataset/
│   ├── gatgru/train_gatgru_v05_selected_features_final.ipynb
│   ├── gatlstm/, lstm/, gru/
│   └── comparison/
├── artifacts/gatgru/v05_selected_features_final/
│   ├── gatgru_selected_model_seed_0.pt ... seed_4.pt
│   ├── gatgru_selected_training_meta.json
│   ├── gatgru_selected_best_config.json
│   ├── gatgru_selected_predictions_test.npz
│   ├── gatgru_selected_test_metrics_*.csv/json
│   ├── manifest.json, handoff_manifest.json, status.json
│   ├── reference_input.npz, reference_output.npz
│   └── tuning/
├── configs/gatgru/feature_contract_v01.json
├── load-testing/                              # sinh tải và thu raw telemetry
├── modeling-common/
│   ├── forecast_data.py                      # schema, preprocessing, scaler, window
│   ├── forecast_torch.py                     # GAT-GRU, train loop, inference helpers
│   ├── forecast_metrics.py                   # prediction contract và test metrics
│   ├── forecast_ablation.py                  # v01–v05 experiment orchestration
│   ├── gatgru_inference.py                   # frozen inference wrapper đã kiểm thử
│   ├── verify_gatgru_v05_compatibility.py    # golden-output compatibility test
│   ├── feature_selection.py
│   └── requirements-ml.txt
```

### 2.1 Thứ tự thực thi notebook v05

Notebook v05 có sáu cell và output đã được lưu:

| Cell | Loại | Execution count | Chức năng |
|---:|---|---:|---|
| 0 | Markdown | – | Khai báo phạm vi v05 và điều kiện feature contract phải `APPROVED` |
| 1 | Code | 1 | Tìm repo root, import `forecast_ablation`, load context, feature contract, baseline architecture và device |
| 2 | Markdown | – | Mô tả tuning fixed architecture |
| 3 | Code | 2 | Chạy `run_optuna_fixed_architecture(..., n_trials=24, seed=42)` |
| 4 | Markdown | – | Mô tả final training 5 seed và test một lần |
| 5 | Code | 3 | Chạy `run_final(..., seeds=(0,1,2,3,4))`, lưu model, prediction và metrics |

Notebook không chứa định nghĩa model/training loop; các cell gọi trực tiếp module trong `modeling-common/`.

---

## 3. Dataset Description

### 3.1 Nguồn và quy mô

Dataset được thu từ Online Boutique chạy trên K3s, với Prometheus cung cấp node/resource metrics và Jaeger cung cấp service/edge metrics. Driver tải mở chạy độc lập trên node load generator. Mỗi run dài khoảng 30 phút, telemetry được căn về lưới 10 giây.

Tổng cộng có 32 run, tám run cho mỗi scenario:

| Scenario | Mô hình tải | Đặc điểm chính |
|---|---|---|
| `nhpp` | Non-Homogeneous Poisson Process | Tải tuần hoàn, pha ngẫu nhiên theo seed |
| `mmpp` | Markov Modulated Poisson Process | Chuyển ngẫu nhiên giữa Idle, Normal và High |
| `pareto` | Bounded Pareto workload size | Số request không quá cao nhưng độ nặng của đơn hàng biến thiên lớn |
| `onoff` | Heavy-tailed ON/OFF | Tải dồn cục và có phụ thuộc dài hơn theo thời gian |

### 3.2 Cấu trúc một raw run

Mỗi thư mục `dataset/raw/<scenario>/run_XX/` có các file chính:

| File | Schema/nội dung dùng bởi pipeline |
|---|---|
| `meta.json` | scenario, run id, seed, start/end, duration, step, autoscaler và provenance |
| `node_metrics.csv` | `timestamp, service, cpu_cores, cpu_sidecar_cores, cpu_throttle_ratio, mem_bytes, net_rx_bps, net_tx_bps, cpu_request_cores, cpu_limit_cores, restarts_total, replicas, replicas_desired` |
| `service_metrics.csv` | `timestamp, service, request_count, error_count, latency_p50_ms, latency_p95_ms` |
| `edge_metrics.csv` | `timestamp, source, target, call_count, error_count, latency_p50_ms, latency_p95_ms` |
| `locust_stats_history.csv` | Client-side user count và RPS; dùng kiểm tra coverage, không phải feature của GAT-GRU |
| `arrival_trace.csv`, `designed_rate.csv`, `trace_meta.json` | Tải thiết kế và provenance; không đưa vào input model |
| `collect_report.json`, `driver_report.json` | Chất lượng thu thập, dropped arrival và thống kê collector |

### 3.3 Thứ tự service bắt buộc

Mọi tensor node và output sử dụng đúng thứ tự sau:

```text
0  frontend
1  adservice
2  cartservice
3  checkoutservice
4  currencyservice
5  emailservice
6  paymentservice
7  productcatalogservice
8  recommendationservice
9  redis-cart
10 shippingservice
```

Không được sort lại service khi inference. Service embedding và output head phụ thuộc trực tiếp vào index này.

`forecast_data.SCALED_SERVICES` hiện liệt kê sáu service mà controller/HPA dự kiến điều khiển:

```text
frontend, cartservice, checkoutservice, currencyservice,
productcatalogservice, recommendationservice
```

RL mới phải định nghĩa action mapping tường minh. Mô hình dự báo 11 service không có nghĩa controller bắt buộc scale cả 11 service.

### 3.4 Node features

Preprocessing tạo 15 candidate node features. GAT-GRU v05 chỉ nhận 10 feature được đánh dấu **selected**.

| Feature | Selected | Nguồn/công thức | Transform |
|---|:---:|---|---|
| `rps_in` | ✓ | `request_count / 10 / coverage`; v05 dùng coverage = 1 | `log1p` |
| `rps_in_delta` | ✓ | `rps_in(t) - rps_in(t-1)` | signed `log1p` |
| `rps_per_replica` | ✓ | `rps_in / max(replicas,1)` | `log1p` |
| `latency_p50_ms` | ✓ | service span p50 | `log1p` |
| `latency_p95_ms` | ✓ | service span p95 | `log1p` |
| `error_rate` |  | `error_count / request_count`, clip `[0,1]` | none |
| `cpu_cores` | ✓ | Prometheus CPU service | `log1p` |
| `cpu_util_request` |  | `cpu_cores / cpu_request_cores` | `log1p` |
| `cpu_throttle_ratio` |  | throttle ratio clip `[0,1]` | none |
| `cpu_sidecar_cores` |  | CPU của Istio sidecar | `log1p` |
| `mem_mib` | ✓ | `mem_bytes / 2^20` | `log1p` |
| `net_rx_kBps` | ✓ | `net_rx_bps / 1024` | `log1p` |
| `net_tx_kBps` | ✓ | `net_tx_bps / 1024` | `log1p` |
| `replicas` | ✓ | replica quan sát được, tối thiểu 1 | none |
| `restarts_delta` |  | sai phân `restarts_total`, clip dưới tại 0 | none |

`error_rate` và `restarts_delta` bị loại khỏi input model vì gần như hằng số trong tập train. Chúng không bị xóa khỏi preprocessing: vẫn tồn tại trong full `X`, `runs_raw.npz` và `timeseries_raw.csv`. RL có thể dùng `error_rate` hiện tại như telemetry riêng; GAT-GRU không dự báo error rate.

### 3.5 Edge features và topology

Preprocessing tạo bốn candidate edge features:

| Feature | Selected | Công thức/ý nghĩa |
|---|:---:|---|
| `call_rate` | ✓ | `call_count / 10 / coverage` |
| `call_ratio` | ✓ | `call_rate / source_rps` nếu source RPS > 0 |
| `edge_error_rate` |  | `error_count / call_count` |
| `edge_latency_p95_ms` | ✓ | p95 latency của client span |

Metadata hiện có đúng 15 cạnh, theo thứ tự:

```text
frontend -> productcatalogservice
frontend -> currencyservice
frontend -> cartservice
frontend -> recommendationservice
frontend -> shippingservice
frontend -> checkoutservice
frontend -> adservice
recommendationservice -> productcatalogservice
checkoutservice -> paymentservice
checkoutservice -> productcatalogservice
checkoutservice -> shippingservice
checkoutservice -> emailservice
checkoutservice -> currencyservice
checkoutservice -> cartservice
cartservice -> redis-cart
```

Cạnh `cartservice -> redis-cart` là TCP nên không có span HTTP; topology vẫn giữ cạnh này nhưng edge metrics có thể bằng 0. Trong model, `edge_direction="both"` biến mỗi cạnh caller→callee thành hai hướng message passing và thêm direction flag +1/−1. Self-loop được thêm nội bộ với edge vector bằng 0.

### 3.6 Graph-level features dành cho state/reward

Full preprocessing còn tạo `G [T,5]`:

```text
e2e_rps
e2e_latency_p95_ms
e2e_error_rate
slo_violation
total_replicas
```

`slo_violation` hiện là chỉ báo Boolean khi frontend latency p95 > 300 ms hoặc frontend error rate > 0,01. Các graph features không phải input của GAT-GRU v05 nhưng có thể là nguồn state/reward cho RL mới.

---

## 4. Data Preprocessing

### 4.1 Luồng xử lý

Các hàm chính nằm trong `forecast_data.py`:

```text
load_runs()
  └── đọc meta/node/service/edge/Locust của từng run

prepare_runs()
  ├── floor start timestamp về mốc 10 giây
  ├── ánh xạ timestamp -> t = 0..T-1
  ├── loại sample ngoài khoảng run
  └── tính Prometheus coverage và Jaeger coverage

discover_edges()
  └── static topology + cạnh hợp lệ quan sát thêm

build_run_arrays()
  ├── X [T,11,15]
  ├── E [T,M,4]
  ├── G [T,5]
  └── tidy table cho timeseries_raw.csv

save_runs_npz()
  └── runs_raw.npz chứa full candidate features, split và provenance

fit_scalers()
  └── chỉ fit trên train timesteps của selected features

make_windows()
  └── X/E/Y window bên trong từng run và một split duy nhất
```

### 4.2 Làm sạch và missing values

- Node metrics trùng `(t, service)` được gộp bằng mean của numeric columns.
- Service metrics được gộp theo `(t, service)`: cộng `request_count/error_count`, lấy max latency p50/p95 trong bucket.
- Gauge metrics gồm memory, CPU request/limit, restart counter và replicas chỉ forward-fill tối đa ba bước theo từng service. Không backward-fill từ tương lai.
- Sau forward-fill, `replicas` thiếu được gán 1; các giá trị thiếu còn lại gán 0.
- Service hoặc edge bucket thiếu được gán 0.
- Bỏ sáu bước warm-up đầu, tương ứng 60 giây.
- `correct_coverage=False` trong đường chạy hiện tại; RPS/edge call rate không được chia lại theo Jaeger coverage.

### 4.3 Frozen split

Split nằm tại `dataset/splits.json`, mode `by_run`, seed 42:

| Split | Số run | Run mỗi scenario |
|---|---:|---|
| Train | 24 | 6 |
| Validation | 4 | 1 |
| Test | 4 | 1 |

Test runs:

| Scenario | Test run |
|---|---|
| MMPP | `mmpp/run_04` |
| NHPP | `nhpp/run_01` |
| ON/OFF | `onoff/run_06` |
| Pareto | `pareto/run_08` |

Dataset fingerprint: `b18509670bb1ca10b356da33c2c282b169fdea1103ac45fdca6e788a097977d2`.

Split fingerprint: `05c2ba9d78432b338f736c3c16b186b9d17c5d4614ce16607c4f9511b9851d76`.

Mọi window phải nằm hoàn toàn trong một run và một split. Không random split các overlapping windows.

### 4.4 Chuẩn hóa

Với từng feature, scaler được fit trên train timesteps theo ba bước:

1. Clip phía trên tại percentile 99,9 của train.
2. Áp dụng `log1p`, signed `log1p` hoặc không transform theo feature contract.
3. Z-score bằng mean/std của train; std nhỏ hơn `1e-6` được thay bằng 1.

```text
z = (transform(min(x, clip_p99.9)) - mean_train) / std_train
```

Target scaler dùng `log1p` cho cả `rps_in` và `cpu_cores`. Dự báo model ở scaled space được inverse transform bằng `forecast_data.invert_scaler()` trước khi lưu/evaluate.

### 4.5 Sliding window v05

Với cấu hình cuối:

```text
W = 18 history steps = 180 giây
H = 6 future steps  = 60 giây
N = 11 services
F = 10 selected node features
M = 15 edges
Fe = 3 selected edge features
K = 2 targets

X     [S,18,11,10]
E     [S,18,15,3]
Y     [S,6,11,2]
Y_raw [S,6,11,2]
Y_last[S,11,2]
info  [S,3] = (run key, scenario, first target timestep)
```

Số window ở cấu hình v05 hiện tại:

| Split | Runs | Windows |
|---|---:|---:|
| Train | 24 | 3.661 |
| Validation | 4 | 609 |
| Test | 4 | 611 |

### 4.6 File trung gian và phạm vi sử dụng

| File | Nội dung | Vai trò cho RL |
|---|---|---|
| `dataset/processed/gatgru/runs_raw.npz` | X/E/G full candidate features cho từng run, split và metadata | Nguồn tốt nhất cho offline replay/preprocessing mới |
| `dataset/processed/gatgru/timeseries_raw.csv` | Bảng node-level raw sau warm-up; có `error_rate` | Dễ kiểm tra và xây state node-level |
| `dataset/processed/gatgru/gatgru_dataset.npz` | Window baseline mặc định W=12 | Không phải dataset window của v05 W=18 |
| `configs/gatgru/feature_contract_v01.json` | 10 node + 3 edge features được phê duyệt | Contract feature của model v05 |
| `configs/gatgru/graph_metadata.json` | 11 service, 15 named edges và graph semantics | Graph contract chính thức cho inference và RL handoff |

---

## 5. GAT-GRU Architecture

### 5.1 Cấu hình cuối

| Thành phần | Giá trị |
|---|---:|
| Node features | 10 |
| Edge features đầu vào | 3 |
| Service embedding | 4 |
| GAT layers | 2 |
| Attention heads | 4 |
| Hidden mỗi head | 32 |
| GAT output width | 128 |
| Edge direction | both |
| GRU hidden | 128 |
| Forecast horizon | 6 |
| Targets | 2 |
| Trainable parameters | 121.560 |
| Dropout | 0,0089673767 |

### 5.2 Forward pass

```text
X [B,18,11,10]
  + service embedding [4]
        │
        ▼
[B*18,11,14]
        │
        ▼
EdgeGATLayer 1: 4 heads × 32
        │ ELU + dropout
        ▼
EdgeGATLayer 2: 4 heads × 32
        │ ELU + dropout
        ├──────────── skip projection từ input node+embedding
        ▼
[B,11,18,128]
        │ reshape thành B*11 chuỗi, GRU dùng chung trọng số
        ▼
last GRU state [B*11,128]
        │
        ▼
Linear head [B*11,6*2]
        │
        ▼
Y_scaled [B,6,11,2]
```

`EdgeGATLayer` là dense PyTorch implementation, không dùng PyTorch Geometric. Attention score gồm node nguồn, node đích và linear projection của edge attributes. Mask graph chỉ cho phép self-loop và các cặp có cạnh theo direction đã cấu hình.

“Dynamic edge” ở đây nghĩa là giá trị `call_rate`, `call_ratio`, `edge_latency_p95_ms` thay đổi theo từng timestep. Topology 15 cạnh không tự thay đổi trong inference.

---

## 6. GAT-GRU Training, Validation and Testing

### 6.1 Dependency

Lệnh cài môi trường DL hiện có:

```bash
pip install -r modeling-common/requirements-ml.txt
```

Requirements khai báo tối thiểu: NumPy, pandas, matplotlib, PyTorch, Optuna và Jupyter. Environment đã lưu khi train:

```text
Python   3.12.10
NumPy    2.5.4
pandas   3.0.6
PyTorch  2.14.1+cpu
device   cpu
```

Version Optuna chính xác chưa được ghi trong environment JSON.

### 6.2 Fixed-architecture tuning

Notebook v05 giữ cố định kiến trúc và feature set, chỉ tune:

| Hyperparameter | Search space |
|---|---|
| Window | `{12,18,24}` |
| Dropout | `[0,0.20]` |
| Learning rate | `[5e-4,2.5e-3]`, log scale |
| Weight decay | `[1e-6,1e-3]`, log scale |
| Batch size | `{32,64,128}` |

- 24 trials.
- TPE sampler, seed 42.
- `NopPruner`, vì objective cuối là macro theo run/scenario, không đồng nhất với pooled per-epoch validation MAE.
- Trial training vẫn early-stop theo pooled scaled validation MAE.
- Objective Optuna: trước hết mean theo window trong run, sau đó mean đều theo run/scenario, cuối cùng trọng số `0.35 × RPS + 0.65 × CPU` trên scaled MAE.

Best trial là trial 19:

```json
{
  "window": 18,
  "dropout": 0.008967376666503827,
  "lr": 0.0005352359452218873,
  "weight_decay": 1.8854677893580192e-06,
  "batch_size": 32,
  "gat_hidden": 32,
  "heads": 4,
  "gat_layers": 2,
  "gru_hidden": 128,
  "edge_direction": "both",
  "use_edge_features": true
}
```

Best Optuna objective: `0.10436421938585208`.

### 6.3 Final training loop

- Loss: `torch.nn.HuberLoss(delta=1.0)` trên scaled `rps_in` và `cpu_cores`.
- Optimizer: AdamW.
- Gradient clipping: global norm 1,0.
- Tối đa 60 epoch.
- Early stopping patience 8, improvement threshold `1e-5`.
- Validation checkpoint criterion: pooled scaled MAE, hai target có trọng số phần tử bằng nhau.
- Không dùng learning-rate scheduler trong pipeline v05.
- Five final seeds: 0, 1, 2, 3, 4.
- Mỗi seed restore best validation state trước khi predict test và save checkpoint.

Validation MAE scaled tốt nhất theo seed:

| Seed | Validation MAE | Epoch đã chạy |
|---:|---:|---:|
| 0 | 0,113164656 | 32 |
| 1 | 0,113677591 | 26 |
| 2 | 0,114270911 | 25 |
| 3 | 0,112822317 | 33 |
| 4 | 0,111888453 | 37 |

Tổng final training time đã lưu là khoảng 3.206,61 giây trên CPU. Latency tham khảo của model seed đầu là median `8,0701 ms/window` cho batch một window; đây không phải latency end-to-end của controller.

### 6.4 Test metrics đã lưu

Pooled trên 611 test windows, mean ± sample standard deviation qua năm training seed:

| Target | MAE | RMSE | Under-MAE | Over-MAE |
|---|---:|---:|---:|---:|
| `rps_in` | 0,987760 ± 0,012423 | 2,974199 ± 0,029794 | 0,599854 | 0,387906 |
| `cpu_cores` | 0,012718 ± 0,000153 | 0,036282 ± 0,000621 | 0,006781 | 0,005937 |

Các file chi tiết:

- `gatgru_selected_test_metrics_by_seed.csv`
- `gatgru_selected_test_metrics_by_scenario.csv`
- `gatgru_selected_test_metrics_by_horizon.csv`
- `gatgru_selected_test_metrics_summary.csv/json`

Giới hạn diễn giải:

- Chỉ có bốn test run độc lập, mỗi scenario một run. Các sliding window chồng lấn không phải quan sát độc lập.
- Test đã được truy cập và nhiều phân tích post-hoc đã được thực hiện; không dùng lại test để train/tune RL.
- MAE RPS phản ánh series RPS quan sát từ Jaeger, không đảm bảo là toàn bộ tải thực.
- Forecast quality chưa tự động chứng minh cải thiện autoscaling hoặc SLA; RL cần evaluation closed-loop riêng.

### 6.5 Xác minh inference package

Inference package được kiểm tra trong environment đã lưu (`Python 3.12.10`, `PyTorch 2.14.1+cpu`, CPU):

```powershell
.\kltn-env\Scripts\python.exe .\modeling-common\verify_gatgru_v05_compatibility.py
```

Kết quả:

- Xác minh đủ `611` test windows với `X [611,18,11,10]` và `E [611,18,15,3]`.
- Sample identity, `Y_true` và `Y_last` khớp prediction artifact.
- Cả năm checkpoint load bằng `strict=True`.
- Cả năm output khớp `gatgru_selected_predictions_test.npz`; tổng số phần tử mismatch là 0 với `rtol=1e-5`, `atol=1e-6`.

Checksum và kết quả xác minh nằm trong `handoff_manifest.json`. Hai file `reference_input.npz` và `reference_output.npz` là fixture cố định dùng để smoke test package sau khi copy hoặc triển khai sang môi trường RL.

---

## 7. Model Input/Output Specifications

### 7.1 Input contract

Input phải là `float32` và đúng thứ tự:

```text
X_raw [B,18,11,10]
node feature axis:
  0 rps_in
  1 rps_in_delta
  2 rps_per_replica
  3 latency_p50_ms
  4 latency_p95_ms
  5 cpu_cores
  6 mem_mib
  7 net_rx_kBps
  8 net_tx_kBps
  9 replicas

E_raw [B,18,15,3]
edge feature axis:
  0 call_rate
  1 call_ratio
  2 edge_latency_p95_ms
```

Node axis và edge axis phải đúng thứ tự ở mục 3. Không được sort độc lập tại runtime.

Trước khi gọi model:

```python
X_scaled = forecast_data.apply_scaler(X_raw, meta["scalers"]["node"])
E_scaled = forecast_data.apply_scaler(E_raw, meta["scalers"]["edge"])
```

### 7.2 Output contract

Model trả scaled tensor:

```text
Y_scaled [B,6,11,2]

target axis:
  0 rps_in
  1 cpu_cores

horizon axis:
  0 -> +10 giây
  1 -> +20 giây
  2 -> +30 giây
  3 -> +40 giây
  4 -> +50 giây
  5 -> +60 giây
```

Đổi về đơn vị gốc:

```python
Y_raw = forecast_data.invert_scaler(Y_scaled, meta["scalers"]["target"])
```

`invert_scaler()` hiện không clamp giá trị âm sau inverse transform. Consumer RL phải validate output hữu hạn và xác định rõ policy clamp vật lý nếu cần; không được âm thầm thay đổi contract khi chưa ghi lại.

### 7.3 Cấu trúc checkpoint

Mỗi `gatgru_selected_model_seed_<seed>.pt` được lưu bằng:

```python
{
    "state_dict": model.state_dict(),
    "meta": {...}
}
```

`meta` có:

- model/model kind và config;
- seeds, validation scores, histories và train time;
- node/edge feature ordering;
- target và service ordering;
- node/edge/target scalers;
- data contract và fingerprints;
- parameter count, device, inference latency;
- source SHA-256 tại thời điểm train.

Các thành phần đi kèm checkpoint:

- named/indexed edge list và graph semantics: `configs/gatgru/graph_metadata.json`;
- feature ordering: `configs/gatgru/feature_contract_v01.json`;
- packaged inference API: `modeling-common/gatgru_inference.py`;
- package checksum và smoke-test result: `handoff_manifest.json`, `reference_input.npz`, `reference_output.npz`.

Checkpoint không chứa optimizer state để resume training. Deployment seed/ensemble và live telemetry adapter thuộc phạm vi triển khai RL.

### 7.4 Reference load skeleton

Wrapper dưới đây là API inference đã được kiểm thử. Checkpoint PyTorch phải là trusted artifact. Seed 4 chỉ là ví dụ vì chưa có quyết định deployment checkpoint chính thức.

```python
import sys
from pathlib import Path

ROOT = Path("/path/to/thesis-gnn-rl-autoscaling")
sys.path.insert(0, str(ROOT / "modeling-common"))

from gatgru_inference import GATGRUPredictor, load_named_edges

CKPT = (
    ROOT / "artifacts" / "gatgru" / "v05_selected_features_final"
    / "gatgru_selected_model_seed_4.pt"
)
GRAPH_META = ROOT / "configs" / "gatgru" / "graph_metadata.json"

predictor = GATGRUPredictor(
    checkpoint_path=CKPT,
    edges_named=load_named_edges(GRAPH_META),
    device="cpu",
)

# X_raw: [B,18,11,10], E_raw: [B,18,15,3], float32, đúng ordering.
Y_raw = predictor.predict(X_raw, E_raw, batch_size=128)
# Y_raw: [B,6,11,2], target order = [rps_in, cpu_cores]
```

Wrapper kiểm tra metadata, service/target/feature/edge contract, input shape, finite values, load `state_dict` với `strict=True`, scale input và inverse target. Nó chưa xây `X_raw/E_raw` từ live Prometheus/Jaeger; checksum của package phải được kiểm tra theo `handoff_manifest.json` ở tầng deploy.

### 7.5 Prediction artifact contract

`gatgru_selected_predictions_test.npz` có:

| Key | Shape hiện tại | Nội dung |
|---|---|---|
| `Y_pred` | `[5,611,6,11,2]` | Dự báo năm seed, original units |
| `Y_true` | `[611,6,11,2]` | Ground truth test |
| `Y_last` | `[611,11,2]` | Last observed target |
| `info` | `[611,3]` | `(run key, scenario, t_target0)` |
| `meta` | scalar JSON string | Config, scaler và contract |

File này phù hợp để phân tích offline hoặc thử thiết kế observation từ forecast đã có. Nó chỉ chứa test windows; không được dùng làm train data của RL.

---

## 8. Required Artifacts and Missing Components

### 8.1 Artifact đã tồn tại

| Artifact | Trạng thái | Online inference | Offline analysis | Ghi chú |
|---|---|:---:|:---:|---|
| `gatgru_selected_model_seed_0..4.pt` | Có | Bắt buộc ít nhất một | Không bắt buộc | Chưa có deployment seed chính thức |
| `gatgru_selected_training_meta.json` | Có | Bắt buộc | Bắt buộc | Chứa scaler/config/feature/service order |
| `gatgru_selected_best_config.json` | Có | Nên có | Nên có | Bản config nhỏ, dễ audit |
| `manifest.json` | Có | Nên có | Bắt buộc audit | Dataset/split/source hash |
| `gatgru_selected_environment.json` | Có | Nên có | Nên có | Exact runtime phần lớn dependency |
| `status.json` | Có | Nên có | Nên có | `EVALUATED`, test đã truy cập |
| `configs/gatgru/feature_contract_v01.json` | Có | Bắt buộc audit | Bắt buộc audit | Feature ordering được phê duyệt |
| `configs/gatgru/graph_metadata.json` | Có | Bắt buộc | Nên có | Graph contract self-contained: service/edge ordering và graph semantics |
| `dataset/splits.json` | Có, tracked | Không | Bắt buộc nếu đánh giá/replay | Frozen run split |
| `gatgru_selected_predictions_test.npz` | Có | Không | Tùy chọn | Test-only predictions |
| `modeling-common/gatgru_inference.py` | Có, đã kiểm thử | Bắt buộc | Nên có | Stable load/scale/predict/inverse API |
| `verify_gatgru_v05_compatibility.py` | Có, đã pass | Nên có | Bắt buộc audit | Kiểm tra toàn bộ 611 windows × 5 seed |
| `handoff_manifest.json` | Có | Bắt buộc | Bắt buộc audit | Checksum, runtime và compatibility result; chưa chọn deployment seed |
| `reference_input.npz`, `reference_output.npz` | Có | Nên có | Bắt buộc smoke test | Fixture một window, output của năm seed |
| Metrics CSV/JSON | Có | Không | Nên có | Acceptance/audit |
| Histories CSV | Có | Không | Tùy chọn | Learning history theo seed |
| Tuning summary/trials CSV | Có | Không | Tùy chọn | Audit tuning |
| `runs_raw.npz` | Có, đang ignored | Không nếu có live adapter | Bắt buộc cho offline rebuild/replay | Full X/E/G và metadata |
| `timeseries_raw.csv` | Có, đang ignored | Không | Tùy chọn | Dễ truy cập node telemetry, có `error_rate` |

### 8.2 Thành phần còn thiếu

| Thành phần | Mức độ | Lý do cần |
|---|---|---|
| Quyết định deployment checkpoint/ensemble | **Bắt buộc** | Hiện có năm model nhưng chưa có consumer contract chọn seed |
| Online telemetry/window adapter | **Bắt buộc cho online RL** | Source hiện chỉ có offline run preprocessing; cần buffer 18 bước và tạo đúng X/E |
| `requirements-rl.txt` hoặc environment RL riêng | **Bắt buộc khi bắt đầu RL** | DL requirements chưa có Gymnasium/Stable-Baselines3 và version lock |
| Predictions cho RL train/validation traces | Tùy thiết kế | Chỉ cần nếu RL dùng precomputed forecast thay vì gọi model trực tiếp |
| TorchScript/ONNX export | Tùy chọn | Giảm phụ thuộc Python class, nhưng hiện chưa tồn tại |

### 8.3 Minimum handoff package

Để inference mà không train lại DL, tối thiểu phải giao:

```text
artifacts/gatgru/v05_selected_features_final/<1 pretrained checkpoint được chọn>
artifacts/gatgru/v05_selected_features_final/gatgru_selected_training_meta.json
artifacts/gatgru/v05_selected_features_final/gatgru_selected_best_config.json
artifacts/gatgru/v05_selected_features_final/manifest.json
artifacts/gatgru/v05_selected_features_final/handoff_manifest.json
artifacts/gatgru/v05_selected_features_final/gatgru_selected_environment.json
configs/gatgru/feature_contract_v01.json
configs/gatgru/graph_metadata.json  # service order, named/indexed edge order, graph semantics
modeling-common/gatgru_inference.py
modeling-common/verify_gatgru_v05_compatibility.py
artifacts/gatgru/v05_selected_features_final/reference_input.npz
artifacts/gatgru/v05_selected_features_final/reference_output.npz
checksum của toàn bộ package
```

Scaler rời không bắt buộc vì đã nằm trong checkpoint/meta. Prediction NPZ không bắt buộc cho online inference.

---

## 9. Repository Structure and GitHub Handoff

### 9.1 Git tracking policy

| Nhóm | Chính sách |
|---|---|
| `dataset/raw/` | Bắt buộc Git track và push; hiện gồm 448 file, khoảng 88,76 MiB, file lớn nhất khoảng 1,54 MiB |
| `dataset/splits.json`, `configs/`, source và README | Git track bằng Git thường |
| `dataset/processed/` | Cache dẫn xuất, bị ignore bởi `dataset/.gitignore` |
| Checkpoint và prediction binary | Dùng Git LFS hoặc GitHub Release/object storage; luôn kèm checksum |

### 9.2 File nên push bằng Git thường

| Nhóm | File |
|---|---|
| Tài liệu | `README_DL_TO_RL_HANDOFF.md`, báo cáo GAT-GRU |
| Notebook chính | `notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb` với output/provenance đã kiểm tra |
| Source | `forecast_data.py`, `forecast_torch.py`, `gatgru_inference.py`, compatibility verifier; training/evaluation source nếu cần audit đầy đủ |
| Contract | `configs/gatgru/feature_contract_v01.json`, `configs/gatgru/graph_metadata.json`, `dataset/splits.json` |
| Artifact text | best config, training meta, manifest, handoff manifest, environment, status |
| Kết quả text | metrics CSV/JSON, history CSV, tuning summary/trials CSV |

Không commit secret, token, kubeconfig, node IP private hoặc virtual environment.

### 9.3 Binary nên dùng Git LFS hoặc GitHub Release/storage riêng

| Binary | Khuyến nghị |
|---|---|
| Deployment `.pt` checkpoint | Git LFS hoặc release asset; bắt buộc checksum |
| Bốn checkpoint seed còn lại | Release asset nếu cần ensemble/reproducibility |
| `gatgru_selected_predictions_test.npz` | Release asset hoặc LFS |
| `runs_raw.npz`, `gatgru_dataset.npz` | Dataset release/storage riêng |
| `timeseries_raw.csv` | Nén và đưa vào dataset release nếu cần offline RL |
| `optuna.db` | Không cần push nếu đã có trials CSV/summary; archive riêng nếu cần resume |
| Raw `dataset/raw/` | Bắt buộc push bằng Git theo ràng buộc hiện tại; tổng khoảng 88,76 MiB, file lớn nhất khoảng 1,54 MiB |

Vì các binary v05 hiện chỉ khoảng 4,14 MiB, GitHub có thể nhận theo kích thước file; LFS/release vẫn phù hợp hơn vì đây là immutable artifact, không phải source code.

### 9.4 `.gitignore` và artifact policy

`dataset/.gitignore` hiện chỉ loại cache dẫn xuất:

```gitignore
/processed/
__pycache__/
```

Raw dataset không bị ignore. Nếu binary artifact được phân phối qua GitHub Release thay cho Git LFS, có thể bổ sung ở root:

```gitignore
# Binary artifacts distributed through releases
artifacts/**/tuning/*.db
artifacts/**/*.pt
artifacts/**/*predictions*.npz
```

Nếu dùng Git LFS cho `.pt/.npz`, không thêm hai pattern binary vào `.gitignore`; cấu hình chúng trong `.gitattributes`.

### 9.5 Vị trí artifact GAT-GRU

Giữ artifact ở đúng thư mục hiện có:

```text
artifacts/gatgru/v05_selected_features_final/
```

Inference package gồm:

```text
handoff_manifest.json     # selection status, checksums, source version
reference_input.npz       # fixture nhỏ, không chứa toàn dataset
reference_output.npz      # expected output cho smoke test
```

`deployment_checkpoint` trong handoff manifest là `null`; cần quyết định seed/ensemble trước khi triển khai. RL đọc graph contract từ `configs/gatgru/graph_metadata.json` và gọi model qua `modeling-common/gatgru_inference.py`; không nhúng lại model class vào notebook RL.

---

## 10. RL Development Guide

### 10.1 Chọn cách sử dụng forecast

RL mới có hai chế độ hợp lệ:

| Chế độ | Cách dùng | Phù hợp |
|---|---|---|
| Pretrained inference | Mỗi control step tạo X/E window, gọi checkpoint, nhận forecast 10–60s | Online controller hoặc environment cần forecast cho state hiện tại |
| Precomputed predictions | Đọc prediction file theo `(run, scenario, t_target0)` | Phân tích/offline prototype trên đúng trace đã export |

Prediction test hiện có không đủ để train RL và không nên dùng train RL. Nếu chọn precomputed mode, cần xuất forecast riêng cho RL train/validation runs bằng pretrained model, giữ test runs chỉ cho đánh giá cuối.

### 10.2 Quy trình tích hợp pretrained model

1. Chọn và ghi chính thức checkpoint hoặc ensemble policy trong `handoff_manifest.json`.
2. Kiểm tra checksum theo `handoff_manifest.json` và chạy compatibility verifier sau khi copy/restructure.
3. Load checkpoint, validate service/feature/target/horizon contract.
4. Load graph metadata, convert named edges sang index theo service order trong checkpoint.
5. Thu telemetry mỗi 10 giây và tạo đủ 18 bước causal history.
6. Tính đúng 10 node features và ba edge features theo công thức preprocessing.
7. Áp scaler nằm trong checkpoint metadata.
8. Chạy `model.eval()` và `torch.inference_mode()`.
9. Inverse target scaler để nhận RPS/CPU original units.
10. Đưa forecast vào RL observation qua một adapter có versioned schema.
11. Dùng RL normalizer riêng nếu cần; không tái chuẩn hóa state bằng cách giả định output GAT đã nằm trong `[0,1]`.

### 10.3 Forecast fields cho RL

Không có một observation schema RL mới nào được triển khai trong repo. Consumer nên giữ đầy đủ forecast trước khi quyết định aggregate:

```text
forecast_rps     [6,11]
forecast_cpu     [6,11]
horizon_seconds  [10,20,30,40,50,60]
```

Các lựa chọn như chỉ dùng +60s, dùng max/mean qua horizon, hoặc đưa cả sáu horizon vào state phải được coi là hyperparameter của RL và đánh giá riêng. Không sửa output của pretrained GAT-GRU để phù hợp một observation shape tùy ý.

### 10.4 `error_rate` trong RL mới

`error_rate` không phải selected input và không phải target của GAT-GRU v05, nhưng preprocessing vẫn lưu nó trong full node telemetry. RL có thể dùng:

- `error_rate` hiện tại theo service từ full `X`/`timeseries_raw.csv`;
- `e2e_error_rate` và `slo_violation` từ graph features `G`;
- forecast RPS/CPU từ GAT-GRU.

Không giả lập `error_rate=0` chỉ vì model không dự báo metric này. Nếu RL reward có error-SLA, error phải đến từ telemetry hoặc environment transition được định nghĩa và kiểm chứng riêng.

### 10.5 State, action và reward phải tách khỏi DL contract

- DL contract quy định cách tạo X/E và ý nghĩa forecast.
- RL state contract có thể bổ sung current latency, error rate, replicas, cooldown và action history.
- RL action contract phải chỉ rõ service nào được scale, min/max replica, scale delay và cooldown.
- RL reward phải dựa trên post-action/environment metrics, không được coi forecast là ground-truth outcome.
- Nếu chỉ điều khiển sáu `SCALED_SERVICES`, action tensor phải có mapping riêng tới 11 node forecast.

### 10.6 Split discipline cho RL

Khuyến nghị giữ nguyên nguyên tắc:

```text
RL train      : chỉ dùng DL train runs hoặc tập workload train mới
RL selection  : validation runs
RL final test : test runs chưa dùng để chọn state/reward/hyperparameter
```

Do test DL đã được xem nhiều lần, một đánh giá RL có kết luận mạnh nên thu thêm run hoặc tạo held-out workload mới. Ít nhất không được đưa `gatgru_selected_predictions_test.npz` vào replay buffer train.

### 10.7 Acceptance checklist trước khi bắt đầu train PPO

- [ ] Có một checkpoint/ensemble được ghi chính thức.
- [x] Source loader khớp cả năm checkpoint và strict `state_dict` load thành công.
- [x] Service order đúng 11 service trong compatibility test.
- [x] Edge order đúng 15 cạnh từ graph metadata hiện tại.
- [x] X/E feature order đúng contract.
- [x] Scaler lấy từ checkpoint v05, không lấy nhầm full-feature scaler.
- [x] Golden-output test ở original units pass trên 611 windows × 5 seed.
- [ ] Window buffer đủ 18 bước, không dùng dữ liệu tương lai.
- [ ] RL train/validation/test workloads tách biệt.
- [ ] `error_rate` và SLO metrics có nguồn rõ ràng ngoài GAT output.
- [ ] Action mapping chỉ rõ service điều khiển được.
- [ ] NaN/Inf, missing service, stale telemetry và model failure có fallback.
- [ ] Không retrain hoặc ghi đè artifact DL trong pipeline RL.

### 10.8 Phần cần bổ sung sau tài liệu này

Theo phạm vi bàn giao hiện tại, chưa triển khai các mục sau:

1. Quyết định deployment seed/ensemble.
2. Streaming Prometheus/Jaeger feature adapter.
3. Precomputed forecast cho RL train/validation traces nếu chọn offline mode.
4. Gymnasium environment, PPO agent, observation/reward/action schema.
5. Closed-loop simulator hoặc integration controller trên Kubernetes.

Các phần này nên được thêm theo module mới, không sửa checkpoint, split hay pipeline training DL đã đóng băng.
