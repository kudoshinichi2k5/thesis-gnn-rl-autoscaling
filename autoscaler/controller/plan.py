"""Plan: PPO đề xuất Δ replica rồi đi qua lớp an toàn."""
from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path

import numpy as np

from .analyze import verify_checksums
from .env import ContractError, action_offset, load_contract

log = logging.getLogger("autoscaler.plan")


class PPOPolicy:
    """PPO đã huấn luyện (Stable-Baselines3) + hợp đồng + training_meta, kiểm tra trước khi dùng."""

    def __init__(self, ppo_dir: str, policy_path: str, gatgru_model_version: str | None):
        self.dir = Path(ppo_dir)
        self.n_checked = verify_checksums(self.dir)
        self.contract = load_contract(self.dir / "observation_contract.json")
        meta_path = self.dir / "training_meta.json"
        self.meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        self.dummy = bool(self.meta.get("dummy", False))
        self.offset = action_offset(self.contract)
        if gatgru_model_version and self.contract.get("gatgru_model_version") != gatgru_model_version:
            raise ContractError(f"PPO huấn luyện với GAT-GRU '{self.contract.get('gatgru_model_version')}' "
                                f"nhưng đang nạp '{gatgru_model_version}'")
        from stable_baselines3 import PPO                       # import muộn: chỉ cần khi dùng PPO
        self.model = PPO.load(policy_path, device="cpu")
        obs_shape = tuple(self.model.observation_space.shape)
        if obs_shape != (self.contract["observation_dim"],):
            raise ContractError(f"observation_dim mismatch: policy {obs_shape}, hợp đồng {self.contract['observation_dim']}")
        nvec = list(getattr(self.model.action_space, "nvec", []))
        if [int(n) for n in nvec] != list(self.contract["action_space"]["nvec"]):
            raise ContractError(f"action_space mismatch: policy {nvec}, hợp đồng {self.contract['action_space']['nvec']}")
        self.vecnorm = None
        vn = self.dir / "vecnormalize.pkl"
        if vn.exists():
            with open(vn, "rb") as fh:                          # file do nhóm tạo, đã kiểm checksum
                self.vecnorm = pickle.load(fh)

    def deltas(self, obs: np.ndarray) -> dict:
        o = obs
        if self.vecnorm is not None:
            o = self.vecnorm.normalize_obs(obs[None])[0].astype(np.float32)
        action, _ = self.model.predict(o, deterministic=True)
        return {s: int(a) - self.offset for s, a in zip(self.contract["scaled_services"], np.asarray(action).ravel())}


class SafetyLayer:
    def __init__(self, policy):
        self.p = policy

    def apply(self, current: dict, proposed: dict, now: float, last_scale: dict, max_step: int):
        """-> (final: dict, notes: list). Áp theo thứ tự: biên độ, khoảng [min,max], cooldown, trần tổng."""
        notes = []
        final = {}
        for svc in self.p.scaled_services:
            cur = int(current[svc])
            want = int(proposed.get(svc, cur))
            d = max(-max_step, min(max_step, want - cur))
            if d != want - cur:
                notes.append(f"{svc}: Δ {want - cur:+d} kẹp về {d:+d} (max_step {max_step})")
            t = max(self.p.min_replicas, min(self.p.max_replicas, cur + d))
            if t < cur and now - last_scale.get(svc, 0.0) < self.p.scale_down_cooldown_sec:
                notes.append(f"{svc}: hoãn scale down (cooldown {self.p.scale_down_cooldown_sec}s)")
                t = cur
            final[svc] = t
        over = sum(final.values()) - self.p.max_total_replicas
        while over > 0:
            inc = [s for s in final if final[s] > current[s]]
            if not inc:
                break
            s = max(inc, key=lambda x: (final[x] - current[x], final[x]))
            final[s] -= 1
            over -= 1
            notes.append(f"{s}: bớt 1 vì vượt trần tổng {self.p.max_total_replicas}")
        return final, notes
