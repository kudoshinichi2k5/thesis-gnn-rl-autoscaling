# Comprehensive Aggregate Audit Report: Campaign v6.2 (30 Runs)

## Executive Summary
- **Collection Protocol:** `collection_v6_2` (30 Official Clean Runs).
- **Technical Analysis Pipeline:** 30/30 runs analyzed (`failed = 0`, request conservation passed 100%).
- **Observation Contract:** 15/30 passed under Protocol v6.2; 27/30 passed under Amendment v6.3.
- **Capacity Identification ($\mu$):** `currencyservice` identified ($\mu_{\text{ONOFF}} \approx 250.21$, $\mu_{\text{Pareto}} \approx 294.67$). `frontend` capacity is **right-censored** within the safe envelope ($\le 60\text{ RPS}$).

---

## Key Audit Findings

### 1. Effective Latency Disaggregation
`checkoutservice:effective_latency` failures were disaggregated. The vast majority of window failures were attributed to `insufficient_spans` ($< 3\text{ spans}$ collected due to 10% sampling on low-frequency checkout requests), rather than actual latency ceiling breaches ($> 500\text{ ms}$).

### 2. Control Runs Stability (Drift Audit)
Drift across the 3 Control runs was evaluated:
- **Client P95 Latency Drift:** $< 8.5\%$
- **Capture Rate Consistency:** $100\%$ trace integrity
- **Verdict:** Control runs exhibit high reproducibility; no platform-level performance drift observed.

### 3. Service Capacity ($\mu$) & Workload Covariance
- `currencyservice` exhibits a statistically significant 17.8% difference in $\mu$ between ONOFF and Pareto workloads ($p < 0.05$, non-overlapping 95% Bootstrap CIs). **Recommendation:** Do not pool into a single scalar $\mu$.
- `frontend` $\mu$ cannot be point-identified under safe load ($\le 60\text{ RPS}$). Reported as `status = "capacity right-censored"` with lower bound $\mu \ge 58.4\text{ RPS}$.

### 4. Semi-Parametric Digital Twin Validation
For services without point-identified $\mu$ (`frontend`), a semi-parametric mapping $[ \text{Load/Replica}, \text{CPU/Limit} ] \to \text{Latency}$ was constructed:
- **Support Region:** Bounded by Local Outlier Factor (LOF) convex hull.
- **LOOCV MAPE:** $12.4\%$ on Train split (meets acceptance threshold $\le 20\%$).
- **Validation Evaluation:** All validation runs fell within the support region.

---

## Final Decision Branch & Operating Scope

**Decision:** **BRANCH A — Eligible for Semi-Parametric Digital Twin Fitting.**

### Bounded Operating Envelope:
1. **Workload Limit:** Peak Load $\le 60\text{ RPS}$.
2. **Support Constraint:** Policy actions must remain strictly inside the LOF Convex Hull. No extrapolation outside the support region is permitted.
3. **PPO Training Scope:** RL agent state space bounded by semi-parametric twin empirical limits.