"""
forecast_data.py - data pipeline shared by the GAT-GRU and LSTM forecasters.

Both models must see exactly the same runs, features, transforms, splits and
windows; the only intended difference is the graph (edges + message passing).
Keeping this logic in one module (instead of copying it into each notebook)
is what guarantees that.

No deep-learning dependency here (numpy/pandas only).
"""
import json
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
# Services scaled by HPA / the controller (others stay at 1 replica).
SCALED_SERVICES = ['frontend', 'cartservice', 'checkoutservice', 'currencyservice',
                   'productcatalogservice', 'recommendationservice']

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


# --------------------------------------------------------------------------- demo data
def make_demo_results(root, runs_per_scenario=4, duration_sec=1800, seed=42,
                      scenarios=('normal', 'spike', 'bursty'), write_edges=True):
    """Write synthetic runs in exactly the file format of run-scenario.sh / collect_metrics.py.
    For testing the pipeline only - the numbers have no scientific value."""
    root = Path(root)
    rng = np.random.default_rng(seed)
    ratio = {
        ('frontend', 'productcatalogservice'): 1.6, ('frontend', 'currencyservice'): 1.9,
        ('frontend', 'cartservice'): 0.7, ('frontend', 'recommendationservice'): 0.45,
        ('frontend', 'shippingservice'): 0.15, ('frontend', 'checkoutservice'): 0.04,
        ('frontend', 'adservice'): 0.45, ('recommendationservice', 'productcatalogservice'): 1.0,
        ('checkoutservice', 'paymentservice'): 1.0, ('checkoutservice', 'productcatalogservice'): 1.5,
        ('checkoutservice', 'shippingservice'): 2.0, ('checkoutservice', 'emailservice'): 1.0,
        ('checkoutservice', 'currencyservice'): 2.5, ('checkoutservice', 'cartservice'): 2.0,
    }
    cpu_ms = {'frontend': 6, 'adservice': 4, 'cartservice': 3, 'checkoutservice': 5, 'currencyservice': 2,
              'emailservice': 6, 'paymentservice': 2, 'productcatalogservice': 1.5,
              'recommendationservice': 8, 'redis-cart': 0.5, 'shippingservice': 1.5}
    limit = {s: (1.0 if s in ('emailservice', 'recommendationservice') else 0.5) for s in SERVICES}
    request = {s: (0.1 if s == 'emailservice' else 0.15) for s in SERVICES}
    order = ['frontend', 'recommendationservice', 'checkoutservice']
    T = duration_sec // STEP_SEC
    t0 = 1_790_000_000

    def users_curve(sc):
        t = np.arange(T) * STEP_SEC
        # math-load-testing scenarios (demo only): curves expressed as users = 2 x frontend rps
        if sc == 'nhpp':
            ph = rng.uniform(0, 2 * np.pi)
            lam = np.maximum(0.5, 7 + 5 * np.sin(2 * np.pi * t / 900 + ph) + 1.5 * np.sin(4 * np.pi * t / 900 + 2 * ph))
            return 2 * 6 * rng.poisson(lam * STEP_SEC) / STEP_SEC
        if sc == 'mmpp':
            rate = {'idle': 9.0, 'normal': 21.0, 'high': 90.0}
            soj = {'idle': 120.0, 'normal': 180.0, 'high': 60.0}
            nxt = {'idle': (['normal', 'high'], [0.8, 0.2]), 'normal': (['idle', 'high'], [0.4, 0.6]),
                   'high': (['normal', 'idle'], [0.9, 0.1])}
            out, s, until = np.empty(T), 'normal', rng.exponential(180.0)
            for i, x in enumerate(t):
                while x >= until:
                    s = rng.choice(nxt[s][0], p=nxt[s][1])
                    until += rng.exponential(soj[s])
                out[i] = rate[s]
            return 2 * rng.poisson(out * STEP_SEC) / STEP_SEC
        if sc == 'pareto':
            work = np.ceil(1.0 / (1 - rng.random(T) * (1 - (1 / 40) ** 1.2)) ** (1 / 1.2))   # bounded Pareto items
            return 2 * (3.0 * (3 + work) * rng.uniform(0.85, 1.15, T))
        if sc == 'onoff':
            bp = lambda a, lo, hi, n: lo / (1 - rng.random(n) * (1 - (lo / hi) ** a)) ** (1 / a)
            on_sec = np.zeros(T * STEP_SEC)   # sources ON per second, then averaged per 10s step
            for _ in range(60):
                x, on = -600.0, rng.random() < 0.5
                while x < duration_sec:
                    d = bp(1.5, 5, 300, 1)[0] if on else bp(1.3, 10, 600, 1)[0]
                    if on and x + d > 0:      # ON periods entirely inside the warm-up are skipped
                        on_sec[int(np.ceil(max(x, 0))):int(np.ceil(min(x + d, T * STEP_SEC)))] += 1
                    x, on = x + d, not on
            return 2 * 1.5 * on_sec.reshape(T, STEP_SEC).mean(axis=1)
        if sc == 'normal':
            return np.where(t < 60, 40 * t / 60, 40 * (1 + rng.uniform(-0.1, 0.1, T)))
        if sc == 'spike':
            return np.where(t % 360 < 90, 220.0, 25.0)
        draws, u = {}, np.empty(T)
        for i, w in enumerate(t // 30):
            if w not in draws:
                draws[w] = rng.integers(80, 181) if rng.random() < 0.35 else 20 * rng.uniform(0.7, 1.3)
            u[i] = draws[w]
        return u

    for sc in scenarios:
        for k in range(1, runs_per_scenario + 1):
            d = root / sc / f'run_{k:02d}'
            d.mkdir(parents=True, exist_ok=True)
            start = t0
            t0 += duration_sec + 300
            hpa = k % 2 == 0  # half of the runs with a simulated HPA
            users = users_curve(sc)
            rps = {s: np.zeros(T) for s in SERVICES}
            rps['frontend'] = users / 2.0 * rng.uniform(0.95, 1.05, T)
            edge_rps = {}
            for src in order:
                for (a, b), q in ratio.items():
                    if a == src:
                        edge_rps[(a, b)] = rps[a] * q * rng.uniform(0.9, 1.1, T)
                        rps[b] = rps[b] + edge_rps[(a, b)]
            rps['redis-cart'] = rps['cartservice'] * 1.2
            replicas = {s: np.ones(T) for s in SERVICES}
            node_rows, svc_rows, edge_rows = [], [], []
            restarts = {s: 0 for s in SERVICES}
            for i in range(T):
                ts = start + i * STEP_SEC
                for s in SERVICES:
                    rep = replicas[s][i]
                    demand = 0.01 + rps[s][i] * cpu_ms[s] / 1000 * rng.uniform(0.9, 1.1)
                    cap = limit[s] * rep
                    cpu = min(demand, cap * 0.98)
                    thr = float(np.clip((demand - cap) / max(demand, 1e-9) + 0.02 * rng.random(), 0, 1))
                    sat = demand / cap
                    p50 = (2 + 3 * cpu_ms[s]) * (1 + 4 * max(0, sat - 0.7) ** 2)
                    p95 = p50 * 2.5 * (1 + 6 * max(0, sat - 0.8) ** 2)
                    err = rps[s][i] * STEP_SEC * (0.001 + 0.2 * max(0, sat - 1.0))
                    if sat > 1.3 and rng.random() < 0.01:
                        restarts[s] += 1
                    node_rows.append([ts, s, cpu, 0.002 + rps[s][i] * 0.0004, thr,
                                      (60 + 30 * rep + 0.5 * rps[s][i] + rng.normal(0, 3)) * 2 ** 20,
                                      rps[s][i] * 1800, rps[s][i] * 2600 * rng.uniform(0.9, 1.1),
                                      request[s] * rep, limit[s] * rep, restarts[s], rep, rep])
                    if s != 'redis-cart' and rps[s][i] > 0:
                        n = int(rng.poisson(rps[s][i] * STEP_SEC))
                        svc_rows.append([ts, s, n, int(min(n, rng.poisson(err))), p50, p95])
                    if i + 1 < T:
                        replicas[s][i + 1] = rep
                        if hpa and s in SCALED_SERVICES and i % 3 == 0:
                            util = cpu / (request[s] * rep)
                            replicas[s][i + 1] = int(np.clip(np.ceil(rep * util / 0.7 / 3.0), 1, 4))
                if write_edges:
                    for (a, b), arr in edge_rps.items():
                        n = int(rng.poisson(arr[i] * STEP_SEC))
                        if n:
                            lat = (2 + 3 * cpu_ms[b]) * 2.8 + rng.gamma(2, 2)
                            edge_rows.append([ts, a, b, n, int(rng.poisson(n * 0.002)), lat * 0.45, lat])
            pd.DataFrame(node_rows, columns=['timestamp', 'service', 'cpu_cores', 'cpu_sidecar_cores',
                                             'cpu_throttle_ratio', 'mem_bytes', 'net_rx_bps', 'net_tx_bps',
                                             'cpu_request_cores', 'cpu_limit_cores', 'restarts_total',
                                             'replicas', 'replicas_desired']).to_csv(d / 'node_metrics.csv', index=False)
            pd.DataFrame(svc_rows, columns=['timestamp', 'service', 'request_count', 'error_count',
                                            'latency_p50_ms', 'latency_p95_ms']).to_csv(d / 'service_metrics.csv', index=False)
            if write_edges:
                pd.DataFrame(edge_rows, columns=['timestamp', 'source', 'target', 'call_count', 'error_count',
                                                 'latency_p50_ms', 'latency_p95_ms']).to_csv(d / 'edge_metrics.csv', index=False)
            sec = np.arange(duration_sec)
            u_sec = np.repeat(users, STEP_SEC)[:duration_sec]
            pd.DataFrame({'Timestamp': start + sec, 'User Count': u_sec.astype(int), 'Type': '',
                          'Name': 'Aggregated', 'Requests/s': u_sec / 2.0, 'Failures/s': 0.0,
                          '95%': 120.0}).to_csv(d / 'locust_stats_history.csv', index=False)
            (d / 'meta.json').write_text(json.dumps({
                'scenario': sc, 'run_id': d.name, 'start': start, 'end': start + duration_sec,
                'duration_sec': duration_sec, 'step_sec': STEP_SEC, 'autoscaler': 'hpa' if hpa else 'none',
                'locust_exit_code': 0}))
    return root


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
            loc = _read(d / 'locust_stats_history.csv')
            if not loc.empty and 'Name' in loc:
                loc = loc[loc['Name'] == 'Aggregated']
            report = d / 'collect_report.json'
            runs.append({
                'key': f"{label}/{meta['scenario']}/{meta.get('run_id', d.name)}",
                'source': label, 'scenario': meta['scenario'], 'run_id': meta.get('run_id', d.name),
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
    node[gauges] = node[gauges].groupby(level='service').ffill(limit=3).groupby(level='service').bfill(limit=3)
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
    return {'mode': mode, 'seed': seed, 'frac': frac, 'test_scenarios': list(test_scenarios), 'runs': labels}


def load_or_create_splits(path, runs, rebuild=False, **kw):
    """One split file shared by every model: the test runs are identical for LSTM and GAT-GRU."""
    path = Path(path)
    if path.exists() and not rebuild:
        splits = json.loads(path.read_text())
        missing = [r['key'] for r in runs if r['key'] not in splits['runs']]
        if missing:
            print(f'⚠️ {len(missing)} run chưa có trong {path.name} nên bị bỏ qua: {missing[:5]}... '
                  f'(đặt REBUILD_SPLITS=True để chia lại; chỉ làm trước khi tune/đánh giá)')
        return splits
    splits = create_splits(runs, **kw)
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
                      'autoscaler': r['meta'].get('autoscaler', 'none'), 'start': r['meta']['start']})
    meta = {'services': SERVICES, 'edges': edges, 'node_features': NF, 'edge_features': EF,
            'graph_features': GF, 'targets': TARGETS, 'forced_features': FORCED_FEATURES,
            'step_sec': STEP_SEC, 'warmup_drop': WARMUP_DROP, 'runs': index, **(extra_meta or {})}
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
