# Readiness thu dữ liệu Digital Twin

Cập nhật 08/11/2026. Đây là **tài liệu duy nhất cần review** trước khi chạy profiling
trên cluster. Nếu tài liệu khác mâu thuẫn với file này thì dừng campaign và đối chiếu
code/plan, không chọn ngưỡng thuận lợi hơn.

HTTP 4xx/5xx trong profiling là telemetry của hệ thống chịu tải, không phải lỗi tiến
trình load generator. Wrapper hiện dùng `--exit-code-on-error 0`. Với artifact cũ có
Locust exit `1`, validator chỉ chấp nhận khi execution journal chứng minh toàn bộ arrival
đã có outcome, không drop, không unfinished và logger không lỗi. Transport outcome chỉ
được giữ khi từng driver exception khớp đúng một request `status=0` đã log; lỗi code không
có bằng chứng request tương ứng vẫn bị loại. Raw `meta.json` không bị sửa.

## 1. Kết luận hiện tại

**Protocol thu dữ liệu v6 đã được đóng băng. Smoke và diagnostic v5 đã PASS về vận hành.
Campaign chính được mở theo các gate vận hành ở mục 8; kết quả capture/capacity là đầu ra
phân tích hậu thu và không còn chặn việc thu raw data. `mu=None` là kết quả fail-closed
hợp lệ, không phải lý do tự động chạy lại workload.**

Không thể cam kết trước rằng dữ liệu thật sẽ làm mọi `mu` được nhận dạng hoặc PPO về sau
sẽ thắng baseline. Thiết kế đúng ở đây có nghĩa là: tạo excitation có kiểm soát, giữ
train/validation/test tách biệt, định lượng uncertainty và dừng sớm khi giả định không
được dữ liệu ủng hộ. Một kết quả fail-closed có chẩn đoán rõ vẫn là kết quả khoa học hợp lệ;
ép gate PASS mới làm khóa luận mất độ tin cậy.

Hướng đề tài vẫn là GAT-GRU + PPO trong Digital Twin bán cơ chế. Pha hiện tại chỉ thu dữ
liệu để nhận dạng dynamics của Twin; chưa train PPO. Twin kết hợp vòng đời replica, lan
truyền tải theo call graph, bảo toàn work, CPU model và phân phối latency. Không dùng
lookup table toàn bộ cấu hình và không cho một MLP tự sinh toàn bộ trạng thái kế tiếp.

### Vì sao tuần trước bị lặp nhiều lần và cách v6 khắc phục

Ba lỗi quy trình đã làm chậm campaign: collector từng gọi analyzer ngay sau mỗi run;
hash khóa từng chứa cả code phân tích; và một scientific gate thất bại từng bị diễn giải
như raw run thất bại. Vì vậy sửa estimator/audit có thể buộc đăng ký lại, archive hoặc
phát tải lại dù telemetry thô vẫn nguyên vẹn.

Protocol v6 sửa ở ranh giới hệ thống: collector chỉ quyết định tính toàn vẹn raw data;
analyzer chạy riêng; collection hash chỉ khóa bốn file thu; kết quả khoa học được phép
fail-closed. Mọi thay đổi sau tag `profiling-protocol-v6` phải được phân loại trước:
collection safety/integrity mới được sửa collector, còn analyzer chỉ yêu cầu unit test và
phân tích lại bản rsync.

### Ngân sách concurrency của load generator

Concurrency là một phần của protocol, không còn dùng mặc định `400` cho mọi mức tải.
Smoke 0.5x dùng 400, diagnostic v5 dùng concurrency 1000 và main hiện vẫn khóa với trần
2.5x/2000. Diagnostic không còn dùng một multiplier chung: MMPP 0.5x (peak 45 RPS),
NHPP 0.75x (peak khoảng 57 RPS), ON/OFF 1.25x (peak khoảng 60 RPS). Các mức này khóa
theo peak của trace seed 7600, không theo mean hoặc multiplier đơn thuần. Cấu hình này
được khóa trong `profiling-plan.json` và campaign order. Lần diagnostic đầu đã chứng
minh cap 400 làm rơi 106/611 arrival dù scheduling lag p99 chỉ 40.5 ms, không có lỗi
HTTP/OOM và loadgen còn khoảng 1.57 GiB RAM; đó là saturation của guard phía driver,
không phải capacity của cluster. `dropped=0`, RAM, scheduling lag và client P95 vẫn là
gate fail-closed. Nếu cap mới vẫn bị chạm, không tăng tiếp một cách mù quáng: phải audit
`loadgen_resources.csv`, in-flight trajectory và latency trước khi version protocol.
Wrapper kiểm tra thêm `nofile`: soft limit được nâng tới `max_concurrent + 256` nếu hard
limit cho phép; nếu hard limit không đủ, run dừng trước khi Locust phát request.

Diagnostic v3 tại 2.0x đã phát đủ arrival (`dropped=0`), scheduling không chạm cap
(`max_in_flight=629 < 1600`) nhưng rolling client P95 vượt 10 giây. Đây là overload thật,
không phải lỗi loadgen. Không nới safety threshold. Các điều kiện dừng runtime vẫn áp
dụng cho main; một cell chạm guard sẽ dừng và được giữ làm bằng chứng, không âm thầm nới tải.

Diagnostic v4 tại MMPP 1.5x cũng fail đúng guard: trace chuyển từ Normal sang High tại
giây 6.35 và giữ tới giây 92.15, tạo peak 135 RPS; client P95 đạt 14 giây trước action
đầu ở giây 180. Điều này chứng minh multiplier chung giữa MMPP/NHPP/ON-OFF không cùng
đơn vị peak load. Diagnostic v5 dùng mapping theo scenario nêu trên; v3/v4 chỉ giữ làm
bằng chứng protocol selection, không nhập dataset fit.

## 2. Review prompt cuối và quyết định kỹ thuật

| Yêu cầu đầu vào | Quyết định đã cài đặt |
|---|---|
| Train 20 run cô lập; validation/test joint 8+8 | **Đúng.** Train giữ focus sweep và ghim cả bốn service điều khiển không phải focus ở guard capacity; validation/test dùng constrained random walk deterministic trên `[1,3]^5`, ít nhất hai service đổi đồng thời, tổng Pod không vượt 17. |
| Kubernetes Scheduling Framework là hàng đợi tuần tự toàn bộ | **Sửa lại.** Chỉ queue/binding được tuần tự hóa gần đúng; image pull và container startup có thể chồng lấp. Synthetic lifecycle lưu `queueing + binding + pull + startup`; dữ liệu thật chỉ tuyên bố tổng command→Available nếu không có event đủ chi tiết. |
| Diagnostic đạt thì nhập vào train | **Không chấp nhận.** Diagnostic dùng để chọn protocol nên không được nhập hồi tố vào official train. Việc trộn sẽ gây protocol-selection leakage. |
| `R_real` cho mọi microservice lấy từ Locust | **Sai.** Locust chỉ quan sát trực tiếp frontend. Internal `R_real` không được bịa; giữ trống/fail closed. Internal throughput hiện dùng Horvitz–Thompson `R_obs/0.10` kèm capture gate. |
| `cost_real = cost_obs × p_dynamic` trong mọi trường hợp | **Không dùng máy móc.** Công thức này chỉ sửa slope nếu fit trực tiếp trên sampled RPS. Estimator hiện fit frontend bằng `R_real`, internal bằng estimator quan sát có sampling; nhân thêm `p` sẽ sửa hai lần. |
| Cart operation lấy từ `client_requests.csv` | **Sửa lại.** Locust chỉ có operation phía client/frontend. `GetCart`, `AddItem`, `EmptyCart` được nhóm từ Jaeger cartservice operation và ghép cgroup throttle. |
| Low-utilization service vẫn phải có `mu` | **Bác bỏ.** Nếu không có X(N) plateau cùng Effective Service Latency/throttle/CPU pressure và downstream-normal thì `mu=None`; không q10/q95 fallback. |

## 3. Thiết kế campaign được khóa

### 3.1 Official study

Main gồm **36 study run + 3 drift-control = 39 execution**, mỗi execution 35 phút:

- Train: 20 run = 4 workload × 5 focus service. Mỗi run chỉ sweep focus service
  `1→2→3→1`; caller/callee khác dùng guard replicas.
- Validation: 8 run = 4 workload × tải 0.5/2.0; replica trajectory joint.
- Test: 8 run = 4 workload × tải 0.5/2.0; replica trajectory joint và được niêm phong.
- Drift control: 3 run MMPP 1.0 ở đầu/giữa/cuối; không fit estimator.
- Action space: `frontend`, `cartservice`, `currencyservice`,
  `productcatalogservice`, `recommendationservice`; mỗi service 1–3 replica; budget 17.
- Joint schedule là constrained random walk, không phải Cartesian product. Mỗi target
  đổi ít nhất hai service, giữ 510 giây và quay về baseline cuối run.
- Action nhiều service được gửi gần đồng thời; không gọi tuần tự `kubectl scale` làm sai
  joint intervention.

Tổng tải thật là 22.75 giờ; wall-clock dự kiến 26–31 giờ do settle, cooldown, export và
validation. Đây không phải 23 giờ dữ liệu liên tục của một run.

### 3.2 Diagnostic

Diagnostic gồm 3 run, tổng tải 1.5 giờ: MMPP 0.5x, NHPP 0.75x và ON/OFF 1.25x để kiểm
tra currency, frontend và cart trong dải peak xấp xỉ 45–60 RPS.
Nó nằm trong thư mục riêng và **không được merge vào train/validation/test**.

Năm tiêu chí audit diagnostic:

1. scheduling lag P99 ≤ 1 giây;
2. frontend và currency tại một replica có bằng chứng CPU pressure/saturation;
3. mọi validation/test cell trong plan là joint;
4. stress misspecification không làm estimator crash và CPU cost/idle bias ≤ 8%;
5. với cửa sổ frontend quan sát được, `p_dynamic = R_obs/R_real` đúng đến sai số số học.

`promotable_to_main=false` vẫn đúng về khoa học: diagnostic không được nhập vào train.
Actual-contract report dùng dự báo khả năng nhận dạng sau thu, không còn là khóa vận hành
của collector v6.

Kết quả diagnostic v5 ngày 07/11/2026: đủ 3 run, raw artifact hợp lệ, scheduling P99,
CPU pressure, joint validation/test, misspecification stress và identity
`p_dynamic=R_obs/R_real` tại frontend đều PASS. Có 17 pressure window frontend, 11
pressure window currency và 176/176 frontend unit window nhất quán. Đây là PASS cho
pipeline/miền tải diagnostic. Bản analyzer mới không còn gán cứng ba cờ:
`capture_valid` được suy luận từ Wilson interval 99% theo endpoint và trace propagation;
`effective_latency_valid` từ số server span quan sát; `callee_normal` từ latency/cgroup
của downstream trực tiếp. Kết quả observation contract của v5 hiện là **CHƯA TÁI PHÂN
TÍCH**, không được suy từ audit cũ và không được sửa report bằng tay.

Mỗi diagnostic run chỉ gate service mà run đó chủ động excite: MMPP→currency, ON/OFF→cart,
NHPP→frontend. Frontend capture vẫn là prerequisite chung. Không yêu cầu frontend
downstream-normal trong lúc chính currency/cart downstream đang bị cố ý gây áp lực.

## 4. Replica lifecycle

Trong synthetic study, một scale-up Pod có:

`ready_at = command + queueing + binding + pull + startup`.

- queueing/binding dùng một binding timeline chung để mô phỏng contention scheduler;
- pull/startup được phép chồng lấp giữa các Pod;
- scale-down giảm capacity phục vụ request mới trực tiếp;
- không tự nhận đã mô phỏng chính xác kube-scheduler, image cache, terminating drain hoặc
  post-Ready warm-up.

Trong cluster thật, `capacity_events.jsonl` lưu command và desired/ready/available mỗi 5
giây. Nếu Kubernetes event không đủ để tách bốn thành phần, estimator chỉ dùng tổng delay;
không phân rã bằng hằng số giả.

## 5. Contract đơn vị và dữ liệu

`service_windows_30s.csv` phân biệt rõ:

- `R_real`: completion RPS 100% từ `client_requests.csv`, **chỉ có frontend**;
- `R_obs`: sampled completion RPS từ Jaeger server spans;
- `p_dynamic`: `R_obs/R_real`, chỉ có khi hai đại lượng cùng quan sát được;
- `completed_rps`: frontend dùng `R_real`; internal service dùng `R_obs/0.10` và bị chặn
  bởi capture-validity gate.

Replica dùng cho estimator lấy từ snapshot `available` gần nhất trong
`capacity_events.jsonl` (tuổi tối đa 15 giây). Prometheus deployment gauge vẫn được lưu
ở `replicas_prometheus` để đối chiếu, nhưng một gauge trống tại đúng timestamp query không
được đổi thành 0 hay làm mất toàn bộ run. Nếu journal thiếu/cũ, hoặc gauge và journal lệch
nhau trên plateau ổn định, analyzer fail closed.

CPU không được nội suy. Cửa sổ thiếu `cpu_cores` bị loại khỏi estimator và coverage được
ghi theo service. Frontend và service thuộc action space phải đạt ít nhất 90%; service phụ
không được sweep phải đạt ít nhất 75%. CPU limit/throttling thiếu không chặn CPU cost fit,
nhưng cửa sổ đó không thể làm bằng chứng capacity pressure. Collector giữ throttling thiếu
là missing, không đổi thành 0. Các ngưỡng được ghi trong diagnostics theo từng service.

`capture_by_endpoint.csv` có cả summary và window row, cùng `p_dynamic` cho frontend.
Không điền `p_dynamic=0.10` cho internal service. Analyzer chỉ dùng xác suất head sampling
đã cấu hình làm xác suất Horvitz–Thompson sau khi tỷ lệ frontend nằm trong Wilson interval 99%
theo endpoint và có đủ support. Trace-ID overlap nội bộ/frontend được lưu để audit propagation,
nhưng không phải một probability gate riêng: các Jaeger query theo service có thể giữ phần khác
nhau của cùng trace ở ranh giới lookback/chunk. `R_ht=R_obs/p` là estimate có cluster variance,
không phải `R_real` 100%.

CPU model là:

`CPU_busy = idle × ready_replicas + cost_real × completed_real_RPS`.

Estimator không được fit CPU trên sampled RPS chưa hiệu chỉnh. Với internal service,
errors-in-variables/capture uncertainty phải được giữ trong estimator; không nhân sampling
hai lần.

Cart diagnostics được xuất thành `cart_operation_latency_30s.csv`, gồm operation, count,
P95, P99 và `cpu_throttle_ratio`. Operation lấy từ raw Jaeger span, không suy ra từ tên
request Locust.

Các artifact bắt buộc khác gồm `sessions.csv`, `client_requests.csv`, `in_flight.csv`,
`loadgen_resources.csv`, `capacity_events.jsonl`, `node_metrics.csv`,
`node_windows_30s.csv`, `jaeger_server_spans.csv`, `calibration_features_30s.csv` và
`calibration_diagnostics.json`.

## 6. Estimator fail-closed

Capacity `mu` chỉ được xác định trên train/study/stable/focus windows khi đồng thời có:

- workload/concurrency excitation đủ;
- X(N) throughput plateau;
- Effective Service Latency tăng;
- CPU pressure hoặc throttling;
- downstream bình thường;
- capture hợp lệ.

Nếu thiếu bất kỳ bằng chứng nào, kết quả là `mu=None`. Điều này đặc biệt quan trọng cho
cart, productcatalog và recommendation vốn có thể dưới 45% CPU limit ở 1 replica. Không
có q10/q95 fallback và không dùng latency tail đơn độc làm bằng chứng capacity.

Frontend và currency được kỳ vọng có thể bão hòa ở một replica tại workload diagnostic,
nhưng đây là giả thuyết phải đo, không phải hằng số để gate pass.

## 7. Synthetic baseline và misspecification stress

Baseline synthetic dùng FCFS. Cờ `--stress-misspecification` chạy thêm một model sai đặc
tả có:

- bounded fluid processor-sharing thay FCFS;
- `mu` drift ngẫu nhiên ±10–20% giữa run;
- cartservice LogNormal heavy tail với sigma 1.7;
- closed-loop session: request sau phụ thuộc response completion + think time.

Stress gate chỉ yêu cầu graceful degradation: estimator hoàn tất, không crash, và bias
CPU cost/idle của từng service ≤ 8%. Nó **không** yêu cầu `mu` phải được nhận dạng dưới mọi
misspecification và không biến synthetic thành bằng chứng cho cluster thật.

Kết quả chính thức 20 campaign, seed `20261105`, plan SHA256
`f2ced3ad7445a515eefcf35a8e5e524ddd2b488f99b14836fb3897bf3596f2cb`:

| Service | Baseline `mu` bias | Baseline cost/idle bias | Stress cost/idle bias | Stress `mu` |
|---|---:|---:|---:|---|
| frontend | 0.074% | 0.053% / 0.050% | 0.175% / 0.392% | `None` |
| cartservice | 0.261% | 0.056% / 0.451% | 0.384% / 1.419% | `None` |
| currencyservice | 0.434% | 0.062% / 0.618% | 0.455% / 1.731% | `None` |
| productcatalogservice | 0.975% | 0.121% / 1.272% | 0.373% / 2.434% | `None` |
| recommendationservice | 0.598% | 0.028% / 0.467% | 0.458% / 1.136% | `None` |

Baseline đạt toàn bộ gate `mu≤5%`, cost≤3%, idle≤5%, fan-out≤2%. Stress đạt graceful
degradation vì mọi cost/idle bias thấp hơn 8% và estimator hoàn tất. `mu=None` trong stress
là hành vi fail-closed đúng khi drift/PS/heavy-tail phá điều kiện X(N), không phải crash và
không được thay bằng quantile fallback. Cả hai artifact vẫn giữ `GO=false` có chủ đích vì
synthetic không thể xác nhận observation contract của cluster thật.

Capture gate dùng Wilson 99% vì cùng một xác suất được kiểm định lặp qua endpoint/run.
Với seed khóa, cả năm service được nhận dạng 20/20 campaign; không hạ acceptance 95%.

**Trạng thái artifact hiện tại:** hai JSON 20-campaign, seed `20261105`, được giữ nguyên
làm bằng chứng khoa học. `estimator_sha256` được ghi để truy vết nhưng không khóa collector.
Hồi quy 3–5 campaign chỉ kiểm tra tính tương thích; nó không ghi đè artifact 20-campaign.

Hồi quy tất định 3 campaign ngày 08/11/2026, seed `20261105`, cùng plan:

| Nhánh | max Δ `mu` | max Δ cost | max Δ idle | max Δ fan-out | Kết luận |
|---|---:|---:|---:|---:|---|
| Baseline | 2.126% | 0.176% | 0.549% | 0.087% | tương thích |
| Misspecified | `None` như tham chiếu | 0.469% | 2.513% | 0.128% | tương thích |

Các Δ được chuẩn hóa theo ground truth và đều nằm trong tolerance đã đăng ký; đây là
sai khác Monte Carlo 3-vs-20 campaign, không phải thay đổi kết luận. Vì vậy giữ nguyên
`synthetic_recovery_v4.json` (SHA-256
`B1B2A75E8B4AC1DBAF49CB7249E63EE8B5E0AD10B1A7F60132F8E4011C146932`) và
`synthetic_stress_report.json` (SHA-256
`DD7CA94B50E03817F78A52A4ADA98195628B9BAFDD53B6F8F8D1161C0C16631F`); chi tiết máy nằm trong
`artifacts/rl/synthetic_regression_3.json`. Không chạy lại 20 campaign.

Lệnh chuẩn:

```bash
python3 -u -B load-testing/synthetic_recovery.py \
  --campaigns 20 --seed 20261105 --workers 4 \
  --stress-misspecification \
  --output artifacts/rl/synthetic_stress_report.json
```

## 8. Gate mở official campaign

Protocol v6 tách **collection integrity** khỏi **scientific identifiability**. Main chỉ
được thu khi các điều kiện vận hành sau đạt:

1. preflight PASS: NTP, disk, RAM, K3s, HPA absent, Istio 10%, Prometheus/Jaeger;
2. order/plan đã đăng ký và collection lock không đổi;
3. collection lock chỉ gồm `run-profiling.py`, `locustfile.py`, `collect_metrics.py` và
   `profiling-plan.json`; analyzer, synthetic, test và docs không nằm trong hash chặn;
4. mỗi run phải có driver journal hoàn chỉnh, `dropped=0`, `unfinished=0`, không lỗi
   logging, đủ raw artifact và không vi phạm runtime safety guard;
5. trap khôi phục replica/HPA, campaign lock và resume đều hoạt động fail-closed.

Capture interval, Effective Service Latency, downstream-normal, recovery synthetic và
`mu` là kết quả phân tích sau thu. Chúng được báo cáo đầy đủ nhưng không làm collector
archive hoặc phát lại một run raw đã hợp lệ.

## 9. Lệnh review offline

```bash
python3 -B -m unittest discover -s load-testing -p test_profiling.py -v
python3 -B load-testing/run-profiling.py --profile main
python3 -B load-testing/run-profiling.py --profile diagnostic
```

Kỳ vọng: unit test pass; main in 39 execution/22.8 load hours; diagnostic in 3
execution/1.5 load hours. Preview không liên hệ cluster.

Sau diagnostic, chạy audit cục bộ trên thư mục diagnostic. Artifact này chỉ là chẩn đoán,
không phải giấy phép mở main:

```bash
python3 -B load-testing/run-profiling.py \
  --profile diagnostic \
  --output dataset/digital-twin-diagnostic-v5 \
  --diagnostic-input dataset/digital-twin-diagnostic-v5-nhpp-retry \
  --audit-diagnostic artifacts/rl/actual_contract_report.json \
  --stress-report artifacts/rl/synthetic_stress_report.json
```

Nếu audit trả `actual_contract_pass=false`, giữ nguyên kết quả và đọc
`observation_contract_runs.*.services.*.failure_counts`. Điều đó dự báo service nào có
thể cho `mu=None`; không còn buộc phát lại diagnostic hoặc chặn collection main.

## 10. Những gì đã thay đổi trong code

- `run-profiling.py`: joint val/test schedule, concurrent action dispatch, diagnostic
  audit và ba gate recovery/stress/actual-contract.
- `profiling-plan.json`: 20/8/8, dwell 510 giây, diagnostic 3 run peak-normalized.
- `calibration.py` (estimator adapter): khóa `R_real/R_obs/p_dynamic`, cart operation
  diagnostics và fail-closed capacity.
- `synthetic_recovery.py`: lifecycle bốn pha và misspecification stress.
- `collect_metrics.py`: collector hiện đã giữ operation/raw spans và cgroup throttle cần
  cho adapter; không thêm một nguồn `R_real` nội bộ giả.
- `test_profiling.py`: kiểm tra 20/8/8, joint budget/change constraint và gate mới.

## 11. Rủi ro còn lại

- Jaeger head sampling có tương quan theo trace; 10% cấu hình không bảo đảm capture thực
  bằng đúng 10% trong mọi window/endpoint.
- `Effective Service Latency` là duration của Istio server span: gồm application, Envoy
  overhead và thời gian chờ downstream. Nó không được gọi là application-exclusive time;
  vì vậy capacity chỉ được kết luận khi downstream-normal cũng đạt.
- Internal `R_real` chưa có instrumentation 100%; capacity có thể tiếp tục fail closed.
- Scheduler phase decomposition trong synthetic là model assumption; cluster validation
  chỉ dùng phần thực sự quan sát được.
- Joint validation/test đánh giá interaction trong support đã thiết kế, không bao phủ mọi
  tổ hợp `[1,3]^5`.

**Quyết định cuối tại thời điểm này: không chạy lại smoke/diagnostic đã hợp lệ chỉ vì
analyzer thay đổi. Đóng băng v6, đăng ký order, chạy preflight và thu official campaign
đúng một lần; analyzer chạy tăng dần trên bản raw đã rsync.**

## 12. Quy trình chạy bền vững trên cloud

### 12.1 Nguyên tắc

- Controller phải chạy trên VM `node-app`, không chạy trong WSL/laptop.
- `tmux` trên `node-app` tiếp tục chạy khi mất SSH hoặc tắt laptop, vì process thuộc VM.
- `tmux` không sống qua reboot VM. Nếu VM reboot, dùng `--resume`; run dở được archive và
  chạy lại từ đầu, run đã validate được skip.
- Không đóng/tắt ba VM, không xóa Jaeger/Prometheus và không thay plan/code trong một
  campaign đang chạy.
- Chỉ dùng fixed IP giữa các VM. Floating IP chỉ dùng từ laptop để vào `node-app`.

### 12.2 Đồng bộ code từ laptop lên controller

Chạy trong WSL ở laptop:

```bash
cd /mnt/d/School/HK7/thesis-gnn-rl-autoscaling
source cluster-setup/node-ips.env

ssh -i "$HOME/.ssh/kltn_autoscaling" \
  "ubuntu@${NODE_APP_FLOATING_IP}" \
  'mkdir -p ~/thesis-controller/load-testing ~/thesis-controller/artifacts/rl'

rsync -av -e "ssh -i $HOME/.ssh/kltn_autoscaling" \
  load-testing/ \
  "ubuntu@${NODE_APP_FLOATING_IP}:thesis-controller/load-testing/"

rsync -av -e "ssh -i $HOME/.ssh/kltn_autoscaling" \
  artifacts/rl/synthetic_recovery_v4.json \
  artifacts/rl/synthetic_stress_report.json \
  "ubuntu@${NODE_APP_FLOATING_IP}:thesis-controller/artifacts/rl/"

rsync -av -e "ssh -i $HOME/.ssh/kltn_autoscaling" \
  cluster-setup/node-ips.env \
  "ubuntu@${NODE_APP_FLOATING_IP}:thesis-controller/node-ips.env"
```

Không dùng `rsync --delete` với `~/thesis-controller`, vì thư mục đó còn chứa dataset.

### 12.3 Đồng bộ controller sang loadgen và kiểm tra SSH không tương tác

SSH vào controller cloud:

```bash
ssh -i "$HOME/.ssh/kltn_autoscaling" \
  "ubuntu@${NODE_APP_FLOATING_IP}"
```

Sau đó chạy trên `node-app`:

```bash
cd ~/thesis-controller
source node-ips.env

chmod 600 "$HOME/.ssh/profiling_controller"

ssh -i "$HOME/.ssh/profiling_controller" -o BatchMode=yes \
  "ubuntu@${NODE_LOADGEN_FIXED_IP}" hostname
ssh -i "$HOME/.ssh/profiling_controller" -o BatchMode=yes \
  "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" hostname

rsync -av \
  -e "ssh -i $HOME/.ssh/profiling_controller -o BatchMode=yes" \
  load-testing/ \
  "ubuntu@${NODE_LOADGEN_FIXED_IP}:/home/ubuntu/load-testing/"

kubectl --request-timeout=10s get --raw=/readyz
```

Hai lệnh SSH phải in hostname mà không hỏi passphrase. Nếu thất bại, chưa chạy profiling.

### 12.4 Test offline trên controller

```bash
cd ~/thesis-controller
python3 -B -m unittest discover -s load-testing -p test_profiling.py -v
python3 -B load-testing/run-profiling.py --profile smoke
python3 -B load-testing/run-profiling.py --profile diagnostic
```

Phải thấy test PASS, smoke 4 cell và diagnostic 3 cell.

### 12.5 Smoke v2 bắt buộc

Đăng ký order và chạy preflight read-only:

```bash
cd ~/thesis-controller
source node-ips.env

python3 -B load-testing/run-profiling.py \
  --profile smoke --register-order

python3 -B load-testing/run-profiling.py \
  --profile smoke --preflight-only --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller"
```

Chỉ khi preflight PASS, tạo tmux trên `node-app`:

```bash
tmux new -s twin-smoke-v2
```

Bên trong tmux:

```bash
cd ~/thesis-controller
source node-ips.env
mkdir -p dataset/digital-twin-smoke-v2
set -o pipefail

python3 -u -B load-testing/run-profiling.py \
  --profile smoke --confirm --resume --max-new 4 \
  --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller" \
  2>&1 | tee -a dataset/digital-twin-smoke-v2/controller-smoke.log

echo "SMOKE_EXIT=${PIPESTATUS[0]}"
```

Nhấn `Ctrl-b`, rồi `d` để detach. Sau đó có thể tắt laptop. Kiểm tra từ lần SSH sau:

```bash
tmux ls
tail -n 100 ~/thesis-controller/dataset/digital-twin-smoke-v2/controller-smoke.log
python3 -m json.tool \
  ~/thesis-controller/dataset/digital-twin-smoke-v2/profiling_progress.json
```

Smoke hoàn tất khi process không còn chạy, exit code bằng 0, progress là `complete`, đủ
4 cell đã validate và log không có traceback.

### 12.6 Diagnostic thật

Không tái sử dụng tmux smoke đang chạy. Sau khi smoke PASS:

```bash
cd ~/thesis-controller
source node-ips.env

python3 -B load-testing/run-profiling.py \
  --profile diagnostic --register-order

python3 -B load-testing/run-profiling.py \
  --profile diagnostic --preflight-only --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller"

tmux new -s twin-diagnostic-v5
```

Bên trong tmux:

```bash
cd ~/thesis-controller
source node-ips.env
mkdir -p dataset/digital-twin-diagnostic-v5
set -o pipefail

python3 -u -B load-testing/run-profiling.py \
  --profile diagnostic --confirm --resume --max-new 1 \
  --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller" \
  2>&1 | tee -a dataset/digital-twin-diagnostic-v5/controller-diagnostic.log

echo "DIAGNOSTIC_EXIT=${PIPESTATUS[0]}"
```

Lần đầu chỉ chạy một cell MMPP 0.5x. Chỉ khi `driver_report.json` có
`planned=started=completed`, `dropped=unfinished=0`, không có
`execution_logging_error`, scheduling p99 <1 giây và controller exit 0 mới attach lại
tmux rồi chạy cùng lệnh với `--max-new 2` để thu ON/OFF và NHPP. Không chạy cả ba cell
ngay sau một thay đổi protocol.

Nếu một transport exception đã được đếm nhưng `requests_started != requests_completed`,
run đó bị loại vì `client_requests.csv` không còn là R_real 100%. Không nới validator và
không thu lại các cell đã hợp lệ. Archive riêng cell lỗi, retry đúng scenario trong một
output mới, rồi audit ghép hai raw root bằng `--diagnostic-input`. Audit bắt buộc có đúng
một artifact hợp lệ cho mỗi cell id và từ chối duplicate.

Detach bằng `Ctrl-b`, `d`. Khi hoàn tất, chạy trên `node-app`:

```bash
# Tái tạo feature/diagnostic bằng observation contract mới; không phát tải.
for meta in \
  dataset/digital-twin-diagnostic-v5/*/meta.json \
  dataset/digital-twin-diagnostic-v5-nhpp-retry/*/meta.json
do
  python3 -B load-testing/calibration.py "$(dirname "$meta")" >/dev/null
done

python3 -B load-testing/run-profiling.py \
  --profile diagnostic \
  --output dataset/digital-twin-diagnostic-v5 \
  --diagnostic-input dataset/digital-twin-diagnostic-v5-nhpp-retry \
  --audit-diagnostic artifacts/rl/actual_contract_report.json \
  --stress-report artifacts/rl/synthetic_stress_report.json

python3 -m json.tool artifacts/rl/actual_contract_report.json
```

Đường dẫn `digital-twin-diagnostic-v2` trong prompt Gemini không phải dữ liệu diagnostic
cuối. Nguồn đúng là hai root v5 ở trên: MMPP/ON-OFF hợp lệ trong root chính và NHPP retry
trong root phụ. Không chạy lại cloud chỉ để đổi tên contract.

Sau đó tải diagnostic về laptop và gửi kết quả để review:

```bash
rsync -av --info=progress2 \
  -e "ssh -i $HOME/.ssh/kltn_autoscaling" \
  "ubuntu@${NODE_APP_FLOATING_IP}:thesis-controller/dataset/digital-twin-diagnostic-v5/" \
  dataset/digital-twin-diagnostic-v5/

rsync -av \
  -e "ssh -i $HOME/.ssh/kltn_autoscaling" \
  "ubuntu@${NODE_APP_FLOATING_IP}:thesis-controller/artifacts/rl/actual_contract_report.json" \
  artifacts/rl/actual_contract_report.json
```

### 12.7 Main campaign

Sau khi code/tag v6 được đồng bộ, thực hiện đúng một chuỗi:

1. `--profile main --register-order`;
2. `--profile main --preflight-only`;
3. chạy `--profile main --collect-only --resume --max-new 39` trong tmux riêng;
4. trên bản raw đã rsync, chạy `--analyze-collected` bất kỳ lúc nào;
5. detach và tắt laptop.

Analyzer crash chỉ ghi `analysis_failed`; run raw vẫn là `collected` và resume phải skip.
Không tự sửa report hoặc nới ngưỡng để biến `mu=None` thành số.

Lệnh chính thức trên `node-app` sau khi sync đúng tag v6:

```bash
cd ~/thesis-controller
source node-ips.env

python3 -B load-testing/run-profiling.py --profile main --register-order

python3 -B load-testing/run-profiling.py \
  --profile main --preflight-only --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller"

tmux new -s twin-main-v6
```

Trong tmux, chỉ khi preflight PASS:

```bash
cd ~/thesis-controller
source node-ips.env
mkdir -p dataset/digital-twin-calibration-v2-official
set -o pipefail

python3 -u -B load-testing/run-profiling.py \
  --profile main --collect-only --resume --max-new 39 \
  --direct-worker-ssh \
  --ssh-target "ubuntu@${NODE_LOADGEN_FIXED_IP}" \
  --observability-ssh-target "ubuntu@${NODE_OBSERVABILITY_FIXED_IP}" \
  --k3s-ssh-target "ubuntu@${NODE_APP_FIXED_IP}" \
  --ssh-key "$HOME/.ssh/profiling_controller" \
  2>&1 | tee -a dataset/digital-twin-calibration-v2-official/controller-main-v6.log
```

Phân tích tăng dần chỉ chạy trên bản đã rsync, không chạy trong thư mục controller đang
được collector ghi:

```bash
python3 -B load-testing/run-profiling.py \
  --profile main --analyze-collected \
  --output dataset/digital-twin-calibration-v2-official-rsync \
  --analysis-report artifacts/rl/main_analysis_progress.json
```

## 13. Sự cố smoke: Prometheus replica gauge trống

Nếu analyzer báo `could not convert string to float: ''` tại trường `replicas`, nguyên
nhân là Prometheus không trả deployment gauge tại đúng timestamp của một cửa sổ 30 giây;
đây không phải lỗi tmux hay mất telemetry toàn run.

Code hiện dùng `available` từ snapshot gần nhất trong `capacity_events.jsonl` với tuổi tối
đa 15 giây, giữ gauge Prometheus để audit và fail nếu hai nguồn lệch trên plateau ổn định.
Không forward-fill bằng plan target và không đổi ô trống thành 0.

Sau khi collection lock đổi, dùng output v6 mới và đăng ký order đúng một lần. Khi v6 đã
đóng băng, thay đổi analyzer không làm hash đổi và không yêu cầu đăng ký lại.

## 14. Lịch sử protocol và luật chạy lại

- **v5:** collection và analyzer còn ghép chung; kết quả scientific gate có thể khiến raw
  run hợp lệ bị chạy lại. Cách này gây tốn thời gian và đã bị loại bỏ.
- **v6:** raw run được công nhận bằng artifact + driver journal + safety checks. Phân tích
  chạy riêng, chỉ đọc bản rsync. Trace overlap không còn là capture gate vì estimand
  Horvitz–Thompson chỉ cần xác suất lấy mẫu biên hợp lệ. Overlap vẫn được báo bằng
  `traces_shared_with_frontend / sampled_traces` cho cartservice và currencyservice.
- Không thay đổi `min_contract_windows`, Wilson 99%, resource coverage hay bất kỳ ngưỡng
  khoa học nào để cứu một cửa sổ thiếu. `failure_counts` và fail-closed được giữ nguyên.
- Chỉ chạy lại đủ 20 campaign khi công thức trong `identify_capacity`, `fit_cpu`,
  `rps_trace_variance`, synthetic generator hoặc plan thí nghiệm thay đổi. Thay đổi
  collector/analyzer/audit/docs chỉ chạy unit test và hồi quy tất định 3–5 campaign nếu
  cần; không ghi đè artifact 20-campaign.
- Sau khi tag v6, chỉ lỗi an toàn hoặc toàn vẹn raw data mới được phép sửa collector.
- **v6.1:** sửa duy nhất validator transport outcome. Locust có thể trả response
  `status=0` đã được ghi đầy đủ nhưng không raise lên session greenlet; session đó hợp lệ
  ở trạng thái `completed`. Validator vẫn bắt mọi `driver_exception` phải có transport
  request cùng session và cùng loại lỗi, nhưng dùng quan hệ tập con thay vì buộc hai tập
  bằng nhau. Không đổi load, schedule, logger, collector hay ngưỡng khoa học. Campaign
  v6 đang chạy được tiếp tục bằng order v6 đã đăng ký chỉ khi hash Locust/collector/plan,
  toàn bộ execution order và validator fingerprint v6.1 khớp tuyệt đối; raw run hợp lệ
  được resume skip, không phát lại.
