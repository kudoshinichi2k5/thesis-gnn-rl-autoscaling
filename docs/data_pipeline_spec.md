# Data pipeline — contract theo pha

> Nội dung đã được hợp nhất vào [campaign_readiness.md](campaign_readiness.md), là nguồn
> quyết định chuẩn. File này giữ diễn giải schema chi tiết để audit.

Ngày 06/11/2026. **Contract v2 đã có code và test cục bộ; chưa đủ bằng chứng GO cho campaign.**
Đây là contract thu dữ liệu; không phải tuyên bố Twin đã được kiểm định.

## P1–P3: bằng chứng và đơn vị

| Tiền đề | Bằng chứng source | Kết luận |
|---|---|---|
| P1 | `load-testing/arrival_models.py:131`, `requests_per_session`; `:182`, `:204`, `:243` | `designed_rate.csv` có cả `lambda_sessions` và `expected_rps`. NHPP/MMPP: hệ số kỳ vọng 6 operation/session; Pareto: 6.761484164656639 operation/session = 3 + E[ceil(items)]. |
| P1, ngoại lệ | `arrival_models.py:204` | Với ON/OFF, `lambda_sessions` là số nguồn đang ON, không phải session/s. `expected_rps = active_sources × rate_on`. Metadata mới ghi rõ đơn vị; không đổi generator. |
| P2 | `collect_metrics.py`, `calibration.py`; `monitoring-stack/prometheus.yml` | Legacy CSV vẫn bucket theo startTime 10s và CPU rate40s; calibration v2 thêm completion cohort/CPU rate30s cùng origin và raw CPU counter10s. Scrape interval vẫn10s. |
| P3 | Driver trước Pha 1 chỉ có `arrival_trace.csv`, `driver_started.json`, `driver_report.json`; code mới ở `locustfile.py`, `ExecutionLog`, `Client.request`, `ArrivalDriver.drive` | Trước đây không ghi actual arrival/request. Nay ghi ledger session, request, in-flight và resource. |

`expected_rps` là kỳ vọng số operation HTTP top-level, chưa gồm redirect hops và chưa
tính thời điểm request thực sự diễn ra. Không gọi nó là completed throughput. Với session
thì đã có `lambda_sessions`; không cần chia ngược từ cột HTTP nếu đọc được cột này.
Ngoại sinh là lịch session và mark công việc. Request trong session phụ thuộc response.
ON/OFF giữ hành vi request-level từ persistent source, không đổi thành shopping session.

## Pha 1: execution schema 1

Các timestamp là Unix seconds trên loadgen; duration/lag dùng monotonic clock. Cần kiểm
tra clock trước khi ghép với Prometheus/Jaeger. Không chuyển NaN/missing thành zero.

### sessions.csv

`session_id, scheduled_ts, actual_start_ts, planned_size, actual_size, end_ts, dropped,
kind, source, planned_requests, lag_ms, outcome, driver_error, censored_at`

- Mỗi row ứng với một arrival event; ID gồm scenario, seed, event index, duy nhất trong
  run. Với ON/OFF, ID là event request, `source` mới là persistent client.
- `planned_size`: giữ mark generator (K actions, S cart items, hoặc 1 với ON/OFF).
- `planned_requests`: K+1 cho NHPP/MMPP, S+3 cho Pareto, 1 cho ON/OFF.
- `actual_size`: số lần gọi HTTP top-level đã bắt đầu, gồm lần lỗi; không đếm redirect.
- `scheduled_ts`: thời điểm dự kiến. `actual_start_ts`: lúc greenlet thực sự bắt đầu.
- `lag_ms`: độ trễ monotonic so với lịch; dropped row ghi lag lúc xét admission nhưng
  không có actual_start_ts và không tham gia phân phối lag của session đã start.
- `outcome`: completed, driver_exception, dropped, right_censored.
- `end_ts` chỉ có khi routine thực sự kết thúc; censored row ghi cutoff riêng.
- Completed session có thể chứa HTTP errors. Không đồng nhất với thành công nghiệp vụ.

### client_requests.csv

`session_id, endpoint, t_start, t_end, status, latency_ms, redirect_count, error,
request_id, method, redirect_chain, censored_at`

- Một row là một HTTP operation top-level. Endpoint dùng tên ổn định của Locust, ví dụ
  `GET /product/[id]`. `latency_ms` gồm redirect chain, đo bằng monotonic clock.
- `redirect_chain` là JSON chứa path/method/status từng response hop, gồm response cuối.
  Không lưu query string, cookie, form, token. Chưa có timestamp riêng từng redirect hop.
- `status` là final response status; 0 là không có HTTP response. `error` ghi loại lỗi,
  tránh đưa nội dung response/secret vào log.
- `http_error_responses`: tổng response hop 4xx/5xx; 4xx và 5xx có counter riêng.
  `transport_errors`: operation không có final HTTP response; `driver_exceptions`: lỗi
  Exception thoát khỏi routine arrival. Một lỗi có thể thuộc transport và driver, không
  được cộng hai counter để suy ra số request lỗi.
- Request còn chạy tại cuối logging có `t_end/status/latency_ms` rỗng,
  `error=right_censored`, cutoff ở `censored_at`; không tính là completion.
- Cutoff này là lúc Locust dừng sau drain. Pha 2 phải phân biệt thêm cutoff cuối cửa sổ
  đo với completion trong drain; chưa triển khai trong collector hiện tại.

### in_flight.csv

`timestamp, elapsed_sec, sessions_started, sessions_completed, driver_exceptions,
sessions_in_flight, requests_started, requests_completed, requests_in_flight`

Sample mục tiêu mỗi giây, ghi timestamp thực tế. Session in-flight = started − completed
− driver_exceptions. Request in-flight = started − completed, gồm request lỗi đã kết thúc
ở completed. Session gồm think time, không dùng session in-flight cho Little's Law HTTP.
Logging scheduler có thể trễ: phải kiểm tra gap của chính file này ở test tải cao.

### loadgen_resources.csv

`timestamp, elapsed_sec, node_cpu_busy_pct, node_mem_total_bytes, node_mem_available_bytes,
process_cpu_cores, process_rss_bytes`

Đọc Linux `/proc` và clock của process, không gọi cluster. CPU node chuẩn hóa 0–100% trên
tổng CPU, loại idle/iowait. CPU process có đơn vị core. CPU sample đầu để trống vì chưa có
delta. Memory là byte. Sampler hoạt động từ trước arrival đầu đến khi Locust dừng, gồm
drain; chưa đo giai đoạn exporter. Đọc /proc lỗi khiến logging bị đánh dấu lỗi và yêu cầu
runner dừng; validator từ chối log lỗi. Không có số đo resource live ở local Windows.

### Report và độ bền

`driver_report.json` giữ counters cũ để tương thích, thêm execution schema, counters
phân loại, censored counts và lag p50/p95/p99/max/count. Toàn bộ lag của event đã start
nằm trong sessions.csv. File CSV flush mỗi lần sampler chạy và khi close; đây không
phải WAL chịu mất điện. Kill -9/VM reboot có thể làm mất log cuối, run phải bị từ chối.
Logger phục vụ đúng một driver process, không hỗ trợ distributed Locust.

Validator schema mới kiểm tra artifact, ID/count, thời gian request, completion và lỗi
logger. HTTP 4xx/5xx không bị tự động loại chỉ vì tồn tại. Ngưỡng abort hiệu năng xem
`campaign_readiness.md` và `abort_rules` trong plan.
Smoke schema cũ vẫn đọc được để audit lịch sử, không chứng minh schema 1 đã chạy live.

Metadata wrapper thêm arrival_model, execution_schema_version và danh sách artifact.
Code hash hiện có bao phủ locustfile.py và arrival_models.py; không đổi seed, sampling,
lịch workload hoặc cluster. Code hash đã thay đổi: không resume trộn code cũ/mới.

## Calibration v2 — cập nhật 06/11/2026

Các mục dưới đây đã có code trong `calibration.py` và collector. Điều kiện bằng chứng
chưa đo được vẫn trả về unidentified; không đồng nghĩa đã kiểm định trên cluster.

Artifact bổ sung:

| File | Nội dung |
|---|---|
| `node_windows_30s.csv` | CPU rate30s tại `origin+30, origin+60,...`, replica/resource cùng timestamp |
| `cpu_counter_10s.csv` | Counter từng series, đầy đủ label identity, phục vụ kiểm tra reset/zero/double delta |
| `jaeger_server_spans.csv` | Server-span duration, end_timestamp, endpoint và trace/span identity; dữ liệu cũ có thêm cột exclusive vẫn đọc được |
| `client_windows_30s.csv` | Arrival theo start, completion theo end, occupancy chính xác theo interval, residual, censor, phase và session occupancy |
| `service_windows_30s.csv` | Completion cohort theo service; frontend dùng client operation, nội bộ dùng sampled Jaeger |
| `capture_by_endpoint.csv` | Operations, redirect hops, span count, ratio và CI bootstrap300 lần trên block30s |
| `calibration_features_30s.csv` | Join completion với CPU cùng window; capture, Effective Service Latency và downstream-normal được suy luận động, thiếu bằng chứng thì False |
| `calibration_diagnostics.json` | Invariants, aliasing, estimator outcome, rules và input/code hash |

Legacy `node_metrics.csv`, `edge_metrics.csv`, `service_metrics.csv` vẫn giữ schema phục vụ
audit; không được dùng chúng như completion-aligned windows. Query Jaeger mở rộng120s
trước origin và đến hết drain. Span không xuất hiện do censor/query retention vẫn không
thể dựng lại; lookback không phải bằng chứng không mất span.

CPU cuối cửa sổ là `rate(...[30s])`, replica là gauge cuối cửa sổ. Chỉ fit plateau ổn định
được journal chứng minh; không giả vờ gauge cuối cửa sổ là trung bình replica ở transition.

Các giới hạn và nguyên tắc:

1. Collector: arrival theo start, completion theo end; latency completion cohort; frontend
   client log; cửa sổ 30s; censor và capture confidence interval theo endpoint/redirect.
2. Aliasing: 10s là scrape interval, không bảo đảm độ phân giải nội tại cAdvisor. Kiểm tra
   counter delta, reset, gap và căn cửa sổ; 30s chỉ được dùng sau khi kiểm tra hỗ trợ.
3. Effective Service Latency: duration của Istio server span, gồm application, Envoy
   overhead và thời gian chờ downstream. Không gọi đây là CPU service time hay
   application-exclusive time; downstream-normal là điều kiện riêng của capacity gate.
4. Estimator: X(N), CPU/limit/throttle và downstream evidence; thiếu bằng chứng thì
   capacity_identified=False. Không dựng backlog bằng tích lũy sampled span count.
5. Little's Law: chỉ dùng cohort/window/đơn vị thống nhất và plateau ổn định, có xử lý
   censor và boundary; ngưỡng 15% không áp dụng máy móc cho burst hoặc window ít mẫu.
6. Orchestration: khóa local/cluster, đăng ký thứ tự và hash, run kiểm soát, nhãn transient,
   abort thresholds, remote process lifecycle và khôi phục đúng trạng thái HPA ban đầu.

CPU corrected-score chỉ dùng train/study/stable/capture-valid. Công thức nhiễu Binomial
độc lập theo từng span không đúng khi nhiều child spans chia sẻ một trace decision.
V2 ghi `trace_id`, cộng bình phương số span của từng trace trong cùng cửa sổ để
ước lượng phương sai Horvitz–Thompson cho RPS. Điều này vẫn cần xác nhận head sampling
theo root và không tự sửa missing trace/capture. Analyzer chỉ đặt `capture_valid=True`
khi Wilson interval theo endpoint và trace propagation đạt điều kiện đã khóa.
Capacity yêu cầu đồng thời X(N) plateau,
Effective Service Latency tăng, CPU/limit hoặc throttling và callee-normal; không fallback q95/q10.

`little_relative_error` luôn là diagnostic; chưa có bộ xác nhận stationarity độc lập nên
không gán PASS chỉ vì sai số dưới15%. Bảo toàn request interval là invariant khác và
được kiểm tra chính xác, bao gồm request đi qua biên cửa sổ.

Dữ liệu lịch sử có `exclusive_valid=0` vẫn được tái phân tích từ `latency_ms`; analyzer
không lật cờ cũ mà áp dụng contract Effective Service Latency mới. Capture ratio chỉ
được chấp nhận cùng endpoint support và trace propagation; internal `R_ht` không được
đổi tên thành `R_real` và không bù sampling lần hai.

Test split chính thức chỉ mở cuối; diagnostic tests được dùng kiểm tra/chọn protocol
thu thập, không fit tham số Twin. Câu “không dùng run test quyết định thiết kế” áp dụng
cho held-out test chính thức; nếu áp dụng cả diagnostic thì mục đích aliasing/capture test
sẽ mâu thuẫn với yêu cầu tự kiểm tra pipeline.
