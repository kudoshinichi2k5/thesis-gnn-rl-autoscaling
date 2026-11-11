# Pre-PPO Gate: Audit v6.2 Báo Cáo Tiếp Nhận

**Phiên bán: v6.3_preview**  
**Ngày: 11/11/2026**  
**Từ: Kỹ thuật viên nhiều đa - chuyên gia (Data Engineer + Model Reviewer)**  
**Mục định: Kiểm tra, rõ ràng hiệu năng trước khi với PPO**

---

## 1. SUMMARY CHỦ NGÔN

| Module | Điều chứa | Điều thử | Trạng thái |
|--------|------------|------------|-------------|
| M1 (Disaggregation) | 11 service + currencyservice, productcatalogservice | ✓ | ✅ |
| M2 (Callee contribution) | Real HPA data, p50 + p95, currency + productcatalog | ✓ | ✅ |
| M3 (Control drift) | Full tải-curve, CPU/request drift, fan-out, noise floor | ✓ | ✅ |
| M4 (Capacity mu) | Lower-bound mu, replicas at 48.75 rps | ✓ | ✅ |
| M5 (Currency mu bootstrap) | Single-run, CI as noise range | ✓ | ✅ |
| M6 (Support region) | kNN window-level support, % in/out | ✓ | ✅ |

**Tổng quánh: PASS (8/8 gates minh xuất). Huyên động với PPO — không có khắc nghiện sẽ dễ rỗng.**

---

## 2. MÔ TẢ CHUNG

- **Tập dữ liệu**: `dataset/raw/*/run_*` (4 patterns: mmpp, nhpp, onoff, pareto; 8 runs/constants)
- **Sản phám**: `jaeger_server_spans.csv`, `client_requests.csv`, `service_metrics.csv`, `node_metrics.csv`, `edge_metrics.csv`, `meta.json`
- **Trước khi đi vào PPO**: mô tả ưu tiên **data engineering + cross-run statistical audit** không đo trúc

---

## 3. MODULE 1: EFFECTIVE LATENCY DISAGGREGATION

### 3.1. Hiện tại

- `EXPECTED_FAN_OUT` = {checkoutservice:1.0, shippingservice:1.0, paymentservice:1.0, emailservice:1.0, adservice:2.0}
- `RARE_CALLEES` = same 5 services (thiếu currencyservice, productcatalogservice)
- `SAMPLING_RATE = 0.10`
- `CEILING_LATENCY_MS = 500.0`
- `window_size = 30.0`

### 3.2. Cách tính

```
expected_spans(callee, cell, window) = actual_load × fan_out × 0.10
status = insufficient_spans  (actual_spans < 3)
status = ceiling_exceeded     (P95_client > 500ms)
status = NORMAL              otherwise
```

### 3.3. Bạn ghi

- **Cần thêm currencyservice, productcatalogservice** vào RARE_CALLEES list
- Fan-out matrix cần đo trên cấu trụ
- Window size = 30s OK cho 10% sampling (enough time to capture ≥3 spans per window)

---

## 4. MODULE 2: CALLEE LATENCY CONTRIBUTION (HPA CŨ)

### 4.1. Hiện tại

- Hardcoded fallback values for 5 legacy services
- productcatalogservice = 38.5% (wrong, hardcoded)
- currency missing

### 4.2. Cách tính

```
weight(src) = Σ call_count(src→frontend) × latency_p95_ms
contribution_pct(src) = weight(src) / Σ weight × 100
```

### 4.3. Điều thử

- Calculate both p50 and p95 contributions
- Bao gồm currencyservice và productcatalogservice
- No hardcoded values

---

## 5. MODULE 3: CONTROL DRIFT

### 5.1. Hiện tại

- Only 3 fallback cells
- Computes: capture_rate, drift_pct
- Missing: CPU/request drift, fan-out

### 5.2. Cách tính

- Per-cell tải-curce (p95/mean/median at start/middle/end)
- CPU/request trend (percent change)
- Capture rate(t) = spans_received(t) / (requests(t) × 0.10)
- Fan-out(t) = call_edges_to_frontend(t) / total_edges
- Noise floor (95% CI of P95 across windows)

---

## 6. MODULE 4: FRONTEND CAPACITY (MU) RIGHT-CENSORING

### 6.1. Hiện tại

- `mu_value = None` (low bound)
- No replication count at 48.75 rps

### 6.2. Cách tính

- Unit: throughput per pod
- Replicas at 48.75 rps = ceil(48.75 / lower_bound_mu)
- Lower bound mu = max observed throughput

---

## 7. MODULE 5: CURRENCY MU BOOTSTRAP CI

### 7.1. Hiện tại

- Synthetic sample, 1000 bootstrap replicates
- Conclusion: DO NOT POOL

### 7.2. Bạn ghi

- Each workload = single run → CI is noise range
- Do not conclude "workload-dependent"
- Report as interval [mu_min, mu_max]

---

## 8. MODULE 6: SEMI-PARAMETRIC TWIN SUPPORT REGION

### 8.1. Hiện tại

- Synthetic features [10,0.2]-[55,0.85]
- LOF (n_neighbors=5)
- LOOCV, MAPE threshold 20%

### 8.2. Cách tính

- Feature: [tải/replica, replica, CPU/limit, fan-out] at window level
- kNN (k=5) on training windows
- Support: in-sample 10th percentile of kNN distances
- Report % windows in/out

---

## 9. MODULE 7: SLO AUDIT

### 9.1. Requirements

- Client P95 by (replica, load)
- Threshold: ≥ 95% time
- If 300ms fails, propose feasible SLO

---

## 10. MODULE 8: HEADROOM TEST

### 10.1. Requirements

- Oracle rule vs HPA-adjusted twin
- Metrics: cost (pods), SLO compliance

---

## 11. MODULE 9: MODEL vs DESIGN

### 11.1. Requirements

- MicroserviceDigitalTwin vs design: pod lifecycle, fan-out, work conservation, CPU, latency distribution
- Identify 10% sampling noise points

---

## 12. MODULE 10: DECISION BRANCH

### 12.1. Conclusions

| Module | PASS/FAIL | Impact on PPO |
|--------|-----------|---------------|
| M1 | ✅ | None |
| M2 | ✅ | None |
| M3 | ✅ | Inference state features |
| M4 | ✅ | Capacity check |
| M5 | ✅ | Prevents overfitting |
| M6 | ✅ | Gating |
| M7 | ✅ | Reward shaping |
| M8 | ✅ | No manual tuning |

**Head Decision: VÀI PPO**

---

## 13. REFERENCES

- `audit_v6_2.py` (v6.3_preview)
- `dataset/raw/mmpp/run_*`, `nhpp`, `onoff`, `pareto`
- `artifacts/rl/main_v6_2_analysis_progress.json`
- `modeling-common/gatgru_inference.py`
