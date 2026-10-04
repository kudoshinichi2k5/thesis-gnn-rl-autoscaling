# lstm-load-testing: baseline LSTM để so sánh với GAT-GRU

**Mục tiêu:** trả lời câu hỏi *đưa đồ thị phụ thuộc dịch vụ vào mô hình dự báo có thực sự giúp ích không?* bằng cách so sánh GAT-GRU với một mô hình chuỗi thời gian **không có đồ thị** (LSTM), trong điều kiện mọi yếu tố khác giống hệt nhau.

## Thiết kế so sánh công bằng

| Yếu tố | LSTM | GAT-GRU | Đảm bảo bởi |
|---|---|---|---|
| Tải sinh ra | Dùng chung `locustfile.py` và `scenarios/` | | `run-scenario.sh` gọi kịch bản của `load-testing/` |
| Truy vấn Prometheus/Jaeger | Dùng chung | | `collect_lstm_metrics.py` import `load-testing/collect_metrics.py` |
| Run train/val/test | Cùng các run | | `modeling-common/splits.json` |
| Feature nút ứng viên, chuẩn hóa, target, h = 6 | Giống hệt | | `modeling-common/forecast_data.py` |
| Quy trình chọn feature | Giống hệt | | `modeling-common/feature_selection.py` |
| Ngân sách tune, loss, optimizer, early stopping, seed | Giống hệt | | `modeling-common/forecast_torch.py` (`BUDGET`) |
| **Đồ thị** (cạnh, feature cạnh, message passing) | **Không** | **Có** | **Biến được so sánh** |

## Thu dữ liệu: chỉ các thông số LSTM cần

| File | Thu | Lý do |
|---|---|---|
| `node_metrics.csv` | ✅ | CPU, memory, network, throttling, replicas, restarts theo service (Prometheus) |
| `service_metrics.csv` | ✅ | `rps_in`, latency, lỗi theo service (span `server` của Jaeger). Bắt buộc vì `rps_in` là target |
| `locust_*.csv`, `meta.json`, `collect_report.json` | ✅ | Kiểm tra độ phủ, thời điểm run |
| `edge_metrics.csv` | ❌ | LSTM không có cạnh |

```bash
# Yêu cầu: đã chạy cluster-setup/06-setup-loadgen.sh (venv Locust + load-testing/ trên node-loadgen)
RUN_DURATION_SEC=120 COOLDOWN_SEC=0 bash lstm-load-testing/run-scenario.sh spike 1   # chạy thử
bash lstm-load-testing/run-scenario.sh normal 8
bash lstm-load-testing/run-scenario.sh spike  8
bash lstm-load-testing/run-scenario.sh bursty 8
```

Script tự rsync thư mục này lên `node-loadgen` (`~/lstm-load-testing`), chạy Locust bằng kịch bản và venv của `~/load-testing`, rồi kéo kết quả về `lstm-load-testing/results/<scenario>/run_XX/`. Biến môi trường giống `load-testing/run-scenario.sh` (`RUN_DURATION_SEC`, `COOLDOWN_SEC`, `STEP_SEC`, `BURSTY_SEED`, `AUTOSCALER`).

### Dùng dữ liệu nào để so sánh?

- **Mặc định (khuyến nghị):** `preprocess_lstm.ipynb` đọc các run trong `../load-testing/results/`. Các run đó đã chứa mọi thông số LSTM cần, nên hai mô hình được **huấn luyện và đánh giá trên cùng các run**. Đây là so sánh công bằng nhất, vì độ biến thiên giữa các run không lẫn vào chênh lệch giữa hai mô hình.
- **Run LSTM tự thu** (`results/` của thư mục này): đặt `INCLUDE_OWN_RUNS = True` trong `preprocess_lstm.ipynb`. Các run này chỉ được thêm vào **train**; tập test vẫn là các run dùng chung. Khi đó LSTM có nhiều dữ liệu train hơn GAT-GRU, nên phải ghi rõ trong báo cáo. Dữ liệu thu riêng hữu ích khi cần dataset LSTM độc lập, ví dụ cho proactive baseline trong thực nghiệm cuối với controller.

## Chạy mô hình

```bash
pip install -r modeling-common/requirements-ml.txt
```

Chạy notebook GAT-GRU trong `load-testing/` trước (`preprocess_gatgru` → `feature_selection_gatgru` → `train_gatgru`), rồi đến các notebook ở đây:

| Thứ tự | Notebook | Đầu ra trong `processed/` |
|---|---|---|
| 1 | `preprocess_lstm.ipynb` | `runs_raw.npz`, `lstm_dataset.npz` (cửa sổ mặc định) |
| 2 | `feature_selection_lstm.ipynb` | `selected_features.json` |
| 3 | `train_lstm.ipynb` | `lstm_predictions_test.npz`, `lstm_model.pt`, `lstm_best_config.json` |
| 4 | `compare_lstm_gatgru.ipynb` | Bảng và biểu đồ so sánh (đọc predictions của cả hai mô hình) |

Đặt `DATASET = 'math'` ở đầu **mọi** notebook (cả GAT-GRU lẫn LSTM) để so sánh trên dataset theo mô hình toán học ([`../math-load-testing/`](../math-load-testing/README.md)). Khi đó cửa sổ "đầu spike" được lấy từ kịch bản `mmpp`.

Thứ tự giữa hai pipeline không quan trọng: notebook tiền xử lý nào chạy trước sẽ tạo `modeling-common/splits.json`, notebook còn lại dùng lại file đó. Chi tiết chọn feature và tune: [`../modeling-common/README.md`](../modeling-common/README.md).

## Notebook so sánh đo những gì

| Chỉ số | Ý nghĩa cho autoscaling |
|---|---|
| MAE, RMSE (đơn vị gốc, trung bình ± độ lệch chuẩn qua 5 seed) | Độ chính xác chung, theo đề cương |
| MAE theo khoảng dự báo (10s…60s) | Dự báo xa có tốt không, quyết định khả năng scale *trước* tải |
| Sai số dự báo thiếu / thừa | Dự báo thiếu: thiếu replica, vi phạm SLO. Dự báo thừa: lãng phí tài nguyên |
| MAE theo kịch bản, theo service | Đồ thị giúp ở đâu (kỳ vọng: service downstream, kịch bản spike) |
| Sai số tại cửa sổ đầu spike | Thời điểm proactive scaling có giá trị nhất |
| Thắng/thua theo run + sign test | Run là đơn vị độc lập; các cửa sổ chồng lấn thì không |
| Số tham số, thời gian suy luận | Chi phí khi đưa vào controller MAPE-K |

Notebook cũng in kết quả của baseline **persistence** ($\hat y_{t+k} = y_t$). Một mô hình chỉ có ích cho scaling chủ động khi tốt hơn rõ rệt so với baseline này.

## Cấu trúc

```text
lstm-load-testing/
├── run-scenario.sh            # thu dữ liệu (dùng chung kịch bản với load-testing/)
├── collect_lstm_metrics.py    # chỉ xuất node_metrics + service_metrics
├── preprocess_lstm.ipynb
├── feature_selection_lstm.ipynb
├── train_lstm.ipynb
├── compare_lstm_gatgru.ipynb
└── results/                   # run LSTM tự thu (gitignored)
```
