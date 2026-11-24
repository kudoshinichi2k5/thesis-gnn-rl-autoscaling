"""Hợp đồng quan sát/hành động của PPO và lớp Gymnasium bọc cụm thật.

Theo cách của Agarwal et al. (IEEE TSC 2024): môi trường Gymnasium bọc chính hệ thống thật, nên cùng
một hợp đồng dùng được cho cả huấn luyện và triển khai. Controller chỉ cần `build_observation` +
`model.predict(obs)`; PPO nào huấn luyện đúng observation_contract.json đều thay vào được.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import forecast_data as fd


class ContractError(ValueError):
    pass


SINCE_CAP_SEC = 600.0


def load_contract(path: str | Path) -> dict:
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_contract(c)
    return c


def action_offset(contract: dict) -> int:
    nvec = contract["action_space"]["nvec"]
    k = nvec[0]
    if any(n != k for n in nvec) or k % 2 == 0:
        raise ContractError("action_space.nvec phải đều nhau và lẻ (vd. [5]*10)")
    off = k // 2
    if contract["action_space"].get("mapping") != f"delta = a - {off}":
        raise ContractError(f"mapping phải là 'delta = a - {off}'")
    return off


# Cách tính từng feature quan sát được. Tên phải khớp observation_contract.json.
def _node(name):
    i = fd.NF.index(name)
    return lambda ctx, s: float(ctx["x_last"][s, i])


def _fc(target, k):
    def f(ctx, s):
        return float(ctx["forecast"][k - 1, s, ctx["target_idx"][target]])
    return f


PER_SERVICE = {
    "cpu_util_request": _node("cpu_util_request"),
    "latency_p95_ms": _node("latency_p95_ms"),
    "error_rate": _node("error_rate"),
    "rps_in": _node("rps_in"),
    "cpu_cores": _node("cpu_cores"),
    "replicas": lambda ctx, s: float(ctx["replicas"][fd.SERVICES[s]]),
    "slo_violation": lambda ctx, s: float(
        ctx["x_last"][s, fd.NF.index("latency_p95_ms")] > ctx["slo_ms"]
        or ctx["x_last"][s, fd.NF.index("error_rate")] > ctx["slo_err"]),
    "sec_since_last_scale": lambda ctx, s: float(min(ctx["since"][fd.SERVICES[s]], SINCE_CAP_SEC)),
}
for _k in range(1, 7):
    PER_SERVICE[f"fc_rps_t{_k}"] = _fc("rps_in", _k)
    PER_SERVICE[f"fc_cpu_t{_k}"] = _fc("cpu_cores", _k)

GLOBAL = {
    "e2e_latency_p95_ms": lambda ctx: float(ctx["g_last"][fd.GF.index("e2e_latency_p95_ms")]),
    "e2e_error_rate": lambda ctx: float(ctx["g_last"][fd.GF.index("e2e_error_rate")]),
    "e2e_rps": lambda ctx: float(ctx["g_last"][fd.GF.index("e2e_rps")]),
    "total_replicas": lambda ctx: float(sum(ctx["replicas"].values())),
}


def validate_contract(c: dict) -> None:
    unknown = [f for f in c["per_service_features"] if f not in PER_SERVICE]
    unknown += [f for f in c["global_features"] if f not in GLOBAL]
    if unknown:
        raise ContractError(f"Hợp đồng có feature controller không biết tính: {unknown}")
    dim = len(c["per_service_features"]) * len(c["scaled_services"]) + len(c["global_features"])
    if dim != c["observation_dim"]:
        raise ContractError(f"observation_dim={c['observation_dim']} nhưng tính được {dim}")
    if len(c["action_space"]["nvec"]) != len(c["scaled_services"]):
        raise ContractError("Số phần tử nvec khác số scaled_services")
    bad = [s for s in c["scaled_services"] if s not in fd.SERVICES]
    if bad:
        raise ContractError(f"Service không có trong mô hình: {bad}")
    action_offset(c)


def build_observation(contract, arrays, forecast, targets, replicas, since, slo_ms, slo_err) -> np.ndarray:
    """arrays: features.WindowArrays; forecast: [6, 11, 2]; replicas/since: {service: số}."""
    ctx = {"x_last": arrays.X[-1], "g_last": arrays.G[-1], "forecast": forecast,
           "target_idx": {t: i for i, t in enumerate(targets)},
           "replicas": {s: replicas.get(s, 1) for s in fd.SERVICES},
           "since": {s: since.get(s, SINCE_CAP_SEC) for s in fd.SERVICES},
           "slo_ms": slo_ms, "slo_err": slo_err}
    vals = []
    for svc in contract["scaled_services"]:
        s = fd.SERVICES.index(svc)
        vals.extend(PER_SERVICE[f](ctx, s) for f in contract["per_service_features"])
    vals.extend(GLOBAL[f](ctx) for f in contract["global_features"])
    obs = np.asarray(vals, dtype=np.float32)
    if not np.isfinite(obs).all():
        raise ValueError("Observation có NaN/Inf")
    return obs


try:  # Gymnasium chỉ cần khi chạy trong image (đã cài) hoặc khi huấn luyện online
    import gymnasium as gym
    from gymnasium import spaces

    class ClusterEnv(gym.Env):
        """Môi trường Gymnasium bọc cụm thật (dùng cho huấn luyện/tinh chỉnh online sau này).

        observe_fn() -> (obs, info);  act_fn(deltas: dict) -> None (đi qua lớp an toàn của controller).
        Phần thưởng theo README §5.3: -λ_slo·SLOviol - λ_res·replica/ngân sách - λ_stab·|Δ|.
        """

        metadata = {"render_modes": []}

        def __init__(self, contract, observe_fn, act_fn, budget, lam=(5.0, 0.5, 0.2)):
            super().__init__()
            self.contract = contract
            self.offset = action_offset(contract)
            self.observation_space = spaces.Box(-np.inf, np.inf, (contract["observation_dim"],), np.float32)
            self.action_space = spaces.MultiDiscrete(contract["action_space"]["nvec"])
            self.observe_fn, self.act_fn, self.budget, self.lam = observe_fn, act_fn, budget, lam

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            return self.observe_fn()

        def step(self, action):
            deltas = {s: int(a) - self.offset for s, a in zip(self.contract["scaled_services"], action)}
            self.act_fn(deltas)
            obs, info = self.observe_fn()
            viol = float(info.get("slo_violation", 0.0))
            rep = float(info.get("total_replicas", 0.0))
            stab = sum(abs(d) for d in deltas.values()) / (len(deltas) * self.offset)
            l1, l2, l3 = self.lam
            reward = -l1 * viol - l2 * rep / self.budget - l3 * stab
            return obs, reward, False, False, info
except ImportError:  # pragma: no cover
    ClusterEnv = None
