"""
forecast_torch.py - models, training, hyper-parameter search and permutation importance
shared by the LSTM baseline and GAT-GRU (PyTorch only, no PyTorch Geometric).

Fairness rules enforced here (both models go through the same functions):
  * same loss (Huber on scaled targets), optimiser (AdamW), gradient clipping,
    max epochs and early-stopping patience;
  * same Optuna sampler/pruner/seed and the same number of trials;
  * same service embedding (node identity) for both models;
  * the objective is val MAE in scaled units, averaged over both targets.

Tensor layout: X [B, W, N, F], E [B, W, M, Fe] (GAT-GRU only), Y [B, H, N, K].
"""
import copy
import json
import math
import random
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

import forecast_data as fd

DEFAULTS = {'max_epochs': 60, 'patience': 8, 'weight_decay': 1e-4, 'grad_clip': 1.0, 'emb_dim': 4}
# Identical search/evaluation budget for every model (change here, never per notebook).
BUDGET = {'n_trials': 40, 'search_seed': 42, 'final_seeds': (0, 1, 2, 3, 4), 'importance_repeats': 5}
DEMO_BUDGET = {'n_trials': 3, 'search_seed': 42, 'final_seeds': (0, 1), 'importance_repeats': 2, 'max_epochs': 3}


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


# =============================================================================== models
class LSTMForecaster(nn.Module):
    """Baseline without graph.
    per_service: one LSTM with shared weights runs over each service's own sequence
                 (+ service embedding) - sees no other service.
    joint      : one LSTM over the concatenation of all services (N*F inputs) - can learn
                 cross-service correlations, but with no structural prior."""

    def __init__(self, n_nodes, n_feat, horizon, n_targets, hidden=64, layers=2, dropout=0.1,
                 mode='per_service', emb_dim=4, **_):
        super().__init__()
        self.N, self.H, self.K, self.mode = n_nodes, horizon, n_targets, mode
        self.emb = nn.Embedding(n_nodes, emb_dim)
        in_dim = n_feat + emb_dim if mode == 'per_service' else n_nodes * (n_feat + emb_dim)
        out_dim = horizon * n_targets if mode == 'per_service' else n_nodes * horizon * n_targets
        self.lstm = nn.LSTM(in_dim, hidden, layers, batch_first=True, dropout=dropout if layers > 1 else 0.0)
        self.drop = nn.Dropout(dropout)
        self.head = nn.Linear(hidden, out_dim)

    def forward(self, X, E=None):
        B, W, N, _ = X.shape
        emb = self.emb.weight[None, None].expand(B, W, N, -1)          # [B, W, N, emb]
        x = torch.cat([X, emb], dim=-1)
        if self.mode == 'per_service':
            x = x.permute(0, 2, 1, 3).reshape(B * N, W, -1)
            out, _ = self.lstm(x)
            y = self.head(self.drop(out[:, -1]))                         # [B*N, H*K]
            return y.view(B, N, self.H, self.K).permute(0, 2, 1, 3)
        out, _ = self.lstm(x.reshape(B, W, -1))
        y = self.head(self.drop(out[:, -1]))                             # [B, N*H*K]
        return y.view(B, N, self.H, self.K).permute(0, 2, 1, 3)


class EdgeGATLayer(nn.Module):
    """Multi-head graph attention with edge features (GAT, Velickovic et al. 2018;
    edge term as in PyG GATConv(edge_dim=...)). Dense implementation: N = 11 nodes.
        e_ij = LeakyReLU(a_dst . W h_i + a_src . W h_j + a_edge . W_e x_ij)
        alpha_ij = softmax_j(e_ij) over in-neighbours j of i (incl. self loop)"""

    def __init__(self, in_dim, out_dim, heads, edge_dim=0, dropout=0.1):
        super().__init__()
        self.H, self.D = heads, out_dim
        self.lin = nn.Linear(in_dim, heads * out_dim, bias=False)
        self.att_src = nn.Parameter(torch.empty(1, 1, heads, out_dim))
        self.att_dst = nn.Parameter(torch.empty(1, 1, heads, out_dim))
        self.lin_edge = nn.Linear(edge_dim, heads, bias=False) if edge_dim else None
        self.bias = nn.Parameter(torch.zeros(heads * out_dim))
        self.drop = nn.Dropout(dropout)
        nn.init.xavier_uniform_(self.lin.weight)
        nn.init.xavier_uniform_(self.att_src)
        nn.init.xavier_uniform_(self.att_dst)

    def forward(self, x, mask, e=None):
        # x [BW, N, in]; mask [N_dst, N_src] bool; e [BW, N_dst, N_src, edge_dim]
        BW, N, _ = x.shape
        h = self.lin(x).view(BW, N, self.H, self.D)
        a_src = (h * self.att_src).sum(-1)                               # [BW, N, H]
        a_dst = (h * self.att_dst).sum(-1)
        score = a_dst.unsqueeze(2) + a_src.unsqueeze(1)                  # [BW, N_dst, N_src, H]
        if self.lin_edge is not None and e is not None:
            score = score + self.lin_edge(e)
        score = F.leaky_relu(score, 0.2).masked_fill(~mask[None, :, :, None], float('-inf'))
        alpha = self.drop(torch.softmax(score, dim=2))
        out = torch.einsum('bijh,bjhd->bihd', alpha, h).reshape(BW, N, self.H * self.D)
        return out + self.bias, alpha


class GATGRUForecaster(nn.Module):
    """GAT on every snapshot (spatial: who influences whom, weighted by learned attention)
    -> GRU over the window for each service (temporal) -> linear head for H steps x K targets.

    edge_direction: 'forward' = message caller -> callee (load propagates downstream),
                    'reverse' = callee -> caller (back-pressure / latency propagates upstream),
                    'both'    = both, with a +1/-1 direction flag appended to edge features."""

    def __init__(self, n_nodes, n_feat, horizon, n_targets, edges, n_edge_feat=0, gat_hidden=32, heads=2,
                 gat_layers=1, gru_hidden=64, dropout=0.1, use_edge_features=True, edge_direction='forward',
                 emb_dim=4, **_):
        super().__init__()
        self.N, self.H, self.K = n_nodes, horizon, n_targets
        self.emb = nn.Embedding(n_nodes, emb_dim)
        self.n_edge_feat = n_edge_feat if use_edge_features else 0
        self.edge_dim = self.n_edge_feat + 1 if self.n_edge_feat else 0   # +1: direction flag

        mask = torch.eye(n_nodes, dtype=torch.bool)                     # self loops keep own state
        dst, src, eid, flag = [], [], [], []
        for k, (a, b) in enumerate(edges):                              # a = caller, b = callee
            pairs = {'forward': [(b, a, 1.0)], 'reverse': [(a, b, -1.0)],
                     'both': [(b, a, 1.0), (a, b, -1.0)]}[edge_direction]
            for d, s, fl in pairs:
                mask[d, s] = True
                dst.append(d), src.append(s), eid.append(k), flag.append(fl)
        self.register_buffer('mask', mask)
        self.register_buffer('e_dst', torch.tensor(dst, dtype=torch.long))
        self.register_buffer('e_src', torch.tensor(src, dtype=torch.long))
        self.register_buffer('e_id', torch.tensor(eid, dtype=torch.long))
        self.register_buffer('e_flag', torch.tensor(flag, dtype=torch.float32))

        in_dim = n_feat + emb_dim
        dims = [in_dim] + [heads * gat_hidden] * gat_layers
        self.gat = nn.ModuleList(EdgeGATLayer(dims[i], gat_hidden, heads, self.edge_dim, dropout)
                                 for i in range(gat_layers))
        self.skip = nn.Linear(in_dim, heads * gat_hidden)               # keep the node's own signal
        self.drop = nn.Dropout(dropout)
        self.gru = nn.GRU(heads * gat_hidden, gru_hidden, batch_first=True)
        self.head = nn.Linear(gru_hidden, horizon * n_targets)
        self.last_attention = None

    def _dense_edges(self, E):
        # E [BW, M, Fe] -> [BW, N_dst, N_src, Fe + 1]; zeros where there is no edge (self loops)
        BW = E.shape[0]
        out = E.new_zeros(BW, self.N, self.N, self.edge_dim)
        out[:, self.e_dst, self.e_src, :self.n_edge_feat] = E[:, self.e_id, :self.n_edge_feat]
        out[:, self.e_dst, self.e_src, self.n_edge_feat] = self.e_flag
        return out

    def forward(self, X, E=None):
        B, W, N, _ = X.shape
        emb = self.emb.weight[None, None].expand(B, W, N, -1)
        x = torch.cat([X, emb], dim=-1).reshape(B * W, N, -1)
        e = self._dense_edges(E.reshape(B * W, E.shape[2], E.shape[3])) if self.edge_dim and E is not None else None
        h = x
        for layer in self.gat:
            h, alpha = layer(h, self.mask, e)
            h = self.drop(F.elu(h))
        self.last_attention = alpha.detach().view(B, W, N, N, -1)
        h = h + self.skip(x)
        h = h.view(B, W, N, -1).permute(0, 2, 1, 3).reshape(B * N, W, -1)
        out, _ = self.gru(h)
        y = self.head(self.drop(out[:, -1]))
        return y.view(B, N, self.H, self.K).permute(0, 2, 1, 3)


def build_model(kind, cfg, n_feat, n_edge_feat=0, edges=None, horizon=6):
    common = dict(n_nodes=len(fd.SERVICES), n_feat=n_feat, horizon=horizon, n_targets=len(fd.TARGETS))
    if kind == 'lstm':
        return LSTMForecaster(**common, **cfg)
    if kind == 'gatgru':
        return GATGRUForecaster(**common, edges=edges, n_edge_feat=n_edge_feat, **cfg)
    raise ValueError(kind)


def n_params(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# =============================================================================== data
class WindowSet(torch.utils.data.Dataset):
    def __init__(self, w, use_edges):
        self.X = torch.from_numpy(w['X'])
        self.Y = torch.from_numpy(w['Y'])
        self.E = torch.from_numpy(w['E']) if use_edges and 'E' in w else None

    def __len__(self):
        return len(self.X)

    def __getitem__(self, i):
        return (self.X[i], self.E[i] if self.E is not None else torch.zeros(0), self.Y[i])


def loader(w, use_edges, batch_size, shuffle, seed=0):
    g = torch.Generator()
    g.manual_seed(seed)
    return torch.utils.data.DataLoader(WindowSet(w, use_edges), batch_size=batch_size, shuffle=shuffle,
                                       generator=g, drop_last=False)


def _forward(model, xb, eb):
    return model(xb, eb if eb.numel() else None)


# =============================================================================== training
@torch.no_grad()
def predict(model, dl, device='cpu'):
    model.eval()
    return np.concatenate([_forward(model, xb.to(device), eb.to(device)).cpu().numpy() for xb, eb, _ in dl])


def scaled_mae(pred, y):
    return float(np.abs(pred - y).mean())


def train_model(model, train_w, val_w, cfg, use_edges, device='cpu', trial=None, seed=0, verbose=False):
    """Huber loss on scaled targets, AdamW, early stopping on val MAE (scaled).
    With an Optuna `trial`, reports every epoch so the pruner can stop bad trials early."""
    p = {**DEFAULTS, **cfg}
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=p['lr'], weight_decay=p['weight_decay'])
    tr = loader(train_w, use_edges, p['batch_size'], True, seed)
    va = loader(val_w, use_edges, 512, False)
    loss_fn = nn.HuberLoss(delta=1.0)
    best, best_state, bad, history = math.inf, None, 0, []
    for epoch in range(p['max_epochs']):
        model.train()
        total, n = 0.0, 0
        for xb, eb, yb in tr:
            xb, eb, yb = xb.to(device), eb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(_forward(model, xb, eb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), p['grad_clip'])
            opt.step()
            total, n = total + loss.item() * len(xb), n + len(xb)
        val = scaled_mae(predict(model, va, device), val_w['Y'])
        if not math.isfinite(val):          # diverged (e.g. lr too high): stop, keep best so far
            val = math.inf
        history.append({'epoch': epoch, 'train_loss': total / n, 'val_mae': val})
        if verbose:
            print(f'epoch {epoch:3d}  train {total / n:.4f}  val_mae {val:.4f}')
        if val < best - 1e-5:
            best, best_state, bad = val, copy.deepcopy(model.state_dict()), 0
        else:
            bad += 1
        if trial is not None:
            import optuna
            trial.report(val, epoch)
            if trial.should_prune():
                raise optuna.TrialPruned()
        if bad >= p['patience'] or val == math.inf:
            break
    if best_state is not None:
        model.load_state_dict(best_state)
    return best, pd.DataFrame(history)


# =============================================================================== dataset cache
class Data:
    """Windows for a given feature set, built lazily per window length and cached
    (scalers are fit once on train timesteps and do not depend on the window)."""

    def __init__(self, runs, splits, node_feats, edge_feats=None, horizon=6):
        self.runs, self.splits, self.horizon = runs, splits, horizon
        self.node_feats, self.edge_feats = list(node_feats), list(edge_feats or [])
        self.scalers = fd.fit_scalers(runs, splits, self.node_feats, self.edge_feats)
        self._cache = {}

    def get(self, window, split):
        key = (window, split)
        if key not in self._cache:
            self._cache[key] = fd.make_windows(self.runs, self.splits, split, window, self.horizon,
                                               self.node_feats, self.edge_feats or None, self.scalers)
        return self._cache[key]


# =============================================================================== search spaces
def suggest_config(trial, kind, has_edge_features):
    cfg = {
        'window': trial.suggest_categorical('window', [6, 12, 18]),
        'dropout': trial.suggest_float('dropout', 0.0, 0.3),
        'lr': trial.suggest_float('lr', 1e-4, 3e-3, log=True),
        'batch_size': trial.suggest_categorical('batch_size', [64, 128, 256]),
    }
    if kind == 'lstm':
        cfg.update(hidden=trial.suggest_categorical('hidden', [32, 64, 128]),
                   layers=trial.suggest_int('layers', 1, 3),
                   mode=trial.suggest_categorical('mode', ['per_service', 'joint']))
    else:
        cfg.update(gat_hidden=trial.suggest_categorical('gat_hidden', [16, 32, 64]),
                   heads=trial.suggest_categorical('heads', [1, 2, 4]),
                   gat_layers=trial.suggest_int('gat_layers', 1, 2),
                   gru_hidden=trial.suggest_categorical('gru_hidden', [32, 64, 128]),
                   edge_direction=trial.suggest_categorical('edge_direction', ['forward', 'reverse', 'both']),
                   use_edge_features=(trial.suggest_categorical('use_edge_features', [True, False])
                                      if has_edge_features else False))
    return cfg


DEFAULT_CONFIG = {
    'lstm': {'window': 12, 'dropout': 0.1, 'lr': 1e-3, 'batch_size': 128, 'hidden': 64, 'layers': 2,
             'mode': 'per_service'},
    'gatgru': {'window': 12, 'dropout': 0.1, 'lr': 1e-3, 'batch_size': 128, 'gat_hidden': 32, 'heads': 2,
               'gat_layers': 1, 'gru_hidden': 64, 'edge_direction': 'forward', 'use_edge_features': True},
}


def model_args(cfg):
    return {k: v for k, v in cfg.items() if k not in ('window', 'lr', 'batch_size', 'weight_decay',
                                                       'max_epochs', 'patience', 'grad_clip')}


def fit_one(kind, cfg, data, edges=None, seed=0, device='cpu', trial=None, verbose=False):
    set_seed(seed)
    use_edges = kind == 'gatgru' and cfg.get('use_edge_features', False) and bool(data.edge_feats)
    model = build_model(kind, model_args(cfg), len(data.node_feats),
                        len(data.edge_feats) if use_edges else 0, edges, data.horizon)
    best, hist = train_model(model, data.get(cfg['window'], 'train'), data.get(cfg['window'], 'val'),
                             cfg, use_edges, device, trial, seed, verbose)
    return model, best, hist, use_edges


def run_optuna(kind, data, edges=None, n_trials=40, timeout=None, seed=42, device='cpu', max_epochs=None):
    """Same sampler, pruner, seed and budget for every model kind."""
    import optuna
    extra = {'max_epochs': max_epochs} if max_epochs else {}

    def objective(trial):
        cfg = {**suggest_config(trial, kind, bool(data.edge_feats)), **extra}
        _, best, _, _ = fit_one(kind, cfg, data, edges, seed, device, trial)
        return best

    study = optuna.create_study(direction='minimize',
                                sampler=optuna.samplers.TPESampler(seed=seed),
                                pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=5))
    study.optimize(objective, n_trials=n_trials, timeout=timeout, gc_after_trial=True)
    return study


def study_config(study, kind, has_edge_features):
    cfg = dict(study.best_params)
    if kind == 'gatgru' and not has_edge_features:
        cfg['use_edge_features'] = False
    return cfg


# =============================================================================== importance
@torch.no_grad()
def permutation_importance(model, w, node_feats, edge_feats=None, use_edges=False, groups=None,
                           n_repeats=5, seed=0, device='cpu'):
    """Increase of val MAE (scaled) when one feature (or group) is shuffled across samples.
    The whole window of a sample is shuffled together, so temporal structure inside the
    feature is kept but its link to the target is broken."""
    rng = np.random.default_rng(seed)
    base_dl = loader(w, use_edges, 512, False)
    base = scaled_mae(predict(model, base_dl, device), w['Y'])
    items = [('node', f, [node_feats.index(f)]) for f in node_feats]
    if use_edges:
        items += [('edge', f, [edge_feats.index(f)]) for f in edge_feats]
    for gname, members in (groups or {}).items():
        ni = [node_feats.index(f) for f in members if f in node_feats]
        ei = [edge_feats.index(f) for f in members if use_edges and edge_feats and f in edge_feats]
        items.append(('group', gname, (ni, ei)))
    rows = []
    for kind, name, idx in items:
        deltas = []
        for _ in range(n_repeats):
            perm = rng.permutation(len(w['X']))
            Xp, Ep = w['X'].copy(), (w['E'].copy() if use_edges else None)
            if kind == 'node':
                Xp[..., idx] = w['X'][perm][..., idx]
            elif kind == 'edge':
                Ep[..., idx] = w['E'][perm][..., idx]
            else:
                ni, ei = idx
                if ni:
                    Xp[..., ni] = w['X'][perm][..., ni]
                if ei:
                    Ep[..., ei] = w['E'][perm][..., ei]
            wp = {'X': Xp, 'Y': w['Y'], **({'E': Ep} if use_edges else {})}
            deltas.append(scaled_mae(predict(model, loader(wp, use_edges, 512, False), device), w['Y']) - base)
        rows.append({'type': kind, 'feature': name, 'delta_mean': float(np.mean(deltas)),
                     'delta_std': float(np.std(deltas))})
    return pd.DataFrame(rows).set_index('feature'), base


# =============================================================================== final eval
@torch.no_grad()
def inference_latency_ms(model, w, use_edges, device='cpu', repeats=20):
    """Latency of one control-loop forecast (batch of 1 window), median over repeats."""
    model.eval()
    xb = torch.from_numpy(w['X'][:1]).to(device)
    eb = torch.from_numpy(w['E'][:1]).to(device) if use_edges else torch.zeros(0)
    times = []
    for _ in range(repeats):
        t = time.perf_counter()
        _forward(model, xb, eb)
        times.append((time.perf_counter() - t) * 1000)
    return float(np.median(times))


def final_runs(kind, cfg, data, edges=None, seeds=(0, 1, 2, 3, 4), device='cpu'):
    """Retrain the chosen config with several seeds; predict the test split in ORIGINAL units."""
    test_w = data.get(cfg['window'], 'test')
    preds, vals, models = [], [], []
    for s in seeds:
        model, best, _, use_edges = fit_one(kind, cfg, data, edges, s, device)
        pz = predict(model, loader(test_w, use_edges, 512, False), device)
        preds.append(fd.invert_scaler(pz, data.scalers['target']))
        vals.append(best)
        models.append(model)
    meta = {'model': kind, 'config': cfg, 'seeds': list(seeds), 'val_mae_scaled': vals,
            'node_features': data.node_feats, 'edge_features': data.edge_feats if use_edges else [],
            'targets': fd.TARGETS, 'services': fd.SERVICES, 'n_params': n_params(models[0]),
            'inference_ms': inference_latency_ms(models[0], test_w, use_edges, device),
            'scalers': data.scalers}
    return np.stack(preds), test_w, models, meta


def save_model(path, model, meta):
    torch.save({'state_dict': model.state_dict(), 'meta': json.loads(json.dumps(meta, default=float))}, path)
