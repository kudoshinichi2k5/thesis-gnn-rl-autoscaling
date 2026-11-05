# Thiết kế RL: GAT-GRU + PPO trong Digital Twin

Cập nhật 05/11/2026. Hướng chính thức là **Digital Twin bán cơ chế có hiệu chỉnh xác
suất**, không dùng lookup table và không dùng MLP dự đoán toàn bộ trạng thái kế tiếp.
Phạm vi giữ nguyên bốn workload NHPP/MMPP/ON-OFF/Pareto, Jaeger sampling 10%, không
curriculum và GAT-GRU v05 đóng băng.

## Mô hình chuyển trạng thái

Một tick của twin dài 10 giây; PPO quyết định mỗi ba tick.

1. Trace cung cấp workload ngoại sinh tiếp theo. Forecast GAT-GRU chỉ nằm trong
   observation, không được dùng làm ground truth dynamics.
2. Action cập nhật `desired`; pod scale-up đi vào hàng đợi `pending` với delay lấy mẫu
   từ phân phối đo theo service. Scale-down cập nhật capacity trực tiếp.
3. Topology được lưu bằng adjacency matrix tĩnh có thứ tự cạnh cố định. Online Boutique
   không tự thay đổi quan hệ gọi service trong một episode, nên adjacency không bị sửa theo
   tick; `call_rate`, `call_ratio` và edge latency mới là thuộc tính động. Fan-out học từ
   client spans được lan truyền tuần tự caller→callee, thay vì nhân mọi service trực tiếp
   với RPS frontend.
4. Với service `i`, capacity là `mu_i × ready_i`; backlog tuân theo bảo toàn công việc:

   `q[t+1] = max(0, q[t] + (lambda[t] - completed[t]) × 10)`.

5. CPU dùng mô hình không âm `idle_i × ready_i + cost_i × completed_rps_i`.
6. Phân phối latency của từng service là mixture LogNormal có điều kiện theo service,
   arrival rate, ready replica, utilization và queue pressure. Model không sinh
   workload hoặc replica.
7. Observation layer lấy mẫu 10% request, tính P50/P95 theo cửa sổ và tạo đúng node/
   edge feature contract cho frozen GAT-GRU.

Đây là simulator học tham số từ dữ liệu offline; PPO vẫn là on-policy learner tương tác
với simulator. Không gọi phương pháp này là MOPO hoặc thuật toán offline RL.

## MDP

- **State:** với năm service điều khiển: RPS, CPU, RAM, P95, ready, pending, cooldown,
  backlog; cộng forecast RPS/CPU tại 30 và 60 giây từ GAT-GRU và ba thuộc tính động của
  15 cạnh theo đúng thứ tự topology (`call_rate`, `call_ratio`, edge P95). Adjacency tĩnh
  được lưu trong twin, không lặp lại 121 hằng số vô ích trong vector PPO.
- **Action:** `MultiDiscrete([3] × 5)`, ánh xạ `0/1/2 → -1/0/+1`; replica bị giới hạn
  trong miền đã hiệu chỉnh 1–3.
- **Reward:** phạt bình phương phần latency vượt SLO 300 ms, error rate frontend, tổng
  replica, số service thay đổi, đảo chiều scale, action bị chặn bởi cooldown/biên và mức
  ngoại suy OOD ngoài support profiling.
- **Episode:** một workload trace độc lập; hết trace là `terminated`.

## Dữ liệu hiệu chỉnh

Entry point duy nhất là `load-testing/run-profiling.py` và cấu hình duy nhất nằm trong
`load-testing/profiling-plan.json`.

- Smoke: một run ON/OFF tải 0.5, `recommendationservice` 1→2→3→1.
- Main: 4 scenario × 3 load multiplier × 3 split = **36 run**, mỗi run 35 phút.
- Mỗi run quét 1→2→3→1 cho từng service và thêm một điểm joint. Pair joint được đổi
  có kiểm soát theo split/scenario/mức tải; latency model nhận tổng replica cluster để
  quan sát interaction pressure. Đây không phải quét Cartesian product.
- Train fit tham số/model; validation dùng gate; test niêm phong cho đánh giá cuối.
- Dataset 32 run HPA cũ vẫn dùng cho workload, call graph và GAT-GRU; không dùng làm
  bằng chứng nhân quả cho tác động replica.

## Gate trước PPO

Notebook mặc định `TWIN_APPROVED=False`. PPO chỉ được mở khi:

- train và validation đều có support replica 1–3 cho từng service điều khiển;
- có startup-delay sample cho từng service;
- validation NLL hữu hạn, interval coverage hợp lý và latency tốt hơn baseline median
  theo service;
- held-out rollout tái tạo mean/P95/P99, SLO violation, CPU và action direction;
- policy không đi ngoài miền workload/replica hiệu chỉnh.

## Ranh giới các mô-đun trong code

Notebook không dùng một lớp duy nhất để vừa giả lập Kubernetes vừa dự đoán telemetry:

1. `ReplicaLifecycle`: desired/ready/pending/cooldown và startup-delay;
2. `GraphLoadModel`: adjacency tĩnh, fan-out động và lan truyền tải;
3. `InfrastructureDynamics`: capacity, completed work, backlog, CPU/RAM/error và OOD;
4. `SurrogateObservationModel`: mixture-density latency và lớp quan sát Jaeger 10%;
5. `DigitalTwin`: chỉ điều phối bốn mô-đun trên; `DigitalTwinEnv` định nghĩa MDP.

Do topology triển khai không đổi trong campaign này, "cập nhật adjacency theo thời gian"
sẽ là sai mô hình. Nếu sau này service/call edge được deploy/remove động, phải version hóa
topology và thu một campaign riêng; không tự suy diễn edge mới từ một tick không có span.

Notebook ghi rõ rollout dùng replica quan sát từ validation là **conditional rollout với
oracle replica schedule**, chỉ để kiểm định dynamics. Action-sensitivity và autonomous
finite/OOD được kiểm tra riêng; replica tương lai từ trace không bao giờ được giả làm rollout
tự chủ của PPO. Không hạ gate để ép training chạy.

## Nguồn phương pháp

| Nguồn | Phần áp dụng |
|---|---|
| [Borsatti et al., KubeTwin, IEEE TNSM 2024](https://github.com/DSG-UniFE/KubeTwin) | Digital Twin mô phỏng ứng dụng Kubernetes, replica và request/service behavior thay vì lookup toàn cấu hình. |
| [Manca et al., CNSM 2023](https://dl.ifip.org/index.html/db/conf/cnsm/cnsm2023/1570931820.pdf) | Học phân phối processing time có điều kiện theo request rate và replica bằng mixture density, rồi dùng phân phối trong simulator. |
| [Yu et al., MOPO, NeurIPS 2020](https://proceedings.neurips.cc/paper_files/paper/2020/file/a322852ce0df73e204b7e67cbbef0d0a-Paper.pdf) | Cảnh báo policy khai thác sai số dynamics ngoài support; chỉ dùng làm cơ sở cho gate/OOD, không tuyên bố notebook đang triển khai MOPO. |

## Artifact chính

- Thu dữ liệu: `load-testing/run-profiling.py`
- Plan: `load-testing/profiling-plan.json`
- Train/evaluate: `notebooks/rl/train_ppo_kaggle.ipynb`
- Frozen forecast: `modeling-common/gatgru_inference.py`

Lịch sử surrogate v5–v16 chỉ được giữ trong `artifacts/rl/` để audit; không còn nằm
trên đường chạy hiện tại.
