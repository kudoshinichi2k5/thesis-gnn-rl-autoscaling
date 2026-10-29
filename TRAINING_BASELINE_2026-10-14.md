# Training baseline hiện tại — 2026-10-14

Tài liệu này ghi lại version huấn luyện hiện tại của LSTM và GAT-GRU trên bộ `math-load-testing`. Mục tiêu là đóng băng một baseline có thể truy vết để so sánh với các đợt cải thiện sau. Đây không phải kết luận cuối cùng về kiến trúc tốt nhất.

## 1. Nguồn kết quả

- [`notebooks/lstm/train_lstm.ipynb`](notebooks/lstm/train_lstm.ipynb)
- [`notebooks/gatgru/train_gatgru.ipynb`](notebooks/gatgru/train_gatgru.ipynb)
- [`notebooks/comparison/compare_lstm_gatgru.ipynb`](notebooks/comparison/compare_lstm_gatgru.ipynb)
- Artifact LSTM: [`dataset/processed/lstm`](dataset/processed/lstm)
- Artifact GAT-GRU: [`dataset/processed/gatgru`](dataset/processed/gatgru)

Các notebook đã chạy thành công và không có exception trong output được lưu.

## 2. Định danh version và tính công bằng

| Thuộc tính | Giá trị |
|---|---|
| Ngày ghi nhận | 2026-10-14 |
| Dataset fingerprint | `b18509670bb1ca10b356da33c2c282b169fdea1103ac45fdca6e788a097977d2` |
| Split fingerprint | `05c2ba9d78432b338f736c3c16b186b9d17c5d4614ce16607c4f9511b9851d76` |
| Tổng số run | 32: 8 run × 4 scenario |
| Split | 24 train / 4 validation / 4 test |
| Scenario | MMPP, NHPP, on-off, Pareto |
| Services | 11 |
| Sampling interval | 10 giây |
| Warm-up bị loại | 6 bước = 60 giây |
| Input window tốt nhất | 18 bước = 180 giây cho cả hai mô hình |
| Forecast horizon | 6 bước = 60 giây |
| Test samples chung | 611 cửa sổ |
| Final seeds | 0, 1, 2, 3, 4 |
| Optuna budget | 40 trials/mô hình, search seed 42 |
| Thiết bị | CPU |

Môi trường được lưu trong artifact:

| Thành phần | Version |
|---|---|
| OS | Windows 11 (`10.0.26200`) |
| Python | 3.12.10 |
| NumPy | 2.5.4 |
| pandas | 3.0.6 |
| PyTorch | 2.14.1+cpu |
| Optuna | 5.0.0 |
| `forecast_data.py` SHA-256 | `3ecd638da1cd508789a1f2ed8194d3f71acf819cbaada47a1471e4dc5b244175` |
| `forecast_torch.py` SHA-256 | `d738a4cbd994580dbf00bce80432ec170b2e0353393edd3152d4497f3422eda2` |

Hai prediction artifact đã vượt qua kiểm tra contract: cùng dataset, split, run IDs, service order, node features, targets, horizon và đúng cùng 611 sample identities. Vì vậy so sánh hiện tại không bị lệch sample giữa hai pipeline.

Các test run:

- MMPP: `math-load-testing/mmpp/run_04`
- NHPP: `math-load-testing/nhpp/run_01`
- On-off: `math-load-testing/onoff/run_06`
- Pareto: `math-load-testing/pareto/run_08`

Mỗi scenario hiện chỉ có **một test run**. Đây là hạn chế thống kê quan trọng nhất của baseline.

## 3. Feature set

Hai mô hình dùng cùng 13 node features:

`rps_in`, `rps_in_delta`, `rps_per_replica`, `latency_p50_ms`, `latency_p95_ms`, `cpu_cores`, `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`, `mem_mib`, `net_rx_kBps`, `net_tx_kBps`, `replicas`.

Hai feature bị loại do gần như hằng số:

- `error_rate`
- `restarts_delta`

GAT-GRU dùng thêm ba edge features:

- `call_rate`
- `call_ratio`
- `edge_latency_p95_ms`

`edge_error_rate` bị loại do gần như hằng số. Permutation importance của toàn bộ nhóm edge chỉ khoảng `0.000725`, thấp hơn nhiều so với nhóm load (`0.108953`) và resource (`0.056031`). Đây là dấu hiệu edge hiện tại đóng góp yếu, nhưng chưa phải bằng chứng nhân quả vì chưa có controlled ablation cùng cấu hình và seed.

## 4. Cấu hình tốt nhất

### LSTM

- Best Optuna validation MAE chuẩn hóa: `0.110227`
- Window: 18
- Hidden size: 128
- Layers: 2
- Mode: `joint`
- Dropout: `0.068154`
- Learning rate: `0.001208`
- Batch size: 64
- Optuna states: 29 complete, 11 pruned

### GAT-GRU

- Best Optuna validation MAE chuẩn hóa: `0.112560`
- Window: 18
- GAT hidden: 32
- Attention heads: 4
- GAT layers: 2
- GRU hidden: 128
- Edge direction: `both`
- Use edge features: `true`
- Dropout: `0.055739`
- Learning rate: `0.001546`
- Batch size: 64
- Optuna states trong artifact: 18 complete, 21 pruned, 1 còn trạng thái `RUNNING`

Trial `RUNNING` còn sót của GAT-GRU cần được xử lý trước khi gọi đây là study 40 trials hoàn tất. Best trial và năm final-seed models vẫn đã được lưu đầy đủ.

## 5. Validation và chi phí tính toán

| Chỉ số | LSTM | GAT-GRU | Nhận xét |
|---|---:|---:|---|
| Best Optuna val MAE, scaled | 0.110227 | 0.112560 | LSTM thấp hơn khoảng 2.1% |
| Final-seed val MAE, mean ± std | 0.11157 ± 0.00125 | 0.11549 ± 0.00103 | LSTM thấp hơn khoảng 3.4% |
| Tham số | 311,472 | 122,328 | GAT-GRU ít hơn khoảng 60.7% |
| Inference CPU/window | 1.69 ms | 6.59 ms | GAT-GRU chậm hơn khoảng 3.9× |
| Final training, 5 seeds | 375.5 s | 3,311.5 s | GAT-GRU chậm hơn khoảng 8.8× |
| Epochs theo seed | 23, 26, 30, 28, 25 | 30, 27, 25, 22, 25 | Cùng mức early stopping tương đối |

GAT-GRU có ít tham số hơn nhưng chậm hơn đáng kể vì graph message passing và xử lý edge. Nếu mục tiêu triển khai ưu tiên latency/chi phí CPU, LSTM hiện có lợi thế rõ.

## 6. Kết quả test chính

Các giá trị dưới đây là mean ± population standard deviation qua 5 final seeds, đúng với bảng hiển thị trong notebook.

| Target | Model | MAE | RMSE | Under-MAE | Over-MAE |
|---|---|---:|---:|---:|---:|
| `rps_in` | LSTM | **0.9741 ± 0.0194** | **2.9419 ± 0.0497** | **0.5498 ± 0.0145** | 0.4243 ± 0.0245 |
| `rps_in` | GAT-GRU | 1.0039 ± 0.0166 | 2.9610 ± 0.0414 | 0.5878 ± 0.0467 | **0.4161 ± 0.0441** |
| `rps_in` | Persistence | 1.1860 | 3.4151 | 0.6005 | 0.5855 |
| `cpu_cores` | LSTM | 0.0129 ± 0.0001 | 0.0371 ± 0.0005 | 0.0071 ± 0.0002 | 0.0058 ± 0.0002 |
| `cpu_cores` | GAT-GRU | **0.0128 ± 0.0002** | **0.0359 ± 0.0004** | **0.0070 ± 0.0002** | **0.0058 ± 0.0003** |
| `cpu_cores` | Persistence | 0.0199 | 0.0609 | 0.0102 | 0.0096 |

So với LSTM, GAT-GRU:

- `rps_in`: MAE kém hơn `3.1%`, RMSE kém hơn `0.6%`.
- `cpu_cores`: MAE tốt hơn `1.0%`, RMSE tốt hơn `3.1%`.

So với persistence, cả hai mô hình đều cải thiện rõ:

- LSTM giảm khoảng 17.9% MAE `rps_in` và 35.2% MAE `cpu_cores`.
- GAT-GRU giảm khoảng 15.4% MAE `rps_in` và 35.8% MAE `cpu_cores`.

Cả hai mô hình đều có under-MAE lớn hơn over-MAE. Khuynh hướng dự báo thiếu tải/tài nguyên cần được ưu tiên vì underprediction có rủi ro trực tiếp đối với autoscaling và SLO.

Lưu ý: các file `*_test_metrics_summary.csv` dùng sample standard deviation (`ddof=1`), trong khi notebook hiển thị population standard deviation (`ddof=0`). Mean không thay đổi; version sau nên thống nhất một quy ước.

## 7. Kết quả theo scenario

Bảng sau dùng prediction ensemble trung bình qua 5 seeds, giống cell phân tích scenario trong notebook compare:

| Scenario | LSTM `rps_in` MAE | GAT-GRU `rps_in` MAE | LSTM `cpu_cores` MAE | GAT-GRU `cpu_cores` MAE |
|---|---:|---:|---:|---:|
| MMPP | **1.8322** | 1.8323 | 0.0214 | **0.0203** |
| NHPP | **0.7481** | 0.8165 | 0.0115 | **0.0115** |
| On-off | **0.6007** | 0.6175 | **0.0082** | 0.0082 |
| Pareto | **0.6098** | 0.6320 | **0.0083** | 0.0084 |

Nhận xét:

- MMPP là scenario khó nhất cho cả hai target.
- LSTM tốt hơn trên `rps_in` ở cả bốn test runs.
- GAT-GRU cải thiện CPU rõ nhất ở MMPP; các scenario còn lại chênh lệch rất nhỏ.
- Chênh lệch MMPP `rps_in` gần như bằng 0, nên không nên tuyên bố hai mô hình khác nhau tại scenario này.

## 8. Forecast horizon

MAE trung bình qua seed tăng theo horizon:

| Target | Model | 10 giây | 60 giây | Mức tăng |
|---|---|---:|---:|---:|
| `rps_in` | LSTM | 0.6864 | 1.1684 | +70.2% |
| `rps_in` | GAT-GRU | 0.7383 | 1.1844 | +60.4% |
| `cpu_cores` | LSTM | 0.00890 | 0.01888 | +112.2% |
| `cpu_cores` | GAT-GRU | 0.00815 | 0.01898 | +133.0% |

LSTM tốt hơn cho `rps_in` ở mọi horizon. GAT-GRU tốt hơn cho CPU ở horizon ngắn, nhưng lợi thế gần như biến mất ở 50–60 giây.

## 9. Đầu spike MMPP

Có 66 cửa sổ được đánh dấu là đầu spike. Bảng sử dụng prediction ensemble trung bình qua seed:

| Target | Model | MAE | RMSE | Under-MAE | Over-MAE |
|---|---|---:|---:|---:|---:|
| `rps_in` | LSTM | 2.2222 | 6.5281 | 1.7717 | 0.4505 |
| `rps_in` | GAT-GRU | **2.1543** | 6.4319 | **1.7549** | 0.3994 |
| `rps_in` | Persistence | 2.1660 | **6.3990** | 1.9956 | **0.1704** |
| `cpu_cores` | LSTM | 0.0221 | 0.0780 | **0.0166** | 0.0055 |
| `cpu_cores` | GAT-GRU | **0.0213** | **0.0766** | 0.0169 | **0.0044** |
| `cpu_cores` | Persistence | 0.0296 | 0.1008 | 0.0228 | 0.0068 |

GAT-GRU phản ứng tốt hơn LSTM ở đầu spike cho cả hai target. Tuy nhiên:

- Với `rps_in`, GAT-GRU chỉ nhỉnh hơn persistence rất ít về MAE và vẫn kém persistence về RMSE.
- Underprediction tại đầu spike vẫn rất lớn đối với cả hai mô hình.
- Với CPU, cả hai mô hình đều cải thiện đáng kể so với persistence.

Đây là khu vực GAT-GRU có tín hiệu lợi ích đáng chú ý nhất, nhưng cần thêm spike runs để xác nhận.

## 10. Kết quả theo service

Các service khó nhất cho `rps_in`:

| Service | LSTM MAE | GAT-GRU MAE |
|---|---:|---:|
| `currencyservice` | 4.2623 | 4.2810 |
| `productcatalogservice` | **2.8168** | 2.9745 |
| `frontend` | 0.8843 | 0.9026 |
| `cartservice` | 0.8783 | 0.9175 |

Các service khó nhất cho `cpu_cores`:

| Service | LSTM MAE | GAT-GRU MAE |
|---|---:|---:|
| `currencyservice` | 0.0500 | **0.0465** |
| `frontend` | 0.0375 | **0.0370** |
| `recommendationservice` | **0.0125** | 0.0134 |
| `cartservice` | 0.0122 | **0.0104** |

Sai số tổng bị chi phối mạnh bởi một số service có scale lớn. Version sau nên báo cáo thêm normalized error hoặc macro-average theo service để tránh chỉ tối ưu các service lớn.

## 11. Ý nghĩa thống kê

So sánh trên bốn test runs:

| Target | GAT-GRU thắng | LSTM thắng | Sign-test p-value |
|---|---:|---:|---:|
| `rps_in` | 0 | 4 | 0.125 |
| `cpu_cores` | 2 | 2 | 1.000 |

LSTM thắng cả bốn run về `rps_in`, nhưng `p=0.125` chưa đủ để kết luận ở ngưỡng 0.05. CPU không cho thấy mô hình thắng nhất quán. Không nên coi 611 overlapping windows là 611 quan sát độc lập; đơn vị độc lập hợp lý hơn là run, hiện chỉ có bốn test runs.

## 12. Kết luận cho baseline hiện tại

1. **Không có mô hình thắng tuyệt đối.** LSTM tốt hơn cho dự báo tải tổng thể; GAT-GRU nhỉnh hơn cho CPU và đầu spike.
2. **LSTM là lựa chọn thực dụng hơn ở thời điểm hiện tại** nếu ưu tiên MAE `rps_in`, latency CPU và thời gian train.
3. **GAT-GRU chưa chứng minh được lợi ích đủ lớn từ topology/edge features** để bù chi phí tính toán trong metric tổng. Tín hiệu tốt nhất nằm ở CPU MMPP và đầu spike.
4. **MMPP, horizon dài và underprediction đầu spike là ba điểm yếu chính** của cả hai mô hình.
5. **Độ chắc chắn thống kê còn thấp** vì mỗi scenario chỉ có một test run. Các chênh lệch 1–3% chưa đủ mạnh để khẳng định ưu thế kiến trúc.

## 13. Ưu tiên cho đợt cải thiện tiếp theo

### Ưu tiên cao

1. Tăng số test runs độc lập cho từng scenario hoặc dùng repeated group split/cross-validation theo run. Báo confidence interval được cluster theo run.
2. Chạy controlled ablation cho GAT-GRU với cùng seed và hyperparameters:
   - không graph/không edge;
   - graph topology nhưng không edge features;
   - edge features hiện tại;
   - forward/reverse/both direction.
3. Tối ưu mục tiêu phù hợp autoscaling: asymmetric loss hoặc spike/onset-weighted loss để phạt underprediction mạnh hơn.
4. Bổ sung dữ liệu MMPP và spike onset; hiện đây là vùng sai số lớn nhất và cũng là nơi graph có tín hiệu hữu ích nhất.
5. Đóng hoặc resume trial GAT-GRU còn trạng thái `RUNNING`; thống nhất budget theo completed trials hoặc wall-clock thay vì chỉ số trial tổng.

### Ưu tiên trung bình

6. Báo cáo macro-average theo service, normalized MAE/NRMSE và SLO-oriented metrics bên cạnh global MAE.
7. Thử loss/heads riêng cho `rps_in` và `cpu_cores`, hoặc điều chỉnh target weights theo rủi ro vận hành.
8. Kiểm tra feature/trace quality của `currencyservice` và `productcatalogservice`, hai nguồn lỗi `rps_in` lớn nhất.
9. Benchmark batch inference và end-to-end serving latency trên đúng phần cứng triển khai; số hiện tại chỉ là CPU latency cho một window.
10. Thống nhất quy ước standard deviation (`ddof`) và phân biệt rõ:
    - mean metric qua các seed;
    - metric của ensemble prediction trung bình qua seed.

## 14. Điều kiện để công nhận version mới tốt hơn

Một version tiếp theo chỉ nên thay baseline này khi:

- sử dụng đúng frozen dataset/split hoặc ghi rõ fingerprint mới;
- chạy cùng tập final seeds và cùng evaluation contract;
- cải thiện trên nhiều test runs, không chỉ trên overlapping windows;
- không làm tăng underprediction/spike risk ngoài ngưỡng chấp nhận;
- báo cáo đồng thời accuracy, latency, parameter count và training cost;
- với GAT-GRU, chứng minh graph/edge contribution bằng ablation có kiểm soát.

