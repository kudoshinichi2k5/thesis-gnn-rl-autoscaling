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
from pathlib import Path

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


CONTRACT_FIELDS = ('dataset_fingerprint', 'split_fingerprint', 'run_keys', 'services',
                   'node_features', 'targets', 'step_sec', 'horizon', 'warmup_drop')


def _sample_index(artifact, name):
    index = {tuple(row): i for i, row in enumerate(artifact['info'])}
    if len(index) != len(artifact['info']):
        raise ValueError(f'{name} has duplicate (run, scenario, target timestep) keys')
    return index


def _subset_samples(artifact, index, keys):
    positions = np.asarray([index[key] for key in keys], dtype=int)
    return {**artifact, 'Y_pred': artifact['Y_pred'][:, positions],
            'Y_true': artifact['Y_true'][positions],
            'Y_last': artifact['Y_last'][positions], 'info': artifact['info'][positions]}


def align_many(artifacts):
    """Align two or more named prediction artifacts to their common test windows."""
    if len(artifacts) < 2:
        raise ValueError('At least two prediction artifacts are required')
    indices = {name: _sample_index(artifact, name) for name, artifact in artifacts.items()}
    common = set.intersection(*(set(index) for index in indices.values()))
    if not common:
        raise ValueError('Prediction artifacts have no common test samples')
    keys = sorted(common)
    return {name: _subset_samples(artifacts[name], indices[name], keys) for name in artifacts}


def align(a, b):
    """Backward-compatible two-artifact alignment."""
    aligned = align_many({'left': a, 'right': b})
    return aligned['left'], aligned['right']


def assert_compatible_prediction_set(artifacts, require_same_raw_samples=True, atol=1e-6):
    """Validate contracts and align any number of prediction artifacts."""
    if len(artifacts) < 2:
        raise ValueError('At least two prediction artifacts are required')
    names = list(artifacts)
    reference_name = names[0]
    reference = artifacts[reference_name]
    reference_contract = reference.get('meta', {}).get('data_contract', {})
    reference_keys = set(map(tuple, reference['info']))
    for name in names:
        artifact = artifacts[name]
        contract = artifact.get('meta', {}).get('data_contract', {})
        missing = [field for field in CONTRACT_FIELDS
                   if field not in reference_contract or field not in contract]
        if missing:
            raise ValueError(f'{name} is missing prediction contract fields: {missing}')
        mismatch = [field for field in CONTRACT_FIELDS
                    if reference_contract[field] != contract[field]]
        if mismatch:
            raise ValueError(
                f'{reference_name}/{name} prediction contracts differ in: {mismatch}'
            )
        if require_same_raw_samples:
            keys = set(map(tuple, artifact['info']))
            if keys != reference_keys:
                only_reference = len(reference_keys - keys)
                only_current = len(keys - reference_keys)
                raise ValueError(
                    f'Test sample sets differ for {reference_name}/{name}: '
                    f'only-reference={only_reference}, only-current={only_current}'
                )

    aligned = align_many(artifacts)
    reference = aligned[reference_name]
    for name, artifact in aligned.items():
        if not np.array_equal(reference['info'], artifact['info']):
            raise ValueError(f'Aligned sample identities differ for {reference_name}/{name}')
        if not np.allclose(reference['Y_true'], artifact['Y_true'], atol=atol, rtol=0):
            raise ValueError(f'Aligned ground truth differs for {reference_name}/{name}')
        if not np.allclose(reference['Y_last'], artifact['Y_last'], atol=atol, rtol=0):
            raise ValueError(f'Aligned last observations differ for {reference_name}/{name}')
    return aligned


def assert_compatible_predictions(a, b, require_same_raw_samples=True, atol=1e-6):
    """Backward-compatible two-artifact contract validation and alignment."""
    aligned = assert_compatible_prediction_set(
        {'left': a, 'right': b}, require_same_raw_samples=require_same_raw_samples, atol=atol
    )
    return aligned['left'], aligned['right']


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


def save_evaluation_artifacts(out_dir, model_name, d):
    """Save numeric global/scenario/horizon test metrics instead of notebook-only displays."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    targets = d['meta']['targets']
    seeds = d['meta']['seeds']
    global_rows, scenario_rows, horizon_rows = [], [], []
    for si, seed in enumerate(seeds):
        tab = error_table(d['Y_pred'][si], d['Y_true'], targets)
        for target, row in tab.iterrows():
            global_rows.append({'model': model_name, 'seed': seed, 'target': target, **row.to_dict()})
        for scenario in sorted(set(d['info'][:, 1])):
            mask = d['info'][:, 1] == scenario
            tab_sc = error_table(d['Y_pred'][si][mask], d['Y_true'][mask], targets)
            for target, row in tab_sc.iterrows():
                scenario_rows.append({'model': model_name, 'seed': seed, 'scenario': scenario,
                                      'target': target, **row.to_dict()})
        for k, target in enumerate(targets):
            values = by_horizon(d['Y_pred'][si], d['Y_true'], k)
            for h, value in enumerate(values, 1):
                horizon_rows.append({'model': model_name, 'seed': seed, 'target': target,
                                     'horizon_step': h, 'horizon_sec': h * d['meta']['data_contract']['step_sec'],
                                     'MAE': float(value)})
    global_df = pd.DataFrame(global_rows)
    scenario_df = pd.DataFrame(scenario_rows)
    horizon_df = pd.DataFrame(horizon_rows)
    global_df.to_csv(out / f'{model_name}_test_metrics_by_seed.csv', index=False)
    scenario_df.to_csv(out / f'{model_name}_test_metrics_by_scenario.csv', index=False)
    horizon_df.to_csv(out / f'{model_name}_test_metrics_by_horizon.csv', index=False)
    summary = global_df.groupby(['model', 'target']).agg(
        MAE_mean=('MAE', 'mean'), MAE_std=('MAE', 'std'),
        RMSE_mean=('RMSE', 'mean'), RMSE_std=('RMSE', 'std'),
        under_MAE_mean=('under_MAE', 'mean'), over_MAE_mean=('over_MAE', 'mean'),
    ).reset_index()
    summary.to_csv(out / f'{model_name}_test_metrics_summary.csv', index=False)
    (out / f'{model_name}_test_metrics_summary.json').write_text(
        json.dumps(summary.to_dict('records'), indent=2, default=float)
    )
    return summary
