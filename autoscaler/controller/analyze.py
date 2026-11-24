"""Analyze: GAT-GRU v05 dự báo rps_in và cpu_cores của 11 service cho t+1..t+6 (60 s tới).

- Nạp checkpoint qua modeling-common/gatgru_inference.py (strict=True, scaler trong checkpoint).
- Kiểm checksum của gói model và tự kiểm tra với fixture reference_input/reference_output.
- Theo dõi sai số online: so dự báo t+1 với giá trị thực khi bước đó đến (MAE trượt 10 phút).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import deque
from pathlib import Path

import numpy as np

import forecast_data as fd
import gatgru_inference as gi

from .features import select

log = logging.getLogger("autoscaler.analyze")


class ModelPackageError(RuntimeError):
    pass


def verify_checksums(directory: str | Path) -> int:
    """Kiểm SHA256SUMS ('<hash>  <tên file>'). Trả về số file đã kiểm."""
    d = Path(directory)
    sums = d / "SHA256SUMS"
    if not sums.exists():
        raise ModelPackageError(f"Thiếu {sums}")
    n = 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        path = d / name
        if not path.exists():
            raise ModelPackageError(f"Thiếu file {path} trong SHA256SUMS")
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        if h != digest:
            raise ModelPackageError(f"Sai checksum: {path}")
        n += 1
    return n


class Forecaster:
    def __init__(self, gatgru_dir: str, checkpoint: str, device: str = "cpu"):
        self.dir = Path(gatgru_dir)
        self.checkpoint = Path(checkpoint)
        self.edges = gi.load_named_edges(self.dir / "graph_metadata.json")
        self.predictor = gi.GATGRUPredictor(self.checkpoint, self.edges, device=device)
        meta = self.predictor.meta
        self.node_features = list(meta["node_features"])
        self.edge_features = list(meta["edge_features"])
        self.targets = list(meta["targets"])
        self.services = list(meta["services"])
        self.window = self.predictor.window
        self.horizon = self.predictor.horizon
        self.model_version = None
        gm = self.dir / "graph_metadata.json"
        try:
            import json
            self.model_version = json.loads(gm.read_text(encoding="utf-8")).get("model_version")
        except Exception:  # noqa: BLE001
            pass

    def self_test(self, rtol=1e-5, atol=1e-6) -> dict:
        """So dự báo trên reference_input với reference_output của đúng seed checkpoint."""
        ref_in = np.load(self.dir / "reference_input.npz")
        ref_out = np.load(self.dir / "reference_output.npz")
        y = self.predictor.predict(ref_in["X_raw"], ref_in["E_raw"])
        m = re.search(r"seed_(\d+)", self.checkpoint.name)
        seeds = [int(s) for s in ref_out["seeds"]] if "seeds" in ref_out.files else []
        if m and int(m.group(1)) in seeds:
            expected = ref_out["Y_pred"][seeds.index(int(m.group(1)))]
        else:
            expected = None
            for i in range(ref_out["Y_pred"].shape[0]):
                if np.allclose(y, ref_out["Y_pred"][i], rtol=rtol, atol=atol):
                    expected = ref_out["Y_pred"][i]
                    break
            if expected is None:
                expected = ref_out["Y_pred"][0]
        diff = float(np.max(np.abs(y - expected)))
        ok = bool(np.allclose(y, expected, rtol=rtol, atol=atol))
        if not ok:
            raise ModelPackageError(f"Self-test GAT-GRU lệch fixture (max_abs_diff={diff:.3g})")
        return {"ok": ok, "max_abs_diff": diff}

    def forecast(self, arrays) -> np.ndarray:
        """arrays: features.WindowArrays -> [horizon, 11, 2] đơn vị gốc (rps, core)."""
        x, e = select(arrays, self.node_features, self.edge_features)
        if x.shape[0] != self.window:
            raise ValueError(f"Cần {self.window} bước, có {x.shape[0]}")
        return self.predictor.predict(x[None], e[None])[0]


class ForecastErrorTracker:
    """MAE online của dự báo t+1 so với giá trị thực, trượt `maxlen` bước (mặc định 60 = 10 phút)."""

    def __init__(self, services, targets, maxlen=60):
        self.services, self.targets = services, targets
        self.pending = {}                                   # ts bước được dự báo -> [11, 2]
        self.errors = {t: deque(maxlen=maxlen) for t in targets}

    def add_forecast(self, last_step_ts: int, step: int, y: np.ndarray) -> None:
        self.pending[last_step_ts + step] = y[0]
        for ts in [ts for ts in self.pending if ts < last_step_ts - 10 * step]:
            del self.pending[ts]

    def observe(self, arrays) -> None:
        ti = [fd.NF.index(t) for t in self.targets]
        for k, ts in enumerate(arrays.step_ts):
            pred = self.pending.pop(ts, None)
            if pred is None:
                continue
            actual = arrays.X[k][:, ti]
            for j, t in enumerate(self.targets):
                self.errors[t].append(float(np.mean(np.abs(pred[:, j] - actual[:, j]))))

    def mae(self) -> dict:
        return {t: (float(np.mean(v)) if v else None) for t, v in self.errors.items()}
