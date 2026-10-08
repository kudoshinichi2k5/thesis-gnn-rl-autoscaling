# lstm-load-testing: baseline LSTM để so sánh với GAT-GRU

**Mục tiêu:** trả lời câu hỏi *đưa đồ thị phụ thuộc dịch vụ vào mô hình dự báo có thực sự giúp ích không?* bằng cách so sánh GAT-GRU với một mô hình chuỗi thời gian **không có đồ thị** (LSTM), trong điều kiện mọi yếu tố khác giống hệt nhau.

## Thiết kế so sánh công bằng

| Yếu tố | LSTM | GAT-GRU | Đảm bảo bởi |
|---|---|---|---|
| Tải sinh ra, telemetry | Cùng các run | | Cả hai đọc `math-load-testing/results/` |
| Run train/val/test | Cùng các run | | `modeling-common/splits_math.json` |
| Feature nút ứng viên, chuẩn hóa, target, h = 6 | Giống hệt | | `modeling-common/forecast_data.py` |
| Quy trình chọn feature | Giống hệt | | `modeling-common/feature_selection.py` |
| Ngân sách tune, loss, optimizer, early stopping, seed | Giống hệt | | `modeling-common/forecast_torch.py` (`BUDGET`) |
| **Đồ thị** (cạnh, feature cạnh, message passing) | **Không** | **Có** | **Biến được so sánh** |

## Dữ liệu

Thư mục này **không thu dữ liệu riêng**. LSTM đọc trực tiếp các run do [`../math-load-testing/run-scenario.sh`](../math-load-testing/README.md) thu (kịch bản nhpp, mmpp, pareto, onoff, thu khi HPA bật). Mỗi run đó đã có mọi thông số LSTM cần:

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

Chạy notebook GAT-GRU trong `math-load-testing/` trước (`preprocess_gatgru` → `feature_selection_gatgru` → `train_gatgru`), rồi đến các notebook ở đây:

| Thứ tự | Notebook | Đầu ra trong `processed_math/` |
|---|---|---|
| 1 | `preprocess_lstm.ipynb` | `runs_raw.npz`, `lstm_dataset.npz` (cửa sổ mặc định) |
| 2 | `feature_selection_lstm.ipynb` | `selected_features.json` |
| 3 | `train_lstm.ipynb` | `lstm_predictions_test.npz`, `lstm_model.pt`, `lstm_best_config.json` |
| 4 | `compare_lstm_gatgru.ipynb` | Bảng và biểu đồ so sánh (đọc predictions của cả hai mô hình). Cửa sổ "đầu spike" được lấy từ kịch bản `mmpp` |

Thứ tự giữa hai pipeline không quan trọng: notebook tiền xử lý nào chạy trước sẽ tạo `modeling-common/splits_math.json`, notebook còn lại dùng lại file đó. Khi chưa có run thật, các notebook chạy bằng dữ liệu demo (`processed_math_demo/`). Chi tiết chọn feature và tune: [`../modeling-common/README.md`](../modeling-common/README.md).

## Notebook so sánh đo những gì

| Chỉ số | Ý nghĩa cho autoscaling |
|---|---|
| MAE, RMSE (đơn vị gốc, trung bình ± độ lệch chuẩn qua 5 seed) | Độ chính xác chung, theo đề cương |
| MAE theo khoảng dự báo (10s…60s) | Dự báo xa có tốt không, quyết định khả năng scale *trước* tải |
| Sai số dự báo thiếu / thừa | Dự báo thiếu: thiếu replica, vi phạm SLO. Dự báo thừa: lãng phí tài nguyên |
| MAE theo kịch bản, theo service | Đồ thị giúp ở đâu (kỳ vọng: service downstream, kịch bản mmpp) |
| Sai số tại cửa sổ đầu spike | Thời điểm proactive scaling có giá trị nhất |
| Thắng/thua theo run + sign test | Run là đơn vị độc lập; các cửa sổ chồng lấn thì không |
| Số tham số, thời gian suy luận | Chi phí khi đưa vào controller MAPE-K |

Notebook cũng in kết quả của baseline **persistence** ($\hat y_{t+k} = y_t$). Một mô hình chỉ có ích cho scaling chủ động khi tốt hơn rõ rệt so với baseline này.

## Cấu trúc

```text
lstm-load-testing/
├── preprocess_lstm.ipynb
├── feature_selection_lstm.ipynb
├── train_lstm.ipynb
└── compare_lstm_gatgru.ipynb
```
