# Báo cáo lựa chọn đặc trưng và đánh giá huấn luyện GAT-GRU

Ngày tổng hợp: 22/10/2026. Phạm vi: các kết quả đã lưu của chuỗi thí nghiệm chọn feature, graph/edge ablation, GAT-GRU cuối và LSTM dùng chung node features.

Báo cáo được lập bằng cách đọc code và các artifact JSON/CSV hiện có; không thực hiện huấn luyện, chạy notebook hoặc mô phỏng RL. Các phép tổng hợp bổ sung chỉ tính từ bảng metric đã lưu. Tất cả kết luận chính áp dụng cho tập hợp **bốn kịch bản MMPP, NHPP, on-off và Pareto**, với trọng số bằng nhau giữa các kịch bản; không chọn một mô hình riêng cho từng kịch bản.

## 1. Kết quả chính

1. Bộ node features giảm từ 13 xuống 10, bỏ `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`. Trong xác nhận bằng 5 seed trên validation, score giảm từ 0,115510 xuống 0,113994, tương đương cải thiện khoảng 1,31%. Đây là bộ đặc trưng được chọn trong phạm vi đã thử, chưa phải chứng minh một tập tối thiểu hoặc tối ưu toàn cục.
2. Đưa GAT vào giúp giảm lỗi so với đối chứng **GRU node-only** trên validation tổng hợp bốn kịch bản: static GAT-GRU giảm score khoảng 3,36%; GAT-GRU với ba edge features giảm khoảng 3,79%. Đối chứng có MLP trước GRU và số tham số gần tương đương, không phải một GRU thuần không có tầng tiền xử lý.
3. Bằng chứng mạnh hơn nằm ở lợi ích trao đổi thông tin giữa các node. Topology thật chưa vượt nhất quán các topology hoán vị; ba edge metrics chỉ bổ sung mức cải thiện nhỏ. Chưa thể khẳng định mô hình đã học đúng quan hệ phụ thuộc nhân quả hoặc edge metrics là nguyên nhân giúp vượt LSTM.
4. Trên test đã căn chỉnh, lấy macro-average đều giữa bốn kịch bản, GAT-GRU đạt MAE **0,999493 requests/s** và **0,012839 CPU cores**; LSTM đạt **0,962377 requests/s** và **0,012618 CPU cores**. LSTM có MAE thấp hơn khoảng **3,71% cho RPS** và **1,72% cho CPU**.
5. Chênh lệch test nghiêng về LSTM nhưng chưa đủ bằng chứng thống kê để khẳng định ưu thế tổng quát: sign test hai phía cho cả hai output có `p = 0,125` trên bốn cặp test run. Năm seed dùng chung test set, không phải năm tập kiểm thử độc lập.

## 2. Bài toán GAT-GRU trong dự án

### 2.1. Đầu vào và đầu ra

Bài toán là dự báo đa biến, đa service và nhiều bước thời gian cho hệ thống microservices. Mô hình dùng lịch sử tải, tài nguyên và giao tiếp giữa các service để dự báo tải yêu cầu đến và mức CPU sử dụng trong tương lai. Dự báo có thể làm đầu vào cho bộ điều khiển autoscaling; thí nghiệm trong báo cáo chỉ đánh giá chất lượng dự báo.

Hệ thống gồm 11 node: `frontend`, `adservice`, `cartservice`, `checkoutservice`, `currencyservice`, `emailservice`, `paymentservice`, `productcatalogservice`, `recommendationservice`, `redis-cart`, `shippingservice`.

Mỗi mẫu được lấy cách nhau 10 giây. Tại thời điểm dự báo, mô hình nhận cửa sổ lịch sử dài W và xuất trực tiếp 6 bước tương lai, tương ứng 10, 20, 30, 40, 50 và 60 giây. Hai biến cần dự đoán tại từng service là:

| Output | Ý nghĩa | Đơn vị khi báo cáo test |
|---|---|---|
| `rps_in` | Tốc độ yêu cầu đến service | requests/s |
| `cpu_cores` | Mức sử dụng CPU của service theo dữ liệu dự án | CPU cores |

Với batch B, số service N = 11, số node features F = 10, số cạnh M, số edge features Fe = 3, horizon H = 6 và số target K = 2:

```text
X: [B, W, 11, 10]       lịch sử đặc trưng node
E: [B, W, M, 3]         lịch sử đặc trưng cạnh của GAT-GRU
Y: [B, 6, 11, 2]        giá trị mục tiêu tương lai
```

Một cửa sổ tạo ra 132 giá trị dự báo: 6 bước × 11 service × 2 target. Các output không phải tổng RPS/CPU của cả hệ thống; metric báo cáo là lỗi trung bình trên các phần tử dự báo tương ứng.

### 2.2. Luồng xử lý của GAT-GRU

```text
10 node features + service embedding 4 chiều tại mỗi thời điểm
                 ↓
GAT trên graph service, dùng edge metrics để điều chỉnh attention
                 ↓
Biểu diễn từng node + nhánh skip từ đầu vào của chính node
                 ↓
GRU theo chuỗi thời gian riêng của từng node, dùng chung trọng số
                 ↓
Linear head → 6 bước × 2 target cho mỗi node
```

GAT xử lý quan hệ không gian giữa các service tại từng thời điểm. GRU xử lý sự thay đổi theo thời gian của biểu diễn sau GAT. Cấu hình cuối dùng 2 tầng GAT, 4 attention heads, 32 chiều mỗi head, GRU hidden size 128 và truyền thông tin theo cả hai chiều của cạnh. Self-loop giữ thông tin của chính node; skip connection bổ sung một phép chiếu trực tiếp của đầu vào node.

Danh sách cạnh là cấu trúc được cung cấp cho mô hình. “Dynamic edges” trong thí nghiệm nghĩa là **giá trị edge features thay đổi theo thời gian**, không có nghĩa mô hình tự học hoặc tự thay đổi danh sách cạnh.

Nguồn triển khai: [forecast_torch.py](modeling-common/forecast_torch.py), các lớp `EdgeGATLayer`, `GATGRUForecaster` và `LSTMForecaster`.

## 3. Dữ liệu và giao thức đánh giá

Dataset gồm 32 run, chia đều 8 run cho mỗi kịch bản. Phân chia thực tế theo nhãn từng run trong [dataset/splits.json](dataset/splits.json): 24 train, 4 validation và 4 test. Không chia ngẫu nhiên các sliding window giữa train và test.

| Kịch bản | Test run đóng góp vào kết quả tổng hợp |
|---|---|
| MMPP | `math-load-testing/mmpp/run_04` |
| NHPP | `math-load-testing/nhpp/run_01` |
| on-off | `math-load-testing/onoff/run_06` |
| Pareto | `math-load-testing/pareto/run_08` |

Mỗi run bỏ 6 bước warm-up đầu tiên. Các cửa sổ không đi qua ranh giới run hoặc split. Scaler chỉ fit trên train: clip theo percentile 99,9 của train, biến đổi theo loại feature rồi chuẩn hóa z-score. Hai target sử dụng `log1p` trước z-score. Thống kê scaler được gộp theo từng feature trên các timestep/node của train, không fit lại trên validation hoặc test.

Với target không âm k, giá trị dùng để huấn luyện có dạng:

$$
z_k(y)=\frac{\log(1+\min(y,c_k))-\mu_k}{\sigma_k},
$$

trong đó c, μ, σ đều lấy từ train. Test MAE/RMSE được tính sau inverse transform của dự báo, so với `Y_raw` còn giữ giá trị mục tiêu gốc. Vì vậy scaled score và lỗi trong đơn vị gốc có thể xếp hạng hơi khác nhau, đặc biệt ở đỉnh tải vượt ngưỡng clip.

Quy trình thực tế là: học trọng số trên train → chọn checkpoint bằng validation → chọn cấu hình bằng validation → huấn luyện các seed cuối vẫn với train/validation tách biệt → đánh giá checkpoint trên test. **Không có bước refit trên train + validation trong `run_final` hiện tại.** Test không phải đánh giá trên tập train + validation.

Các bước audit dùng train; ablation và tuning dùng validation để lựa chọn. Các phân tích bổ sung sau khi đã xem test là phân tích hậu nghiệm. Không thể xem toàn bộ vòng nghiên cứu kéo dài qua nhiều lần xem test như một thử nghiệm xác nhận duy nhất đã đăng ký trước.

## 4. Score function, loss function và gradient descent

### 4.1. Phân biệt attention score và score đánh giá mô hình

“Score” có hai nghĩa khác nhau trong dự án. Attention score nằm bên trong GAT để xác định mức đóng góp của hàng xóm. Validation score nằm bên ngoài mô hình để so sánh cấu hình và chọn hyperparameters.

Với node nhận i, node gửi j và attention head q, code GAT tính:

$$
e_{ij}^{(q)}=\operatorname{LeakyReLU}_{0.2}
\left((a_{dst}^{(q)})^\top Wh_i
+(a_{src}^{(q)})^\top Wh_j
+(w_e^{(q)})^\top x_{ij}\right),
$$

$$
\alpha_{ij}^{(q)}=
\frac{\exp(e_{ij}^{(q)})}
{\sum_{u\in\mathcal N(i)}\exp(e_{iu}^{(q)})}.
$$

Ở đây các phép chiếu được hiểu theo head q; `lin_edge` ánh xạ edge features thành một giá trị cho mỗi head. Khi dùng edge features, vector cạnh có thêm cờ hướng +1/−1; các self-loop mặc định có edge vector bằng 0. Với static GAT, bỏ hạng edge. Các cặp node ngoài mask bị loại khỏi softmax. Biểu diễn nhận được là tổng có trọng số của các node gửi, ghép các head; code còn áp dụng attention dropout, ELU, dropout và skip connection.

Attention weights được học gián tiếp qua loss dự báo. Việc có attention khác nhau chưa tự chứng minh quan hệ nhân quả hoặc mức quan trọng đáng tin cậy của từng cạnh; phải đối chiếu với các thí nghiệm can thiệp graph/edge ở các mục sau.

### 4.2. Validation score để chọn feature và cấu hình

Ký hiệu A(r,k) là scaled MAE của target k trong run r, trung bình trên cửa sổ, horizon và service:

$$
A(r,k)=\frac{1}{S_rHN}\sum_{s=1}^{S_r}\sum_{h=1}^{H}\sum_{n=1}^{N}
|\hat z_{s,h,n,k}-z_{s,h,n,k}|.
$$

Với bốn kịch bản C, tập run R(c) của mỗi kịch bản và trọng số target w:

$$
S(w)=\frac{1}{4}\sum_{c\in C}\frac{1}{|R(c)|}
\sum_{r\in R(c)}\left[w_{RPS}A(r,RPS)+w_{CPU}A(r,CPU)\right].
$$

Score càng thấp càng tốt. Cách tổng hợp này cho mỗi kịch bản trọng số 1/4, thay vì để kịch bản có nhiều cửa sổ hơn chi phối kết quả.

| Mục đích trong code hiện tại | Trọng số RPS / CPU | Tiêu chí |
|---|---|---|
| Chọn feature, graph/edge ablation, matched-window validation | 0,50 / 0,50 | `primary_score`, ký hiệu S50 |
| Chẩn đoán ưu tiên CPU trong các ablation | 0,35 / 0,65 | `cpu_weighted_diagnostic`, ký hiệu SCPU |
| Optuna cuối của GAT-GRU và LSTM | 0,35 / 0,65 | Objective macro validation SCPU |
| Chọn checkpoint trong từng lần train | Bình quân đều mọi phần tử | Pooled scaled validation MAE |

Hai target phải ở thang chuẩn hóa khi cộng thành score; không cộng trực tiếp MAE requests/s với MAE CPU cores. Các số `primary_score` trong ablation không cùng trọng số với `best_value` của Optuna cuối, nên không dùng chênh lệch giữa chúng làm phần trăm cải thiện do tuning.

Nguồn: `_metric_tables`, `BASELINE_SCORE_WEIGHTS`, `CPU_DIAGNOSTIC_WEIGHTS` và `run_optuna_fixed_architecture` trong [forecast_ablation.py](modeling-common/forecast_ablation.py).

### 4.3. Loss function để học trọng số

Cả hai mô hình dùng `nn.HuberLoss(delta=1.0)` trên target đã chuẩn hóa. Với sai số e = dự báo chuẩn hóa − mục tiêu chuẩn hóa:

$$
\ell(e)=\begin{cases}
\frac{1}{2}e^2,& |e|\leq 1,\\
|e|-\frac{1}{2},& |e|>1.
\end{cases}
$$

$$
\mathcal L(\theta)=\frac{1}{BHNK}
\sum_{b,h,n,k}\ell\big(f_\theta(X,E)_{b,h,n,k}-z(Y)_{b,h,n,k}\big).
$$

Huber có dạng bình phương ở lỗi nhỏ và tuyến tính ở lỗi lớn, giúp giảm mức chi phối của những sai số cực lớn so với squared loss. Loss hiện tại lấy trung bình đều các phần tử, **không dùng trọng số CPU 0,65**, không phạt bất đối xứng under-prediction và không cân bằng riêng từng kịch bản trong minibatch. Trọng số 0,35/0,65 thuộc bước chọn cấu hình, không phải loss backpropagation.

### 4.4. Gradient descent và optimizer đang sử dụng

Gradient descent là cách cập nhật tham số theo hướng ngược gradient của loss. Dạng cơ bản là:

$$
\theta_{t+1}=\theta_t-\eta\nabla_\theta\mathcal L_t.
$$

Trong dự án, thuật toán thực tế là **AdamW trên minibatch**, không phải vanilla SGD. Mỗi batch thực hiện `zero_grad` → forward → Huber loss → `backward` → clip gradient → `step`. Gradient lan từ head qua GRU, GAT và các phép chiếu/embedding để cập nhật chung các tham số học được.

Gọi g là gradient sau clip chuẩn L2 tối đa 1, AdamW duy trì trung bình trượt bậc một và bậc hai:

$$
m_t=\beta_1m_{t-1}+(1-\beta_1)g_t,\qquad
v_t=\beta_2v_{t-1}+(1-\beta_2)g_t^2,
$$

$$
\hat m_t=\frac{m_t}{1-\beta_1^t},\qquad
\hat v_t=\frac{v_t}{1-\beta_2^t},\qquad
\theta_{t+1}=(1-\eta\lambda)\theta_t-
\eta\frac{\hat m_t}{\sqrt{\hat v_t}+\epsilon}.
$$

η là learning rate; λ là weight decay tách khỏi gradient loss. Code chỉ truyền learning rate và weight decay vào AdamW, giữ các tùy chọn khác theo mặc định thư viện. Không thấy scheduler thay đổi learning rate trong `train_model`.

Ngân sách mặc định là tối đa 60 epoch, early-stopping patience 8 epoch và ngưỡng cải thiện checkpoint 10⁻⁵. Sau khi dừng, khôi phục checkpoint có pooled scaled validation MAE tốt nhất theo điều kiện này. Do đó epoch cuối trong history chưa chắc là checkpoint được dùng để test. Loss train, checkpoint metric và Optuna objective là ba đại lượng khác nhau.

## 5. Lựa chọn node features

Audit thống kê trước hết kiểm tra phân phối và tương quan trên train. Giai đoạn v01 dùng group ablation và leave-one-out để sàng lọc, sau đó xác nhận các ứng viên bằng 5 seed trên validation. Ranking một seed ban đầu không được dùng như bằng chứng cuối cùng.

| Nhóm | Node features được giữ |
|---|---|
| Tải yêu cầu | `rps_in`, `rps_in_delta`, `rps_per_replica` |
| Độ trễ | `latency_p50_ms`, `latency_p95_ms` |
| Tài nguyên | `cpu_cores`, `mem_mib` |
| Lưu lượng mạng | `net_rx_kBps`, `net_tx_kBps` |
| Năng lực phục vụ | `replicas` |

Ba feature bị bỏ ở v01 là `cpu_util_request`, `cpu_throttle_ratio`, `cpu_sidecar_cores`. `error_rate` và `restarts_delta` đã bị loại ở giai đoạn trước vì gần như hằng số; chúng không thuộc bộ 13 đầu vào của phép so sánh finalist dưới đây.

| Bộ node features | S50 validation, mean ± SD theo seed | MAE RPS macro | MAE CPU macro |
|---|---:|---:|---:|
| Đầy đủ 13 | 0,115510 ± 0,001158 | 1,021248 | 0,013750 |
| Giữ 10, bỏ nhóm ba feature CPU phụ | 0,113994 ± 0,001107 | 1,008243 | 0,013603 |

Loại ba feature giúp score trung bình giảm khoảng 1,31%, đồng thời giảm MAE cả hai target trên validation tổng hợp. Kết quả hỗ trợ lựa chọn bộ 10 cho các bước sau và dùng lại nguyên bộ này cho LSTM. Nó không chứng minh từng feature bị bỏ đều vô ích khi đứng riêng, hoặc mọi feature còn lại đều không thể thay thế; tương quan và tương tác giữa feature có thể ảnh hưởng kết quả.

Nguồn: [v01 finalists](artifacts/gatgru/v01_node_ablation/finalists/variant_summary.csv) và [feature contract](configs/gatgru/feature_contract_v01.json).

## 6. Đưa GAT vào có tốt hơn GRU đơn lẻ không?

Đối chứng `gru_node_only` xử lý từng service riêng bằng MLP, skip connection, GRU và linear head; có service embedding 4 chiều nhưng không nhìn node khác hoặc edge metrics. MLP có độ sâu/rộng tương ứng GAT để hạn chế chênh lệch năng lực mô hình. Có 121.016 tham số, gần với static GAT-GRU 121.528 và dynamic GAT-GRU 121.560.

Các biến thể dưới đây dùng cùng 10 node features, window 18, cấu hình train cố định của ablation và 5 seed. Chúng được đánh giá trên validation, macro đều cả bốn kịch bản.

| Biến thể | S50 mean ± SD | MAE RPS macro | MAE CPU macro |
|---|---:|---:|---:|
| GRU node-only | 0,118488 ± 0,000528 | 1,035398 | 0,014178 |
| GAT-GRU chỉ self-loop | 0,118082 ± 0,000484 | 1,032978 | 0,014146 |
| Static GAT-GRU, topology thật, không edge metrics | 0,114503 ± 0,000825 | 1,006580 | 0,013711 |
| Dynamic GAT-GRU, topology thật, ba edge metrics | 0,113994 ± 0,001107 | 1,008243 | 0,013603 |

So với node-only, static GAT giảm S50 khoảng 3,36%, dynamic GAT giảm khoảng 3,79%. Self-loop gần node-only hơn hai cấu hình có trao đổi giữa các service. Vì vậy, **có bằng chứng thực nghiệm rằng thêm cơ chế tổng hợp giữa node bằng GAT giúp dự báo tốt hơn đối chứng GRU node-only trong thiết kế validation này**.

Cần giới hạn phạm vi: hyperparameters ablation được kế thừa từ cấu hình GAT baseline, không phải tuning riêng tối ưu cho GRU; so sánh này cũng chưa có đánh giá test độc lập của GRU node-only. Không được viết rằng “GAT đã được chứng minh vượt GRU tối ưu trên test” hoặc quy toàn bộ mức giảm lỗi cho riêng attention thay vì quyền truy cập thông tin giữa các node.

### 6.1. Topology thật có tạo lợi thế riêng không?

| Static graph control | S50 validation |
|---|---:|
| Topology thật | 0,114503 |
| Hoán vị node seed 42 | 0,114340 |
| Hoán vị node seed 101 | 0,114035 |
| Hoán vị node seed 202 | 0,114830 |
| Hoán vị node seed 303 | 0,114091 |
| Đồ thị đầy đủ | 0,114959 |
| Đồ thị ngẫu nhiên với cùng số cạnh đầu vào | 0,115609 |

Topology thật tốt hơn full graph và random control này theo score trung bình, nhưng 3 trong 4 topology hoán vị có score thấp hơn topology thật. Vì vậy, chưa có bằng chứng nhất quán cho lợi ích riêng của đúng sơ đồ gọi service. Các hoán vị giữ dạng graph nhưng đổi gán node, với node features vẫn gắn với service ban đầu.

Tên `random_same_density` cần đọc theo triển khai: code giữ số cạnh đầu vào, sau đó GAT thêm self-loop và áp dụng hướng `both`. Mật độ mask truyền thông tin cuối cùng không được bảo đảm giống tuyệt đối graph thật. Đó là một giới hạn của đối chứng này.

Nguồn: [v02 graph controls](artifacts/gatgru/v02_graph_controls/variant_summary.csv). Các control mở rộng là bằng chứng khám phá trên validation, không phải một bộ test xác nhận mới.

## 7. Chọn edge features và mức độ quan trọng

Ba edge features đang được dùng là `call_rate`, `call_ratio`, `edge_latency_p95_ms`. v03 khảo sát các tập con của ba feature bằng 3 seed, rồi xác nhận các finalist bằng 5 seed. Bảng dưới dùng kết quả finalist, tránh trộn số liệu của hai ngân sách seed.

| Edge set | S50 validation, 5 seed |
|---|---:|
| Cả ba feature | 0,113994 |
| Không edge feature, static GAT | 0,114503 |
| Chỉ `call_rate` | 0,114605 |
| Chỉ `call_ratio` | 0,114714 |

Cả ba feature cùng nhau giảm S50 khoảng 0,445% so với static GAT. Tuy nhiên raw RPS MAE tăng nhẹ từ 1,006580 lên 1,008243, trong khi raw CPU MAE giảm từ 0,013711 xuống 0,013603. Do đó không nói thêm edge metrics cải thiện đồng thời mọi metric.

Phân tích main effect từ vòng factorial 3 seed cho mức cải thiện score trung bình: `call_rate` +0,000192; `call_ratio` +0,000228; `edge_latency_p95_ms` −0,000224. Dấu dương nghĩa là thêm feature làm score giảm trong trung bình các tổ hợp. Latency có main effect âm dù bộ cả ba tốt nhất trong finalist; điều này phù hợp với khả năng tương tác và biến động, không đủ để kết luận latency độc lập là feature có ích nhất.

v04 kiểm tra khi giữ kiến trúc nhưng thay đổi dữ liệu edge rồi huấn luyện lại:

| Control | S50 validation, 5 seed |
|---|---:|
| Edge trễ 6 bước, tương đương 60 giây | 0,113869 |
| Edge thật | 0,113994 |
| Hoán vị danh tính cạnh | 0,114169 |
| Gán edge metrics bằng 0 | 0,114279 |
| Dùng trung bình edge lấy từ train | 0,114425 |

Edge thật tốt hơn zero và train-mean một lượng nhỏ, nhưng không tốt hơn edge trễ 60 giây. Các control này thay đổi dữ liệu trước bước fit scaler; không phải permutation importance chỉ tại inference trên một checkpoint cố định. “Không có edge feature” ở v03 cũng khác “edge feature bằng 0” ở v04 về kiến trúc và cờ hướng.

Kết luận chung cho bốn kịch bản: bộ ba edge features là lựa chọn hiện được sử dụng với lợi ích validation nhỏ; chưa chứng minh thông tin edge đồng thời và gán đúng cạnh là yếu tố quyết định chất lượng dự báo. Không có bằng chứng rằng edge metrics giúp GAT-GRU vượt LSTM trên test hiện tại.

Nguồn: [v03 finalists](artifacts/gatgru/v03_edge_ablation/finalists/variant_summary.csv), [edge main effects](artifacts/comparison/v01_selected_features/edge_main_effects.csv), [v04 controls](artifacts/gatgru/v04_edge_controls/variant_summary.csv). Ghi chú trong feature contract vẫn gọi edge set là provisional chờ v04; báo cáo mô tả các kết quả v04 đã có, không tự sửa trạng thái contract.

## 8. Cấu hình cuối và chi phí huấn luyện

Cả hai bên tuning 24 trial bằng TPE, search seed 42, giữ kiến trúc cố định và tìm window, dropout, learning rate, weight decay, batch size trong cùng không gian tìm kiếm. Hàm tuning nhánh selected-features dùng `NopPruner`; mỗi trial vẫn có early stopping. Sau đó đánh giá 5 training seed 0–4. Cùng số trial là cùng ngân sách theo lượt thử, không phải cùng thời gian tính toán.

| Thuộc tính | GAT-GRU cuối | LSTM cuối |
|---|---|---|
| Node features | Bộ 10 đã chọn | Cùng bộ 10 |
| Service embedding | 4 chiều | 4 chiều |
| Window | 18 bước, 180 giây | 24 bước, 240 giây |
| Kiến trúc | GAT 2 tầng, 4 heads × 32; GRU 128 | LSTM joint, hidden 128, 2 tầng |
| Số tham số | 121.560 | 294.576 |
| Dropout | 0,008967 | 0,103387 |
| Learning rate | 0,000535236 | 0,001140611 |
| Weight decay | 0,000001885 | 0,000006487 |
| Batch size | 32 | 32 |
| Best Optuna SCPU validation | 0,104364 | 0,101015 |
| Epoch đã chạy theo 5 seed | 32 / 26 / 25 / 33 / 37 | 23 / 22 / 23 / 25 / 24 |
| Tổng thời gian train cuối, chưa tính tuning | 3.206,61 giây ≈ 53,44 phút | 477,56 giây ≈ 7,96 phút |
| Inference CPU được lưu | 8,070 ms/window | 1,177 ms/window |

Latency là median của 20 lần forward cho batch một cửa sổ, đo trên model seed đầu theo helper hiện tại; đây không phải phép đo end-to-end của hệ thống autoscaling. GAT-GRU có ít tham số hơn khoảng 58,73% nhưng thời gian train cuối ghi nhận dài hơn khoảng 6,71 lần, inference dài hơn khoảng 6,86 lần. Thời gian có thể chịu ảnh hưởng của môi trường/tải máy, không phải cam kết hiệu năng triển khai.

LSTM cuối là **joint LSTM**: tại mỗi thời điểm, nối dữ liệu của cả 11 node thành vector 11 × (10 + 4) = 154 chiều. Nó nhìn được thông tin giữa các service, dù không có graph hoặc edge features. Vì vậy, so sánh GAT-GRU–LSTM cuối còn khác recurrent cell, window, số tham số và kiến trúc tổng hợp; không cô lập duy nhất tác dụng của graph.

Nguồn: [GAT training metadata](artifacts/gatgru/v05_selected_features_final/gatgru_selected_training_meta.json), [LSTM training metadata](artifacts/lstm/v01_shared_selected_features/lstm_shared_training_meta.json), và `tuning/optuna_summary.json` trong hai thư mục tương ứng.

## 9. Chỉ số test của GAT-GRU và so sánh với LSTM

### 9.1. Quy tắc tổng hợp chung bốn kịch bản

Metric chính để kết luận là macro-average theo kịch bản. Với mỗi seed, tính MAE/RMSE trong từng run, trung bình các run của từng kịch bản, rồi lấy trung bình đều bốn kịch bản. Cuối cùng báo mean ± sample SD qua 5 seed. Test hiện có một run mỗi kịch bản.

$$
M_{macro}^{(s)}=\frac{1}{4}\sum_{c\in C}
\frac{1}{|R(c)|}\sum_{r\in R(c)}M_r^{(s)}.
$$

Macro-RMSE ở đây là trung bình các RMSE của run/kịch bản, không phải căn bậc hai của MSE gộp. Pooled metric tính trên tất cả cửa sổ và có trọng số theo số cửa sổ. Hai cách đều được ghi rõ, không dùng thay thế lẫn nhau.

MAE = mean(|ŷ − y|); RMSE = sqrt(mean((ŷ − y)²)); under-MAE = mean(max(y − ŷ, 0)); over-MAE = mean(max(ŷ − y, 0)). Under/over đều lấy trung bình trên toàn bộ phần tử, không chỉ các điểm dự báo thiếu/thừa; tổng hai giá trị bằng MAE, sai khác nhỏ có thể do làm tròn.

### 9.2. GAT-GRU trên toàn bộ test khả dụng của chính nó

Bảng sau chép từ artifact test riêng của GAT-GRU, pooled các cửa sổ của cả bốn kịch bản, mean ± SD theo seed:

| Output | MAE | RMSE | Under-MAE | Over-MAE |
|---|---:|---:|---:|---:|
| RPS | 0,987760 ± 0,012423 | 2,974199 ± 0,029794 | 0,599854 | 0,387906 |
| CPU cores | 0,012718 ± 0,000153 | 0,036282 ± 0,000621 | 0,006781 | 0,005937 |

Nguồn: [GAT test summary](artifacts/gatgru/v05_selected_features_final/gatgru_selected_test_metrics_summary.csv). Không so trực tiếp bảng này với metric riêng của LSTM vì hai window khác nhau tạo ra tập mẫu test khả dụng khác nhau.

### 9.3. So sánh chính: cùng mẫu test, macro đều bốn kịch bản

Hai artifact được căn chỉnh theo run và thời điểm target đầu tiên, còn **587 cửa sổ chung**. Những cửa sổ chỉ có ở một mô hình bị loại khỏi phép so sánh. Bảng sau tổng hợp lại từ `metrics_by_run.csv` theo công thức mục 9.1:

| Output | Mô hình | MAE macro, mean ± SD | RMSE macro, mean ± SD |
|---|---|---:|---:|
| RPS | GAT-GRU | 0,999493 ± 0,012287 | 2,472591 ± 0,020259 |
| RPS | LSTM | 0,962377 ± 0,021229 | 2,394604 ± 0,025745 |
| CPU cores | GAT-GRU | 0,012839 ± 0,000151 | 0,030616 ± 0,000428 |
| CPU cores | LSTM | 0,012618 ± 0,000237 | 0,030509 ± 0,000403 |

Trên tổng hợp đều cả bốn kịch bản, LSTM giảm MAE RPS 0,037117 requests/s, tương đương khoảng 3,71%; giảm MAE CPU 0,000221 cores, tương đương khoảng 1,72%, lấy GAT-GRU làm mẫu số. Đây là kết luận chung của phép so sánh, không phải kết luận chọn mô hình theo từng kịch bản.

| Output | GAT-GRU under-MAE macro | LSTM under-MAE macro | GAT-GRU over-MAE macro | LSTM over-MAE macro |
|---|---:|---:|---:|---:|
| RPS | 0,611881 | 0,556634 | 0,387612 | 0,405743 |
| CPU cores | 0,006858 | 0,007059 | 0,005981 | 0,005558 |

GAT-GRU có under-MAE CPU thấp hơn dù tổng MAE CPU cao hơn. Đây là một khác biệt có thể liên quan đến autoscaling, nhưng không đủ để suy ra giảm vi phạm SLA; còn cần thời gian khởi động pod, capacity và cơ chế điều khiển thực tế.

Nguồn: [metrics theo run](artifacts/comparison/v01_selected_features/metrics_by_run.csv), [comparison contract](artifacts/comparison/v01_selected_features/comparison_contract.json).

### 9.4. Số liệu pooled để đối chiếu với notebook

`summary.csv` trong notebook đang ghi pooled metric, không phải bảng macro ở trên:

| Output | GAT-GRU MAE ± SD | LSTM MAE ± SD | GAT-GRU RMSE | LSTM RMSE |
|---|---:|---:|---:|---:|
| RPS | 1,000115 ± 0,012294 | 0,962985 ± 0,021266 | 3,019753 | 2,952503 |
| CPU cores | 0,012846 ± 0,000151 | 0,012625 ± 0,000238 | 0,036806 | 0,036759 |

Kết luận về hướng chênh lệch MAE không đổi khi chuyển từ pooled sang macro. RMSE khác đáng kể giữa hai cách do phép căn bậc hai và sự khác nhau về mức lỗi giữa run; không phải sai lệch chép số.

Nguồn: [aligned pooled summary](artifacts/comparison/v01_selected_features/summary.csv).

### 9.5. Output theo horizon trên tập bốn kịch bản

Bảng này lấy mean qua 5 seed từ `metrics_by_horizon.csv`; mỗi horizon pooled toàn bộ cửa sổ chung của bốn kịch bản. Đây là phân tích bổ sung, không thay thế macro kết luận chính.

| Horizon | GAT-GRU MAE RPS | LSTM MAE RPS | GAT-GRU MAE CPU | LSTM MAE CPU |
|---|---:|---:|---:|---:|
| 10 giây | 0,724862 | 0,694365 | 0,008193 | 0,008928 |
| 20 giây | 0,890126 | 0,833354 | 0,008496 | 0,008781 |
| 30 giây | 1,001492 | 0,963021 | 0,010466 | 0,010429 |
| 40 giây | 1,079200 | 1,042117 | 0,013904 | 0,013395 |
| 50 giây | 1,125742 | 1,101069 | 0,016854 | 0,016031 |
| 60 giây | 1,179269 | 1,143985 | 0,019163 | 0,018187 |

Sai số tăng khi dự báo xa hơn. LSTM có RPS MAE thấp hơn ở cả sáu horizon; GAT-GRU có CPU MAE thấp hơn tại 10–20 giây, còn tổng hợp cả sáu horizon nghiêng về LSTM. Chưa có thời gian khởi động pod cố định trong bằng chứng này để chọn một horizon làm thước đo vận hành chính.

Nguồn: [metrics theo horizon](artifacts/comparison/v01_selected_features/metrics_by_horizon.csv).

## 10. Kiểm soát window và số tham số trên validation

Thí nghiệm bổ sung giữ cùng window cho từng cặp, thu nhỏ joint LSTM về hidden 73 với 119.896 tham số, gần GAT-GRU 121.560. Cả hai dùng embedding 4 chiều, dropout 0,1, learning rate 0,001, batch 128, weight decay 0 và 5 seed; không tuning lại, không sử dụng test.

| Window | Mô hình | S50 mean ± SD | SCPU | MAE RPS macro | MAE CPU macro |
|---|---|---:|---:|---:|---:|
| 18 | GAT-GRU | 0,113944 ± 0,000423 | 0,106566 | 1,009318 | 0,013671 |
| 18 | LSTM joint h73 | 0,113563 ± 0,001627 | 0,106720 | 0,996110 | 0,013917 |
| 24 | GAT-GRU | 0,113720 ± 0,000605 | 0,106391 | 1,004130 | 0,013667 |
| 24 | LSTM joint h73 | 0,112232 ± 0,002638 | 0,105607 | 0,978674 | 0,013872 |

Sau khi kiểm soát window và số tham số, LSTM vẫn có RPS MAE và S50 trung bình thấp hơn ở cả hai window; GAT-GRU có CPU MAE thấp hơn. Với score ưu tiên CPU, GAT-GRU nhỉnh hơn tại window 18, LSTM nhỉnh hơn tại window 24. Vì vậy không thể dùng bảng này để khẳng định một mô hình tốt hơn trên mọi target hoặc mọi trọng số lựa chọn.

Đây là bằng chứng bổ sung trên validation với cấu hình train cố định, không thay thế test của hai mô hình cuối. So sánh vẫn khác GRU/LSTM và cơ chế biểu diễn đầu vào.

Nguồn: [matched-window validation](artifacts/comparison/v02_matched_windows_validation/variant_summary.csv).

## 11. Baseline đơn giản và các chẩn đoán bổ sung

### 11.1. Naive baseline và MASE

Các baseline đã lưu gồm persistence, seasonal naive chu kỳ 6 bước và moving average 6 bước. Chúng dùng lịch sử có sẵn tại thời điểm dự báo. Chu kỳ/độ dài 6 là lựa chọn thiết kế phải giữ cố định, không nên gọi là hoàn toàn không có lựa chọn cấu hình; ở đây chỉ dùng làm tham chiếu hậu nghiệm, không chọn lại trên test.

| Mô hình/baseline | MAE RPS macro | MAE CPU macro | MASE RPS macro | MASE CPU macro |
|---|---:|---:|---:|---:|
| GAT-GRU | 0,999493 | 0,012839 | 1,048614 | 1,513844 |
| LSTM | 0,962377 | 0,012618 | 1,012146 | 1,283901 |
| Persistence | 1,200784 | 0,020253 | 1,279824 | 1,695329 |
| Seasonal naive 6 | 1,519952 | 0,027527 | 1,503864 | 2,141401 |
| Moving average 6 | 1,314146 | 0,024127 | 1,279033 | 1,846867 |

Các cột trên đều macro bằng nhau qua bốn kịch bản; metric mô hình là trung bình metric của từng seed, không phải metric của ensemble dự báo. Cả hai mô hình có MAE thấp hơn các naive baseline này trên tổng hợp test.

MASE chia lỗi từng service/target cho trung bình sai khác tuyệt đối giữa các timestep liền nhau trong train của chính service/target đó; không nối qua ranh giới run. Denominator không hợp lệ hoặc bằng 0 bị loại: RPS còn 10 service hợp lệ, CPU 11. Do khác mẫu số và tập service, MASE không phải MAE toàn hệ thống chia cho một hằng số chung. MASE lớn hơn 1 cũng không có nghĩa mô hình kém persistence trên test đa horizon; bảng trên cho thấy điều đó rõ ràng.

Nguồn: [naive và MASE](artifacts/comparison/v01_selected_features/scenario_mae_and_mase_posthoc.csv).

### 11.2. High-load và transition

Các slice đã lưu dùng ngưỡng tải lấy từ train. Trung bình qua seed, pooled các điểm của cả bốn kịch bản:

| Slice | Output | GAT-GRU MAE | LSTM MAE | GAT-GRU under-MAE | LSTM under-MAE |
|---|---|---:|---:|---:|---:|
| High-load | RPS | 1,648234 | 1,480759 | 1,582933 | 1,376981 |
| High-load | CPU | 0,029528 | 0,029802 | 0,027020 | 0,026920 |
| Transition onset | RPS | 2,194250 | 2,062906 | 2,021230 | 1,852735 |
| Transition onset | CPU | 0,027457 | 0,026527 | 0,022709 | 0,021255 |

CPU high-load MAE của GAT-GRU thấp hơn một chút nhưng under-MAE không thấp hơn. Không có ưu thế nhất quán của GAT-GRU trên các slice này; không chuyển một lợi thế cục bộ thành kết luận chung về SLA hoặc RL.

Nguồn: [high-load/transition diagnostics](artifacts/comparison/v01_selected_features/diagnostic_high_load_transition.csv).

## 12. Độ chắc chắn và giới hạn kết luận

**Năm seed và bốn test run là hai trục khác nhau.** Seed thay đổi khởi tạo và quá trình tối ưu, còn test run là dữ liệu kiểm thử. Không có chuyện từ 5 seed bị loại một để còn 4 run: sign test trung bình qua seed trước, rồi so sánh theo bốn cặp test run. Tên cột đúng là `test_run_pairs`.

| Output | Số cặp test run | Số cặp GAT-GRU có MAE thấp hơn | Sign test hai phía |
|---|---:|---:|---:|
| RPS | 4 | 0 | 0,125 |
| CPU | 4 | 0 | 0,125 |

Với bốn cặp không hòa và cùng chiều, p nhỏ nhất của sign test hai phía là 2 × 0,5⁴ = 0,125. Kết quả không đạt ngưỡng 0,05. Nếu giả định có năm cặp độc lập thì p nhỏ nhất vẫn là 0,0625; nhưng năm seed hiện tại không được đổi thành năm cặp test run độc lập. Các run thuộc những kịch bản khác nhau cũng đòi hỏi thận trọng khi diễn giải giả định của phép kiểm định dấu.

Trên macro MAE chung bốn kịch bản, chênh lệch GAT-GRU − LSTM là 0,037117 RPS, khoảng 1,75 lần SD seed lớn hơn của hai mô hình; với CPU là 0,000221, khoảng 0,93 lần SD seed lớn hơn. Đây chỉ là đối chiếu độ lớn hiệu ứng với dao động do training seed, không phải kiểm định hoặc confidence interval.

Artifact block-bootstrap đã lưu là khoảng tin cậy theo từng kịch bản, block length 6 cửa sổ và 2.000 lần lấy mẫu, phản ánh bất định theo thời gian trong run hiện có. Không lấy trung bình các đầu mút CI để tạo CI chung; không có CI bootstrap macro bốn kịch bản được xác nhận trong báo cáo này. Chỉ một test run mỗi kịch bản cũng không đủ để ước lượng tốt biến thiên giữa các run cùng kịch bản. 587 cửa sổ chồng lấn không phải 587 quan sát độc lập.

Lặp lại nhiều ablation trên cùng validation có thể dẫn đến thích nghi với validation. Graph controls mở rộng và matched-window study sau khi đã xem kết quả cũ nên được coi là khám phá. Các CSV và manifest cho phép kiểm tra split dùng để chọn cấu hình, nhưng không tự chứng minh rằng mọi quyết định nghiên cứu qua toàn bộ lịch sử chưa từng chịu ảnh hưởng của thông tin test đã xem.

Nguồn: [sign test](artifacts/comparison/v01_selected_features/paired_run_sign_test.csv), [bootstrap chẩn đoán](artifacts/comparison/v01_selected_features/scenario_delta_block_bootstrap_posthoc.csv).

## 13. Kết luận cho toàn bộ bốn kịch bản

Chuỗi thí nghiệm đã tạo ra một cấu hình GAT-GRU gọn hơn về node features và có kiểm tra đối chứng: giữ 10 node features, dùng chung bộ này với LSTM, cùng ba edge features trong GAT-GRU cuối. Lựa chọn này cải thiện validation trong phạm vi các ứng viên đã thử; chưa chứng minh tối ưu toàn cục về feature, kiến trúc, loss hoặc ngân sách huấn luyện.

Đối với câu hỏi nghiên cứu về GAT, kết quả tổng hợp bốn kịch bản ủng hộ lợi ích của trao đổi thông tin giữa service so với GRU node-only ở cấu hình gần tương đương. Tuy nhiên, topology thật chưa tạo lợi thế nhất quán trước các topology hoán vị, còn edge metrics chỉ đóng góp mức cải thiện nhỏ. Vì vậy, phát biểu phù hợp là **GAT có ích so với đối chứng node-only trong thí nghiệm validation này**, không phải đã chứng minh học đúng cấu trúc phụ thuộc hoặc luôn tốt hơn mọi mô hình không graph.

Đối với chất lượng test, GAT-GRU dự báo được hai output RPS và CPU tốt hơn các naive baseline đã xét. Khi so với joint LSTM trên cùng mẫu test và tổng hợp đều bốn kịch bản, LSTM có MAE trung bình thấp hơn cho cả hai output, đồng thời chi phí tính toán được ghi nhận thấp hơn. GAT-GRU có một số ưu điểm ở CPU horizon ngắn và under-MAE CPU tổng thể, nhưng chưa đủ để đảo ngược kết luận về MAE chung hoặc chứng minh lợi ích autoscaling. Khác biệt test giữa hai mô hình hiện vẫn chưa đạt ý nghĩa thống kê ở mức 0,05 theo sign test bốn cặp run.

## 14. Danh mục nguồn và khả năng truy vết

| Nội dung | Notebook hoặc code | Artifact chính |
|---|---|---|
| Audit feature trên train | [analyze_feature_contract_v01.ipynb](notebooks/gatgru/analyze_feature_contract_v01.ipynb) | [v00 audit summary](artifacts/gatgru/v00_feature_audit/audit_summary.json) |
| Chọn node features | [train_gatgru_v01_node_ablation.ipynb](notebooks/gatgru/train_gatgru_v01_node_ablation.ipynb) | `v01_node_ablation/finalists/variant_summary.csv` |
| GRU/GAT và topology controls | [train_gatgru_v02_graph_ablation.ipynb](notebooks/gatgru/train_gatgru_v02_graph_ablation.ipynb) | `v02_graph_controls/variant_summary.csv` |
| Chọn edge set | [train_gatgru_v03_edge_ablation.ipynb](notebooks/gatgru/train_gatgru_v03_edge_ablation.ipynb) | `v03_edge_ablation/finalists/variant_summary.csv` |
| Kiểm chứng tín hiệu edge | [train_gatgru_v04_edge_controls.ipynb](notebooks/gatgru/train_gatgru_v04_edge_controls.ipynb) | `v04_edge_controls/variant_summary.csv` |
| GAT-GRU cuối | [train_gatgru_v05_selected_features_final.ipynb](notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb) | `v05_selected_features_final/` |
| LSTM cùng node features | [train_lstm_v01_shared_selected_features.ipynb](notebooks/lstm/train_lstm_v01_shared_selected_features.ipynb) | `artifacts/lstm/v01_shared_selected_features/` |
| Cùng window, gần cùng tham số | [validate_lstm_gatgru_v02_matched_windows.ipynb](notebooks/comparison/validate_lstm_gatgru_v02_matched_windows.ipynb) | `comparison/v02_matched_windows_validation/` |
| So sánh test và hậu nghiệm | [compare_lstm_gatgru_v01_selected_features.ipynb](notebooks/comparison/compare_lstm_gatgru_v01_selected_features.ipynb) | `comparison/v01_selected_features/` |
| Preprocessing/scaler | [forecast_data.py](modeling-common/forecast_data.py) | Split, contract và scaler trong metadata |
| Kiến trúc/loss/optimizer | [forecast_torch.py](modeling-common/forecast_torch.py) | Training metadata, history, checkpoint theo seed |
| Điều phối ablation/tuning/score | [forecast_ablation.py](modeling-common/forecast_ablation.py) | Manifest và variant summaries |
| Metrics/căn chỉnh dự báo | [forecast_metrics.py](modeling-common/forecast_metrics.py) | Predictions NPZ và metric CSV |

Dataset fingerprint: `b18509670bb1ca10b356da33c2c282b169fdea1103ac45fdca6e788a097977d2`.

Split fingerprint: `05c2ba9d78432b338f736c3c16b186b9d17c5d4614ce16607c4f9511b9851d76`.

Khi lập báo cáo, SHA-256 hiện tại của `forecast_torch.py`, `forecast_data.py`, `forecast_metrics.py` khớp các hash được ghi trong metadata huấn luyện cuối. Helper `forecast_ablation.py` đã có các bổ sung phân tích sau đó; cần dùng manifest riêng của từng thí nghiệm khi truy vết phiên bản.

Các tài liệu `GATGRU_TRAINING_CONTEXT.md` và `TRAINING_BASELINE_2026-10-14.md` mô tả baseline lịch sử, không được dùng thay cho số liệu selected-features hiện tại. Đặc biệt, mốc hiện tại dùng 10 node features, 24 tuning trial mỗi bên và 587 cửa sổ test chung; không trộn với số feature, tuning budget hoặc common windows của baseline cũ.
