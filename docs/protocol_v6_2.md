# Protocol v6.2 — Amendment miền tải an toàn

Ngày phát hành: 09/11/2026.

## 1. Phạm vi và lý do amendment

Protocol v6.2 thay thế execution order v6/v6.1 cho các lần thu chưa chạy. Lý do là
an toàn và vật lý của hệ thống, không phải kết quả fit/calibration. Khi cell
`a07f68ae687557f67d50` (train, NHPP 2.5x, focus `currencyservice`) bắt đầu chạy,
safety guard P95 10 giây đã dừng load generator sau khoảng 43 giây. Artifact thô
được giữ nguyên và cell được đánh dấu `safety-censored`; nó không được đưa vào fit.

Bằng chứng an toàn của cell bị dừng:

| Trường | Giá trị |
|---|---:|
| Peak thiết kế từ trace seed 7600 | 190.237101 RPS frontend |
| P95 client cửa sổ cuối | 13,674.744 ms |
| Max in-flight | 802 |
| Trần concurrency | 2,000 |
| Planned / started | 1,078 / 1,078 |
| Dropped | 0 |
| Unfinished sau safety abort | 793 |

Load generator còn khoảng 1.64 GiB RAM và dùng khoảng 0.52 CPU core. Vì
`max_in_flight < max_concurrent`, `dropped=0` và P95 vượt guard, đây là overload
của hệ thống chịu tải chứ không phải saturation của driver. Không hạ hoặc thay đổi
bất kỳ abort rule, threshold khoa học hay coverage gate nào.

Amendment chỉ dùng cấu hình trace, hệ số tiên nghiệm đã công bố trước cloud và CPU
limit khai báo. Không đọc `calibration_diagnostics.json`, `mu`, CPU fit hoặc fan-out
fit của chín run v6 đã hoàn thành để chọn tải thay thế.

## 2. Cổng envelope fail-closed

Trước mỗi cell chưa chạy, controller tái tạo trace từ đúng scenario, seed, duration
và multiplier rồi tính:

```text
peak = max_t designed_rate[t].expected_rps
rho_peak(s) = peak × cost_real_prior(s) × effective_fanout(s)
              / (cpu_limit_per_pod(s) × minimum_replicas(s))
```

`peak` có đơn vị 100% HTTP operation/giây tại frontend. Nó không phải RPS sampled
từ Jaeger. Cell bị từ chối trước khi liên hệ cluster nếu:

```text
peak > 60 RPS hoặc max_s rho_peak(s) > 1.3
```

Với train/control, `s` là focus service. Với validation/test joint không có một
focus duy nhất, gate kiểm tra cả năm controlled service. `minimum_replicas` được
lấy từ initial state và toàn bộ schedule của chính cell.

Các hệ số admission được khóa trong `profiling-plan.json`:

| Service | cost prior (CPU core/internal RPS) | Effective fan-out từ frontend | CPU limit/pod |
|---|---:|---:|---:|
| frontend | 0.0040 | 1.0000 | 0.5 core |
| cartservice | 0.0055 | 0.8500 | 0.5 core |
| currencyservice | 0.0030 | 1.2000 | 0.5 core |
| productcatalogservice | 0.0048 | 2.0375 | 0.5 core |
| recommendationservice | 0.0070 | 0.6500 | 1.0 core |

Nguồn `cost` và fan-out là `COST_TRUE`/`FANOUT_TRUE` trong
`load-testing/synthetic_recovery.py`, đã tồn tại trước khi thu cloud. Đây là prior
bảo thủ cho admission, không được tuyên bố là kết quả fit cluster. Fan-out hiệu lực
của productcatalog bằng `1.55 + 0.65 × 0.75 = 2.0375`, gồm cạnh trực tiếp và đường
qua recommendation. CPU limit lấy từ
`k8s-manifests/online-boutique/values-override.yaml`. Preflight và ranh giới trước
mỗi cell đều đọc Deployment thật, fail-closed nếu CPU limit của application
container không khớp các mẫu số đã đăng ký. `campaign_order.json` khóa riêng SHA-256 của `arrival_models.py` và bốn
scenario module, nên thay đổi định nghĩa trace cũng làm order không tương thích.

## 3. Chuẩn hóa tải theo scenario

| Scenario | Hệ số an toàn đầy đủ | Peak seed 7600/7601/7602 |
|---|---:|---:|
| MMPP | 0.5 | 45 / 45 / 45 RPS |
| NHPP | 0.75 | 57.071 / 57.071 / 57.071 RPS |
| ON/OFF | 1.25 | 54.375 / 52.500 / 58.125 RPS |
| Pareto | 2.4649420543 | 50 / 50 / 50 RPS |

Ba hệ số đầu đã được kiểm chứng ở diagnostic v5. Pareto có designed rate không đổi
theo thời gian; hệ số được tính từ trace model để peak đúng 50 RPS. Pareto train
focus productcatalog là execution đầu tiên và là canary bắt buộc. Nếu canary không
qua integrity/safety gate thì dừng, không chạy các cell sau.

MMPP control cũ 1.0x có peak 90 RPS và bị envelope gate từ chối. Control v6.2 dùng
MMPP 0.5x, peak 45 RPS.

## 4. Thiết kế 30 execution

- Train nhận dạng capacity: `frontend` và `currencyservice` × 4 workload = 8 run.
- Train kiểm tra không-nghẽn: cart/ON-OFF, productcatalog/Pareto,
  recommendation/NHPP = 3 run.
- Validation: 4 workload × `{0.5, 1.0}` của hệ số an toàn, joint random walk = 8 run.
- Test niêm phong: cấu trúc tương tự validation với seed riêng = 8 run.
- Drift control: MMPP 0.5x × 3 run, không fit.

Tổng cộng: 11 train + 8 validation + 8 test + 3 control = 30 execution. Mọi cell
thay thế giữ đúng nhãn split train/validation/test; test vẫn niêm phong.

## 5. Reuse dữ liệu v6

Quy tắc reuse là so sánh toàn bộ profile cell, không so riêng scenario/multiplier.
Không có run nào trong chín run v6 đạt điều kiện trùng tuyệt đối với profile v6.2.
Do đó v6.2 không chạy lại các profile cũ và cũng không nhập chúng vào fit v6.2; cả
chín raw artifact vẫn được giữ làm bằng chứng protocol-selection/safety.

| Cell v6 | Trạng thái reuse | Lý do |
|---|---|---|
| `6a72c67236c48550fa29` | Không | Control MMPP 1.0x có peak 90 RPS; v6.2 dùng 0.5x. |
| `fb6a4501e51b13930a9a` | Không | Train ON/OFF 2.0x; v6.2 dùng 1.25x. |
| `8d375c43e1c020a8a14c` | Không | Test NHPP 0.5x; v6.2 dùng 0.375x và 0.75x. |
| `ef571f0f3dd7fdf2ef2d` | Không | Train NHPP 1.5x/cart; workload và envelope mới khác. |
| `0499e4e0d5e44d949e36` | Không | Scenario/load giống một ô mới nhưng plan SHA, schema/profile amendment khác; không trùng tuyệt đối. |
| `256941f406ddb2e98e12` | Không | Train MMPP 1.0x/cart; v6.2 cart dùng ON/OFF 1.25x. |
| `ef5107c9c7807957a429` | Không | Validation Pareto 2.0x; v6.2 dùng 1.232471x và 2.464942x. |
| `3ef01b85cf4ca039ad72` | Không | Train Pareto 0.5x/cart; v6.2 cart dùng ON/OFF 1.25x. |
| `b90eca2878d0c236bdb8` | Không | Validation Pareto 0.5x; profile v6.2 khác. |

Cell `a07f68ae687557f67d50` là `safety-censored`, `exclude_from_fit=true` và không
được retry. Việc không reuse là hệ quả của quy tắc profile tuyệt đối, không phải do
kết quả estimator.

## 6. Order và hash v6.2

```text
plan_sha256       = da15609f454d0db44678dd26ab080a5ea28279165a84eecfe779863bc58fef27
order_sha256      = 4d04a9940f5a132c501607115e27bc1e32a3c21574119a92ed577ff6d3a6a41a
collection_lock   = d3242b3634d0000ae3c209dc131445fe837023dbf60356395877f6f8180664bb
```

Order đầy đủ; cột cuối là `peak RPS / worst rho_peak`:

| # | Cell | Split | Scenario × load | Envelope |
|---:|---|---|---|---:|
| 1 | `294e916740b8975d3498` | train canary | Pareto 2.464942 | 50.000 / 0.978 |
| 2 | `9e640bc0c8d1566eefa1` | control | MMPP 0.5 | 45.000 / 0.205 |
| 3 | `2d98c92cf406e07a4533` | validation | Pareto 1.232471 | 25.000 / 0.489 |
| 4 | `8928a94fc5d7b48f95e7` | train | ON/OFF 1.25 | 54.375 / 0.391 |
| 5 | `0be3821898ff1bf27850` | train | ON/OFF 1.25 | 54.375 / 0.435 |
| 6 | `e633438280e7a7c1d680` | test | NHPP 0.375 | 28.536 / 0.558 |
| 7 | `6d7faf50c2a8d157c951` | train | Pareto 2.464942 | 50.000 / 0.400 |
| 8 | `757704ff4222a6ff69c2` | train | ON/OFF 1.25 | 54.375 / 0.508 |
| 9 | `7bfa2e184fc1406cb3d2` | validation | NHPP 0.75 | 57.071 / 1.116 |
| 10 | `f44a52f17e188be177d9` | train | MMPP 0.5 | 45.000 / 0.360 |
| 11 | `018bab0ad9ee59461c78` | validation | Pareto 2.464942 | 50.000 / 0.978 |
| 12 | `38f4b6c084f1fd91baad` | validation | MMPP 0.5 | 45.000 / 0.880 |
| 13 | `036158ee7a06d7842c86` | validation | ON/OFF 0.625 | 30.000 / 0.587 |
| 14 | `82fde6ca8d94c9b6ff7a` | test | MMPP 0.25 | 22.500 / 0.440 |
| 15 | `83a33ae482badedbf14d` | train | NHPP 0.75 | 57.071 / 0.411 |
| 16 | `8a8586d310d5159184dd` | control | MMPP 0.5 | 45.000 / 0.205 |
| 17 | `1dd565e9b789d6b6cb22` | train | NHPP 0.75 | 57.071 / 0.457 |
| 18 | `8b63f02b77796c99c606` | test | ON/OFF 1.25 | 58.125 / 1.137 |
| 19 | `2f99bf4a758a62afec6e` | train | MMPP 0.5 | 45.000 / 0.324 |
| 20 | `36a2426ee8d787dc2ae3` | train | NHPP 0.75 | 57.071 / 0.260 |
| 21 | `81521eccd1bb530363c3` | test | ON/OFF 0.625 | 26.250 / 0.513 |
| 22 | `0dc5ffce882c82923642` | test | Pareto 1.232471 | 25.000 / 0.489 |
| 23 | `fc0c3da4ed1b101c109a` | test | Pareto 2.464942 | 50.000 / 0.978 |
| 24 | `30e6810df142dd5efc2a` | validation | ON/OFF 1.25 | 52.500 / 1.027 |
| 25 | `3935fed9a4c00e58098e` | test | MMPP 0.5 | 45.000 / 0.880 |
| 26 | `b52b37c778e4a8dd33cd` | test | NHPP 0.75 | 57.071 / 1.116 |
| 27 | `1e6be7d18e1ca892b159` | validation | MMPP 0.25 | 22.500 / 0.440 |
| 28 | `44a64f877559add0fc7b` | validation | NHPP 0.375 | 28.536 / 0.558 |
| 29 | `37bac30acf5e38fa82af` | train | Pareto 2.464942 | 50.000 / 0.360 |
| 30 | `dabc8dea927e4cf3afe1` | control | MMPP 0.5 | 45.000 / 0.205 |

## 7. Thời gian còn lại

Không có profile cũ nào được reuse, nên còn 30 execution v6.2:

- tải thật: `30 × 35 phút = 17.5 giờ`;
- settle + cooldown đăng ký: khoảng 1.75 giờ;
- tối thiểu trước export/validation: khoảng 19.25 giờ;
- wall-clock thực tế dự kiến: 21–24 giờ, tùy thời gian export và readiness.

## 8. Checklist trước khi thu

1. Giữ nguyên thư mục v6 và cell safety-censored; không `rsync --delete`.
2. Đồng bộ bốn file collection và `arrival_models.py`, scenario modules, wrapper.
3. Chạy 54 unit test offline và preview phải báo 30 execution/17.5 load hours.
4. Dùng output mới cho v6.2; không resume vào output v6.
5. Đăng ký order v6.2 đúng một lần rồi chạy preflight read-only.
6. Preflight phải PASS: clock, disk, RAM, K3s, app health, HPA absent,
   Prometheus/Jaeger và SSH không tương tác.
7. Chạy duy nhất Pareto canary với `--max-new 1`; review driver integrity,
   dropped, scheduling lag và P95 safety margin.
8. Chỉ sau khi canary PASS mới resume 29 cell còn lại.
9. Analyzer chạy riêng trên bản rsync; `mu=None` là kết quả hợp lệ và không replay raw.

Phạm vi kết luận của Twin v6.2 bị giới hạn ở workload có designed frontend peak
không vượt 60 RPS và nằm trong support replica/schedule đã đăng ký. Không ngoại suy
kết luận định lượng cho peak cao hơn 60 RPS.
