# modeling-common: code dùng chung cho GAT-GRU và baseline LSTM

Thư mục này chứa logic dữ liệu, chọn feature, huấn luyện, tìm siêu tham số và đánh giá dự báo dùng chung. Notebook GAT-GRU và LSTM trong [`notebooks/`](../notebooks/README.md) đều đọc các run trong [`dataset/raw/`](../dataset/README.md), dùng cùng frozen split và cùng metric implementation. Đóng góp của đồ thị chỉ được kết luận từ phép so sánh có cùng node-feature contract và protocol đánh giá; không suy ra từ mọi cặp artifact GAT-GRU/LSTM bất kỳ.

## Các module

| File | Phụ thuộc | Nội dung |
|---|---|---|
| `forecast_data.py` | numpy, pandas | Hằng số (service, topology, feature, target, `HORIZON = 6`); đọc run; căn lưới 10s; tính feature; đọc split dùng chung (`dataset/splits.json`); chuẩn hóa (clip p99,9 → log1p → z-score fit trên train); cắt cửa sổ trượt; dữ liệu demo |
| `feature_selection.py` | numpy, pandas | Bước 1 của chọn feature (lọc thống kê) và luật chọn theo permutation importance |
| `forecast_torch.py` | PyTorch, Optuna | `LSTMForecaster`, `GATGRUForecaster` (GAT có feature cạnh, viết bằng PyTorch thuần); vòng huấn luyện; Optuna; permutation importance; huấn luyện lại nhiều seed; đo thời gian suy luận |
| `forecast_metrics.py` | numpy, pandas | Quy ước file dự đoán; MAE/RMSE; sai số dự báo thiếu/thừa; theo khoảng dự báo, kịch bản, service; cửa sổ đầu spike; sign test theo run |
| `requirements-ml.txt` | | Môi trường huấn luyện (máy cá nhân hoặc Colab), không cài lên `node-loadgen` |
| `dataset/splits.json` | | Frozen mapping train/val/test theo run, dùng chung cho mọi model và được Git track |

## Quy trình chung (cho mỗi mô hình)

```text
preprocess_<model>  ──▶ runs_raw.npz (mảng thô, mọi feature ứng viên) + dataset/splits.json (dùng chung)
        │
feature_selection_<model>
        │  Bước 1  lọc thống kê (train): gần hằng số · độ liên quan với target tương lai · trùng lặp
        │  Bước 2  permutation importance (train → val) với cấu hình mặc định
        │  Bước 3  huấn luyện lại với tập đã chọn; nếu val xấu hơn > 2% thì giữ tập đã lọc
        ▼  selected_features.json
train_<model>
        │  Optuna (TPE + MedianPruner) trên val: window, kiến trúc, lr, dropout, batch
        │  Huấn luyện lại cấu hình tốt nhất với 5 seed
        ▼  <model>_predictions_test.npz  (đánh giá test MỘT lần)
compare_lstm_gatgru (notebooks/comparison/)
```

### Chọn feature: vì sao làm hai lớp

| Lớp | Bắt được | Không bắt được |
|---|---|---|
| **Lọc thống kê** (không cần mô hình, nhanh) | Feature không đổi (vd. `restarts_delta`), cặp gần như trùng nhau (`net_rx`/`net_tx`, `latency_p50`/`p95`, `rps_in`/`rps_per_replica` khi replica ít thay đổi) | Feature có tương quan với target nhưng mô hình không dùng tới, hoặc tương tác phi tuyến |
| **Permutation importance** (theo từng mô hình) | Feature mà *chính mô hình đó* thực sự dựa vào. Xáo trộn feature trên val làm MAE tăng thì feature có ích | Hai feature thay thế được cho nhau có thể đều trông "không quan trọng"; vì vậy cần bước lọc trùng lặp trước, và bước kiểm tra lại sau |

- Feature **bắt buộc giữ**: `rps_in`, `cpu_cores` (target dùng làm đầu vào tự hồi quy) và `replicas` (biến PPO điều khiển).
- Với GAT-GRU, feature **cạnh** (`call_rate`, `call_ratio`, `edge_error_rate`, `edge_latency_p95_ms`) cũng đi qua cùng quy trình. Optuna còn được phép tắt hẳn feature cạnh (`use_edge_features`).
- Permutation importance được báo cáo cả theo **nhóm** (load / perf / resource / capacity / edge). Đây là một dạng ablation rẻ: không cần huấn luyện lại cho mỗi nhóm.

### Tìm siêu tham số

| | LSTM | GAT-GRU |
|---|---|---|
| Chung | window w ∈ {6, 12, 18}, dropout [0; 0,3], lr [1e-4; 3e-3] (log), batch {64, 128, 256} | như LSTM |
| Kiến trúc | hidden {32, 64, 128}, số lớp {1, 2, 3}, `per_service` / `joint` | GAT hidden {16, 32, 64}, heads {1, 2, 4}, số lớp GAT {1, 2}, GRU hidden {32, 64, 128}, hướng cạnh {xuôi, ngược, cả hai}, dùng feature cạnh {có, không} |

**Ngân sách mặc định** trong `forecast_torch.BUDGET` cho các notebook baseline:
- 40 trial, TPE sampler với seed 42, MedianPruner (5 trial khởi động, 5 epoch warm-up);
- tối đa 60 epoch, early stopping sau 8 epoch không cải thiện;
- loss Huber trên target đã chuẩn hóa, AdamW, gradient clipping 1,0;
- 5 seed cho đánh giá cuối.

Notebook experiment có version có thể khai báo ngân sách riêng và phải lưu trong manifest. GAT-GRU v05 dùng 24 Optuna trials; cấu hình và runtime nằm trong `artifacts/gatgru/v05_selected_features_final/`.

**Mục tiêu tối ưu:** MAE trên val ở thang đã chuẩn hóa, trung bình hai target. Chuẩn hóa giúp `rps_in` (hàng chục) và `cpu_cores` (phần trăm core) đóng góp cân bằng.

### Hai mô hình

- **LSTM (baseline)** có hai chế độ:
  - `per_service`: một LSTM có trọng số chung chạy riêng trên chuỗi của từng service, cộng service embedding; không thấy service khác.
  - `joint`: ghép 11 service thành một chuỗi; học được tương quan chéo nhưng không có cấu trúc đồ thị.
- **GAT-GRU:** tại mỗi bước, một lớp GAT tổng hợp thông tin từ các service láng giềng. Hệ số attention $\alpha_{ij}$ được tính từ trạng thái hai nút cộng feature cạnh (tần suất gọi, latency...). Sau đó một GRU chạy trên chuỗi embedding của từng service, rồi một lớp tuyến tính dự báo 6 bước × 2 target. Có thêm skip connection giữ tín hiệu riêng của nút và service embedding giống LSTM.

## Quy ước file dự đoán

`<model>_predictions_test.npz` (ghi bởi `forecast_metrics.save_predictions`):

| Khóa | Shape | Ý nghĩa |
|---|---|---|
| `Y_pred` | [seeds, S, 6, 11, 2] | dự báo, **đơn vị gốc** (rps, core) |
| `Y_true` | [S, 6, 11, 2] | giá trị thực |
| `Y_last` | [S, 11, 2] | giá trị quan sát cuối (cho baseline persistence và phát hiện đầu spike) |
| `info` | [S, 3] | (run key, kịch bản, `t_target0`): bước đầu tiên được dự báo, dùng để ghép hai mô hình có window khác nhau |
| `meta` | json | cấu hình, feature, số tham số, thời gian suy luận, scaler |

Một mô hình mới (ví dụ proactive baseline cho thực nghiệm cuối) chỉ cần ghi đúng định dạng này là so sánh được ngay.

## Chạy

```bash
pip install -r modeling-common/requirements-ml.txt
```

Notebook nằm trong `notebooks/gatgru/`, `notebooks/lstm/` và `notebooks/comparison/`; source cells xác định repo root rồi import `modeling-common/`.

- **Dữ liệu:** mọi notebook đọc `dataset/raw/` và dùng `dataset/splits.json`. Preprocessing cùng notebook baseline ghi cache/output dẫn xuất vào `dataset/processed/<model>/`; notebook experiment có version ghi checkpoint, prediction và metrics vào `artifacts/<model>/`.
- **Raw data:** `dataset/raw/` được Git track và phải push; `dataset/processed/` bị ignore vì là dữ liệu dẫn xuất.
