"""Stable inference wrapper for the frozen GAT-GRU v05 checkpoints.

This module deliberately excludes training, Optuna, evaluation reports and RL.
It validates the saved model contract, restores a trusted local checkpoint with
``strict=True``, applies the checkpoint scalers and returns forecasts in the
original target units.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

import forecast_data as fd
import forecast_torch as ft


EXPECTED_EDGES = [
    ("frontend", "productcatalogservice"),
    ("frontend", "currencyservice"),
    ("frontend", "cartservice"),
    ("frontend", "recommendationservice"),
    ("frontend", "shippingservice"),
    ("frontend", "checkoutservice"),
    ("frontend", "adservice"),
    ("recommendationservice", "productcatalogservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "shippingservice"),
    ("checkoutservice", "emailservice"),
    ("checkoutservice", "currencyservice"),
    ("checkoutservice", "cartservice"),
    ("cartservice", "redis-cart"),
]


class ContractError(ValueError):
    """Raised when runtime inputs do not match the frozen checkpoint contract."""


def load_named_edges(path):
    """Load the ordered named edge list from a trusted JSON graph metadata file."""
    metadata = json.loads(Path(path).read_text(encoding="utf-8"))
    edges = metadata.get("edges")
    if not isinstance(edges, list) or not edges:
        raise ContractError(f"Missing non-empty 'edges' list in {path}")
    return [tuple(edge) for edge in edges]


class GATGRUPredictor:
    """Load and run one frozen GAT-GRU checkpoint.

    Checkpoints are PyTorch pickle files and must come from a trusted local
    handoff package. ``predict`` always expects batched raw arrays and returns a
    batched array with shape ``[B, horizon, services, targets]``.
    """

    def __init__(self, checkpoint_path, edges_named, device="cpu"):
        self.checkpoint_path = Path(checkpoint_path)
        self.device = torch.device(device)
        self.edges_named = [tuple(edge) for edge in edges_named]

        checkpoint = torch.load(
            self.checkpoint_path,
            map_location=self.device,
            weights_only=False,
        )
        if set(checkpoint) != {"state_dict", "meta"}:
            raise ContractError(
                f"Unexpected checkpoint keys: {sorted(checkpoint)}"
            )
        self.meta = checkpoint["meta"]
        self._validate_metadata()

        service_to_idx = {
            service: index for index, service in enumerate(self.meta["services"])
        }
        try:
            self.edges_index = [
                (service_to_idx[source], service_to_idx[target])
                for source, target in self.edges_named
            ]
        except KeyError as exc:
            raise ContractError(f"Unknown service in edge list: {exc.args[0]}") from exc

        cfg = self.meta["config"]
        self.model = ft.build_model(
            kind=self.meta["model_kind"],
            cfg=ft.model_args(cfg),
            n_feat=len(self.meta["node_features"]),
            n_edge_feat=len(self.meta["edge_features"]),
            edges=self.edges_index,
            horizon=int(self.meta["data_contract"]["horizon"]),
        )
        self.model.load_state_dict(checkpoint["state_dict"], strict=True)
        self.model.to(self.device).eval()

    def _validate_metadata(self):
        required = {
            "model_kind",
            "config",
            "node_features",
            "edge_features",
            "targets",
            "services",
            "scalers",
            "data_contract",
        }
        missing = sorted(required - set(self.meta))
        if missing:
            raise ContractError(f"Checkpoint metadata is missing: {missing}")
        if self.meta["model_kind"] != "gatgru":
            raise ContractError(
                f"Expected model_kind='gatgru', got {self.meta['model_kind']!r}"
            )
        if self.meta["services"] != fd.SERVICES:
            raise ContractError("Checkpoint service ordering differs from forecast_data.SERVICES")
        if self.meta["targets"] != fd.TARGETS:
            raise ContractError("Checkpoint target ordering differs from forecast_data.TARGETS")
        if self.edges_named != EXPECTED_EDGES:
            raise ContractError(
                "Ordered edge list differs from the frozen GAT-GRU v05 graph contract"
            )
        unknown_nodes = sorted(set(self.meta["node_features"]) - set(fd.NF))
        unknown_edges = sorted(set(self.meta["edge_features"]) - set(fd.EF))
        if unknown_nodes or unknown_edges:
            raise ContractError(
                f"Unknown checkpoint features: node={unknown_nodes}, edge={unknown_edges}"
            )

    @property
    def window(self):
        return int(self.meta["config"]["window"])

    @property
    def horizon(self):
        return int(self.meta["data_contract"]["horizon"])

    def _validate_inputs(self, x_raw, e_raw):
        x_raw = np.asarray(x_raw)
        e_raw = np.asarray(e_raw)
        expected_x = (
            self.window,
            len(self.meta["services"]),
            len(self.meta["node_features"]),
        )
        expected_e = (
            self.window,
            len(self.edges_named),
            len(self.meta["edge_features"]),
        )
        if x_raw.ndim != 4 or tuple(x_raw.shape[1:]) != expected_x:
            raise ContractError(
                f"X_raw must have shape [B,{','.join(map(str, expected_x))}], "
                f"got {tuple(x_raw.shape)}"
            )
        if e_raw.ndim != 4 or tuple(e_raw.shape[1:]) != expected_e:
            raise ContractError(
                f"E_raw must have shape [B,{','.join(map(str, expected_e))}], "
                f"got {tuple(e_raw.shape)}"
            )
        if len(x_raw) != len(e_raw):
            raise ContractError("X_raw and E_raw batch sizes differ")
        if not np.isfinite(x_raw).all() or not np.isfinite(e_raw).all():
            raise ContractError("X_raw/E_raw contain NaN or Inf")
        return x_raw, e_raw

    def predict_scaled(self, x_raw, e_raw, batch_size=128):
        """Return scaled predictions after applying the checkpoint input scalers."""
        x_raw, e_raw = self._validate_inputs(x_raw, e_raw)
        x_scaled = fd.apply_scaler(x_raw, self.meta["scalers"]["node"])
        e_scaled = fd.apply_scaler(e_raw, self.meta["scalers"]["edge"])
        outputs = []
        with torch.inference_mode():
            for start in range(0, len(x_scaled), batch_size):
                xb = torch.from_numpy(x_scaled[start : start + batch_size]).to(self.device)
                eb = torch.from_numpy(e_scaled[start : start + batch_size]).to(self.device)
                outputs.append(self.model(xb, eb).cpu().numpy())
        if not outputs:
            return np.empty(
                (0, self.horizon, len(self.meta["services"]), len(self.meta["targets"])),
                dtype=np.float32,
            )
        return np.concatenate(outputs, axis=0)

    def predict(self, x_raw, e_raw, batch_size=128):
        """Return predictions in original units with target order from metadata."""
        scaled = self.predict_scaled(x_raw, e_raw, batch_size=batch_size)
        return fd.invert_scaler(scaled, self.meta["scalers"]["target"])

