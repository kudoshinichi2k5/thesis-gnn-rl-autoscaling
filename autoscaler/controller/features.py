"""Dựng mảng feature cho GAT-GRU từ Snapshot, bằng CHÍNH forecast_data.build_run_arrays.

Lúc huấn luyện (notebooks/gatgru/preprocess_gatgru.ipynb):
    runs = fd.load_runs(...); fd.prepare_runs(runs); edges = fd.discover_edges(runs)
    X, E, G, tidy = fd.build_run_arrays(r, edges, CORRECT_COVERAGE=False)
Ở đây cửa sổ online được coi như một "run" ngắn và đi qua đúng các bước đó (căn lưới bằng
fd._to_grid, bỏ warm-up 6 bước, ffill nhân quả). Test autoscaler/tests đối chiếu kết quả với
dataset/processed/gatgru/runs_raw.npz và với dự báo test đã lưu.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import forecast_data as fd


@dataclass
class WindowArrays:
    X: np.ndarray          # [history, 11, len(fd.NF)]  mọi feature nút ứng viên, đơn vị gốc
    E: np.ndarray          # [history, M, len(fd.EF)]
    G: np.ndarray          # [history, len(fd.GF)]
    step_ts: list          # unix s đầu mỗi bước giữ lại
    edges: list


def build_window(snapshot, edges, history: int) -> WindowArrays:
    T = snapshot.n_steps
    if T - fd.WARMUP_DROP < history:
        raise ValueError(f"Cửa sổ {T} bước không đủ cho {history} bước lịch sử + {fd.WARMUP_DROP} warm-up")
    r = {"key": "online", "scenario": "online", "meta": {"autoscaler": "online"}, "T": T,
         "jaeger_coverage": float("nan")}
    for k, df in (("node", snapshot.node), ("svc", snapshot.svc), ("edge", snapshot.edge)):
        if not df.empty:
            g = fd._to_grid(df, snapshot.start)
            df = g[(g.t >= 0) & (g.t < T)]
        r[k] = df
    edges = [tuple(e) for e in edges]
    X, E, G, _ = fd.build_run_arrays(r, edges, correct_coverage=False)
    step_ts = [snapshot.start + (fd.WARMUP_DROP + i) * snapshot.step for i in range(len(X))]
    return WindowArrays(X[-history:], E[-history:], G[-history:], step_ts[-history:], edges)


def select(arrays: WindowArrays, node_features, edge_features):
    """Chọn feature theo TÊN từ metadata checkpoint (không hard-code vị trí)."""
    ni = [fd.NF.index(f) for f in node_features]
    ei = [fd.EF.index(f) for f in edge_features]
    return arrays.X[..., ni], arrays.E[..., ei]
