"""
forecast_data.py - data pipeline shared by the GAT-GRU and LSTM forecasters.

Both models must see exactly the same runs, features, transforms, splits and
windows; the only intended difference is the graph (edges + message passing).
Keeping this logic in one module (instead of copying it into each notebook)
is what guarantees that.

No deep-learning dependency here (numpy/pandas only).
"""
import json
import hashlib
import math
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- constants
STEP_SEC = 10
WARMUP_DROP = 6
HORIZON = 6            # forecast t+1..t+6 (60s) - fixed for every model, not tuned
LATENCY_SLO_MS = 300.0
ERROR_RATE_SLO = 0.01

SERVICES = [
    'frontend', 'adservice', 'cartservice', 'checkoutservice', 'currencyservice',
    'emailservice', 'paymentservice', 'productcatalogservice', 'recommendationservice',
    'redis-cart', 'shippingservice',
]
SVC_IDX = {s: i for i, s in enumerate(SERVICES)}
STATIC_EDGES = [
    ('frontend', 'productcatalogservice'), ('frontend', 'currencyservice'),
    ('frontend', 'cartservice'), ('frontend', 'recommendationservice'),
    ('frontend', 'shippingservice'), ('frontend', 'checkoutservice'),
    ('frontend', 'adservice'), ('recommendationservice', 'productcatalogservice'),
    ('checkoutservice', 'paymentservice'), ('checkoutservice', 'productcatalogservice'),
    ('checkoutservice', 'shippingservice'), ('checkoutservice', 'emailservice'),
    ('checkoutservice', 'currencyservice'), ('checkoutservice', 'cartservice'),
    ('cartservice', 'redis-cart'),
]

# name: (group, transform). 'log1p' non-negative heavy-tailed, 'slog1p' signed, 'none' ratios/counts.
NODE_FEATURES = {
    'rps_in': ('load', 'log1p'), 'rps_in_delta': ('load', 'slog1p'), 'rps_per_replica': ('load', 'log1p'),
    'latency_p50_ms': ('perf', 'log1p'), 'latency_p95_ms': ('perf', 'log1p'), 'error_rate': ('perf', 'none'),
    'cpu_cores': ('resource', 'log1p'), 'cpu_util_request': ('resource', 'log1p'),
    'cpu_throttle_ratio': ('resource', 'none'), 'cpu_sidecar_cores': ('resource', 'log1p'),
    'mem_mib': ('resource', 'log1p'), 'net_rx_kBps': ('resource', 'log1p'), 'net_tx_kBps': ('resource', 'log1p'),
    'replicas': ('capacity', 'none'), 'restarts_delta': ('capacity', 'none'),
}
EDGE_FEATURES = {'call_rate': 'log1p', 'call_ratio': 'log1p', 'edge_error_rate': 'none',
                 'edge_latency_p95_ms': 'log1p'}
GRAPH_FEATURES = {'e2e_rps': 'log1p', 'e2e_latency_p95_ms': 'log1p', 'e2e_error_rate': 'none',
                  'slo_violation': 'none', 'total_replicas': 'none'}
NF, EF, GF = list(NODE_FEATURES), list(EDGE_FEATURES), list(GRAPH_FEATURES)
TARGETS = ['rps_in', 'cpu_cores']
# Never removed by feature selection: autoregressive targets + the variable PPO controls.
FORCED_FEATURES = ['rps_in', 'cpu_cores', 'replicas']


def _sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _raw_file_hashes(run):
    if run.get('raw_file_hashes'):
        return run['raw_file_hashes']
    if run.get('_raw_file_hashes'):
        return run['_raw_file_hashes']
    directory = run.get('dir')
    if directory is None:
        return None
    names = ('meta.json', 'node_metrics.csv', 'service_metrics.csv', 'edge_metrics.csv')
    hashes = {name: _sha256_file(Path(directory) / name)
              for name in names if (Path(directory) / name).exists()}
    run['_raw_file_hashes'] = hashes
    return hashes


def run_manifest(runs):
    """Stable, JSON-serialisable identity of the raw runs used by a model pipeline.

    The manifest contains acquisition identity/interval metadata and SHA-256 hashes
    of the raw model inputs. It catches replaced, added, removed or renamed runs
    before an old frozen split can silently be reused.
    """
    rows = []
    for r in sorted(runs, key=lambda x: x['key']):
        m = r.get('meta', {})
        rows.append({
            'key': r['key'],
            'scenario': r.get('scenario', m.get('scenario')),
            'run_id': r.get('run_id', m.get('run_id')),
            'source_run_id': r.get('source_run_id', m.get('run_id')),
            'start': m.get('start', r.get('start')),
            'end': m.get('end', r.get('end')),
            'duration_sec': m.get('duration_sec', r.get('duration_sec')),
            'step_sec': m.get('step_sec', r.get('step_sec', STEP_SEC)),
            'seed': m.get('seed', r.get('seed')),
            'autoscaler': m.get('autoscaler', r.get('autoscaler', 'none')),
            'raw_file_hashes': _raw_file_hashes(r),
        })
    return rows


def manifest_fingerprint(manifest):
    payload = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def data_contract(runs, splits, node_features=None, targets=None):
    """Contract shared by LSTM/GRU/GAT-GRU preprocessing and prediction artifacts."""
    manifest = run_manifest(runs)
    return {
        'schema_version': 1,
        'dataset_fingerprint': manifest_fingerprint(manifest),
        'split_fingerprint': split_fingerprint(splits),
        'run_keys': [r['key'] for r in manifest],
        'services': list(SERVICES),
        'node_features': list(node_features or NF),
        'targets': list(targets or TARGETS),
        'step_sec': STEP_SEC,
        'horizon': HORIZON,
        'warmup_drop': WARMUP_DROP,
    }


def assert_contract_equal(left, right, fields=None, context='data contracts'):
    fields = fields or ('dataset_fingerprint', 'split_fingerprint', 'run_keys', 'services',
                        'node_features', 'targets', 'step_sec', 'horizon', 'warmup_drop')
    mismatches = [f for f in fields if left.get(f) != right.get(f)]
    if mismatches:
        details = {f: {'left': left.get(f), 'right': right.get(f)} for f in mismatches}
        raise ValueError(f'{context} do not match: {json.dumps(details, default=str)}')
    return True


# --------------------------------------------------------------------------- loading
def _read(path, **kw):
    return pd.read_csv(path, **kw) if path.exists() and path.stat().st_size > 0 else pd.DataFrame()


def load_runs(sources):
    """sources: {label: results_dir}. Run key = '<label>/<scenario>/<run_id>' (unique across sources)."""
    runs = []
    for label, root in sources.items():
        for meta_path in sorted(Path(root).glob('*/run_*/meta.json')):
            d = meta_path.parent
            meta = json.loads(meta_path.read_text())
            # The directory name is the canonical dataset identity used by quality
            # artifacts and frozen splits. Keep a differing historical meta.run_id
            # as provenance, but never let it silently rename a run key.
            run_id = d.name
            source_run_id = meta.get('run_id', run_id)
            loc = _read(d / 'locust_stats_history.csv')
            if not loc.empty and 'Name' in loc:
                loc = loc[loc['Name'] == 'Aggregated']
            report = d / 'collect_report.json'
            runs.append({
                'key': f"{label}/{meta['scenario']}/{run_id}",
                'source': label, 'scenario': meta['scenario'], 'run_id': run_id,
                'source_run_id': source_run_id,
                'dir': d, 'meta': meta, 'node': _read(d / 'node_metrics.csv'),
                'svc': _read(d / 'service_metrics.csv'), 'edge': _read(d / 'edge_metrics.csv'),
                'locust': loc, 'report': json.loads(report.read_text()) if report.exists() else {},
            })
    return runs


def _to_grid(df, start, col='timestamp'):
    out = df.copy()
    out['t'] = ((out[col] - start) // STEP_SEC).astype(int)
    return out


def prepare_runs(runs):
    """Align every run to the 10s grid (t = 0..T-1) and compute data-quality indicators."""
    rows = []
    for r in runs:
        m = r['meta']
        start = m['start'] - m['start'] % STEP_SEC
        r['T'] = int(math.ceil((m['end'] - start) / STEP_SEC))
        for k in ('node', 'svc', 'edge'):
            if not r[k].empty:
                r[k] = _to_grid(r[k], start)
                r[k] = r[k][(r[k].t >= 0) & (r[k].t < r['T'])]
        loc = r['locust']
        if not loc.empty:
            loc = loc.assign(t=((loc['Timestamp'] - start) // STEP_SEC).astype(int))
            r['locust_grid'] = loc.groupby('t').agg(users=('User Count', 'mean'),
                                                   locust_rps=('Requests/s', 'mean'))
        else:
            r['locust_grid'] = pd.DataFrame()
        prom_cov = (r['node'].dropna(subset=['cpu_cores']).groupby(['t', 'service']).ngroups
                    / (r['T'] * len(SERVICES))) if not r['node'].empty else 0.0
        fe = r['svc'][r['svc'].service == 'frontend'] if not r['svc'].empty else pd.DataFrame()
        traced = fe['request_count'].sum() if not fe.empty else 0
        sent = r['locust_grid']['locust_rps'].sum() * STEP_SEC if not r['locust_grid'].empty else np.nan
        r['jaeger_coverage'] = traced / sent if sent and sent > 0 else np.nan
        rows.append({'key': r['key'], 'scenario': r['scenario'], 'autoscaler': m.get('autoscaler', 'none'),
                     'T': r['T'], 'prometheus_coverage': round(prom_cov, 3),
                     'jaeger_coverage': round(float(r['jaeger_coverage']), 3),
                     'has_edges': not r['edge'].empty,
                     'truncated_windows': r['report'].get('jaeger_truncated_windows', 0)})
    return pd.DataFrame(rows)


def discover_edges(runs):
    """Static topology + any extra caller->callee pair seen in traces."""
    observed = set()
    for r in runs:
        if not r['edge'].empty:
            g = r['edge'].groupby(['source', 'target'])['call_count'].sum()
            observed |= {e for e, n in g.items()
                         if n > 0 and e[0] in SVC_IDX and e[1] in SVC_IDX and e[0] != e[1]}
    return list(STATIC_EDGES) + sorted(observed - set(STATIC_EDGES))


# --------------------------------------------------------------------------- features
def build_run_arrays(r, edges, correct_coverage=False):
    """Raw (untransformed) arrays of one run, warm-up removed.
    Returns X [T,N,|NF|], E [T,M,|EF|], G [T,|GF|], tidy DataFrame."""
    T, N, M = r['T'], len(SERVICES), len(edges)
    edge_idx = {e: i for i, e in enumerate(edges)}
    full = pd.MultiIndex.from_product([range(T), SERVICES], names=['t', 'service'])

    node = (r['node'].groupby(['t', 'service']).mean(numeric_only=True).reindex(full)
            if not r['node'].empty else pd.DataFrame(index=full))
    for c in ['cpu_cores', 'cpu_sidecar_cores', 'cpu_throttle_ratio', 'mem_bytes', 'net_rx_bps', 'net_tx_bps',
              'cpu_request_cores', 'cpu_limit_cores', 'restarts_total', 'replicas', 'replicas_desired']:
        if c not in node:
            node[c] = np.nan
    gauges = ['mem_bytes', 'cpu_request_cores', 'cpu_limit_cores', 'restarts_total', 'replicas', 'replicas_desired']
    # Causal imputation only: a value at t may depend on observations at <= t,
    # never on a later sample. Initial/long gaps use the explicit defaults below.
    node[gauges] = node[gauges].groupby(level='service').ffill(limit=3)
    node = node.fillna({'replicas': 1}).fillna(0.0)

    svc_cols = ['request_count', 'error_count', 'latency_p50_ms', 'latency_p95_ms']
    if r['svc'].empty:
        svc = pd.DataFrame(0.0, index=full, columns=svc_cols)
    else:
        g = r['svc'].groupby(['t', 'service'])
        svc = g[['request_count', 'error_count']].sum().join(g[['latency_p50_ms', 'latency_p95_ms']].max())
        svc = svc.reindex(full).astype(float).fillna(0.0)

    cov = 1.0
    if correct_coverage and pd.notna(r['jaeger_coverage']) and r['jaeger_coverage'] > 0:
        cov = r['jaeger_coverage']

    df = pd.DataFrame(index=full)
    df['rps_in'] = svc['request_count'] / STEP_SEC / cov
    df['rps_in_delta'] = df['rps_in'].groupby(level='service').diff().fillna(0.0)
    df['replicas'] = node['replicas'].clip(lower=1)
    df['rps_per_replica'] = df['rps_in'] / df['replicas']
    df['latency_p50_ms'] = svc['latency_p50_ms']
    df['latency_p95_ms'] = svc['latency_p95_ms']
    df['error_rate'] = (svc['error_count'] / svc['request_count'].where(svc['request_count'] > 0)).fillna(0).clip(0, 1)
    df['cpu_cores'] = node['cpu_cores']
    df['cpu_util_request'] = (node['cpu_cores'] / node['cpu_request_cores'].where(node['cpu_request_cores'] > 0)).fillna(0)
    df['cpu_throttle_ratio'] = node['cpu_throttle_ratio'].clip(0, 1)
    df['cpu_sidecar_cores'] = node['cpu_sidecar_cores']
    df['mem_mib'] = node['mem_bytes'] / 2 ** 20
    df['net_rx_kBps'] = node['net_rx_bps'] / 1024
    df['net_tx_kBps'] = node['net_tx_bps'] / 1024
    df['restarts_delta'] = node['restarts_total'].groupby(level='service').diff().fillna(0).clip(lower=0)

    X = df[NF].to_numpy(dtype=np.float32).reshape(T, N, len(NF))

    E = np.zeros((T, M, len(EF)), dtype=np.float32)
    if not r['edge'].empty and M:
        rps = df['rps_in'].unstack('service')
        for row in r['edge'].itertuples(index=False):
            k = edge_idx.get((row.source, row.target))
            if k is None:
                continue
            calls = row.call_count / STEP_SEC / cov
            src_rps = rps.at[row.t, row.source]
            E[row.t, k] = [calls, calls / src_rps if src_rps > 0 else 0.0,
                           row.error_count / row.call_count if row.call_count else 0.0,
                           0.0 if pd.isna(row.latency_p95_ms) else row.latency_p95_ms]

    fe = df.xs('frontend', level='service')
    G = pd.DataFrame({
        'e2e_rps': fe['rps_in'], 'e2e_latency_p95_ms': fe['latency_p95_ms'], 'e2e_error_rate': fe['error_rate'],
        'slo_violation': ((fe['latency_p95_ms'] > LATENCY_SLO_MS) | (fe['error_rate'] > ERROR_RATE_SLO)).astype(float),
        'total_replicas': df['replicas'].groupby(level='t').sum(),
    })[GF].to_numpy(dtype=np.float32)

    tidy = df.reset_index().assign(key=r['key'], scenario=r['scenario'],
                                   autoscaler=r['meta'].get('autoscaler', 'none'))
    tidy = tidy[tidy.t >= WARMUP_DROP]
    keep = slice(WARMUP_DROP, T)
    return X[keep], E[keep], G[keep], tidy


# --------------------------------------------------------------------------- splits
def time_labels(T, frac):
    a, b = int(T * frac['train']), int(T * (frac['train'] + frac['val']))
    return np.array(['train'] * a + ['val'] * (b - a) + ['test'] * (T - b), dtype=object)


def create_splits(runs, mode='by_run', frac=None, test_scenarios=('spike',), seed=42):
    """{run key: 'train'|'val'|'test'|'time'}; 'time' = split inside the run (scenario has < 3 runs)."""
    frac = frac or {'train': 0.70, 'val': 0.15, 'test': 0.15}
    rng = np.random.default_rng(seed)
    by_scen = {}
    for r in runs:
        by_scen.setdefault(r['scenario'], []).append(r['key'])
    labels = {}
    for sc, keys in sorted(by_scen.items()):
        keys = sorted(keys)
        if mode == 'by_scenario':
            if sc in test_scenarios:
                labels.update({k: 'test' for k in keys})
            else:
                n_val = max(1, round(len(keys) * frac['val'])) if len(keys) >= 2 else 0
                for i, k in enumerate(keys):
                    labels[k] = 'val' if i >= len(keys) - n_val else 'train'
        elif len(keys) >= 3:
            idx = rng.permutation(len(keys))
            n_test = max(1, round(len(keys) * frac['test']))
            n_val = max(1, round(len(keys) * frac['val']))
            for j, i in enumerate(idx):
                labels[keys[i]] = 'test' if j < n_test else 'val' if j < n_test + n_val else 'train'
        else:
            labels.update({k: 'time' for k in keys})
    manifest = run_manifest(runs)
    return {'schema_version': 1, 'mode': mode, 'seed': seed, 'frac': frac,
            'test_scenarios': list(test_scenarios), 'runs': labels,
            'run_keys': [r['key'] for r in manifest],
            'dataset_fingerprint': manifest_fingerprint(manifest)}


def split_fingerprint(splits):
    """Hash only the frozen key-to-split mapping and split policy."""
    payload = {
        'mode': splits.get('mode'),
        'seed': splits.get('seed'),
        'frac': splits.get('frac'),
        'runs': dict(sorted(splits.get('runs', {}).items())),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def validate_splits(splits, runs):
    """Fail fast when a frozen split no longer describes exactly the raw dataset."""
    manifest = run_manifest(runs)
    current_keys = [r['key'] for r in manifest]
    split_keys = sorted(splits.get('runs', {}))
    raw_only = sorted(set(current_keys) - set(split_keys))
    split_only = sorted(set(split_keys) - set(current_keys))
    if raw_only or split_only:
        raise ValueError(
            'Frozen split does not match raw runs. '
            f'raw-only={raw_only}; split-only={split_only}. '
            'Rebuild deliberately before feature selection/tuning, then freeze it again.'
        )
    expected_keys = splits.get('run_keys')
    if expected_keys is not None and sorted(expected_keys) != current_keys:
        raise ValueError('Frozen split run_keys do not match the current raw runs')
    current_fp = manifest_fingerprint(manifest)
    expected_fp = splits.get('dataset_fingerprint')
    if expected_fp is not None and expected_fp != current_fp:
        raise ValueError(
            f'Raw dataset fingerprint changed: split={expected_fp}, current={current_fp}. '
            'Do not reuse results or a tuned study across this change.'
        )
    return {'manifest': manifest, 'dataset_fingerprint': current_fp,
            'split_fingerprint': split_fingerprint(splits)}


def load_or_create_splits(path, runs, rebuild=False, **kw):
    """One split file shared by every model: the test runs are identical for LSTM and GAT-GRU."""
    path = Path(path)
    if path.exists() and not rebuild:
        splits = json.loads(path.read_text())
        validate_splits(splits, runs)
        return splits
    splits = create_splits(runs, **kw)
    validate_splits(splits, runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(splits, indent=2))
    print(f'Đã tạo {path}')
    return splits


def split_labels(split_value, T, frac):
    return time_labels(T, frac) if split_value == 'time' else np.full(T, split_value, dtype=object)


# --------------------------------------------------------------------------- persistence
def save_runs_npz(path, runs, edges, extra_meta=None):
    """Raw per-run arrays (all candidate features, not windowed): input of feature selection / tuning."""
    arrays, index = {}, []
    for i, r in enumerate(runs):
        arrays[f'X_{i}'], arrays[f'E_{i}'], arrays[f'G_{i}'] = r['X'], r['E'], r['G']
        index.append({'key': r['key'], 'scenario': r['scenario'],
                      'run_id': r.get('run_id', r['meta'].get('run_id')),
                      'source_run_id': r.get('source_run_id', r['meta'].get('run_id')),
                      'autoscaler': r['meta'].get('autoscaler', 'none'),
                      'start': r['meta']['start'], 'end': r['meta'].get('end'),
                      'duration_sec': r['meta'].get('duration_sec'),
                      'step_sec': r['meta'].get('step_sec', STEP_SEC),
                      'seed': r['meta'].get('seed'),
                      'raw_file_hashes': _raw_file_hashes(r)})
    extra_meta = extra_meta or {}
    contract = (data_contract(runs, extra_meta['splits'], NF, TARGETS)
                if 'splits' in extra_meta else None)
    meta = {'services': SERVICES, 'edges': edges, 'node_features': NF, 'edge_features': EF,
            'graph_features': GF, 'targets': TARGETS, 'forced_features': FORCED_FEATURES,
            'step_sec': STEP_SEC, 'warmup_drop': WARMUP_DROP, 'runs': index,
            **({'data_contract': contract} if contract else {}), **extra_meta}
    arrays['meta'] = np.array(json.dumps(meta))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrays)


def load_runs_npz(path):
    d = np.load(path, allow_pickle=False)
    meta = json.loads(str(d['meta']))
    runs = [{**info, 'X': d[f'X_{i}'], 'E': d[f'E_{i}'], 'G': d[f'G_{i}']} for i, info in enumerate(meta['runs'])]
    return runs, meta


# --------------------------------------------------------------------------- scaling & windows
def transform(x, kind):
    if kind == 'log1p':
        return np.log1p(np.clip(x, 0, None))
    if kind == 'slog1p':
        return np.sign(x) * np.log1p(np.abs(x))
    return x


def inverse_transform(z, kind):
    if kind == 'log1p':
        return np.expm1(z)
    if kind == 'slog1p':
        return np.sign(z) * np.expm1(np.abs(z))
    return z


def fit_scaler(arrays, kinds):
    """Clip at train p99.9 -> transform -> z-score. arrays: list of [..., F]."""
    flat = np.concatenate([a.reshape(-1, a.shape[-1]) for a in arrays])
    clip = np.nanpercentile(flat, 99.9, axis=0)
    tr = np.stack([transform(np.minimum(flat[:, i], clip[i]), k) for i, k in enumerate(kinds)], axis=1)
    mean, std = tr.mean(axis=0), tr.std(axis=0)
    std[std < 1e-6] = 1.0
    return {'clip': clip.tolist(), 'mean': mean.tolist(), 'std': std.tolist(), 'kinds': list(kinds)}


def apply_scaler(a, sc):
    out = np.empty(a.shape, dtype=np.float32)
    for i, k in enumerate(sc['kinds']):
        out[..., i] = (transform(np.minimum(a[..., i], sc['clip'][i]), k) - sc['mean'][i]) / sc['std'][i]
    return out


def invert_scaler(z, sc):
    out = np.empty(z.shape, dtype=np.float32)
    for i, k in enumerate(sc['kinds']):
        out[..., i] = inverse_transform(z[..., i] * sc['std'][i] + sc['mean'][i], k)
    return out


def make_windows(runs, splits, split, window, horizon, node_feats, edge_feats=None, scalers=None,
                 graph_feats=None, frac=None):
    """Sliding windows inside each run (never across runs / split boundaries).
    info row = (run key, scenario, t_target0) where t_target0 is the run-relative index of the
    first predicted step: windows of models with different `window` align on it."""
    frac = frac or splits.get('frac')
    ni = [NF.index(f) for f in node_feats]
    ti = [NF.index(f) for f in TARGETS]
    ei = [EF.index(f) for f in (edge_feats or [])]
    gi = [GF.index(f) for f in (graph_feats or [])]
    out = {k: [] for k in ('X', 'E', 'G', 'Y', 'Y_raw', 'Y_last', 'info')}
    for r in runs:
        lab = splits['runs'].get(r['key'])
        if lab is None:
            continue
        T = len(r['X'])
        labels = split_labels(lab, T, frac)
        Xs = apply_scaler(r['X'][..., ni], scalers['node']) if scalers else r['X'][..., ni]
        Ys = apply_scaler(r['X'][..., ti], scalers['target']) if scalers else r['X'][..., ti]
        Es = (apply_scaler(r['E'][..., ei], scalers['edge']) if scalers and ei else r['E'][..., ei]) if ei else None
        Gs = (apply_scaler(r['G'][..., gi], scalers['graph']) if scalers and gi else r['G'][..., gi]) if gi else None
        for s in range(T - window - horizon + 1):
            if not (labels[s:s + window + horizon] == split).all():
                continue
            out['X'].append(Xs[s:s + window])
            out['Y'].append(Ys[s + window:s + window + horizon])
            out['Y_raw'].append(r['X'][s + window:s + window + horizon][..., ti])
            out['Y_last'].append(r['X'][s + window - 1][..., ti])
            out['info'].append((r['key'], r['scenario'], s + window))
            if Es is not None:
                out['E'].append(Es[s:s + window])
            if Gs is not None:
                out['G'].append(Gs[s:s + window])
    if not out['X']:
        return None
    res = {k: np.stack(v).astype(np.float32) for k, v in out.items() if v and k != 'info'}
    res['info'] = np.array(out['info'], dtype=object)
    return res


def fit_scalers(runs, splits, node_feats, edge_feats=None, graph_feats=None, frac=None):
    """Scalers fit on train timesteps only (time-split runs contribute their train part)."""
    frac = frac or splits.get('frac')
    ni = [NF.index(f) for f in node_feats]
    ti = [NF.index(f) for f in TARGETS]
    ei = [EF.index(f) for f in (edge_feats or [])]
    gi = [GF.index(f) for f in (graph_feats or [])]
    trX, trY, trE, trG = [], [], [], []
    for r in runs:
        lab = splits['runs'].get(r['key'])
        if lab is None:
            continue
        mask = split_labels(lab, len(r['X']), frac) == 'train'
        if not mask.any():
            continue
        trX.append(r['X'][mask][..., ni])
        trY.append(r['X'][mask][..., ti])
        if ei:
            trE.append(r['E'][mask][..., ei])
        if gi:
            trG.append(r['G'][mask][..., gi])
    sc = {'node': fit_scaler(trX, [NODE_FEATURES[f][1] for f in node_feats]),
          'target': fit_scaler(trY, [NODE_FEATURES[f][1] for f in TARGETS])}
    if ei:
        sc['edge'] = fit_scaler(trE, [EDGE_FEATURES[f] for f in edge_feats])
    if gi:
        sc['graph'] = fit_scaler(trG, [GRAPH_FEATURES[f] for f in graph_feats])
    return sc


def train_arrays(runs, splits, frac=None):
    """Raw train timesteps (for statistics such as feature filtering)."""
    frac = frac or splits.get('frac')
    out = []
    for r in runs:
        lab = splits['runs'].get(r['key'])
        if lab is None:
            continue
        mask = split_labels(lab, len(r['X']), frac) == 'train'
        if mask.any():
            out.append({'key': r['key'], 'X': r['X'][mask], 'E': r['E'][mask]})
    return out
