# Thiết kế RL: GAT-GRU + PPO trong Digital Twin

> **Collection update 07/11/2026:** synthetic recovery v4 PASS với focus sweep, split
> 20/8/8, service-local concurrency và gated Binomial MLE. Main thật vẫn NO-GO do
> `actual_contract_pass=False`. Mọi con số 12/12/12, tải tối đa 1.5 và recovery v3 ở
> phần lịch sử bên dưới đã được thay thế bởi [campaign_readiness.md](campaign_readiness.md).

> **Audit collection v2, 06/11/2026:** quyết định GO profiling trong bản thiết kế bên
> dưới đã bị thay thế bởi NO-GO ở [campaign_readiness.md](campaign_readiness.md).
> Session arrivals không đồng nhất request arrivals; capacity không được nhận dạng
> chỉ từ latency cao. Notebook đã chặn fitting legacy. Hướng Twin vẫn giữ, nhưng adapter
> session-aware và bằng chứng own-time/capture còn phải hoàn thiện trước train.

Cập nhật 05/11/2026. Hướng chính thức là **Digital Twin bán cơ chế có hiệu chỉnh xác
suất**, không dùng lookup table và không dùng MLP dự đoán toàn bộ trạng thái kế tiếp.
Phạm vi giữ nguyên bốn workload NHPP/MMPP/ON-OFF/Pareto, Jaeger sampling 10%, không
curriculum và GAT-GRU v05 đóng băng.

## Mô hình chuyển trạng thái

Một tick của twin dài 10 giây; PPO quyết định mỗi ba tick.

1. Trace cung cấp workload ngoại sinh tiếp theo. Forecast GAT-GRU chỉ nằm trong
   observation, không được dùng làm ground truth dynamics.
2. Action cập nhật `desired`; pod scale-up đi vào hàng đợi `pending` với delay lấy mẫu
   từ phân phối đo theo service. Scale-down loại capacity phục vụ request **mới** trực tiếp;
   Twin v1 chưa tuyên bố mô phỏng process Terminating/in-flight drain.
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

- **State:** với năm service điều khiển: RPS, CPU, RAM, P95, số span quan sát, ready,
  pending, cooldown, backlog và OOD score; cộng forecast RPS/CPU tại 30 và 60 giây từ GAT-GRU và ba thuộc tính động của
  15 cạnh theo đúng thứ tự topology (`call_rate`, `call_ratio`, edge P95). Adjacency tĩnh
  được lưu trong twin, không lặp lại 121 hằng số vô ích trong vector PPO. PPO observation
  có thêm `reward_p95_ema/SLO` để reward filter không trở thành hidden state; scalar này
  không đi vào frozen GAT-GRU.
- **Action:** `MultiDiscrete([3] × 5)`, ánh xạ `0/1/2 → -1/0/+1`; replica bị giới hạn
  trong miền đã hiệu chỉnh 1–3.
- **Reward:** bốn nhóm có diễn giải riêng: SLA (P95 và error frontend), resource (tổng
  replica), flapping (số service thực sự đổi replica) và OOD. Reversal/action bị chặn chỉ
  là diagnostic, không bị phạt chồng. P95 reward dùng EWMA liên tục
  `alpha=min(1,K/K0)`, mặc định `K0=30`; P95 feature impute 0 và trace count vẫn nằm trong
  state, còn P95 raw nội bộ giữ NaN khi `K=0` và tuyệt đối không tham gia phép nhân EWMA.
- **Episode:** một workload trace độc lập; hết trace là `terminated`.

## Dữ liệu hiệu chỉnh

Entry point duy nhất là `load-testing/run-profiling.py` và cấu hình duy nhất nằm trong
`load-testing/profiling-plan.json`.

- Smoke: bốn run train NHPP/MMPP/ON-OFF/Pareto tải 0.5, mỗi run cho
  `recommendationservice` đi 1→2→3→1. Bộ smoke kiểm tra cả bốn đường sinh tải, session
  nhiều request, Pareto work đuôi nặng, ON/OFF request trực tiếp và drain.
- Main: 4 scenario × 3 load multiplier **0.5/1.0/1.5** × 3 split = **36 run**, mỗi run
  35 phút. Đây là ba mức tải, không phải multiplier 3×.
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
- autonomous full-episode rollout với hold/reactive/stress policy phải hữu hạn, giữ biên
  replica/budget và không vi phạm bảo toàn tổng work;
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

## Kiểm toán trước profiling: M1–M10

Phần này hợp nhất audit, yêu cầu hiệu chỉnh và readiness vào một tài liệu để tránh sinh thêm
ba file gần trùng nội dung. Trạng thái dưới đây là trạng thái **trước khi có campaign
calibration v1**; không được diễn giải `MỘT PHẦN` thành đã kiểm chứng.

| Mục | Trạng thái | Bằng chứng hiện tại | Quyết định |
|---|---|---|---|
| M1 — tải ngoại sinh và fan-out | **ĐÚNG về cấu trúc** | `designed_rate.csv` cung cấp tải mở; `GraphLoadModel` lan truyền caller→callee bằng fan-out fit trên train | Giữ. Validation phải kiểm tra normalized MAE của tải từng service; không dùng trực tiếp `frontend × traffic_ratio` cho mọi service. |
| M2 — vòng đời Pod | **MỘT PHẦN** | Có desired/pending/ready, startup-delay theo service, scale-down capacity cho request mới, cooldown và giới hạn replica/tổng Pod | `max_total_app_pods=16` là budget thí nghiệm, không phải capacity scheduler đã đo. Terminating/in-flight drain và Pod Pending vì thiếu CPU/RAM node chưa được mô hình hóa; không tự thêm drop penalty khi chưa đo. |
| M3 — hàng đợi service | **MỘT PHẦN, khác spec có chủ đích** | Có capacity `mu × ready`, completed work và backlog bảo toàn công việc | Không tự nhận là G/G/c đầy đủ. Concurrency và service-time chưa nhận dạng được; latency dùng phân phối MDN trên raw span thay vì hồi quy P95 hoặc công thức M/M/c giả định. |
| M4 — latency đầu-cuối | **KHÁC SPEC có chủ đích** | Reward dùng frontend server-span P95, vốn đã bao quanh request frontend; edge latency vẫn nằm trong graph observation | Không cộng critical path vào frontend P95 vì sẽ có nguy cơ double-count. Chỉ đổi nếu trace sau này cung cấp decomposition không chồng lặp và kiểm tra được. |
| M5 — CPU | **MỘT PHẦN** | CPU app = idle/Pod + completed RPS × cost/request; collector tách CPU sidecar | Giữ CPU app cùng contract GAT-GRU. Sidecar được lưu để audit cluster pressure, chưa nhân cứng hệ số chung vì hệ số phải fit theo service. |
| M6 — node contention | **THIẾU, ngoài scope v1** | Dữ liệu hiện gộp theo service; không có mapping pod→node theo tick trong contract calibration | Không thêm hệ số phồng latency tùy ý. Nếu validation cho thấy residual phụ thuộc node pressure, mở protocol v2 và thu node placement/utilization. |
| M7 — warm-up sau Ready | **THIẾU** | Chỉ có command→Available delay; Pod được cấp toàn capacity khi Available | Campaign v1 nhận dạng startup. Chỉ thêm post-Ready ramp nếu sweep cho thấy throughput/latency còn chuyển tiếp sau Available. |
| M8 — observation process | **MỘT PHẦN** | Root trace count lấy Binomial `p=0.10`; downstream count dùng Hypergeometric thinning có chung root decision. P50/P95 tính từ sample; count được đưa vào state; reward dùng EWMA liên tục | Frozen GAT-GRU vẫn nhận feature P95 impute 0 để giữ contract; P95 raw nội bộ là NaN khi không có trace. CPU scrape noise chưa mô hình hóa. Capture rate thực có thể lệch 10%, nên phải báo cáo coverage. |
| M9 — forecast modes | **CHỈ GIỮ MODE B** | Frozen GAT-GRU chạy trên lịch sử node/edge do twin sinh | “Tương lai thật + noise” là oracle leakage nếu dùng train PPO. Chỉ được phép làm upper-bound ablation có nhãn `oracle`, không phải chế độ huấn luyện/chính sách báo cáo. |
| M10 — domain randomization | **THIẾU có chủ đích** | Chưa có khoảng tin cậy hợp lệ trước profiling | Sau sweep, bootstrap theo run trên **train** để lấy khoảng tham số; validation chọn mức randomization. Không đặt khoảng tùy ý và không dùng test. |

### Quyết định đối với sáu rủi ro được review

1. **Không mở replica lên 5:** nhận định “tải 3×” đọc nhầm ba mức tải thành multiplier
   3×. Campaign chỉ tới 1.5 và phải đo T10 trước; mở 1–5 lúc này làm PPO đi ngoài support,
   tăng không gian profiling và có thể vượt budget cluster.
2. **Chưa thêm drain/drop giả:** cooldown đã hạn chế flapping; capacity cho request mới giảm
   khi endpoint bị loại. Termination grace chỉ được thêm nếu transition scale-down trong
   campaign chứng minh ảnh hưởng latency/error có thể nhận dạng.
3. **Không clip input GAT-GRU về Q99 HPA:** clipping che mất drift và biến mọi OOD thành
   giá trị biên giống nhau. Twin giữ input hữu hạn, đưa OOD score vào state/reward và chạy
   full-episode gate; deployment sau này phải fallback khi drift vượt ngưỡng validation.
4. **Chấp nhận reward filtering:** trace count là feature quan sát được. EWMA liên tục dùng
   `alpha=min(1,K/K0)` và chỉ tác động P95 tính reward. Nhánh `K=0` giữ EMA cũ mà không đọc
   NaN raw; feature P95 cấp cho GAT-GRU vẫn impute 0. `K0` phải ablate trên validation.
5. **Đơn giản reward:** bỏ penalty reversal/blocked bị chồng; giữ bốn nhóm SLA, resource,
   flapping và OOD. Cooldown/biên vẫn là transition constraint và được log để audit.
6. **Bổ sung T8b closed-loop:** hold/reactive/stress chạy hết episode và kiểm tra finite,
   replica budget cùng residual của invariant queue/work. T8b chỉ chứng minh ổn định số học; conditional
   rollout T8 vẫn cần để kiểm tra accuracy với validation telemetry.

Các số liệu lịch sử như core/request, fan-out hoặc startup median chỉ là prior để kiểm tra
sanity. Notebook phải fit lại từ campaign có provenance; không hard-code các con số được
chép từ một review bên ngoài.

## Yêu cầu nhận dạng và dữ liệu sweep

| Đại lượng | Có thể lấy từ 32 run HPA? | Campaign calibration v1 cần cung cấp | Rủi ro nhận dạng |
|---|---|---|---|
| Workload/fan-out | Có thể lấy prior | 4 scenario × 3 mức tải; edge call count theo tick | Sampling và mix endpoint làm fan-out thay đổi; bắt buộc validation theo service. |
| `mu_i` và queue onset | Không đáng tin vì HPA gây confounding | Plateau replica 1/2/3, đặc biệt replica thấp × tải 1.5 | Nếu không quan sát onset nghẽn thì không được ngoại suy capacity; gate phải fail. |
| CPU idle và cost/request | Một phần | CPU app, completed load và replica ở từng plateau | CPU limit/throttling và backlog làm RPS quan sát khác offered load. |
| Phân phối latency/service | P95 tick quá nhiễu | Raw server spans, không chỉ P95 đã tổng hợp | Jaeger 10% tạo tail uncertainty; dùng distribution/calibration interval thay vì point-P95 regression. |
| Startup delay | Có prior nhưng bị lẫn HPA sync | command epoch và desired/ready/available mỗi 5 giây | Polling chỉ cho interval-censored delay; phải báo độ phân giải 5 giây. |
| Joint interaction | Hầu như không | Pair joint được đổi theo workload cell; tổng replica cluster là feature | Không đủ để học mọi tổ hợp 5 service; các cấu hình ngoài support phải bị OOD penalty. |
| Sidecar/node pressure | Sidecar có, node placement không | v1 giữ sidecar CPU; node contention chưa nằm trong claim | Không thể tách node contention nếu không có pod→node và node utilization đồng bộ. |
| Observation noise | Có thể ước lượng prior | Span count/tick, raw spans và CPU scrape ở cùng lưới | Capture rate thực có thể lệch 10%; không sửa sampling để làm đẹp dữ liệu. |

Thiết kế hiện tại đã bao phủ vùng replica thấp × tải cao, latency từng service và thời điểm
Available. Không bổ sung S1/S1b/S2/S3 mơ hồ vào cùng campaign. Nếu gate capacity, graph-load
hoặc latency fail, kết quả chẩn đoán mới quyết định protocol v2; không thay đổi plan giữa
campaign rồi gộp dữ liệu như cùng một phân phối.

### Kiểm tra nhận dạng tổng hợp trước campaign

Các recovery cũ chỉ là lịch sử phát triển estimator. Kết quả synthetic, stress và quyết
định GO/NO-GO hiện hành được tổng hợp trong `docs/campaign_readiness.md`; artifact máy mới
nhất nằm trong `artifacts/rl/`. Synthetic PASS không tự mở campaign thật hoặc PPO;
estimator vẫn phải fail closed khi observation contract, excitation hoặc uncertainty
không đạt.

## Acceptance protocol và readiness

| Test đề xuất | Trạng thái trước sweep | Ngưỡng/quyết định |
|---|---|---|
| T1 deterministic theo seed | **PASS bằng unit test** | Cùng plan/seed/salt phải sinh cùng schedule và workload config; seed/salt khác phải tạo ordering/pair khác. |
| T2 bảo toàn CPU work | **POST-SWEEP** | Trước overhead, load-dependent CPU không được đổi theo replica ngoài sai số số học; kiểm tra sau khi fit tham số. |
| T3 đơn điệu | **POST-SWEEP** | Dùng expected latency từ nhiều draw, không dùng một P95 sample nhiễu. Replica tăng không làm expected latency tăng; tải tăng không làm giảm. |
| T4 bão hòa hữu hạn | **POST-SWEEP** | Mọi state/reward hữu hạn; backlog/latency tăng khi overload. Không ép singularity tại `rho=1`. |
| T5 lifecycle | **PASS phần plan/schema; POST-SWEEP phần phân phối** | Unit test kiểm tra biên/schedule; validator yêu cầu plateau thật sự Available. Delay distribution chỉ pass sau dữ liệu. |
| T6 replay HPA | **KHÔNG LÀ GATE CỦA TWIN v1** | HPA cũ bị confounding và cấu hình max replica lịch sử không nhất quán. Chỉ dùng làm baseline/audit riêng, không dùng fit transition. |
| T7 observation process | **POST-SWEEP** | So span-count distribution, coverage và tail uncertainty theo scenario; không chỉ so persistence MAE. |
| T8 held-out latency | **POST-SWEEP** | Validation-only conditional rollout và per-service distribution metrics phải đạt gate đã khóa; test vẫn niêm phong. |
| T8b autonomous closed-loop | **CODE SẴN, POST-SWEEP** | Chạy trọn episode validation bằng hold/reactive/stress; observation/reward/backlog phải hữu hạn, replica đúng biên và residual `queue-(cumulative arrivals-completed)` nằm trong tolerance tương đối `1e-6`. Đây là stability gate, không phải bằng chứng accuracy ngoài support. |
| T9 tốc độ | **POST-SWEEP** | Benchmark trên Kaggle sau khi model fit. Không nhận ngưỡng 5.000 tick/s trước khi đo; báo ticks/s và thời gian dự kiến cho số PPO step thực dùng. |
| T10 khả thi SLO | **POST-SWEEP** | Báo tỷ lệ đạt SLO ở replica tối đa; nếu bất khả thi thì giữ kết quả, không nới SLO để ép pass. |

**Quyết định cũ của thiết kế v1 đã bị thay thế:** hiện **NO-GO cho campaign chính thức
và PPO**, theo `campaign_readiness.md`. Plan, collector và provenance có code nhưng
recovery v3 chưa nhận dạng được `mu`; support replica2/3 và observation contract thực
chưa đạt. PPO vẫn bị chặn bởi
automatic gates và `TWIN_APPROVED=False` cho tới khi dữ liệu train/validation mới chứng minh
M2/M3/M5/M8 và các test hậu-sweep. Fit tham số/model chỉ dùng train; validation chỉ dùng
gate/chọn cấu hình; test chỉ mở đúng một lần sau khi protocol đóng băng.

Sau training, test còn bị khóa lần hai bằng `RUN_FINAL_TEST=False`. Trên validation, PPO
được so paired với hold-policy bằng cùng episode/seed. `POLICY_VALIDATED` chỉ đạt khi quá
bán số seed có return trung bình tốt hơn hold và median scaling event lớn hơn 0; điều này
phát hiện policy thụ động mà không ép policy phải scale bằng một action penalty lớn. Chỉ sau
khi review bảng `ppo_vs_hold_validation.csv` và đóng băng policy-selection mới được mở test.

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
