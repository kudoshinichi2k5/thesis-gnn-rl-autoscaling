# Audit Jaeger sampling và reward EWMA

Ngày hiệu chỉnh: 05/11/2026. Phạm vi:
`modeling-common/twin_observation.py` và Digital Twin trong
`notebooks/rl/train_ppo_kaggle.ipynb`.

## Quyết định sau review

Prompt đúng về lỗi IEEE 754: biểu thức `0.0 * NaN` vẫn tạo NaN, nên không thể dựa vào
`alpha=0` để bảo vệ EWMA. Nhánh `K=0` phải hoàn toàn không đọc P95 raw.

Có hai điều chỉnh cần thiết so với mô tả ban đầu:

1. Sampling từng service bằng các Binomial độc lập có marginal đúng nhưng làm mất quyết
   định head-sampling chung. Frontend root count được lấy Binomial; downstream count dùng
   Hypergeometric thinning có điều kiện trên cùng root count. Khi bỏ điều kiện, mỗi
   downstream count vẫn có marginal `Binomial(N_i, p)`.
2. Gymnasium/SB3 và frozen GAT-GRU không chấp nhận NaN trong observation. Vì vậy lớp
   thống kê giữ `p95_raw=NaN` khi `K=0`, còn feature P95 đúng contract được impute 0 và đi
   kèm count. Reward chỉ đọc raw khi `K>0`.

`span_count` được giữ làm tên compatibility trong state/notebook, nhưng sau hiệu chỉnh nó
biểu diễn số trace quan sát dùng để tạo latency sample; alias `trace_count` cũng được log.

## Hàm sampling hoàn chỉnh

```python
def trace_level_counts(
    rng,
    trace_populations,
    frontend_index,
    sampling,
    no_trace_indices=(),
):
    populations = [
        max(0, int(round(float(value))))
        for value in trace_populations
    ]
    if not 0.0 <= float(sampling) <= 1.0:
        raise ValueError("sampling must be in [0,1]")

    root_population = int(populations[frontend_index])
    sampled_root = int(rng.binomial(root_population, sampling))
    counts = [0 for _ in populations]
    counts[frontend_index] = sampled_root

    for index, population in enumerate(populations):
        if index == frontend_index or index in no_trace_indices:
            continue
        visiting = min(int(population), root_population)
        if visiting and sampled_root:
            counts[index] = rng.hypergeometric(
                visiting,
                root_population - visiting,
                sampled_root,
            )

    for index in no_trace_indices:
        counts[index] = 0
    return counts
```

Giới hạn: từ aggregate RPS chỉ biết số trace ghé service gần đúng, không biết chính xác
trace-overlap hay số span lặp trong từng trace. Các đại lượng này phải được audit từ raw
trace của campaign thật. Model v1 cũng chưa mang sampling decision của một root trace qua
nhiều tick khi request bị queue lâu; simulator không tự tuyên bố đã tái tạo đầy đủ Jaeger.

## Hàm EWMA hoàn chỉnh

```python
def update_latency_ewma(previous, p95_raw, trace_count, threshold):
    count = int(trace_count)
    threshold = int(threshold)
    if count < 0 or threshold <= 0:
        raise ValueError(
            "trace_count must be >=0 and threshold must be >0"
        )

    if count == 0:
        if previous is None:
            raise ValueError(
                "previous must be initialized for an empty trace window"
            )
        value = float(previous)
        if not math.isfinite(value):
            raise FloatingPointError("previous EWMA must be finite")
        return value, value, 0.0

    raw = float(p95_raw)
    if not math.isfinite(raw):
        raise FloatingPointError(
            "non-empty trace window requires finite raw P95"
        )
    old = raw if previous is None else float(previous)
    if not math.isfinite(old):
        raise FloatingPointError("previous EWMA must be finite")

    alpha = min(1.0, float(count) / float(threshold))
    updated = alpha * raw + (1.0 - alpha) * old
    if not math.isfinite(updated):
        raise FloatingPointError("latency EWMA became non-finite")
    return updated, updated, alpha
```

## `SurrogateObservationModel.build()` hoàn chỉnh

```python
def build(self, rps, ready, dyn):
    feat = np.c_[
        np.log1p(rps),
        ready / CFG.max_replicas,
        dyn["rho"],
        dyn["overflow"],
        np.full(
            len(SERVICES),
            ready.sum() / CFG.max_total_app_pods,
        ),
    ].astype("float32")

    populations = np.maximum(
        0,
        np.rint(dyn["completed"] * CFG.tick_sec).astype(int),
    )
    span_count = np.asarray(
        trace_level_counts(
            self.rng,
            populations,
            SVC["frontend"],
            CFG.sampling,
            (SVC["redis-cart"],),
        ),
        dtype=int,
    )
    draws = sample_mdn(
        np.arange(len(SERVICES)),
        feat,
        max(1, int(span_count.max())),
        self.rng,
    )

    p50_raw = np.full(len(SERVICES), np.nan)
    p95_raw = np.full(len(SERVICES), np.nan)
    p50 = np.zeros(len(SERVICES))
    p95 = np.zeros(len(SERVICES))
    for i, service in enumerate(SERVICES):
        if service == "redis-cart":
            continue
        observed = int(span_count[i])
        if observed == 0:
            continue
        sample = draws[i, :observed]
        p50_raw[i] = p50[i] = np.quantile(sample, 0.50)
        p95_raw[i] = p95[i] = np.quantile(sample, 0.95)

    node = {
        name: np.zeros(len(SERVICES), dtype=np.float32)
        for name in fd.NF
    }
    values = {
        "rps_in": rps,
        "rps_in_delta": rps - self.prev_rps,
        "rps_per_replica": rps / np.maximum(ready, 1),
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "error_rate": dyn["error"],
        "cpu_cores": dyn["cpu"],
        "mem_mib": dyn["mem_mib"],
        "replicas": ready,
    }
    for name, value in values.items():
        node[name] = np.asarray(value, dtype=np.float32)

    for i, service in enumerate(SERVICES):
        resource = PARAMS["resources"][service]
        node["net_rx_kBps"][i] = (
            resource["net_rx_per_rps"] * rps[i] / 1024
        )
        node["net_tx_kBps"][i] = (
            resource["net_tx_per_rps"] * rps[i] / 1024
        )

    edge = {
        name: np.zeros(len(EDGES), dtype=np.float32)
        for name in fd.EF
    }
    for k, (source, target) in enumerate(EDGES):
        i, j = SVC[source], SVC[target]
        edge["call_ratio"][k] = self.graph.fanout[i, j]
        edge["call_rate"][k] = rps[i] * self.graph.fanout[i, j]
        edge["edge_latency_p95_ms"][k] = p95[j]

    self.prev_rps = rps.copy()
    return (
        np.stack([node[name] for name in fd.NF], axis=-1),
        np.stack([edge[name] for name in fd.EF], axis=-1),
        p50,
        p95,
        p50_raw,
        p95_raw,
        span_count,
    )
```

## `DigitalTwinEnv.step()` hoàn chỉnh

`_obs()` nối reward-filter state sau các feature cũ:

```python
def _obs(self, state):
    ids = [SVC[s] for s in CFG.controlled]
    current = np.stack(
        [state[key][ids] for key in self.current_fields],
        axis=1,
    ).ravel()
    forecast = self._forecast()
    target = {
        name: i for i, name in enumerate(PREDICTOR.meta["targets"])
    }
    future = np.stack([
        forecast[h, ids][
            :, [target["rps_in"], target["cpu_cores"]]
        ].ravel()
        for h in (2, 5)
    ]).ravel()
    graph = state["e"][
        :,
        [
            fd.EF.index(name)
            for name in PREDICTOR.meta["edge_features"]
        ],
    ].ravel()
    reward_filter = np.array([
        (
            self.reward_p95_ema
            if self.reward_p95_ema is not None
            else CFG.slo_ms
        ) / CFG.slo_ms
    ])
    observation = np.r_[current, future, graph, reward_filter]
    if not np.isfinite(observation).all():
        raise FloatingPointError(
            "Digital Twin/GAT-GRU observation contains NaN or Inf"
        )
    return observation.astype(np.float32)
```

Method `step()` đầy đủ:

```python
def step(self, action):
    action_result = self.twin.apply_action(action)
    reward = 0
    state = self.state
    reward_p95 = (
        float(self.reward_p95_ema)
        if self.reward_p95_ema is not None
        else float(CFG.slo_ms)
    )
    reward_alpha = 0.0
    penalties = {
        "sla": 0.0,
        "resource": 0.0,
        "ood": 0.0,
        "flapping": CFG.reward_scale * action_result["changes"],
    }

    for _ in range(CFG.decision_ticks):
        if self.t >= len(self.ep["rates"]):
            break
        state = self.twin.tick(self.ep["rates"][self.t])
        self.t += 1
        frontend_raw = state["p95_raw"][SVC["frontend"]]
        count = int(state["span_count"][SVC["frontend"]])

        if count == 0 and self.reward_p95_ema is None:
            self.reward_p95_ema = float(CFG.slo_ms)
        (
            self.reward_p95_ema,
            reward_p95,
            reward_alpha,
        ) = update_latency_ewma(
            self.reward_p95_ema,
            frontend_raw,
            count,
            CFG.low_span_threshold,
        )

        excess = max(0, reward_p95 / CFG.slo_ms - 1)
        penalties["sla"] += (
            CFG.reward_sla * excess**2
            + CFG.reward_error * state["error"][SVC["frontend"]]
        )
        penalties["resource"] += (
            CFG.reward_pods * np.sum(state["ready"])
        )
        penalties["ood"] += CFG.reward_ood * np.mean(
            state["ood"][[SVC[s] for s in CFG.controlled]]
        )

    reward -= sum(penalties.values())
    self.state = state
    terminated = self.t >= len(self.ep["rates"])
    truncated = False
    raw_frontend = state["p95_raw"][SVC["frontend"]]
    info = {
        "frontend_p95_ms": float(state["p95"][SVC["frontend"]]),
        "frontend_p95_raw_ms": (
            float(raw_frontend) if np.isfinite(raw_frontend) else None
        ),
        "reward_p95_ms": float(reward_p95),
        "reward_p95_alpha": float(reward_alpha),
        "frontend_span_count": int(
            state["span_count"][SVC["frontend"]]
        ),
        "frontend_error_rate": float(
            state["error"][SVC["frontend"]]
        ),
        "total_replicas": int(state["ready"].sum()),
        "ood_score": float(np.mean(state["ood"])),
        "work_residual": state["work_residual"],
        **action_result,
        **{
            f"penalty_{key}": float(value)
            for key, value in penalties.items()
        },
    }
    return (
        self._obs(state),
        float(reward),
        terminated,
        truncated,
        info,
    )
```

## Kiểm thử và MDP

Ba case bắt buộc và một test tương quan root nằm trong
`load-testing/test_twin_observation.py`:

- `K=0, raw=NaN`: giữ EMA cũ, alpha 0, kết quả hữu hạn;
- `0<K<K0`: alpha tuyến tính `K/K0`;
- `K>=K0`: alpha 1 và reward P95 bằng raw;
- downstream count không vượt sampled root và Redis count bằng 0.

EWMA là reward filtering và tạo phase lag nhẹ. Vì nó ảnh hưởng reward, giấu EMA khỏi PPO
sẽ biến reward process thành hidden state. `DigitalTwinEnv._obs()` vì thế nối thêm scalar
`reward_p95_ema/SLO` vào PPO observation. Scalar này không đi vào frozen GAT-GRU và không
đổi node/edge feature contract. `K0` vẫn phải ablate 15/30/60 trên validation, không dùng
test để chọn.
