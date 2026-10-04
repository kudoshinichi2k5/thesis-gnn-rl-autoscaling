"""
feature_selection.py - step 1 of feature selection: model-agnostic statistical filter.

Computed on TRAIN timesteps only (no information from val/test):
  1. Near-constant  : std ~ 0 or one value covers > `dominance` of the samples.
                      Such a feature carries no information (e.g. restarts_delta
                      when nothing crashed, replicas when no autoscaler ran).
  2. Relevance      : mean |Spearman rho| between the feature at t and each target at
                      t+1..t+h, computed per service and averaged. Measures how much the
                      feature says about the FUTURE values we must forecast.
  3. Redundancy     : features are visited by decreasing relevance; a feature whose
                      |rho| with an already-kept feature exceeds `corr_threshold` is dropped
                      (it adds almost no new information but costs parameters/noise).
Features in `forced` (targets used autoregressively + replicas) are always kept.

Step 2 (model-based permutation importance) lives in forecast_torch.py because it
needs a trained model.
"""
import numpy as np
import pandas as pd

from forecast_data import EF, NF, NODE_FEATURES, STATIC_EDGES, SVC_IDX, TARGETS


def _rank(a):
    return pd.Series(a).rank().to_numpy()


def _spearman(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 10:
        return np.nan
    ra, rb = _rank(a[ok]), _rank(b[ok])
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def _constant_stats(v):
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0, 1.0
    _, counts = np.unique(np.round(v, 6), return_counts=True)
    return float(v.std()), float(counts.max() / v.size)


def _relevance(series_by_unit, target_by_unit, horizon):
    """series/target_by_unit: list of 1-D arrays per (run, service). Mean |rho| over k and units."""
    rhos = []
    for x, ys in zip(series_by_unit, target_by_unit):
        for y in ys:
            for k in range(1, horizon + 1):
                if len(x) > k:
                    r = _spearman(x[:-k], y[k:])
                    if np.isfinite(r):
                        rhos.append(abs(r))
    return float(np.mean(rhos)) if rhos else 0.0


def _greedy_select(names, relevance, corr, forced, constant, threshold):
    status, kept = {}, []
    for f in names:
        if f in constant and f not in forced:
            status[f] = 'dropped: near-constant'
    order = sorted([f for f in names if f not in status], key=lambda f: (f not in forced, -relevance[f]))
    for f in order:
        partner = next((k for k in kept if abs(corr.loc[f, k]) > threshold), None)
        if f in forced:
            status[f] = 'kept: forced'
            kept.append(f)
        elif partner is not None:
            status[f] = f'dropped: redundant with {partner} (|rho|={abs(corr.loc[f, partner]):.2f})'
        else:
            status[f] = 'kept'
            kept.append(f)
    return [f for f in names if status[f].startswith('kept')], status


def filter_node_features(train_runs, horizon=6, candidates=None, forced=None, std_tol=1e-6,
                         dominance=0.99, corr_threshold=0.95):
    """train_runs: list of {'X': [T, N, |NF|]} raw train timesteps. Returns (kept, report DataFrame)."""
    candidates = list(candidates or NF)
    forced = set(forced or [])
    ci = {f: NF.index(f) for f in candidates}
    ti = [NF.index(t) for t in TARGETS]
    pooled = {f: np.concatenate([r['X'][:, :, ci[f]].ravel() for r in train_runs]) for f in candidates}

    stats = {f: _constant_stats(pooled[f]) for f in candidates}
    constant = {f for f, (sd, dom) in stats.items() if sd < std_tol or dom > dominance}

    units_x = {f: [] for f in candidates}
    units_y = []
    for r in train_runs:
        for n in range(r['X'].shape[1]):
            units_y.append([r['X'][:, n, t] for t in ti])
            for f in candidates:
                units_x[f].append(r['X'][:, n, ci[f]])
    relevance = {f: (_relevance(units_x[f], units_y, horizon) if f not in constant else 0.0) for f in candidates}

    # Redundancy: Spearman between features, per service then averaged (avoid cross-service scale effects)
    corr = pd.DataFrame(0.0, index=candidates, columns=candidates)
    for a in candidates:
        for b in candidates:
            if a >= b:
                continue
            vals = [_spearman(x, y) for x, y in zip(units_x[a], units_x[b])]
            vals = [v for v in vals if np.isfinite(v)]
            corr.loc[a, b] = corr.loc[b, a] = float(np.mean(vals)) if vals else 0.0
    for f in candidates:
        corr.loc[f, f] = 1.0

    kept, status = _greedy_select(candidates, relevance, corr, forced, constant, corr_threshold)
    report = pd.DataFrame({
        'group': [NODE_FEATURES[f][0] for f in candidates],
        'std': [stats[f][0] for f in candidates],
        'dominant_value_frac': [stats[f][1] for f in candidates],
        'relevance_|rho|': [relevance[f] for f in candidates],
        'status': [status[f] for f in candidates],
    }, index=candidates).sort_values('relevance_|rho|', ascending=False)
    return kept, report, corr


def filter_edge_features(train_runs, edges, horizon=6, candidates=None, std_tol=1e-6, dominance=0.99,
                         corr_threshold=0.95):
    """Edge features vs the CALLEE's future targets (load propagated along the edge)."""
    candidates = list(candidates or EF)
    ci = {f: EF.index(f) for f in candidates}
    ti = [NF.index(t) for t in TARGETS]
    traced = [k for k, (a, b) in enumerate(edges) if b != 'redis-cart']  # redis edge has no spans
    pooled = {f: np.concatenate([r['E'][:, traced, ci[f]].ravel() for r in train_runs]) for f in candidates}
    stats = {f: _constant_stats(pooled[f]) for f in candidates}
    constant = {f for f, (sd, dom) in stats.items() if sd < std_tol or dom > dominance}

    units_x = {f: [] for f in candidates}
    units_y = []
    for r in train_runs:
        for k in traced:
            dst = SVC_IDX[edges[k][1]]
            units_y.append([r['X'][:, dst, t] for t in ti])
            for f in candidates:
                units_x[f].append(r['E'][:, k, ci[f]])
    relevance = {f: (_relevance(units_x[f], units_y, horizon) if f not in constant else 0.0) for f in candidates}
    corr = pd.DataFrame(0.0, index=candidates, columns=candidates)
    for a in candidates:
        for b in candidates:
            if a < b:
                vals = [v for v in (_spearman(x, y) for x, y in zip(units_x[a], units_x[b])) if np.isfinite(v)]
                corr.loc[a, b] = corr.loc[b, a] = float(np.mean(vals)) if vals else 0.0
    for f in candidates:
        corr.loc[f, f] = 1.0
    kept, status = _greedy_select(candidates, relevance, corr, set(), constant, corr_threshold)
    report = pd.DataFrame({'std': [stats[f][0] for f in candidates],
                           'dominant_value_frac': [stats[f][1] for f in candidates],
                           'relevance_|rho|': [relevance[f] for f in candidates],
                           'status': [status[f] for f in candidates]}, index=candidates)
    return kept, report.sort_values('relevance_|rho|', ascending=False), corr


def select_by_importance(importance, forced, min_delta=0.0, n_std=1.0):
    """Step 2 decision rule: keep a feature when permuting it hurts val MAE significantly:
    mean increase > max(min_delta, n_std * std over repeats). Forced features are kept."""
    keep, status = [], {}
    for f, row in importance.iterrows():
        thr = max(min_delta, n_std * row['delta_std'])
        if f in forced:
            keep.append(f)
            status[f] = 'kept: forced'
        elif row['delta_mean'] > thr:
            keep.append(f)
            status[f] = 'kept'
        else:
            status[f] = f'dropped: Δ={row["delta_mean"]:.4f} ≤ {thr:.4f}'
    return keep, status
