"""
forecast_metrics.py - evaluation shared by every forecaster (numpy/pandas only).

Prediction file contract (`<model>_predictions_test.npz`), written by the train notebooks
and read by compare_lstm_gatgru.ipynb:
    Y_pred  [n_seeds, S, h, N, K]  predictions in ORIGINAL units (rps, cores)
    Y_true  [S, h, N, K]           ground truth, original units
    Y_last  [S, N, K]              last observed value (persistence baseline, spike-onset detection)
    info    [S, 3] str             (run key, scenario, t_target0) - t_target0 = run-relative index
                                   of the first predicted step; models with different input
                                   windows are aligned on it
    meta    json str               model name, config, targets, services, seeds, n_params, latency
"""
import json
import math

import numpy as np
import pandas as pd


def save_predictions(path, Y_pred, Y_true, Y_last, info, meta):
    np.savez_compressed(path, Y_pred=np.asarray(Y_pred, np.float32), Y_true=np.asarray(Y_true, np.float32),
                        Y_last=np.asarray(Y_last, np.float32), info=np.asarray(info, dtype=str),
                        meta=np.array(json.dumps(meta, default=float)))


def load_predictions(path):
    d = np.load(path, allow_pickle=False)
    return {'Y_pred': d['Y_pred'], 'Y_true': d['Y_true'], 'Y_last': d['Y_last'],
            'info': d['info'], 'meta': json.loads(str(d['meta']))}


def align(a, b):
    """Keep only windows present in both prediction sets (same run, same first target step)."""
    ka = {tuple(r): i for i, r in enumerate(a['info'])}
    kb = {tuple(r): i for i, r in enumerate(b['info'])}
    common = sorted(set(ka) & set(kb))
    ia, ib = np.array([ka[k] for k in common]), np.array([kb[k] for k in common])
    sub = lambda d, idx: {**d, 'Y_pred': d['Y_pred'][:, idx], 'Y_true': d['Y_true'][idx],
                          'Y_last': d['Y_last'][idx], 'info': d['info'][idx]}
    return sub(a, ia), sub(b, ib)


def error_table(pred, true, targets):
    """pred/true [S, h, N, K] -> MAE, RMSE, under/over-prediction per target."""
    rows = []
    for k, t in enumerate(targets):
        e = pred[..., k] - true[..., k]
        rows.append({'target': t, 'MAE': np.abs(e).mean(), 'RMSE': math.sqrt((e ** 2).mean()),
                     # under-prediction -> too few replicas -> SLO risk; over -> wasted resources
                     'under_MAE': np.abs(np.minimum(e, 0)).mean(), 'over_MAE': np.maximum(e, 0).mean()})
    return pd.DataFrame(rows).set_index('target')


def seed_summary(d, targets, name):
    """Mean ± std over training seeds of the error table."""
    tabs = [error_table(d['Y_pred'][s], d['Y_true'], targets) for s in range(d['Y_pred'].shape[0])]
    stack = np.stack([t.to_numpy() for t in tabs])
    mean = pd.DataFrame(stack.mean(0), index=tabs[0].index, columns=tabs[0].columns)
    std = pd.DataFrame(stack.std(0), index=tabs[0].index, columns=tabs[0].columns)
    out = mean.round(4).astype(str) + ' ± ' + std.round(4).astype(str)
    out.insert(0, 'model', name)
    return out, mean, std


def persistence(d):
    """y_hat(t+k) = y(t) for every k."""
    h = d['Y_true'].shape[1]
    return np.repeat(d['Y_last'][:, None], h, axis=1)


def by_horizon(pred, true, k):
    return np.abs(pred[..., k] - true[..., k]).mean(axis=(0, 2))


def by_group(pred, true, info, k, col):
    """col: 1 = scenario, 0 = run key."""
    err = np.abs(pred[..., k] - true[..., k]).mean(axis=(1, 2))
    return pd.Series(err).groupby(info[:, col]).mean()


def by_service(pred, true, k, services):
    return pd.Series(np.abs(pred[..., k] - true[..., k]).mean(axis=(0, 1)), index=services)


def spike_onset_mask(d, services, threshold=1.5, scenario='spike'):
    """Windows whose future frontend load rises > threshold x the last observed value:
    the moments where proactive scaling matters most."""
    f = services.index('frontend')
    fut = d['Y_true'][:, :, f, 0].max(axis=1)
    last = d['Y_last'][:, f, 0]
    return (d['info'][:, 1] == scenario) & (fut > threshold * np.maximum(last, 1e-6))


def sign_test(wins, n):
    """Two-sided exact binomial (sign) test p-value for `wins` successes out of `n` paired runs."""
    if n == 0:
        return float('nan')
    k = min(wins, n - wins)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)
