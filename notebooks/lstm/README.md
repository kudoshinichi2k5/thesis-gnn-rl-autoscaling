# Baseline LSTM để so sánh với GAT-GRU

**Mục tiêu:** cung cấp baseline chuỗi thời gian **không có đồ thị** để so sánh với GAT-GRU. Phép so sánh cô lập đóng góp của đồ thị sử dụng artifact `v01_shared_selected_features`, vì model này dùng cùng 10 node features với GAT-GRU v05. Artifact `v02_node_only_selected_features` là baseline bổ sung với feature contract riêng.

## Thiết kế so sánh công bằng

| Yếu tố | LSTM | GAT-GRU | Đảm bảo bởi |
|---|---|---|---|
| Tải sinh ra, telemetry | Cùng các run | Cùng các run | Cả hai đọc `dataset/raw/` |
| Run train/val/test | Cùng split | Cùng split | `dataset/splits.json` |
| Feature nút ứng viên, chuẩn hóa, target, h = 6 | Cùng pipeline | Cùng pipeline | `modeling-common/forecast_data.py` |
| Node features trong phép so sánh chính | Cùng 10 feature | Cùng 10 feature | `v01_shared_selected_features` và `configs/gatgru/feature_contract_v01.json` |
| Loss, optimizer, early stopping, seed | Cùng protocol | Cùng protocol | `modeling-common/forecast_torch.py`, `modeling-common/forecast_ablation.py` |
| **Đồ thị** (cạnh, feature cạnh, message passing) | **Không** | **Có** | **Biến được so sánh** |

## Dữ liệu

Thư mục này **không thu dữ liệu riêng**. LSTM đọc trực tiếp `dataset/raw/`, là các run do [`load-testing/run-scenario.sh`](../../load-testing/README.md) thu (kịch bản nhpp, mmpp, pareto, onoff, thu khi HPA bật). Mỗi run đó đã có mọi thông số LSTM cần:

| File | LSTM dùng | Lý do |
|---|---|---|
| `node_metrics.csv` | ✅ | CPU, memory, network, throttling, replicas, restarts theo service (Prometheus) |
| `service_metrics.csv` | ✅ | `rps_in`, latency, lỗi theo service (span `server` của Jaeger). Bắt buộc vì `rps_in` là target |
| `meta.json`, `collect_report.json`, `locust_*.csv` | ✅ | Kiểm tra độ phủ, thời điểm run |
| `edge_metrics.csv` | ❌ | LSTM không có cạnh |

Nhờ vậy hai mô hình được **huấn luyện và đánh giá trên cùng các run**, nên độ biến thiên giữa các run không lẫn vào chênh lệch giữa hai mô hình.

## Chạy mô hình

```bash
pip install -r modeling-common/requirements-ml.txt
```

Chạy notebook GAT-GRU trong `notebooks/gatgru/` trước (`preprocess_gatgru` → `feature_selection_gatgru` → `train_gatgru`), rồi đến các notebook ở đây:

| Thứ tự | Notebook | Đầu ra |
|---|---|---|
| 1 | `preprocess_lstm.ipynb` | `dataset/processed/lstm/runs_raw.npz`, `lstm_dataset.npz` |
| 2 | `feature_selection_lstm.ipynb` | `dataset/processed/lstm/selected_features.json` |
| 3 | `train_lstm.ipynb` | Baseline output trong `dataset/processed/lstm/` |
| 4 | `train_lstm_v01_shared_selected_features.ipynb`, `train_lstm_v02_node_only_selected_features.ipynb` | Checkpoint, prediction và metrics trong `artifacts/lstm/` |
| 5 | `../comparison/compare_lstm_gatgru.ipynb` | Bảng và biểu đồ so sánh; cửa sổ "đầu spike" lấy từ `mmpp` |

Thứ tự giữa hai pipeline không quan trọng: notebook tiền xử lý nào chạy trước sẽ tạo `dataset/splits.json`, notebook còn lại dùng lại file đó. Chi tiết chọn feature và tune: [`modeling-common/README.md`](../../modeling-common/README.md).

## Notebook so sánh đo những gì

| Chỉ số | Ý nghĩa cho autoscaling |
|---|---|
| MAE, RMSE (đơn vị gốc, trung bình ± độ lệch chuẩn qua 5 seed) | Độ chính xác chung, theo đề cương |
| MAE theo khoảng dự báo (10s…60s) | Độ chính xác theo khoảng dự báo; là một tín hiệu liên quan đến khả năng scale sớm |
| Sai số dự báo thiếu / thừa | Dự báo thiếu: thiếu replica, vi phạm SLO. Dự báo thừa: lãng phí tài nguyên |
| MAE theo kịch bản, theo service | Đồ thị giúp ở đâu (kỳ vọng: service downstream, kịch bản mmpp) |
| Sai số tại cửa sổ đầu spike | Thời điểm proactive scaling có giá trị nhất |
| Thắng/thua theo run + sign test | Run là đơn vị độc lập; các cửa sổ chồng lấn thì không |
| Số tham số, thời gian suy luận | Chi phí khi đưa vào controller MAPE-K |

Notebook cũng in kết quả của baseline **persistence** ($\hat y_{t+k} = y_t$). Một mô hình chỉ có ích cho scaling chủ động khi tốt hơn rõ rệt so với baseline này.

## Cấu trúc

```text
notebooks/lstm/
├── preprocess_lstm.ipynb
├── feature_selection_lstm.ipynb
├── train_lstm.ipynb
├── train_lstm_v01_shared_selected_features.ipynb
└── train_lstm_v02_node_only_selected_features.ipynb
```
