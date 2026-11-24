"""Tạo PPO mẫu để thử đường ống triển khai. KHÔNG dùng để thu kết quả thực nghiệm.

Policy mẫu và PPO thật dùng cùng observation_contract.json, nên khi có PPO thật
chỉ cần thay file mô hình, không phải sửa controller.

Chạy (trong venv đã cài stable-baselines3, gymnasium):
    python autoscaler/rl/make_dummy_policy.py models/ppo/v00-dummy 50000
"""
import hashlib
import json
import pathlib
import sys

import gymnasium as gym
import numpy as np
from gymnasium import spaces
from stable_baselines3 import PPO

ROOT = pathlib.Path(__file__).resolve().parent
CONTRACT = json.loads((ROOT / "observation_contract.json").read_text(encoding="utf-8"))
N = len(CONTRACT["scaled_services"])
F = len(CONTRACT["per_service_features"])
G = len(CONTRACT["global_features"])
LO = CONTRACT["replica_bounds"]["min"]
HI = CONTRACT["replica_bounds"]["max"]
BUDGET = CONTRACT["replica_bounds"]["max_total"]
IDX = {n: i for i, n in enumerate(CONTRACT["per_service_features"])}
CAP_RPS_PER_REPLICA = 25.0          # giả định năng lực mỗi replica (chỉ cho env giả)
NVEC = CONTRACT["action_space"]["nvec"]
K = NVEC[0]                          # số lựa chọn mỗi service (5 -> Δ ∈ {-2..+2})
OFFSET = K // 2                      # delta = a - OFFSET
DECISION_SEC = CONTRACT.get("decision_period_sec", 30)

assert CONTRACT["observation_dim"] == N * F + G, "observation_dim không khớp hợp đồng"
assert NVEC == [K] * N and K % 2 == 1, "action_space phải là MultiDiscrete([K]*N) với K lẻ"
assert CONTRACT["action_space"]["mapping"] == f"delta = a - {OFFSET}", "mapping không khớp nvec"
assert CONTRACT["replica_bounds"].get("max_step", OFFSET) == OFFSET, "max_step phải bằng |Δ| lớn nhất"


class DummyScalingEnv(gym.Env):
    """Env tổng hợp: tải hình sin cộng đột biến, mỗi service một hệ số tải riêng."""

    def __init__(self):
        self.observation_space = spaces.Box(-np.inf, np.inf, (N * F + G,), np.float32)
        self.action_space = spaces.MultiDiscrete(NVEC)
        self.rng = np.random.default_rng(0)
        self.w = self.rng.uniform(0.5, 2.0, N)       # hệ số tải riêng từng service

    def _load(self, t):
        base = 30 + 25 * np.sin(2 * np.pi * t / 90)             # chu kỳ ~15 phút (90 bước x 10s)
        spike = 40 if (t // 30) % 4 == 3 else 0
        return np.clip((base + spike) * self.w + self.rng.normal(0, 3, N), 1, None)

    def _obs(self):
        o = np.zeros((N, F), np.float32)
        load = self.load
        util = load / (self.rep * CAP_RPS_PER_REPLICA)
        o[:, IDX["cpu_util_request"]] = util
        o[:, IDX["rps_in"]] = load
        o[:, IDX["latency_p95_ms"]] = 80 + 400 * np.clip(util - 0.7, 0, None)
        o[:, IDX["error_rate"]] = np.clip(util - 1.2, 0, 1) * 0.1
        nxt = self._load(self.t + 3)
        for k in ("t1", "t3", "t6"):
            o[:, IDX[f"fc_rps_{k}"]] = nxt
            o[:, IDX[f"fc_cpu_{k}"]] = nxt / CAP_RPS_PER_REPLICA
        o[:, IDX["replicas"]] = self.rep
        o[:, IDX["slo_violation"]] = (o[:, IDX["latency_p95_ms"]] > 300).astype(np.float32)
        o[:, IDX["sec_since_last_scale"]] = np.minimum(self.since, 600)
        g = np.array([o[:, IDX["latency_p95_ms"]].max(), o[:, IDX["error_rate"]].max()], np.float32)
        return np.concatenate([o.reshape(-1), g])

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if seed is not None:                         # tái lập được theo seed (Gymnasium yêu cầu)
            self.rng = np.random.default_rng(seed)
        self.t, self.rep, self.since = 0, np.ones(N), np.full(N, 600.0)
        self.load = self._load(0)
        return self._obs(), {}

    def step(self, action):
        delta = np.asarray(action) - OFFSET
        new = np.clip(self.rep + delta, LO, HI)
        # Trần tổng replica: bỏ bớt các lần tăng cho tới khi vừa ngân sách
        over = new.sum() - BUDGET
        while over > 0:
            inc = np.where(new > self.rep)[0]
            if len(inc) == 0:
                break
            j = inc[np.argmax(new[inc])]
            new[j] -= 1
            over -= 1
        delta = new - self.rep
        changed = delta != 0
        self.since = np.where(changed, 0.0, self.since + DECISION_SEC)
        self.rep, self.t = new, self.t + 1
        self.load = self._load(self.t)
        util = self.load / (self.rep * CAP_RPS_PER_REPLICA)
        slo = (util > 0.9).mean()
        reward = -5.0 * slo - 0.5 * (self.rep.sum() / BUDGET) - 0.2 * np.abs(delta).sum() / (N * OFFSET)
        return self._obs(), float(reward), False, self.t >= 180, {}


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "models/ppo/v00-dummy")
    steps = int(sys.argv[2]) if len(sys.argv) > 2 else 50_000
    out.mkdir(parents=True, exist_ok=True)
    model = PPO("MlpPolicy", DummyScalingEnv(), seed=0, verbose=0, device="cpu")
    model.learn(total_timesteps=steps)
    model.save(out / "policy")                                   # tạo policy.zip
    (out / "observation_contract.json").write_text(json.dumps(CONTRACT, indent=2), encoding="utf-8")
    (out / "training_meta.json").write_text(json.dumps({
        "dummy": True,
        "note": "PPO mẫu để thử triển khai. Không dùng cho thực nghiệm so sánh.",
        "timesteps": steps,
        "gatgru_model_version": CONTRACT["gatgru_model_version"],
        "contract_id": CONTRACT["contract_id"],
    }, indent=2), encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
            for p in sorted(out.iterdir()) if p.name != "SHA256SUMS"]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print("Đã tạo", out)


if __name__ == "__main__":
    main()
