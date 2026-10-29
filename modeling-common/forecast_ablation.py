"""Controlled feature/graph ablations for the GAT-GRU forecasting study.

This module is deliberately additive: the trained baseline implementation in
``forecast_torch.py`` is not modified.  Intermediate experiments train on the
frozen train split, select checkpoints on the frozen validation split, and do
not materialize test windows.  Only ``run_final`` is allowed to touch test.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

import feature_selection as fs
import forecast_data as fd
import forecast_metrics as fm
import forecast_torch as ft


BASELINE_NODE_FEATURES = [
    'rps_in', 'rps_in_delta', 'rps_per_replica',
    'latency_p50_ms', 'latency_p95_ms',
    'cpu_cores', 'cpu_util_request', 'cpu_throttle_ratio', 'cpu_sidecar_cores',
    'mem_mib', 'net_rx_kBps', 'net_tx_kBps', 'replicas',
]
EDGE_FEATURES = ['call_rate', 'call_ratio', 'edge_latency_p95_ms']
NODE_GROUPS = {
    'core': ['rps_in', 'cpu_cores', 'replicas'],
    'load': ['rps_in_delta', 'rps_per_replica'],
    'cpu_resource': ['cpu_util_request', 'cpu_sidecar_cores', 'cpu_throttle_ratio'],
    'latency': ['latency_p50_ms', 'latency_p95_ms'],
    'memory_network': ['mem_mib', 'net_rx_kBps', 'net_tx_kBps'],
}
BASELINE_SCORE_WEIGHTS = {'rps_in': 0.5, 'cpu_cores': 0.5}
CPU_DIAGNOSTIC_WEIGHTS = {'rps_in': 0.35, 'cpu_cores': 0.65}
EXPECTED_DATASET_FINGERPRINT = 'b18509670bb1ca10b356da33c2c282b169fdea1103ac45fdca6e788a097977d2'
EXPECTED_SPLIT_FINGERPRINT = '05c2ba9d78432b338f736c3c16b186b9d17c5d4614ce16607c4f9511b9851d76'


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(type(value).__name__)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=_jsonable), encoding='utf-8')


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_repo_root(start=None):
    start = Path(start or Path.cwd()).resolve()
    for p in (start, *start.parents):
        if (p / 'modeling-common' / 'forecast_data.py').exists() and (p / 'dataset' / 'raw').exists():
            return p
    raise FileNotFoundError('Cannot locate repository root from ' + str(start))


def load_context(repo_root=None):
    root = find_repo_root(repo_root)
    raw_path = root / 'dataset' / 'processed' / 'gatgru' / 'runs_raw.npz'
    runs, meta = fd.load_runs_npz(raw_path)
    splits = meta['splits']
    audit = fd.validate_splits(splits, runs)
    contract = meta.get('data_contract') or fd.data_contract(runs, splits, fd.NF, fd.TARGETS)
    if contract['dataset_fingerprint'] != EXPECTED_DATASET_FINGERPRINT:
        raise ValueError('Dataset fingerprint differs from the frozen experiment contract')
    if contract['split_fingerprint'] != EXPECTED_SPLIT_FINGERPRINT:
        raise ValueError('Split fingerprint differs from the frozen experiment contract')
    if meta.get('services') != fd.SERVICES:
        raise ValueError('Service order in runs_raw.npz differs from forecast_data.SERVICES')
    edges_named = [tuple(x) for x in meta['edges']]
    edges_index = [(fd.SVC_IDX[a], fd.SVC_IDX[b]) for a, b in edges_named]
    return {
        'root': root,
        'runs': runs,
        'meta': meta,
        'splits': splits,
        'contract': contract,
        'split_audit': audit,
        'edges_named': edges_named,
        'edges_index': edges_index,
        'raw_path': raw_path,
    }


def load_baseline_config(ctx):
    path = ctx['root'] / 'dataset' / 'processed' / 'gatgru' / 'gatgru_best_config.json'
    cfg = json.loads(path.read_text(encoding='utf-8'))
    expected = {'window', 'dropout', 'lr', 'batch_size', 'gat_hidden', 'heads', 'gat_layers',
                'gru_hidden', 'edge_direction', 'use_edge_features'}
    if set(cfg) != expected:
        raise ValueError(f'Unexpected baseline config keys: {sorted(set(cfg) ^ expected)}')
    return cfg


def load_selection_contract(path, require_node=True, require_edge=False):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f'Missing selection manifest: {path}')
    selection = json.loads(path.read_text(encoding='utf-8'))
    if selection.get('status') != 'APPROVED':
        raise RuntimeError(f'{path} is {selection.get("status")!r}; review prior validation results first')
    if selection.get('dataset_fingerprint') != EXPECTED_DATASET_FINGERPRINT:
        raise ValueError('Selection manifest dataset fingerprint is missing or incompatible')
    if selection.get('split_fingerprint') != EXPECTED_SPLIT_FINGERPRINT:
        raise ValueError('Selection manifest split fingerprint is missing or incompatible')
    node = selection.get('node_features', [])
    edge = selection.get('edge_features', [])
    if require_node and not node:
        raise ValueError('Approved manifest has no node_features')
    if require_edge and not edge:
        raise ValueError('Approved manifest has no edge_features')
    unknown_node = sorted(set(node) - set(fd.NF))
    unknown_edge = sorted(set(edge) - set(fd.EF))
    if unknown_node or unknown_edge:
        raise ValueError(f'Unknown features: node={unknown_node}, edge={unknown_edge}')
    if not set(fd.FORCED_FEATURES).issubset(node):
        raise ValueError(f'node_features must contain forced features {fd.FORCED_FEATURES}')
    return selection


class NodeOnlyRecurrentForecaster(nn.Module):
    """Capacity-oriented non-graph control for GAT-GRU and GAT-LSTM.

    Each service is processed independently by a shared node MLP and the same
    per-service recurrent/head pattern as its GAT counterpart.  It has no access
    to other nodes or edge attributes.  The MLP depth and width mirror the GAT
    stack.  The historical class name is retained for artifact compatibility.
    """

    def __init__(self, n_nodes, n_feat, horizon, n_targets, gat_hidden=32, heads=4,
                 gat_layers=2, gru_hidden=128, dropout=0.1, emb_dim=4,
                 temporal_cell='gru', **_):
        super().__init__()
        self.N, self.H, self.K = n_nodes, horizon, n_targets
        width = heads * gat_hidden
        self.emb = nn.Embedding(n_nodes, emb_dim)
        in_dim = n_feat + emb_dim
        dims = [in_dim] + [width] * gat_layers
        self.node_layers = nn.ModuleList(nn.Linear(dims[i], width) for i in range(gat_layers))
        self.skip = nn.Linear(in_dim, width)
        self.drop = nn.Dropout(dropout)
        if temporal_cell not in ('gru', 'lstm'):
            raise ValueError(f'Unknown temporal_cell: {temporal_cell}')
        self.temporal_cell = temporal_cell
        recurrent = nn.GRU if temporal_cell == 'gru' else nn.LSTM
        # Keep the attribute name so existing GRU checkpoint keys remain stable.
        self.gru = recurrent(width, gru_hidden, batch_first=True)
        self.head = nn.Linear(gru_hidden, horizon * n_targets)

    def forward(self, X, E=None):
        B, W, N, _ = X.shape
        emb = self.emb.weight[None, None].expand(B, W, N, -1)
        x = torch.cat([X, emb], dim=-1)
        h = x
        for layer in self.node_layers:
            h = self.drop(F.elu(layer(h)))
        h = h + self.skip(x)
        h = h.permute(0, 2, 1, 3).reshape(B * N, W, -1)
        out, _ = self.gru(h)
        y = self.head(self.drop(out[:, -1]))
        return y.view(B, N, self.H, self.K).permute(0, 2, 1, 3)


# Backward-compatible public name used by the completed v02 graph-control study.
NodeOnlyGRUForecaster = NodeOnlyRecurrentForecaster


def build_model(kind, cfg, n_feat, n_edge_feat, edges, horizon):
    if kind in ft.GRAPH_MODEL_KINDS or kind == 'lstm':
        return ft.build_model(kind, ft.model_args(cfg), n_feat, n_edge_feat, edges, horizon)
    if kind in ('gru_node_only', 'lstm_node_only'):
        common = dict(n_nodes=len(fd.SERVICES), n_feat=n_feat, horizon=horizon,
                      n_targets=len(fd.TARGETS))
        node_cfg = ft.model_args(cfg)
        node_cfg['temporal_cell'] = 'lstm' if kind == 'lstm_node_only' else 'gru'
        return NodeOnlyRecurrentForecaster(**common, **node_cfg)
    raise ValueError(f'Unknown model kind: {kind}')


def _fit_one(kind, cfg, data, edges, seed, device):
    ft.set_seed(seed)
    use_edges = kind in ft.GRAPH_MODEL_KINDS and cfg.get('use_edge_features', False) and bool(data.edge_feats)
    model = build_model(kind, cfg, len(data.node_feats), len(data.edge_feats) if use_edges else 0,
                        edges, data.horizon)
    best, history = ft.train_model(model, data.get(cfg['window'], 'train'),
                                   data.get(cfg['window'], 'val'), cfg, use_edges,
                                   device=device, seed=seed)
    return model, best, history, use_edges


def _metric_tables(pred_scaled, val_w, data, weights):
    true_scaled = val_w['Y']
    pred_raw = fd.invert_scaler(pred_scaled, data.scalers['target'])
    true_raw = val_w['Y_raw']
    info = val_w['info']
    scaled_err = np.abs(pred_scaled - true_scaled).mean(axis=(1, 2))
    raw_err = np.abs(pred_raw - true_raw).mean(axis=(1, 2))
    rows = []
    for i, (run_key, scenario, target0) in enumerate(info):
        row = {'run': str(run_key), 'scenario': str(scenario), 'target0': int(target0)}
        for k, target in enumerate(fd.TARGETS):
            row[f'{target}_scaled_mae'] = float(scaled_err[i, k])
            row[f'{target}_mae'] = float(raw_err[i, k])
        rows.append(row)
    windows = pd.DataFrame(rows)
    metric_cols = [c for c in windows if c.endswith('_mae')]
    by_run = windows.groupby(['run', 'scenario'], as_index=False)[metric_cols].mean()
    by_scenario = by_run.groupby('scenario', as_index=False)[metric_cols].mean()

    def weighted(frame, ws):
        return sum(float(ws[t]) * frame[f'{t}_scaled_mae'] for t in fd.TARGETS)

    by_run['primary_score'] = weighted(by_run, weights)
    by_scenario['primary_score'] = weighted(by_scenario, weights)
    by_run['cpu_weighted_diagnostic'] = weighted(by_run, CPU_DIAGNOSTIC_WEIGHTS)
    by_scenario['cpu_weighted_diagnostic'] = weighted(by_scenario, CPU_DIAGNOSTIC_WEIGHTS)
    summary = {
        'primary_score': float(by_scenario['primary_score'].mean()),
        'cpu_weighted_diagnostic': float(by_scenario['cpu_weighted_diagnostic'].mean()),
        'pooled_scaled_mae': float(np.abs(pred_scaled - true_scaled).mean()),
    }
    for k, target in enumerate(fd.TARGETS):
        summary[f'{target}_scaled_mae_macro'] = float(by_scenario[f'{target}_scaled_mae'].mean())
        summary[f'{target}_mae_macro'] = float(by_scenario[f'{target}_mae'].mean())
    return summary, windows, by_run, by_scenario, pred_raw


def _source_hashes():
    return {
        'forecast_ablation.py': _sha256(__file__),
        'forecast_torch.py': _sha256(ft.__file__),
        'forecast_data.py': _sha256(fd.__file__),
        'forecast_metrics.py': _sha256(fm.__file__),
    }


def _environment(device):
    return {
        'python': sys.version,
        'platform': platform.platform(),
        'numpy': np.__version__,
        'pandas': pd.__version__,
        'torch': str(torch.__version__),
        'device': str(device),
        'source_sha256': _source_hashes(),
    }


def _variant_manifest(ctx, experiment_id, name, kind, cfg, node_features, edge_features,
                      seeds, weights, edge_control, topology_control, parent_version):
    return {
        'schema_version': 1,
        'experiment_id': experiment_id,
        'parent_version': parent_version,
        'variant': name,
        'model_kind': kind,
        'config': cfg,
        'node_features': node_features,
        'edge_features': edge_features,
        'edge_control': edge_control,
        'topology_control': topology_control,
        'seeds': list(seeds),
        'selection_split': 'validation',
        'test_accessed': False,
        'score_weights': weights,
        'dataset_fingerprint': ctx['contract']['dataset_fingerprint'],
        'split_fingerprint': ctx['contract']['split_fingerprint'],
        'data_contract': ctx['contract'],
        'source_sha256': _source_hashes(),
    }


def _assert_or_write_manifest(path, manifest):
    path = Path(path)
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        if old != manifest:
            raise RuntimeError(f'Refusing to overwrite incompatible experiment manifest: {path}')
    else:
        _write_json(path, manifest)


def _train_edge_means(runs, splits, edge_features):
    idx = [fd.EF.index(f) for f in edge_features]
    arrays = []
    for run in runs:
        labels = fd.split_labels(splits['runs'][run['key']], len(run['E']), splits.get('frac'))
        if np.any(labels == 'train'):
            arrays.append(run['E'][labels == 'train'][..., idx])
    return np.concatenate(arrays, axis=0).mean(axis=0)


def apply_edge_control(runs, splits, edge_features, mode='real', seed=42, lag_steps=6):
    if mode == 'real':
        return runs
    controlled = []
    feature_idx = [fd.EF.index(f) for f in edge_features]
    means = _train_edge_means(runs, splits, edge_features) if mode == 'train_edge_mean' else None
    rng = np.random.default_rng(seed)
    edge_perm = rng.permutation(runs[0]['E'].shape[1]) if mode == 'edge_id_shuffle' else None
    for run in runs:
        clone = dict(run)
        E = run['E'].copy()
        labels = fd.split_labels(splits['runs'][run['key']], len(E), splits.get('frac'))
        usable = labels != 'test'
        if mode == 'zero':
            for fi in feature_idx:
                E[usable, :, fi] = 0.0
        elif mode == 'train_edge_mean':
            for j, fi in enumerate(feature_idx):
                E[usable, :, fi] = means[:, j]
        elif mode == 'edge_id_shuffle':
            E[usable] = E[usable][:, edge_perm]
        elif mode == 'lag_6':
            positions = np.flatnonzero(usable)
            for pos in positions:
                src = max(positions[0], pos - lag_steps)
                for fi in feature_idx:
                    E[pos, :, fi] = run['E'][src, :, fi]
        else:
            raise ValueError(f'Unknown edge control: {mode}')
        clone['E'] = E
        controlled.append(clone)
    return controlled


def apply_topology_control(edges, mode='real', seed=42):
    if mode == 'real':
        return list(edges)
    n_nodes = len(fd.SERVICES)
    if mode.startswith('node_permutation'):
        # A suffix allows multiple deterministic shuffles in one validation study.
        parts = mode.rsplit('_', 1)
        shuffle_seed = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else seed
        rng = np.random.default_rng(shuffle_seed)
        permutation = rng.permutation(len(fd.SERVICES))
        return [(int(permutation[src]), int(permutation[dst])) for src, dst in edges]
    if mode == 'full_directed':
        return [(src, dst) for src in range(n_nodes) for dst in range(n_nodes)]
    if mode == 'self_loops_only':
        return [(node, node) for node in range(n_nodes)]
    if mode == 'random_same_density':
        rng = np.random.default_rng(seed)
        n_possible = n_nodes * n_nodes
        n_edges = len(edges)
        if n_edges > n_possible:
            raise ValueError('Cannot construct a same-density random graph with duplicate/excess edges')
        selected = rng.choice(n_possible, size=n_edges, replace=False)
        return [(int(edge // n_nodes), int(edge % n_nodes)) for edge in selected]
    raise ValueError(f'Unknown topology control: {mode}')


def run_fixed_variants(ctx, experiment_id, out_dir, variants, seeds=(0, 1, 2),
                       device='cpu', score_weights=None, parent_version='baseline-2026-10-14'):
    """Train controlled variants and save validation-only artifacts.

    ``variants`` is a list of dictionaries containing ``name``, ``kind``,
    ``config``, ``node_features`` and ``edge_features``. Optional
    ``edge_control`` is one of the modes accepted by ``apply_edge_control``.
    Completed compatible seed artifacts are reused; incompatible manifests are
    never overwritten.
    """
    score_weights = dict(score_weights or BASELINE_SCORE_WEIGHTS)
    if abs(sum(score_weights.values()) - 1.0) > 1e-9:
        raise ValueError('score_weights must sum to 1')
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for spec in variants:
        name = spec['name']
        kind = spec.get('kind', 'gatgru')
        cfg = copy.deepcopy(spec['config'])
        node_features = list(spec['node_features'])
        edge_features = list(spec.get('edge_features', []))
        edge_control = spec.get('edge_control', 'real')
        topology_control = spec.get('topology_control', 'real')
        if not set(fd.FORCED_FEATURES).issubset(node_features):
            raise ValueError(f'{name} is missing forced features')
        if kind not in ft.GRAPH_MODEL_KINDS and edge_features:
            raise ValueError(f'{kind} cannot consume edge features')
        cfg['use_edge_features'] = bool(edge_features) if kind in ft.GRAPH_MODEL_KINDS else False
        variant_dir = out_dir / name
        variant_dir.mkdir(parents=True, exist_ok=True)
        manifest = _variant_manifest(ctx, experiment_id, name, kind, cfg, node_features,
                                     edge_features, seeds, score_weights, edge_control,
                                     topology_control, parent_version)
        _assert_or_write_manifest(variant_dir / 'manifest.json', manifest)
        controlled_runs = apply_edge_control(ctx['runs'], ctx['splits'], edge_features,
                                             edge_control, seed=42)
        controlled_edges = apply_topology_control(ctx['edges_index'], topology_control, seed=42)
        data = ft.Data(controlled_runs, ctx['splits'], node_features, edge_features,
                       contract=ctx['contract'])
        val_w = data.get(cfg['window'], 'val')
        seed_rows, pred_raw_all = [], []
        for seed in seeds:
            metrics_path = variant_dir / f'val_metrics_seed_{seed}.json'
            prediction_path = variant_dir / f'val_predictions_seed_{seed}.npz'
            if metrics_path.exists() and prediction_path.exists():
                summary = json.loads(metrics_path.read_text(encoding='utf-8'))
                pred_raw_all.append(np.load(prediction_path, allow_pickle=False)['Y_pred'])
                seed_rows.append(summary)
                continue
            started = time.perf_counter()
            model, best, history, use_edges = _fit_one(kind, cfg, data, controlled_edges,
                                                        seed, device)
            seconds = time.perf_counter() - started
            pred_scaled = ft.predict(model, ft.loader(val_w, use_edges, 512, False), device)
            summary, windows, by_run, by_scenario, pred_raw = _metric_tables(
                pred_scaled, val_w, data, score_weights)
            summary.update(seed=int(seed), variant=name, model_kind=kind,
                           train_seconds=float(seconds), early_stop_val_mae=float(best),
                           n_params=int(ft.n_params(model)), epochs_trained=int(len(history)))
            ft.save_model(variant_dir / f'model_seed_{seed}.pt', model,
                          {**manifest, **summary, 'scalers': data.scalers})
            history.to_csv(variant_dir / f'history_seed_{seed}.csv', index=False)
            windows.to_csv(variant_dir / f'val_windows_seed_{seed}.csv', index=False)
            by_run.to_csv(variant_dir / f'val_by_run_seed_{seed}.csv', index=False)
            by_scenario.to_csv(variant_dir / f'val_by_scenario_seed_{seed}.csv', index=False)
            np.savez_compressed(prediction_path, Y_pred=pred_raw.astype(np.float32),
                                Y_true=val_w['Y_raw'].astype(np.float32),
                                info=val_w['info'].astype(str))
            _write_json(metrics_path, summary)
            pred_raw_all.append(pred_raw)
            seed_rows.append(summary)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        seed_df = pd.DataFrame(seed_rows).sort_values('seed')
        seed_df.to_csv(variant_dir / 'val_metrics_by_seed.csv', index=False)
        numeric = ['primary_score', 'cpu_weighted_diagnostic', 'pooled_scaled_mae',
                   'rps_in_scaled_mae_macro', 'cpu_cores_scaled_mae_macro',
                   'rps_in_mae_macro', 'cpu_cores_mae_macro', 'train_seconds',
                   'early_stop_val_mae', 'n_params', 'epochs_trained']
        aggregate = {'variant': name, 'model_kind': kind, 'node_features': node_features,
                     'edge_features': edge_features, 'edge_control': edge_control,
                     'topology_control': topology_control,
                     'n_seeds': len(seed_df)}
        for col in numeric:
            aggregate[f'{col}_mean'] = float(seed_df[col].mean())
            aggregate[f'{col}_std'] = float(seed_df[col].std(ddof=1)) if len(seed_df) > 1 else 0.0
        _write_json(variant_dir / 'summary.json', aggregate)
        _write_json(variant_dir / 'status.json', {'status': 'EVALUATED', 'test_accessed': False})
        all_rows.append(aggregate)
    result = pd.DataFrame(all_rows).sort_values(['primary_score_mean', 'n_params_mean'])
    result.to_csv(out_dir / 'variant_summary.csv', index=False)
    _write_json(out_dir / 'environment.json', _environment(device))
    _write_json(out_dir / 'experiment_status.json', {
        'experiment_id': experiment_id, 'parent_version': parent_version,
        'status': 'EVALUATED', 'test_accessed': False,
        'variants': [x['name'] for x in variants], 'source_sha256': _source_hashes(),
    })
    return result


def node_group_variants(base_cfg):
    core = NODE_GROUPS['core']
    full = BASELINE_NODE_FEATURES
    sets = {
        'core3': core,
        'core_plus_load': core + NODE_GROUPS['load'],
        'core_plus_cpu': core + NODE_GROUPS['cpu_resource'],
        'core_plus_latency': core + NODE_GROUPS['latency'],
        'core_load_cpu': core + NODE_GROUPS['load'] + NODE_GROUPS['cpu_resource'],
        'core_load_cpu_latency': core + NODE_GROUPS['load'] + NODE_GROUPS['cpu_resource'] + NODE_GROUPS['latency'],
        'full13': full,
        'full_minus_load': [f for f in full if f not in NODE_GROUPS['load']],
        'full_minus_cpu_resource': [f for f in full if f not in NODE_GROUPS['cpu_resource']],
        'full_minus_latency': [f for f in full if f not in NODE_GROUPS['latency']],
        'full_minus_memory_network': [f for f in full if f not in NODE_GROUPS['memory_network']],
    }
    sets = {name: [f for f in full if f in set(features)] for name, features in sets.items()}
    return [dict(name=name, kind='gatgru', config=base_cfg, node_features=features,
                 edge_features=EDGE_FEATURES) for name, features in sets.items()]


def node_leave_one_out_variants(base_cfg):
    removable = [f for f in BASELINE_NODE_FEATURES if f not in fd.FORCED_FEATURES]
    return [dict(name='without_' + feature, kind='gatgru', config=base_cfg,
                 node_features=[f for f in BASELINE_NODE_FEATURES if f != feature],
                 edge_features=EDGE_FEATURES) for feature in removable]


def graph_variants(base_cfg, node_features):
    variants = [
        dict(name='gru_node_only', kind='gru_node_only', config=base_cfg,
             node_features=node_features, edge_features=[]),
        dict(name='static_gat_shuffled_topology', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[], topology_control='node_permutation'),
        dict(name='static_gat', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[]),
        dict(name='dynamic_all_edges', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=EDGE_FEATURES),
    ]
    variants.extend(
        dict(name=f'static_gat_shuffled_topology_seed_{seed}', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[], topology_control=f'node_permutation_{seed}')
        for seed in (101, 202, 303)
    )
    variants.extend([
        dict(name='static_gat_full_directed', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[], topology_control='full_directed'),
        dict(name='static_gat_self_loops_only', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[], topology_control='self_loops_only'),
        dict(name='static_gat_random_same_density', kind='gatgru', config=base_cfg,
             node_features=node_features, edge_features=[], topology_control='random_same_density'),
    ])
    return variants


def edge_factorial_variants(base_cfg, node_features):
    variants = []
    for mask in itertools.product((0, 1), repeat=len(EDGE_FEATURES)):
        selected = [f for f, keep in zip(EDGE_FEATURES, mask) if keep]
        name = 'edge_none' if not selected else 'edge_' + '__'.join(selected)
        variants.append(dict(name=name, kind='gatgru', config=base_cfg,
                             node_features=node_features, edge_features=selected))
    return variants


def edge_control_variants(base_cfg, node_features, edge_features):
    return [dict(name='control_' + mode, kind='gatgru', config=base_cfg,
                 node_features=node_features, edge_features=edge_features, edge_control=mode)
            for mode in ('real', 'zero', 'train_edge_mean', 'edge_id_shuffle', 'lag_6')]


def audit_features(ctx, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    train = fd.train_arrays(ctx['runs'], ctx['splits'])
    kept_node, node_report, node_corr = fs.filter_node_features(
        train, fd.HORIZON, candidates=BASELINE_NODE_FEATURES, forced=fd.FORCED_FEATURES)
    kept_edge, edge_report, edge_corr = fs.filter_edge_features(
        train, ctx['edges_named'], fd.HORIZON, candidates=EDGE_FEATURES)
    node_report.to_csv(out_dir / 'node_statistical_filter.csv')
    node_corr.to_csv(out_dir / 'node_spearman.csv')
    edge_report.to_csv(out_dir / 'edge_statistical_filter.csv')
    edge_corr.to_csv(out_dir / 'edge_spearman.csv')

    service_rows, edge_rows = [], []
    for run in ctx['runs']:
        labels = fd.split_labels(ctx['splits']['runs'][run['key']], len(run['X']), ctx['splits'].get('frac'))
        mask = labels == 'train'
        if not mask.any():
            continue
        for ni, service in enumerate(fd.SERVICES):
            for feature in BASELINE_NODE_FEATURES:
                values = run['X'][mask, ni, fd.NF.index(feature)]
                service_rows.append({'run': run['key'], 'scenario': run['scenario'], 'service': service,
                                     'feature': feature, 'mean': float(np.nanmean(values)),
                                     'std': float(np.nanstd(values)),
                                     'missing_frac': float(np.isnan(values).mean()),
                                     'zero_frac': float((values == 0).mean())})
        for ei, edge in enumerate(ctx['edges_named']):
            for feature in EDGE_FEATURES:
                values = run['E'][mask, ei, fd.EF.index(feature)]
                edge_rows.append({'run': run['key'], 'scenario': run['scenario'],
                                  'edge': ' -> '.join(edge), 'feature': feature,
                                  'mean': float(np.nanmean(values)), 'std': float(np.nanstd(values)),
                                  'missing_frac': float(np.isnan(values).mean()),
                                  'zero_frac': float((values == 0).mean())})
    pd.DataFrame(service_rows).to_csv(out_dir / 'node_stats_by_run_service.csv', index=False)
    pd.DataFrame(edge_rows).to_csv(out_dir / 'edge_stats_by_run_edge.csv', index=False)
    summary = {
        'status': 'EVALUATED', 'split_used': 'train', 'test_accessed': False,
        'kept_node_statistical': kept_node, 'kept_edge_statistical': kept_edge,
        'dataset_fingerprint': ctx['contract']['dataset_fingerprint'],
        'split_fingerprint': ctx['contract']['split_fingerprint'],
        'source_sha256': _source_hashes(),
    }
    _write_json(out_dir / 'audit_summary.json', summary)
    return node_report, node_corr, edge_report, edge_corr, summary


def run_optuna_fixed_architecture(ctx, kind, node_features, edge_features, base_cfg, out_dir,
                                  n_trials=24, seed=42, device='cpu', score_weights=None,
                                  parent_version='baseline-2026-10-14'):
    """Tune training hyperparameters while holding architecture and features fixed."""
    import optuna

    score_weights = dict(score_weights or BASELINE_SCORE_WEIGHTS)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = ft.Data(ctx['runs'], ctx['splits'], node_features, edge_features, contract=ctx['contract'])
    study_name = f"{kind}_{data.contract_fingerprint[:16]}_fixed_arch"
    tuning_manifest = {
        'schema_version': 1, 'model_kind': kind, 'architecture_config': base_cfg,
        'parent_version': parent_version,
        'node_features': list(node_features), 'edge_features': list(edge_features),
        'n_trials': int(n_trials), 'search_seed': int(seed), 'score_weights': score_weights,
        'dataset_fingerprint': ctx['contract']['dataset_fingerprint'],
        'split_fingerprint': ctx['contract']['split_fingerprint'],
        'data_contract': ctx['contract'],
        'source_sha256': _source_hashes(), 'test_accessed': False,
    }
    _assert_or_write_manifest(out_dir / 'manifest.json', tuning_manifest)
    storage = f"sqlite:///{(out_dir / 'optuna.db').resolve().as_posix()}"

    def objective(trial):
        cfg = copy.deepcopy(base_cfg)
        if kind in ft.GRAPH_MODEL_KINDS:
            cfg['use_edge_features'] = bool(edge_features)
        cfg.update(window=trial.suggest_categorical('window', [12, 18, 24]),
                   dropout=trial.suggest_float('dropout', 0.0, 0.20),
                   lr=trial.suggest_float('lr', 5e-4, 2.5e-3, log=True),
                   weight_decay=trial.suggest_float('weight_decay', 1e-6, 1e-3, log=True),
                   batch_size=trial.suggest_categorical('batch_size', [32, 64, 128]))
        model, _, _, use_edges = _fit_one(kind, cfg, data, ctx['edges_index'], seed, device)
        val_w = data.get(cfg['window'], 'val')
        pred = ft.predict(model, ft.loader(val_w, use_edges, 512, False), device)
        summary, *_ = _metric_tables(pred, val_w, data, score_weights)
        return summary['primary_score']

    study = optuna.create_study(direction='minimize', study_name=study_name, storage=storage,
                                load_if_exists=True, sampler=optuna.samplers.TPESampler(seed=seed),
                                # Every trial must reach the macro per-run/per-scenario objective;
                                # pooled per-epoch MAE is not a compatible pruning signal.
                                pruner=optuna.pruners.NopPruner())
    remaining = max(0, n_trials - len(study.trials))
    if remaining:
        study.optimize(objective, n_trials=remaining, gc_after_trial=True)
    best_cfg = copy.deepcopy(base_cfg)
    best_cfg.update(study.best_params)
    if kind in ft.GRAPH_MODEL_KINDS:
        best_cfg['use_edge_features'] = bool(edge_features)
    _write_json(out_dir / 'best_config.json', best_cfg)
    study.trials_dataframe().to_csv(out_dir / 'optuna_trials.csv', index=False)
    _write_json(out_dir / 'optuna_summary.json', {'study_name': study_name,
                                                  'best_value': study.best_value,
                                                  'best_params': study.best_params,
                                                  'n_trials': len(study.trials),
                                                  'score_weights': score_weights})
    _write_json(out_dir / 'environment.json', _environment(device))
    return best_cfg, study


def run_final(ctx, kind, node_features, edge_features, cfg, out_dir, seeds=(0, 1, 2, 3, 4),
              device='cpu', model_name=None, parent_version='baseline-2026-10-14'):
    """The only ablation helper that evaluates the frozen test split."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_name = model_name or kind
    final_manifest = {
        'schema_version': 1, 'model_name': model_name, 'model_kind': kind,
        'parent_version': parent_version,
        'config': cfg, 'node_features': list(node_features), 'edge_features': list(edge_features),
        'seeds': list(seeds), 'dataset_fingerprint': ctx['contract']['dataset_fingerprint'],
        'split_fingerprint': ctx['contract']['split_fingerprint'],
        'data_contract': ctx['contract'],
        'source_sha256': _source_hashes(), 'test_accessed': True,
    }
    _assert_or_write_manifest(out_dir / 'manifest.json', final_manifest)
    prediction_path = out_dir / f'{model_name}_predictions_test.npz'
    training_meta_path = out_dir / f'{model_name}_training_meta.json'
    if prediction_path.exists() and training_meta_path.exists():
        return prediction_path, json.loads(training_meta_path.read_text(encoding='utf-8'))
    data = ft.Data(ctx['runs'], ctx['splits'], node_features, edge_features, contract=ctx['contract'])
    test_w = data.get(cfg['window'], 'test')
    predictions, models, val_scores, histories, train_seconds = [], [], [], [], []
    for seed in seeds:
        started = time.perf_counter()
        model, best, hist, use_edges = _fit_one(kind, cfg, data, ctx['edges_index'], seed, device)
        train_seconds.append(time.perf_counter() - started)
        pred_scaled = ft.predict(model, ft.loader(test_w, use_edges, 512, False), device)
        predictions.append(fd.invert_scaler(pred_scaled, data.scalers['target']))
        models.append(model)
        val_scores.append(best)
        histories.append(hist.to_dict('records'))
    meta = {
        'model': model_name, 'model_kind': kind, 'config': cfg, 'seeds': list(seeds),
        'val_mae_scaled': val_scores, 'node_features': node_features,
        'edge_features': edge_features if use_edges else [], 'targets': fd.TARGETS,
        'services': fd.SERVICES, 'n_params': ft.n_params(models[0]),
        'inference_ms': ft.inference_latency_ms(models[0], test_w, use_edges, device),
        'scalers': data.scalers, 'data_contract': data.contract, 'histories': histories,
        'epochs_trained': [len(h) for h in histories], 'train_seconds': train_seconds,
        'device': str(device), 'source_sha256': _source_hashes(), 'test_accessed': True,
    }
    fm.save_predictions(prediction_path, np.stack(predictions), test_w['Y_raw'], test_w['Y_last'],
                        test_w['info'], meta)
    for seed, model, hist in zip(seeds, models, histories):
        ft.save_model(out_dir / f'{model_name}_model_seed_{seed}.pt', model, meta)
        pd.DataFrame(hist).to_csv(out_dir / f'{model_name}_history_seed_{seed}.csv', index=False)
    _write_json(training_meta_path, meta)
    _write_json(out_dir / f'{model_name}_best_config.json', cfg)
    _write_json(out_dir / f'{model_name}_environment.json', _environment(device))
    fm.save_evaluation_artifacts(out_dir, model_name, fm.load_predictions(prediction_path))
    _write_json(out_dir / 'status.json', {'status': 'EVALUATED', 'test_accessed': True})
    return prediction_path, meta


def _train_target_thresholds(ctx, quantile=0.90):
    target_idx = [fd.NF.index(t) for t in fd.TARGETS]
    arrays = []
    for run in ctx['runs']:
        labels = fd.split_labels(ctx['splits']['runs'][run['key']], len(run['X']),
                                 ctx['splits'].get('frac'))
        if np.any(labels == 'train'):
            arrays.append(run['X'][labels == 'train'][..., target_idx])
    return np.quantile(np.concatenate(arrays, axis=0), quantile, axis=0)


def _detailed_test_tables(artifact, model_name):
    rows_run, rows_horizon, rows_service = [], [], []
    for si, seed in enumerate(artifact['meta']['seeds']):
        pred = artifact['Y_pred'][si]
        for run_key in sorted(set(artifact['info'][:, 0])):
            mask = artifact['info'][:, 0] == run_key
            scenario = str(artifact['info'][mask][0, 1])
            table = fm.error_table(pred[mask], artifact['Y_true'][mask], fd.TARGETS)
            for target, row in table.iterrows():
                rows_run.append({'model': model_name, 'seed': seed, 'run': run_key,
                                 'scenario': scenario, 'target': target, **row.to_dict()})
        for k, target in enumerate(fd.TARGETS):
            for h, value in enumerate(fm.by_horizon(pred, artifact['Y_true'], k), 1):
                rows_horizon.append({'model': model_name, 'seed': seed, 'target': target,
                                     'horizon_step': h, 'horizon_sec': h * fd.STEP_SEC,
                                     'MAE': float(value)})
            for service, value in fm.by_service(pred, artifact['Y_true'], k, fd.SERVICES).items():
                rows_service.append({'model': model_name, 'seed': seed, 'target': target,
                                     'service': service, 'MAE': float(value)})
    return pd.DataFrame(rows_run), pd.DataFrame(rows_horizon), pd.DataFrame(rows_service)


def _naive_test_predictions(artifact, ctx, seasonal_period=6, moving_average_window=6):
    """Fixed seasonal-naive and moving-average forecasts aligned to saved test windows.

    All forecast values use observations strictly before the first target timestep. The
    MASE denominator is a one-step naive scale computed only from train timesteps.
    """
    runs = {r['key']: r for r in ctx['runs']}
    ti = [fd.NF.index(f) for f in fd.TARGETS]
    truth = artifact['Y_true']
    n, horizon, n_nodes, n_targets = truth.shape
    persistence = np.repeat(artifact['Y_last'][:, None], horizon, axis=1)
    seasonal = np.empty_like(truth)
    moving = np.empty_like(truth)
    train_diffs = []
    for run in ctx['runs']:
        labels = fd.split_labels(ctx['splits']['runs'][run['key']], len(run['X']),
                                 ctx['splits'].get('frac'))
        train = np.flatnonzero(labels == 'train')
        if len(train) > 1:
            adjacent = train[1:][np.diff(train) == 1]
            prior = adjacent - 1
            if len(prior):
                target_values = run['X'][..., ti]
                train_diffs.append(np.abs(target_values[adjacent] - target_values[prior]))
    scales = np.concatenate(train_diffs, axis=0).mean(axis=0) if train_diffs else np.full(
        (n_nodes, n_targets), np.nan, dtype=np.float64)
    for i, (run_key, _scenario, t0) in enumerate(artifact['info']):
        x = runs[str(run_key)]['X']
        t0 = int(t0)
        offsets = np.arange(horizon)
        seasonal_idx = t0 + offsets - seasonal_period
        if seasonal_idx.min() < 0:
            raise ValueError(f'Insufficient history for seasonal naive at {run_key}, t={t0}')
        seasonal[i] = x[seasonal_idx][..., ti]
        start = t0 - moving_average_window
        if start < 0:
            raise ValueError(f'Insufficient history for moving average at {run_key}, t={t0}')
        moving[i] = np.broadcast_to(x[start:t0, ..., ti].mean(axis=0), truth[i].shape)
    valid_scale = np.isfinite(scales) & (scales > 1e-12)
    if not valid_scale.any(axis=0).all():
        raise ValueError('MASE has no positive train scale for at least one target')
    scales = np.where(valid_scale, scales, np.nan)
    return {'persistence': persistence, 'seasonal_naive_6': seasonal,
            'moving_average_6': moving, 'mase_scale': scales}


def _block_bootstrap_scenario_deltas(gat, lstm, block_len=6, n_boot=2000, seed=20261019):
    """Paired LSTM-GAT MAE difference with circular time-block bootstrap within each run.

    Positive values mean GAT-GRU has lower MAE. Intervals describe within-run temporal
    variability, not generalization over independent test runs.
    """
    rng = np.random.default_rng(seed)
    rows = []
    # Keep target-specific effects rather than pooling RPS and CPU units.
    for k, target in enumerate(fd.TARGETS):
        g = np.abs(gat['Y_pred'][..., k] - gat['Y_true'][None, ..., k]).mean(axis=(0, 2, 3))
        l = np.abs(lstm['Y_pred'][..., k] - lstm['Y_true'][None, ..., k]).mean(axis=(0, 2, 3))
        delta = g - l
        for scenario in sorted(set(gat['info'][:, 1])):
            scenario_mask = gat['info'][:, 1] == scenario
            run_indices = []
            for run_key in sorted(set(gat['info'][scenario_mask, 0])):
                ix = np.flatnonzero(scenario_mask & (gat['info'][:, 0] == run_key))
                ix = ix[np.argsort(gat['info'][ix, 2].astype(int))]
                if len(ix):
                    run_indices.append(ix)
            observed = float(np.mean([delta[ix].mean() for ix in run_indices]))
            boot = np.empty(n_boot, dtype=float)
            for b in range(n_boot):
                run_means = []
                for ix in run_indices:
                    sampled = []
                    while len(sampled) < len(ix):
                        max_start = max(1, len(ix) - block_len + 1)
                        start = int(rng.integers(max_start))
                        sampled.extend(ix[start:min(start + block_len, len(ix))].tolist())
                    run_means.append(delta[np.asarray(sampled[:len(ix)], dtype=int)].mean())
                boot[b] = np.mean(run_means)
            rows.append({'scenario': scenario, 'target': target, 'gatgru_minus_lstm_mae': observed,
                         'ci95_low': float(np.quantile(boot, .025)),
                         'ci95_high': float(np.quantile(boot, .975)),
                         'block_len_windows': block_len, 'n_boot': n_boot,
                         'test_runs': len(run_indices)})
    return pd.DataFrame(rows)


def _save_posthoc_test_diagnostics(gat, lstm, ctx, out_dir):
    """Write descriptive diagnostics from already-saved test predictions; never retunes."""
    out_dir = Path(out_dir)
    baselines = _naive_test_predictions(gat, ctx)
    rows = []
    for model, artifact in [('gatgru', gat), ('lstm', lstm)]:
        seed_errors = np.abs(artifact['Y_pred'] - gat['Y_true'][None])
        for scenario in sorted(set(gat['info'][:, 1])):
            mask = gat['info'][:, 1] == scenario
            for k, target in enumerate(fd.TARGETS):
                err = seed_errors[..., k][:, mask].mean(axis=0)
                mase = err / baselines['mase_scale'][None, :, k]
                rows.append({'model': model, 'baseline': 'mean_across_seed_metrics',
                             'scenario': scenario, 'target': target,
                             'MAE': float(err.mean()), 'MASE': float(np.nanmean(mase)),
                             'mase_valid_service_count': int(np.isfinite(baselines['mase_scale'][:, k]).sum())})
    for name in ('persistence', 'seasonal_naive_6', 'moving_average_6'):
        values = baselines[name]
        for scenario in sorted(set(gat['info'][:, 1])):
            mask = gat['info'][:, 1] == scenario
            for k, target in enumerate(fd.TARGETS):
                err = np.abs(values[mask, ..., k] - gat['Y_true'][mask, ..., k])
                mase = err / baselines['mase_scale'][None, None, :, k]
                rows.append({'model': name, 'baseline': name, 'scenario': scenario,
                             'target': target, 'MAE': float(err.mean()), 'MASE': float(np.nanmean(mase)),
                             'mase_valid_service_count': int(np.isfinite(baselines['mase_scale'][:, k]).sum())})
    pd.DataFrame(rows).to_csv(out_dir / 'scenario_mae_and_mase_posthoc.csv', index=False)
    _block_bootstrap_scenario_deltas(gat, lstm).to_csv(
        out_dir / 'scenario_delta_block_bootstrap_posthoc.csv', index=False)
    seed_rows = []
    for target in fd.TARGETS:
        for scenario in sorted(set(gat['info'][:, 1])):
            group = []
            for model, artifact in [('gatgru', gat), ('lstm', lstm)]:
                k = fd.TARGETS.index(target)
                mask = gat['info'][:, 1] == scenario
                values = []
                for si, seed in enumerate(artifact['meta']['seeds']):
                    err = np.abs(artifact['Y_pred'][si, mask, ..., k] -
                                 gat['Y_true'][mask, ..., k])
                    mae = float(err.mean())
                    values.append(mae)
                    seed_rows.append({'model': model, 'target': target, 'scenario': scenario,
                                      'seed': seed, 'MAE': mae})
                group.append(values)
            gat_sd, lstm_sd = float(np.std(group[0], ddof=1)), float(np.std(group[1], ddof=1))
            gat_mean, lstm_mean = float(np.mean(group[0])), float(np.mean(group[1]))
            seed_rows.append({'model': 'paired_effect_summary', 'target': target,
                              'scenario': scenario, 'seed': '', 'MAE': lstm_mean - gat_mean,
                              'gatgru_seed_sd': gat_sd, 'lstm_seed_sd': lstm_sd,
                              'delta_over_max_seed_sd': ((lstm_mean - gat_mean) /
                                                         max(gat_sd, lstm_sd, 1e-12))})
    pd.DataFrame(seed_rows).to_csv(out_dir / 'seed_variability_vs_effect_posthoc.csv', index=False)


def _slice_metrics(artifact, model_name, thresholds):
    rows = []
    true = artifact['Y_true']
    last = artifact['Y_last']
    for si, seed in enumerate(artifact['meta']['seeds']):
        pred = artifact['Y_pred'][si]
        for k, target in enumerate(fd.TARGETS):
            threshold = thresholds[:, k]
            high = true[..., k] >= threshold[None, None, :]
            transition_node = ((last[..., k] <= threshold[None, :]) &
                               (true[..., k].max(axis=1) > threshold[None, :]))
            transition = np.broadcast_to(transition_node[:, None, :], true[..., k].shape)
            for slice_name, mask in [('high_load', high), ('transition_onset', transition)]:
                error = pred[..., k] - true[..., k]
                values = error[mask]
                rows.append({'model': model_name, 'seed': seed, 'target': target,
                             'slice': slice_name, 'n_points': int(mask.sum()),
                             'MAE': float(np.abs(values).mean()) if values.size else np.nan,
                             'under_MAE': float(np.abs(np.minimum(values, 0)).mean()) if values.size else np.nan,
                             'over_MAE': float(np.maximum(values, 0).mean()) if values.size else np.nan})
    return pd.DataFrame(rows)


def _horizon_by_run_table(artifact, model_name):
    """Per-run horizon MAE used to macro-average scenarios before plotting."""
    rows = []
    for si, seed in enumerate(artifact['meta']['seeds']):
        pred = artifact['Y_pred'][si]
        for run_key in sorted(set(artifact['info'][:, 0])):
            mask = artifact['info'][:, 0] == run_key
            scenario = str(artifact['info'][mask][0, 1])
            for k, target in enumerate(fd.TARGETS):
                values = fm.by_horizon(pred[mask], artifact['Y_true'][mask], k)
                for h, value in enumerate(values, 1):
                    rows.append({'model': model_name, 'seed': seed, 'run': run_key,
                                 'scenario': scenario, 'target': target,
                                 'horizon_step': h, 'horizon_sec': h * fd.STEP_SEC,
                                 'MAE': float(value)})
    return pd.DataFrame(rows)


def _plot_horizon_macro(horizon_summary, out_dir):
    """Save one four-model line chart per target from macro scenario metrics."""
    import matplotlib.pyplot as plt

    labels = {'rps_in': ('RPS', 'MAE (requests/s)'),
              'cpu_cores': ('CPU', 'MAE (CPU cores)')}
    colors = plt.get_cmap('tab10')
    for target in fd.TARGETS:
        frame = horizon_summary[horizon_summary['target'] == target]
        if frame.empty:
            continue
        fig, ax = plt.subplots(figsize=(8, 5))
        for index, (model, group) in enumerate(frame.groupby('model', sort=True)):
            group = group.sort_values('horizon_sec')
            x = group['horizon_sec'].to_numpy(dtype=float)
            mean = group['MAE_mean'].to_numpy(dtype=float)
            std = group['MAE_std'].fillna(0.0).to_numpy(dtype=float)
            ax.plot(x, mean, marker='o', linewidth=2, label=model, color=colors(index))
            ax.fill_between(x, mean - std, mean + std, alpha=0.15, color=colors(index))
        title, ylabel = labels[target]
        ax.set_title(f'{title} MAE theo prediction horizon — macro 4 kịch bản')
        ax.set_xlabel('Prediction horizon (giây)')
        ax.set_ylabel(ylabel)
        ax.set_xticks(sorted(frame['horizon_sec'].unique()))
        ax.grid(True, linestyle='--', alpha=0.4)
        ax.legend(title='Mô hình')
        fig.tight_layout()
        fig.savefig(Path(out_dir) / f'mae_by_horizon_{target}.png', dpi=180,
                    bbox_inches='tight')
        plt.close(fig)


def _horizon_gat_effects(horizon_macro_seed, graph_pairs):
    """Paired per-seed GAT effects at each horizon; positive means lower MAE."""
    rows = []
    keys = ['seed', 'target', 'horizon_step', 'horizon_sec']
    for graph_model, node_model in (graph_pairs or []):
        graph = horizon_macro_seed[horizon_macro_seed['model'] == graph_model]
        node = horizon_macro_seed[horizon_macro_seed['model'] == node_model]
        if graph.empty or node.empty:
            continue
        paired = node.merge(
            graph, on=keys, suffixes=('_node_only', '_graph'), validate='one_to_one'
        )
        for row in paired.itertuples(index=False):
            rows.append({
                'pair': f'{graph_model}_vs_{node_model}',
                'graph_model': graph_model,
                'node_only_model': node_model,
                'seed': row.seed,
                'target': row.target,
                'horizon_step': row.horizon_step,
                'horizon_sec': row.horizon_sec,
                'mae_node_only': row.MAE_node_only,
                'mae_graph': row.MAE_graph,
                'mae_improvement': row.MAE_node_only - row.MAE_graph,
                'positive_means_gat_better': True,
            })
    effects = pd.DataFrame(rows)
    if effects.empty:
        return effects, effects.copy()
    summary = effects.groupby(
        ['pair', 'graph_model', 'node_only_model', 'target',
         'horizon_step', 'horizon_sec'], as_index=False
    ).agg(
        mae_improvement_mean=('mae_improvement', 'mean'),
        mae_improvement_std=('mae_improvement', 'std'),
        seed_count=('seed', 'nunique'),
    )
    summary['positive_means_gat_better'] = True
    return effects, summary


def _plot_horizon_pairwise(horizon_summary, effect_summary, out_dir, graph_pairs):
    """Plot matched pairs and paired differences when four lines overlap."""
    import matplotlib.pyplot as plt

    labels = {'rps_in': ('RPS', 'MAE (requests/s)', 'requests/s'),
              'cpu_cores': ('CPU', 'MAE (CPU cores)', 'CPU cores')}
    display_names = {'gru': 'GRU', 'gatgru': 'GAT-GRU',
                     'lstm': 'LSTM', 'gatlstm': 'GAT-LSTM'}
    styles = {
        'gru': dict(color='#4C78A8', linestyle='--', marker='o'),
        'gatgru': dict(color='#174A7E', linestyle='-', marker='s'),
        'lstm': dict(color='#F58518', linestyle='--', marker='o'),
        'gatlstm': dict(color='#A84D00', linestyle='-', marker='s'),
    }
    for target in fd.TARGETS:
        frame = horizon_summary[horizon_summary['target'] == target]
        if frame.empty:
            continue
        title, ylabel, unit = labels[target]
        valid_pairs = [pair for pair in (graph_pairs or [])
                       if set(pair).issubset(set(frame['model']))]
        if valid_pairs:
            fig, axes = plt.subplots(1, len(valid_pairs), figsize=(6.2 * len(valid_pairs), 4.8),
                                     squeeze=False)
            for ax, (graph_model, node_model) in zip(axes[0], valid_pairs):
                for model in (node_model, graph_model):
                    group = frame[frame['model'] == model].sort_values('horizon_sec')
                    x = group['horizon_sec'].to_numpy(dtype=float)
                    mean = group['MAE_mean'].to_numpy(dtype=float)
                    std = group['MAE_std'].fillna(0.0).to_numpy(dtype=float)
                    style = styles.get(model, dict(color='#555555', linestyle='-', marker='o'))
                    ax.plot(x, mean, linewidth=2.4, label=display_names.get(model, model), **style)
                    ax.fill_between(x, mean - std, mean + std, alpha=0.10,
                                    color=style['color'])
                ax.set_title(f'{display_names.get(node_model, node_model)} vs '
                             f'{display_names.get(graph_model, graph_model)}')
                ax.set_xlabel('Prediction horizon (seconds)')
                ax.set_ylabel(ylabel)
                ax.set_xticks(sorted(frame['horizon_sec'].unique()))
                ax.grid(True, linestyle='--', alpha=0.4)
                ax.legend()
                ax.margins(y=0.10)
            fig.suptitle(f'{title} MAE - matched recurrent-cell comparisons', y=1.02)
            fig.tight_layout()
            fig.savefig(Path(out_dir) / f'mae_by_horizon_{target}_paired.png', dpi=180,
                        bbox_inches='tight')
            plt.close(fig)

        target_effect = effect_summary[
            effect_summary['target'] == target
        ] if not effect_summary.empty else effect_summary
        if target_effect.empty:
            continue
        fig, ax = plt.subplots(figsize=(8, 5))
        for index, (_, group) in enumerate(target_effect.groupby('pair', sort=False)):
            group = group.sort_values('horizon_sec')
            x = group['horizon_sec'].to_numpy(dtype=float)
            mean = group['mae_improvement_mean'].to_numpy(dtype=float)
            std = group['mae_improvement_std'].fillna(0.0).to_numpy(dtype=float)
            graph_model = group['graph_model'].iloc[0]
            node_model = group['node_only_model'].iloc[0]
            color = styles.get(graph_model, {}).get('color', plt.get_cmap('tab10')(index))
            pair_label = (f'{display_names.get(node_model, node_model)} - '
                          f'{display_names.get(graph_model, graph_model)}')
            ax.plot(x, mean, marker='o', linewidth=2.4, color=color, label=pair_label)
            ax.fill_between(x, mean - std, mean + std, alpha=0.12, color=color)
        ax.axhline(0.0, color='black', linewidth=1.2, linestyle=':')
        ax.set_title(f'GAT effect on {title} MAE by prediction horizon')
        ax.set_xlabel('Prediction horizon (seconds)')
        ax.set_ylabel(f'MAE reduction ({unit})')
        ax.set_xticks(sorted(target_effect['horizon_sec'].unique()))
        ax.grid(True, linestyle='--', alpha=0.4)
        ax.legend(title='Positive = GAT better')
        ax.margins(y=0.12)
        fig.tight_layout()
        fig.savefig(Path(out_dir) / f'gat_effect_by_horizon_{target}.png', dpi=180,
                    bbox_inches='tight')
        plt.close(fig)


def compare_model_predictions(prediction_paths, out_dir, graph_pairs=None, make_plots=True):
    """Compare multiple final models on one aligned test sample intersection.

    ``prediction_paths`` maps report labels to saved NPZ artifacts. ``graph_pairs``
    contains ``(graph_model, node_only_model)`` pairs. Positive reported effects
    mean that adding GAT reduces MAE. All macro results average runs within a
    scenario first and then give every scenario equal weight.
    """
    if len(prediction_paths) < 2:
        raise ValueError('At least two models are required for comparison')
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {name: fm.load_predictions(path) for name, path in prediction_paths.items()}
    artifacts = fm.assert_compatible_prediction_set(
        artifacts, require_same_raw_samples=False
    )
    reference_name = next(iter(artifacts))
    expected_scenarios = {'mmpp', 'nhpp', 'onoff', 'pareto'}
    observed_scenarios = set(artifacts[reference_name]['info'][:, 1])
    if observed_scenarios != expected_scenarios:
        raise ValueError(
            f'Four-scenario comparison requires {sorted(expected_scenarios)}, '
            f'found {sorted(observed_scenarios)}'
        )
    reference_seeds = list(artifacts[reference_name]['meta']['seeds'])
    for name, artifact in artifacts.items():
        if list(artifact['meta']['seeds']) != reference_seeds:
            raise ValueError(f'{name} uses different final seeds; paired comparison is invalid')

    metric_rows, detailed, horizon_runs = [], [], []
    for name, artifact in artifacts.items():
        for si, seed in enumerate(artifact['meta']['seeds']):
            table = fm.error_table(artifact['Y_pred'][si], artifact['Y_true'], fd.TARGETS)
            for target, row in table.iterrows():
                metric_rows.append({'model': name, 'seed': seed, 'target': target,
                                    **row.to_dict()})
        detailed.append(_detailed_test_tables(artifact, name))
        horizon_runs.append(_horizon_by_run_table(artifact, name))

    metrics = pd.DataFrame(metric_rows)
    metrics.to_csv(out_dir / 'metrics_by_seed.csv', index=False)
    summary = metrics.groupby(['model', 'target']).agg(
        MAE_mean=('MAE', 'mean'), MAE_std=('MAE', 'std'),
        RMSE_mean=('RMSE', 'mean'), RMSE_std=('RMSE', 'std'),
        under_MAE_mean=('under_MAE', 'mean'), over_MAE_mean=('over_MAE', 'mean'),
    ).reset_index()
    summary.to_csv(out_dir / 'summary.csv', index=False)

    by_run = pd.concat([item[0] for item in detailed], ignore_index=True)
    by_horizon = pd.concat([item[1] for item in detailed], ignore_index=True)
    by_service = pd.concat([item[2] for item in detailed], ignore_index=True)
    by_run.to_csv(out_dir / 'metrics_by_run.csv', index=False)
    by_horizon.to_csv(out_dir / 'metrics_by_horizon.csv', index=False)
    by_service.to_csv(out_dir / 'metrics_by_service.csv', index=False)

    metric_cols = ['MAE', 'RMSE', 'under_MAE', 'over_MAE']
    by_scenario = by_run.groupby(
        ['model', 'seed', 'scenario', 'target'], as_index=False
    )[metric_cols].mean()
    macro_by_seed = by_scenario.groupby(
        ['model', 'seed', 'target'], as_index=False
    )[metric_cols].mean()
    macro_by_seed.to_csv(out_dir / 'metrics_macro_by_seed.csv', index=False)
    macro_summary = macro_by_seed.groupby(['model', 'target']).agg(
        MAE_mean=('MAE', 'mean'), MAE_std=('MAE', 'std'),
        RMSE_mean=('RMSE', 'mean'), RMSE_std=('RMSE', 'std'),
        under_MAE_mean=('under_MAE', 'mean'), under_MAE_std=('under_MAE', 'std'),
        over_MAE_mean=('over_MAE', 'mean'), over_MAE_std=('over_MAE', 'std'),
    ).reset_index()
    macro_summary.to_csv(out_dir / 'summary_macro_four_scenarios.csv', index=False)

    horizon_by_run = pd.concat(horizon_runs, ignore_index=True)
    horizon_by_run.to_csv(out_dir / 'metrics_by_horizon_run.csv', index=False)
    horizon_by_scenario = horizon_by_run.groupby(
        ['model', 'seed', 'scenario', 'target', 'horizon_step', 'horizon_sec'],
        as_index=False,
    )['MAE'].mean()
    horizon_macro_seed = horizon_by_scenario.groupby(
        ['model', 'seed', 'target', 'horizon_step', 'horizon_sec'], as_index=False
    )['MAE'].mean()
    horizon_macro_seed.to_csv(out_dir / 'metrics_by_horizon_macro_seed.csv', index=False)
    horizon_summary = horizon_macro_seed.groupby(
        ['model', 'target', 'horizon_step', 'horizon_sec']
    ).agg(MAE_mean=('MAE', 'mean'), MAE_std=('MAE', 'std')).reset_index()
    horizon_summary.to_csv(out_dir / 'metrics_by_horizon_macro.csv', index=False)
    horizon_effects, horizon_effect_summary = _horizon_gat_effects(
        horizon_macro_seed, graph_pairs
    )
    horizon_effects.to_csv(out_dir / 'gat_effects_by_horizon_seed.csv', index=False)
    horizon_effect_summary.to_csv(out_dir / 'gat_effects_by_horizon.csv', index=False)
    if make_plots:
        _plot_horizon_macro(horizon_summary, out_dir)
        _plot_horizon_pairwise(horizon_summary, horizon_effect_summary, out_dir, graph_pairs)

    effect_seed_rows, effect_run_rows, effect_summary_rows, sign_rows = [], [], [], []
    run_mean = by_run.groupby(
        ['model', 'run', 'scenario', 'target'], as_index=False
    )[metric_cols].mean()
    for graph_model, node_model in (graph_pairs or []):
        if graph_model not in artifacts or node_model not in artifacts:
            raise ValueError(f'Unknown graph pair: {graph_model}/{node_model}')
        pair_name = f'{graph_model}_vs_{node_model}'
        graph_seed = macro_by_seed[macro_by_seed.model == graph_model]
        node_seed = macro_by_seed[macro_by_seed.model == node_model]
        seed_pair = node_seed.merge(
            graph_seed, on=['seed', 'target'], suffixes=('_node_only', '_graph'),
            validate='one_to_one'
        )
        for row in seed_pair.itertuples(index=False):
            effect_seed_rows.append({
                'pair': pair_name, 'graph_model': graph_model, 'node_only_model': node_model,
                'seed': row.seed, 'target': row.target,
                'mae_improvement': row.MAE_node_only - row.MAE_graph,
                'positive_means_gat_better': True,
            })

        graph_run = run_mean[run_mean.model == graph_model]
        node_run = run_mean[run_mean.model == node_model]
        run_pair = node_run.merge(
            graph_run, on=['run', 'scenario', 'target'], suffixes=('_node_only', '_graph'),
            validate='one_to_one'
        )
        for row in run_pair.itertuples(index=False):
            effect_run_rows.append({
                'pair': pair_name, 'graph_model': graph_model, 'node_only_model': node_model,
                'run': row.run, 'scenario': row.scenario, 'target': row.target,
                'mae_improvement': row.MAE_node_only - row.MAE_graph,
                'positive_means_gat_better': True,
            })
        for target, group in run_pair.groupby('target'):
            improvements = group['MAE_node_only'] - group['MAE_graph']
            wins = int((improvements > 0).sum())
            sign_rows.append({
                'pair': pair_name, 'target': target, 'gat_wins': wins,
                'test_run_pairs': len(group), 'sign_test_p': fm.sign_test(wins, len(group)),
                'mean_node_only_minus_graph_mae': float(improvements.mean()),
            })

        for target, group in seed_pair.groupby('target'):
            improvements = group['MAE_node_only'] - group['MAE_graph']
            node_mean = float(group['MAE_node_only'].mean())
            effect_summary_rows.append({
                'pair': pair_name, 'graph_model': graph_model, 'node_only_model': node_model,
                'target': target, 'mae_improvement_mean': float(improvements.mean()),
                'mae_improvement_std_across_seeds': float(improvements.std(ddof=1)),
                'relative_mae_improvement_pct': (100.0 * float(improvements.mean()) / node_mean
                                                 if node_mean else np.nan),
                'positive_means_gat_better': True,
            })

    pd.DataFrame(effect_seed_rows).to_csv(out_dir / 'gat_effects_by_seed.csv', index=False)
    pd.DataFrame(effect_run_rows).to_csv(out_dir / 'gat_effects_by_test_run.csv', index=False)
    pd.DataFrame(effect_summary_rows).to_csv(out_dir / 'gat_effect_summary.csv', index=False)
    pd.DataFrame(sign_rows).to_csv(out_dir / 'paired_run_sign_test.csv', index=False)

    contract = artifacts[reference_name]['meta']['data_contract']
    _write_json(out_dir / 'comparison_contract.json', {
        'status': 'EVALUATED',
        'prediction_paths': {name: str(Path(path).resolve())
                             for name, path in prediction_paths.items()},
        'models': list(prediction_paths), 'graph_pairs': list(graph_pairs or []),
        'common_windows': len(artifacts[reference_name]['info']),
        'final_seeds': reference_seeds,
        'aggregation': 'run -> scenario -> equal-weight scenario macro',
        'node_features': contract['node_features'],
        'dataset_fingerprint': contract['dataset_fingerprint'],
        'split_fingerprint': contract['split_fingerprint'],
    })
    return metrics, macro_summary


def compare_final_predictions(gat_path, lstm_path, out_dir, ctx=None):
    """Backward-compatible two-model comparison plus historical post-hoc diagnostics."""
    out_dir = Path(out_dir)
    metrics, _macro_summary = compare_model_predictions(
        {'gatgru': gat_path, 'lstm': lstm_path}, out_dir, graph_pairs=[], make_plots=True
    )
    summary = pd.read_csv(out_dir / 'summary.csv')
    by_run = pd.read_csv(out_dir / 'metrics_by_run.csv')
    mean_run = by_run.groupby(['model', 'run', 'scenario', 'target'], as_index=False)['MAE'].mean()
    paired = mean_run.pivot(index=['run', 'scenario', 'target'], columns='model', values='MAE').reset_index()
    paired['lstm_minus_gatgru_mae'] = paired['lstm'] - paired['gatgru']
    paired.to_csv(out_dir / 'paired_run_effects.csv', index=False)
    sign_rows = []
    for target, group in paired.groupby('target'):
        wins = int((group['lstm_minus_gatgru_mae'] > 0).sum())
        sign_rows.append({'target': target, 'gatgru_wins': wins, 'test_run_pairs': len(group),
                          'sign_test_p': fm.sign_test(wins, len(group)),
                          'mean_lstm_minus_gatgru_mae': float(group['lstm_minus_gatgru_mae'].mean())})
    pd.DataFrame(sign_rows).to_csv(out_dir / 'paired_run_sign_test.csv', index=False)

    if ctx is not None:
        aligned = fm.assert_compatible_prediction_set(
            {'gatgru': fm.load_predictions(gat_path), 'lstm': fm.load_predictions(lstm_path)},
            require_same_raw_samples=False,
        )
        gat, lstm = aligned['gatgru'], aligned['lstm']
        thresholds = _train_target_thresholds(ctx)
        threshold_rows = [{'service': service, 'target': target, 'train_q90': float(thresholds[n, k])}
                          for n, service in enumerate(fd.SERVICES)
                          for k, target in enumerate(fd.TARGETS)]
        pd.DataFrame(threshold_rows).to_csv(out_dir / 'train_high_load_thresholds.csv', index=False)
        slices = pd.concat([_slice_metrics(gat, 'gatgru', thresholds),
                            _slice_metrics(lstm, 'lstm', thresholds)], ignore_index=True)
        slices.to_csv(out_dir / 'diagnostic_high_load_transition.csv', index=False)
        _save_posthoc_test_diagnostics(gat, lstm, ctx, out_dir)
    contract_path = out_dir / 'comparison_contract.json'
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    contract.update(gat_path=str(Path(gat_path).resolve()),
                    lstm_path=str(Path(lstm_path).resolve()))
    _write_json(contract_path, contract)
    return metrics, summary


def summarize_validation_evidence(graph_csv, edge_csv, controls_csv, out_dir):
    """Convert validation variant scores into positive-is-better effect sizes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = pd.read_csv(graph_csv).set_index('variant')
    edge = pd.read_csv(edge_csv)
    controls_path = Path(controls_csv) if controls_csv is not None else None
    controls = (pd.read_csv(controls_path).set_index('variant')
                if controls_path is not None and controls_path.exists() else None)
    score_cols = ['primary_score_mean', 'cpu_weighted_diagnostic_mean',
                  'rps_in_mae_macro_mean', 'cpu_cores_mae_macro_mean']

    graph_rows = []
    for label, worse, better in [
            ('topology_static_vs_node_only', 'gru_node_only', 'static_gat'),
            ('correct_vs_shuffled_topology', 'static_gat_shuffled_topology', 'static_gat'),
            ('dynamic_edges_vs_static', 'static_gat', 'dynamic_all_edges')]:
        row = {'effect': label, 'positive_means_better': better}
        for col in score_cols:
            row[col.replace('_mean', '') + '_improvement'] = float(graph.loc[worse, col] - graph.loc[better, col])
        graph_rows.append(row)
    graph_effects = pd.DataFrame(graph_rows)
    graph_effects.to_csv(out_dir / 'graph_effects.csv', index=False)

    # Summarize all topology controls against the true static graph. Multiple shuffle
    # permutations are retained as separate rows before their distribution is reported.
    topology_rows = []
    baseline = graph.loc['static_gat']
    controlled = graph[graph.index.str.startswith('static_gat_shuffled_topology') |
                       graph.index.isin(['static_gat_full_directed', 'static_gat_self_loops_only',
                                         'static_gat_random_same_density'])]
    for variant, control in controlled.iterrows():
        row = {'variant': variant}
        for col in score_cols:
            # For a loss, positive means the true topology is better than the control.
            row[col.replace('_mean', '') + '_control_minus_real'] = float(control[col] - baseline[col])
        topology_rows.append(row)
    topology_effects = pd.DataFrame(topology_rows)
    topology_effects.to_csv(out_dir / 'topology_control_effects.csv', index=False)
    shuffle_rows = topology_effects[topology_effects['variant'].str.startswith(
        'static_gat_shuffled_topology')]
    if not shuffle_rows.empty:
        shuffle_summary = []
        for col in [c for c in topology_effects if c.endswith('_control_minus_real')]:
            vals = shuffle_rows[col]
            shuffle_summary.append({'metric': col, 'n_shuffles': int(vals.notna().sum()),
                                    'mean_control_minus_real': float(vals.mean()),
                                    'std_across_shuffles': float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
                                    'min_real_minus_shuffle': float(vals.min()),
                                    'max_real_minus_shuffle': float(vals.max())})
        pd.DataFrame(shuffle_summary).to_csv(out_dir / 'topology_shuffle_summary.csv', index=False)

    def parse_edge_set(name):
        return frozenset() if name == 'edge_none' else frozenset(name.removeprefix('edge_').split('__'))

    edge_sets = {row.variant: parse_edge_set(row.variant) for row in edge.itertuples()}
    edge_lookup = {features: edge[edge.variant == name].iloc[0]
                   for name, features in edge_sets.items()}
    main_rows = []
    for feature in EDGE_FEATURES:
        for other in itertools.chain.from_iterable(
                itertools.combinations([f for f in EDGE_FEATURES if f != feature], r)
                for r in range(len(EDGE_FEATURES))):
            absent = frozenset(other)
            present = absent | {feature}
            if absent not in edge_lookup or present not in edge_lookup:
                continue
            row = {'feature': feature, 'conditioning_edges': '__'.join(sorted(absent)) or 'none'}
            for col in score_cols:
                row[col.replace('_mean', '') + '_improvement'] = float(edge_lookup[absent][col] -
                                                                        edge_lookup[present][col])
            main_rows.append(row)
    edge_effects = pd.DataFrame(main_rows)
    edge_effects.to_csv(out_dir / 'edge_factorial_effects.csv', index=False)
    edge_effects.groupby('feature', as_index=False).mean(numeric_only=True).to_csv(
        out_dir / 'edge_main_effects.csv', index=False)

    control_rows = []
    if controls is not None:
        real = controls.loc['control_real']
        for name, row in controls.drop(index='control_real').iterrows():
            result = {'control': name, 'positive_means_real_edges_better': True}
            for col in score_cols:
                result[col.replace('_mean', '') + '_real_improvement'] = float(row[col] - real[col])
            control_rows.append(result)
    control_effects = pd.DataFrame(control_rows)
    control_effects.to_csv(out_dir / 'edge_control_effects.csv', index=False)
    return graph_effects, edge_effects, control_effects

