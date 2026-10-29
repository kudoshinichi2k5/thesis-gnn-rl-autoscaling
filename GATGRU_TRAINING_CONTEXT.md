# GAT-GRU training context — session handoff

> Đọc file này trước khi tiếp tục tối ưu GAT-GRU. Đây là bản note keyword và trạng thái thí nghiệm, không thay thế báo cáo đầy đủ [`TRAINING_BASELINE_2026-10-14.md`](TRAINING_BASELINE_2026-10-14.md).

## Mục tiêu

- Forecast đa bước cho `rps_in` và `cpu_cores` trên 11 microservices.
- Sampling: 10 giây.
- Forecast horizon: 6 bước = 60 giây.
- Mục tiêu cuối: phục vụ autoscaling, ưu tiên CPU accuracy, high-load/transition behavior và hạn chế underprediction.

## Trạng thái hiện tại

**Baseline đã train:** LSTM và dynamic-edge GAT-GRU.

**Chưa chạy:** GRU node-only, static GAT-GRU, controlled `gat_layers=1`, bỏ edge latency, CPU-weighted objective và các P1 khác.

Không được diễn giải các P1 bên dưới là kết quả đã được kiểm chứng. Chúng mới là giả thuyết/thiết kế thí nghiệm.

## Source of truth

- Train GAT-GRU: [`notebooks/gatgru/train_gatgru.ipynb`](notebooks/gatgru/train_gatgru.ipynb)
- Train LSTM: [`notebooks/lstm/train_lstm.ipynb`](notebooks/lstm/train_lstm.ipynb)
- So sánh: [`notebooks/comparison/compare_lstm_gatgru.ipynb`](notebooks/comparison/compare_lstm_gatgru.ipynb)
- GAT-GRU artifacts: [`dataset/processed/gatgru`](dataset/processed/gatgru)
- LSTM artifacts: [`dataset/processed/lstm`](dataset/processed/lstm)
- Báo cáo chi tiết: [`TRAINING_BASELINE_2026-10-14.md`](TRAINING_BASELINE_2026-10-14.md)

## Quy ước versioning, tracking và rollback

**Bắt buộc:** mỗi lần cải thiện GAT-GRU phải tạo một file train mới. Không chỉnh sửa notebook train của version đã có kết quả và không ghi đè artifact của version cũ.

Quy tắc đặt tên đề xuất:

```text
notebooks/gatgru/train_gatgru_vNN_<change>.ipynb
artifacts/gatgru/vNN_<change>/
```

Ví dụ:

```text
train_gatgru_v01_gat1.ipynb
train_gatgru_v02_gru_node_only.ipynb
train_gatgru_v03_static_gat.ipynb
train_gatgru_v04_dynamic_no_edge_latency.ipynb
```

Mỗi version chỉ nên trả lời một câu hỏi chính. Ví dụ `v01_gat1` chỉ đổi `gat_layers: 2 → 1`; không đồng thời đổi feature set, loss hoặc Optuna objective.

Mỗi artifact directory phải độc lập và có tối thiểu:

- notebook/file train đã dùng;
- parent version;
- trạng thái version;
- mục tiêu và thay đổi duy nhất của version;
- dataset/split/data-contract fingerprints;
- source hashes và Git commit nếu có;
- best config và Optuna DB/trials;
- checkpoints và histories từng seed;
- predictions và metrics;
- train time, inference time và environment;
- kết luận `PROMOTE`, `REJECT` hoặc `INCONCLUSIVE` cùng lý do.

Không được dùng chung hoặc ghi đè:

- Optuna study name/DB giữa các version có hypothesis khác nhau;
- model checkpoints;
- prediction files;
- summary CSV/JSON;
- selected-feature manifest khi feature contract thay đổi.

Trạng thái chuẩn:

```text
PLANNED      chưa chạy
RUNNING      đang train/tune
FAILED       lỗi kỹ thuật, chưa có kết quả hợp lệ
TRAINED      đã tạo model/artifact, chưa so sánh đầy đủ
EVALUATED    đã đánh giá trên contract quy định
REJECTED     không tốt hơn parent hoặc vi phạm trade-off
CANDIDATE    có khả năng thay best hiện tại, cần xác nhận
BEST         version tham chiếu tốt nhất hiện tại
ARCHIVED     giữ để rollback, không còn active
```

Chỉ một version được đánh dấu `BEST` cho một evaluation contract. Version cũ không bị xóa khi có version mới. Muốn rollback chỉ cần chọn lại notebook, config, checkpoint và artifact directory của version `BEST`/`ARCHIVED` tương ứng.

### Experiment registry

Bảng này là append-only. Mỗi file train mới phải thêm một dòng ngay khi được tạo và cập nhật trạng thái sau từng giai đoạn.

| Version | Parent | Train file | Thay đổi chính | Status | Kết quả chính | Artifact | Quyết định |
|---|---|---|---|---|---|---|---|
| `baseline-2026-10-14` | — | `notebooks/gatgru/train_gatgru.ipynb` | Dynamic edges, 2 GAT layers | `BEST` | RPS MAE `1.0039`; CPU MAE `0.0128`; inference `6.59 ms` | `dataset/processed/gatgru` | Baseline tham chiếu |
| `v01_node_ablation` | `baseline-2026-10-14` | `notebooks/gatgru/train_gatgru_v01_node_ablation.ipynb` | Controlled node-feature group và leave-one-out ablation | `PLANNED` | `NOT RUN` | `artifacts/gatgru/v01_node_ablation` | `PENDING` |
| `v02_graph_ablation` | `v01_node_ablation` | `notebooks/gatgru/train_gatgru_v02_graph_ablation.ipynb` | GRU node-only vs static GAT topology đúng/sai và dynamic GAT | `PLANNED` | `NOT RUN` | `artifacts/gatgru/v02_graph_ablation` | `PENDING` |
| `v03_edge_ablation` | `v02_graph_ablation` | `notebooks/gatgru/train_gatgru_v03_edge_ablation.ipynb` | Full-factorial ablation ba edge metrics | `PLANNED` | `NOT RUN` | `artifacts/gatgru/v03_edge_ablation` | `PENDING` |
| `v04_edge_controls` | `v03_edge_ablation` | `notebooks/gatgru/train_gatgru_v04_edge_controls.ipynb` | Real edge values vs negative controls | `PLANNED` | `NOT RUN` | `artifacts/gatgru/v04_edge_controls` | `PENDING` |
| `v05_selected_features_final` | `v04_edge_controls` | `notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb` | Retune selected feature contract và final 5-seed evaluation | `PLANNED` | `NOT RUN` | `artifacts/gatgru/v05_selected_features_final` | `PENDING` |

Template cho version mới:

| Version | Parent | Train file | Thay đổi chính | Status | Kết quả chính | Artifact | Quyết định |
|---|---|---|---|---|---|---|---|
| `vNN_<change>` | `<parent>` | `notebooks/gatgru/train_gatgru_vNN_<change>.ipynb` | `<một thay đổi>` | `PLANNED` | `NOT RUN` | `artifacts/gatgru/vNN_<change>` | `PENDING` |

### Quy tắc promote/rollback

Một version chỉ được promote thành `BEST` khi:

1. So sánh với parent trên cùng dataset/split/evaluation contract.
2. Có đủ final seeds và artifact tái tạo được.
3. Đạt success criteria đã khai báo trước khi train.
4. Không cải thiện một metric bằng cách làm hỏng metric/rủi ro quan trọng khác ngoài ngưỡng cho phép.
5. Ghi rõ compute trade-off và kết quả theo run/scenario/high-load/transition.

Nếu version bị reject hoặc failed:

- giữ nguyên notebook, logs và artifacts;
- đánh dấu trạng thái, nguyên nhân và thời điểm;
- không tiếp tục ghi đè lên cùng version ID;
- version tiếp theo phải trỏ đúng parent thực tế, không mặc định lấy version mới nhất.

Nếu preprocessing, feature contract hoặc split thay đổi, đó là một nhánh experiment mới. Không so metric trực tiếp với nhánh cũ mà không ghi rõ contract khác nhau.

## Frozen data contract

- Dataset vật lý: `dataset/raw/`; logical label lịch sử trong frozen split: `math-load-testing`.
- 32 runs: 8 × MMPP, NHPP, on-off, Pareto.
- Split: 24 train / 4 validation / 4 test.
- Test: `mmpp/run_04`, `nhpp/run_01`, `onoff/run_06`, `pareto/run_08`.
- Common aligned test windows: 611.
- Dataset fingerprint: `b18509670bb1ca10b356da33c2c282b169fdea1103ac45fdca6e788a097977d2`.
- Split fingerprint: `05c2ba9d78432b338f736c3c16b186b9d17c5d4614ce16607c4f9511b9851d76`.
- Final seeds: `0, 1, 2, 3, 4`.
- Optuna search seed: `42`.
- Device của baseline: CPU.

## P0 đã xử lý

- Canonical run IDs lấy theo tên thư mục; split NHPP dùng `run_01`–`run_08`.
- Đã bỏ future `bfill`; imputation hiện causal.
- LSTM/GAT-GRU có data-contract assertions cho run IDs, split, features, targets và aligned samples.
- Đã lưu checkpoints từng seed, histories, predictions, metrics, Optuna DB/trials/config, environment và fingerprints.

## Shared node features hiện tại

13 features giống nhau cho LSTM và GAT-GRU:

```text
rps_in
rps_in_delta
rps_per_replica
latency_p50_ms
latency_p95_ms
cpu_cores
cpu_util_request
cpu_throttle_ratio
cpu_sidecar_cores
mem_mib
net_rx_kBps
net_tx_kBps
replicas
```

Đã loại do gần như hằng số:

- `error_rate`
- `restarts_delta`

Nhận định hiện tại:

- 13 features là baseline rộng, chưa chứng minh tất cả đều cần thiết.
- Core chắc chắn: `rps_in`, `cpu_cores`, `replicas`.
- Core mở rộng đáng giữ: `rps_in_delta`, `rps_per_replica`, `cpu_util_request`, `cpu_sidecar_cores`, một latency feature.
- Ứng viên ablation trước: `cpu_throttle_ratio`, `mem_mib`, `net_tx_kBps`.
- Không loại latency chỉ dựa trên global importance; phải kiểm tra high-load/transition.
- Với benchmark công bằng, tiếp tục dùng shared node feature set.

## GAT-GRU baseline đã train

Best Optuna config:

```yaml
window: 18                 # 180 giây
gat_hidden: 32
heads: 4
gat_layers: 2
gru_hidden: 128
edge_direction: both
use_edge_features: true
dropout: 0.055739
lr: 0.001546
batch_size: 64
```

Dynamic edge features:

```text
call_rate
call_ratio
edge_latency_p95_ms
```

Kết quả:

- Best Optuna val MAE scaled: `0.112560`.
- Final-seed val MAE: `0.11549 ± 0.00103`.
- Parameters: `122,328`.
- CPU inference: `6.59 ms/window`.
- Final training 5 seeds: `3,311.5 s` ≈ `55.2 phút`.
- Test `rps_in`: MAE `1.0039 ± 0.0166`, RMSE `2.9610 ± 0.0414`.
- Test `cpu_cores`: MAE `0.0128 ± 0.0002`, RMSE `0.0359 ± 0.0004`.
- GAT Optuna artifact hiện có: 18 complete, 21 pruned, 1 trial còn `RUNNING`.

## LSTM reference baseline

Best config:

```yaml
window: 18
hidden: 128
layers: 2
mode: joint
dropout: 0.068154
lr: 0.001208
batch_size: 64
```

Kết quả:

- Best Optuna val MAE scaled: `0.110227`.
- Final-seed val MAE: `0.11157 ± 0.00125`.
- Parameters: `311,472`.
- CPU inference: `1.69 ms/window`.
- Final training 5 seeds: `375.5 s` ≈ `6.3 phút`.
- Test `rps_in`: MAE `0.9741 ± 0.0194`, RMSE `2.9419 ± 0.0497`.
- Test `cpu_cores`: MAE `0.0129 ± 0.0001`, RMSE `0.0371 ± 0.0005`.

## Kết luận so sánh hiện tại

- LSTM tốt hơn GAT-GRU khoảng `3.1%` MAE cho `rps_in`.
- GAT-GRU tốt hơn LSTM khoảng `1.0%` MAE và `3.1%` RMSE cho `cpu_cores`.
- LSTM inference nhanh hơn khoảng `3.9×` và final training nhanh hơn khoảng `8.8×`.
- GAT-GRU ít parameters hơn khoảng `60.7%`, nhưng graph operations làm runtime cao hơn.
- GAT-GRU có tín hiệu tốt hơn tại đầu spike, đặc biệt cho CPU.
- LSTM thắng `rps_in` trên cả 4 test runs; sign-test `p=0.125`, chưa đủ ý nghĩa thống kê.
- CPU: mỗi mô hình thắng 2/4 test runs; `p=1.0`.
- Chỉ có 4 independent test runs; 611 overlapping windows không được coi là 611 quan sát độc lập.
- Không có mô hình thắng tuyệt đối.

## Điểm yếu quan sát được

- MMPP là scenario khó nhất.
- Error tăng mạnh theo horizon, đặc biệt CPU tại 50–60 giây.
- Cả hai mô hình thiên về underprediction.
- Underprediction đầu spike còn lớn.
- `currencyservice` và `productcatalogservice` chi phối nhiều lỗi `rps_in`.
- Global edge permutation importance rất thấp:
  - edge group ≈ `0.000725`;
  - load group ≈ `0.108953`;
  - resource group ≈ `0.056031`.
- Graph/edge benefit hiện chưa đủ rõ để bù compute cost.

## `gat_layers=1`: trạng thái thảo luận

**Chưa có controlled experiment.**

- Baseline tốt nhất dùng 2 GAT layers, val `0.112560`.
- Một trial 1-layer tốt trong Optuna đạt khoảng `0.114333`, nhưng khác window, edge direction và hyperparameters; không thể dùng để kết luận 1 layer kém hơn.
- Giả thuyết 1 layer:
  - giảm compute và nguy cơ oversmoothing;
  - phù hợp khi edge signal yếu/nhiễu;
  - có thể mất 2-hop dependency, ảnh hưởng CPU/spike behavior.
- Khi được phép thử: giữ nguyên toàn bộ best config, chỉ đổi `gat_layers: 2 → 1`, cùng 5 seeds và cùng evaluation contract.

Tiêu chí sơ bộ để chấp nhận 1 layer:

- Global `rps_in`/`cpu_cores` MAE không kém quá khoảng 1%.
- Transition underprediction không kém quá khoảng 2%.
- Có cải thiện runtime đáng kể, mục tiêu tối thiểu khoảng 20%.
- Không chỉ thắng trên một run hoặc một seed.

## P1 đang brainstorm — chưa triển khai

1. Thêm GRU node-only để tách ảnh hưởng recurrent cell khỏi graph.
2. Chạy static GAT-GRU trước dynamic edges.
3. Không dùng `edge_latency_p95_ms` trong default dynamic-edge variant; giữ làm ablation.
4. Duy trì shared node feature set khi benchmark công bằng.
5. Đổi Optuna objective sang CPU-weighted và macro per-run/per-scenario.
6. Thêm high-load và transition/spike-onset evaluation.

## P2 — tuning, chưa triển khai

Chỉ bắt đầu P2 sau khi P1 đã chọn được architecture/edge variant. Không tune architecture, feature set, loss và objective cùng lúc.

1. **Thu hẹp Optuna search quanh vùng tốt hiện tại**:
   - `window`: `[12, 18, 24]`;
   - `gat_hidden`: `[16, 32]`;
   - `heads`: `[1, 2, 4]`;
   - `gat_layers`: cố định theo kết quả controlled ablation 1-vs-2 layers;
   - `gru_hidden`: `[64, 96, 128]`;
   - `dropout`: `0.0–0.20`;
   - learning rate: `5e-4–2.5e-3`, log scale;
   - weight decay: `1e-6–1e-3`, log scale;
   - batch size: `[32, 64, 128]`.
2. **CPU-weighted validation objective**:
   - candidate weights: RPS `0.3–0.4`, CPU `0.6–0.7`;
   - metric tính theo run rồi macro-average giữa scenarios;
   - không dùng test, high-load subset hoặc spike subset để chọn hyperparameter.
3. **Balanced training**:
   - giữ split theo run;
   - cân bằng contribution theo run/scenario nếu số valid windows khác nhau;
   - không random split individual windows.
4. **Node-feature group ablation**:
   - core 3: `rps_in`, `cpu_cores`, `replicas`;
   - core + load dynamics;
   - core + load + CPU/resource;
   - thêm latency;
   - full 13;
   - dùng cùng shared feature set khi so với LSTM/GRU.
5. **Training stability tuning**:
   - kiểm tra AdamW/weight decay;
   - gradient clipping;
   - scheduler dựa trên validation plateau;
   - early-stopping patience hợp lý;
   - luôn restore best validation checkpoint.
6. **Reproducibility cleanup**:
   - xử lý trial `RUNNING` còn sót;
   - thống nhất standard deviation convention;
   - version hóa study/output directory;
   - tiếp tục lưu full artifacts và source hashes.

P2 success criteria:

- CPU metric cải thiện nhất quán theo run/scenario, không chỉ global pooled average.
- Không làm `rps_in`, underprediction hoặc transition behavior suy giảm quá ngưỡng đã định trước.
- Chênh lệch được lặp lại qua 5 seeds.
- Báo cả compute cost; không chọn model lớn hơn chỉ vì một cải thiện rất nhỏ.

## P3 — optional experiments, chưa triển khai

Các thí nghiệm này chỉ đáng chạy sau khi P1/P2 đã tạo được compact baseline ổn định.

1. **Forecast horizon ablation**: 30s, 60s, 90s; đánh giá cùng deployment/scaling lead time thực tế.
2. **Normalization ablation**:
   - global scaler per feature;
   - per-service scaler per feature;
   - kiểm tra liệu per-service scaling có làm mất relative magnitude giữa services.
3. **Loss ablation**:
   - MAE hiện tại;
   - Huber loss cho burst/outlier;
   - asymmetric underprediction penalty cho autoscaling;
   - multi-target weights sau khi targets đã scale.
4. **Topology variants**:
   - directed vs bidirectional static graph;
   - self-loop/residual connection;
   - chỉ thử nếu static GAT chứng minh tốt hơn GRU node-only.
5. **Dynamic-edge robustness**:
   - edge dropout/missing-edge simulation;
   - sensitivity theo Jaeger coverage;
   - `CORRECT_COVERAGE=False` vẫn là default, chỉ đổi khi ablation có bằng chứng.
6. **Service-aware modeling**:
   - service embedding;
   - shared head so với service-specific/lightweight heads;
   - macro service loss để giảm domination bởi `currencyservice`/`productcatalogservice`.
7. **Uncertainty/ensemble**:
   - seed ensemble hoặc prediction interval;
   - chỉ dùng nếu runtime deployment cho phép.
8. **Interpretability**:
   - attention/edge contribution theo scenario và transition;
   - kiểm tra attention weight có ổn định qua seed hay không.
9. **Final train+validation refit**:
   - chỉ sau khi khóa architecture/hyperparameters;
   - xác định trước epoch budget hoặc nested validation strategy;
   - test đúng một lần cho final claim.
10. **Closed-loop autoscaling evaluation**:
    - đưa forecast vào controller/simulator;
    - đo SLO violations, under-provisioning, over-provisioning và replica cost;
    - đây mới là kiểm tra cuối xem cải thiện forecast có tạo lợi ích vận hành hay không.

P3 không phải điều kiện để hoàn thành baseline. Không chạy toàn bộ chỉ để mở rộng experiment count; chọn theo câu hỏi còn chưa được P1/P2 trả lời.

## Experiment ladder đề xuất

Không thay nhiều biến cùng lúc:

```text
A. Current dynamic GAT-GRU, 2 layers                 [đã có]
B. Current dynamic GAT-GRU, 1 layer                 [chưa chạy]
C. GRU node-only                                    [chưa chạy]
D. Static GAT-GRU, 1 layer                          [chưa chạy]
E. Dynamic GAT-GRU + call_rate/call_ratio           [chưa chạy]
F. E + edge_latency_p95_ms                           [baseline gần nhất]
G. Chọn architecture rồi mới đổi CPU-weighted loss  [chưa chạy]
```

Lý do giữ thứ tự:

- B cô lập ảnh hưởng `gat_layers`.
- C cô lập lợi ích của graph so với GRU.
- D cô lập topology.
- E/F cô lập dynamic edges và edge latency.
- G thay đổi selection objective sau khi đã chọn kiến trúc, tránh confounding.

## Objective/evaluation đề xuất cho vòng sau

Ý tưởng scalar objective ban đầu:

```text
run_score      = alpha × scaled_rps_MAE + beta × scaled_cpu_MAE
scenario_score = mean(run_score trong scenario)
optuna_score   = macro-average(scenario_score)

candidate: alpha = 0.3–0.4, beta = 0.6–0.7
```

Aggregation:

```text
metric theo run
→ trung bình các run trong scenario
→ macro-average ngang nhau giữa scenarios
```

Luôn báo riêng, không chỉ báo scalar objective:

- MAE/RMSE từng target.
- Under-MAE/over-MAE.
- High-load và transition MAE.
- P95 absolute error.
- Theo horizon, scenario, run và service.
- Parameters, train time và inference latency.

High-load threshold phải xác định từ train data hoặc rule cố định; không fit threshold trên test.

High-load/transition/spike metrics là **evaluation slices sau khi model đã được chọn**, không tham gia Optuna objective và không dùng để chọn feature/hyperparameter trong baseline chính.

## Guardrails cho session sau

- Không dùng test để chọn feature/hyperparameter.
- Không coi overlapping windows là independent samples.
- Không đổi architecture, feature set và Optuna objective trong cùng một ablation.
- Không kết luận edge vô dụng chỉ từ permutation importance khi features tương quan.
- Không kết luận `gat_layers=1` tốt/xấu trước controlled multi-seed comparison.
- Không ghi đè baseline artifacts; dùng output/study name được version hóa.
- Khi dataset hoặc split thay đổi, ghi lại fingerprints mới và không so trực tiếp như cùng một experiment.

## Current decision summary

- Baseline deployability: LSTM đang thực dụng hơn về `rps_in` và runtime.
- Research signal: GAT-GRU đáng tiếp tục nghiên cứu cho CPU và transition/spike.
- Câu hỏi quan trọng nhất tiếp theo: graph có mang lại lợi ích vượt GRU node-only hay không?
- Hướng tối ưu ưu tiên: GAT 1 layer → GRU node-only → static GAT → dynamic edge ablations → CPU/transition-weighted objective.

