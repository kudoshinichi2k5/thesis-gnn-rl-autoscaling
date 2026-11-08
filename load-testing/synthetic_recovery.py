"""Local-only parameter recovery study for the 36-run profiling design.

This script never imports or invokes kubectl/ssh.  It reuses only the pure plan
and arrival-model functions, generates synthetic telemetry, fits the same
capacity/CPU/fan-out estimators used by the RL notebook, and prints JSON.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import statistics
import heapq
import hashlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from calibration import (estimator_sha256, fit_cpu, identify_capacity,
                         rps_trace_variance, wilson_interval)
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
CONTROLLED = (
    "frontend", "cartservice", "currencyservice",
    "productcatalogservice", "recommendationservice",
)
TOPOLOGICAL = (
    "frontend", "cartservice", "currencyservice",
    "recommendationservice", "productcatalogservice",
)
EDGES = (
    ("frontend", "cartservice"),
    ("frontend", "currencyservice"),
    ("frontend", "productcatalogservice"),
    ("frontend", "recommendationservice"),
    ("recommendationservice", "productcatalogservice"),
)

# Deliberately disclosed synthetic truth; these are not claims about the cluster.
MU_TRUE = {
    "frontend": 45.0, "cartservice": 40.0, "currencyservice": 55.0,
    "productcatalogservice": 100.0, "recommendationservice": 30.0,
}
IDLE_TRUE = {
    "frontend": 0.030, "cartservice": 0.022, "currencyservice": 0.018,
    "productcatalogservice": 0.028, "recommendationservice": 0.035,
}
COST_TRUE = {
    "frontend": 0.0040, "cartservice": 0.0055, "currencyservice": 0.0030,
    "productcatalogservice": 0.0048, "recommendationservice": 0.0070,
}
FANOUT_TRUE = {
    ("frontend", "cartservice"): 0.85,
    ("frontend", "currencyservice"): 1.20,
    ("frontend", "productcatalogservice"): 1.55,
    ("frontend", "recommendationservice"): 0.65,
    ("recommendationservice", "productcatalogservice"): 0.75,
}
BASE_P95 = {
    "frontend": 65.0, "cartservice": 28.0, "currencyservice": 20.0,
    "productcatalogservice": 32.0, "recommendationservice": 42.0,
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROFILING = load_module("synthetic_profiling_plan", HERE / "run-profiling.py")
ARRIVALS = load_module("synthetic_arrival_models", HERE / "arrival_models.py")


def scenario_config(name: str):
    return load_module(f"synthetic_scenario_{name}", HERE / "scenarios" / f"{name}.py").CONFIG


def quantile(values, q):
    values = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not values:
        return float("nan")
    position = (len(values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - position) + values[hi] * (position - lo)










def solve_linear(matrix, vector):
    n = len(vector)
    a = [list(map(float, row)) + [float(vector[i])] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            a[row] = [x - factor * y for x, y in zip(a[row], a[col])]
    return [a[i][-1] for i in range(n)]


def ols_r2(target, predictors):
    """OLS R2 with intercept, implemented locally to keep the study dependency-free."""
    n = len(target)
    columns = [[1.0] * n] + predictors
    gram = [[sum(x * y for x, y in zip(a, b)) for b in columns] for a in columns]
    rhs = [sum(x * y for x, y in zip(column, target)) for column in columns]
    beta = solve_linear(gram, rhs)
    if beta is None:
        return 1.0
    fitted = [sum(beta[j] * columns[j][i] for j in range(len(columns))) for i in range(n)]
    mean = statistics.fmean(target)
    total = sum((v - mean) ** 2 for v in target)
    residual = sum((v - f) ** 2 for v, f in zip(target, fitted))
    return 1.0 if total <= 1e-12 else max(0.0, min(1.0, 1.0 - residual / total))


def vif_table(rows, columns):
    result = {}
    for target_name in columns:
        target = [float(row[target_name]) for row in rows]
        other = [[float(row[name]) for row in rows] for name in columns if name != target_name]
        r2 = ols_r2(target, other)
        result[target_name] = float("inf") if r2 >= 1 - 1e-12 else 1.0 / (1.0 - r2)
    return result




def ready_trajectory(cell, plan, rng):
    ticks = plan["duration_sec"] // 10
    desired = {service: int(cell['replicas'][service]) for service in CONTROLLED}
    ready = desired.copy()
    pending = {service: [] for service in CONTROLLED}
    scheduler_available = 0.0
    by_time = {event["at_sec"]: event for event in cell["schedule"]}
    trajectory = []
    for tick in range(ticks):
        now = tick * 10
        for service in CONTROLLED:
            matured = sum(pod['ready_at'] <= now for pod in pending[service])
            ready[service] += matured
            pending[service] = [pod for pod in pending[service] if pod['ready_at'] > now]
        if now in by_time:
            target = by_time[now]["replicas"]
            for service in CONTROLLED:
                new = target[service]
                desired[service] = new
                if new < ready[service]:
                    ready[service] = new
                    pending[service] = []
                else:
                    pending[service] = pending[service][:max(0, new - ready[service])]
                    while ready[service] + len(pending[service]) < new:
                        # Semi-mechanistic decomposition. Scheduler queueing is
                        # serialized only for the binding stage; pull/startup may
                        # overlap across Pods, matching Kubernetes' asynchronous
                        # kubelet lifecycle better than one monolithic delay.
                        queueing=max(0.0,scheduler_available-now)
                        binding=rng.uniform(0.2,1.0)
                        scheduler_available=max(now,scheduler_available)+binding
                        pull=rng.choice((0.0,2.0,5.0,10.0))
                        startup=rng.choice((5.0,10.0,20.0))
                        pending[service].append(dict(ready_at=now+queueing+binding+pull+startup,
                            queueing=queueing,binding=binding,pull=pull,startup=startup))
        for service in CONTROLLED:
            ready[service] = min(ready[service], desired[service])
        trajectory.append(ready.copy())
    return trajectory








def simulate_campaign(plan, cells, synthetic_seed, misspecified=False):
    """Event-level response-dependent sessions, fixed synthetic call mix.

    One root Bernoulli decision is shared across its RPCs. Baseline queues use
    a FCFS virtual-time approximation. The misspecification branch uses a
    bounded fluid processor-sharing approximation so the stress run cannot
    explode quadratically under overload. Service during a scaling transition
    is approximate and excluded from estimation.
    No claim of reproducing Kubernetes, redirects or endpoint-specific work.
    """
    rng=random.Random(synthetic_seed)
    rows=[]; edge_rows=[]; design=[]
    for cell in cells:
        mu_run={s:MU_TRUE[s] for s in CONTROLLED}
        if misspecified:
            for service in CONTROLLED:
                direction=-1 if rng.random()<.5 else 1
                mu_run[service]*=1+direction*rng.uniform(.10,.20)
        cfg=ARRIVALS.scaled_config(scenario_config(cell['scenario']),cell['load_multiplier'])
        trace=ARRIVALS.build_trace(cfg,plan['duration_sec'],cell['seed'])
        ready=ready_trajectory(cell,plan,rng); nw=plan['duration_sec']//30
        exact=[[0]*5 for _ in range(nw)]; sampled=[[0]*5 for _ in range(nw)]
        cluster_squares=[[0]*5 for _ in range(nw)]
        own=[[[] for _ in CONTROLLED] for _ in range(nw)]
        service_active=[[0.]*5 for _ in range(nw)]
        available={s:0. for s in CONTROLLED}
        ps_state={s:{'active':0.0,'last':0.0} for s in CONTROLLED}
        ec={e:0 for e in EDGES}; dc={e:0 for e in EDGES}
        queue=[]
        for ident,event in enumerate(trace['events']):
            size=1 if event['kind']=='request' else event['size']+(3 if cell['scenario']=='pareto' else 1)
            heapq.heappush(queue,(event['t'],ident,size,event['t']))
        while queue:
            t,ident,left,start=heapq.heappop(queue)
            if t>=plan['duration_sec']+plan['drain_sec']:
                continue
            chosen=rng.random()<.1
            calls={'frontend':1}; finishes={}; own_duration={}; sampled_in_root=defaultdict(int)
            for service in TOPOLOGICAL:
                n=calls.get(service,0); replicas=ready[min(int(t//10),len(ready)-1)][service]
                parent_finish=max([finishes.get(a,t) for a,b in EDGES if b==service]+[t])
                finish=parent_finish
                for _ in range(n):
                    mean_service=1/max(mu_run[service]*replicas,1e-9)
                    if misspecified and service=='cartservice':
                        sigma=1.7
                        work=rng.lognormvariate(math.log(mean_service)-sigma*sigma/2,sigma)
                    else:
                        work=rng.expovariate(1/mean_service)
                    if misspecified:
                        # Bounded fluid PS approximation.  The active mass
                        # decays by the nominal service time and a new request
                        # shares capacity with the surviving mass.  This
                        # intentionally differs from FCFS without maintaining
                        # an unbounded list of queued completion events.
                        state=ps_state[service]
                        elapsed=max(0.0,parent_finish-state['last'])
                        state['active']*=math.exp(-elapsed/max(mean_service,1e-9))
                        sharing=min(20.0,1.0+state['active'])
                        finish=parent_finish+work*sharing
                        state['active']=min(20.0,state['active']+1.0)
                        state['last']=parent_finish
                    else:
                        finish=max(parent_finish,available[service])+work
                        available[service]=finish
                    w=int(finish//30); i=CONTROLLED.index(service)
                    for occupancy_window in range(max(0,int(parent_finish//30)),
                                                  min(nw,int(finish//30)+1)):
                        service_active[occupancy_window][i] += max(
                            0, min(finish,(occupancy_window+1)*30)
                            - max(parent_finish,occupancy_window*30)) / 30
                    if w<nw and service!='frontend':
                        exact[w][i]+=1
                        if chosen:
                            sampled[w][i]+=1; sampled_in_root[w,i]+=1
                            own[w][i].append((finish-parent_finish)*1000)
                finishes[service]=finish; own_duration[service]=(finish-parent_finish)*1000
                for edge in (e for e in EDGES if e[0]==service):
                    mean=FANOUT_TRUE[edge]*n
                    count=int(mean)+int(rng.random()<mean-int(mean))
                    calls[edge[1]]=calls.get(edge[1],0)+count
                    if chosen: ec[edge]+=count; dc[edge]+=n
            finish=max(finishes.values()); w=int(finish//30)
            if w<nw:
                exact[w][0]+=1
                if chosen:
                    sampled[w][0]+=1; sampled_in_root[w,0]+=1
                    own[w][0].append(own_duration['frontend'])
            for (sample_window,service_index),multiplicity in sampled_in_root.items():
                cluster_squares[sample_window][service_index]+=multiplicity**2
            if left>1:
                # Same session structure as the driver; think time is explicit.
                think=float(cfg.get('think_mean_sec',cfg.get('think_mean',.5)))
                heapq.heappush(queue,(finish+rng.expovariate(1/max(think,1e-6)),ident,left-1,start))
            else:
                pass
        for w in range(nw):
            series=ready[3*w:3*w+3]
            stable=w*30>=plan['warmup_sec'] and all(x==series[0] for x in series) and not any(
                w*30-60<=e['at_sec']<(w+1)*30 for e in cell['schedule'])
            d={'frontend_load':exact[w][0]/30}
            for i,s in enumerate(CONTROLLED):
                r=statistics.fmean(x[s] for x in series); x=exact[w][i]/30
                mean=IDLE_TRUE[s]*r+COST_TRUE[s]*x
                rows.append(dict(split=cell['split'],role=cell.get('role','study'),service=s,
                    scenario=cell['scenario'],cell_id=cell['cell_id'],stable=stable,mix_id='synthetic-fixed',
                    focus_service=cell.get('focus_service'),
                    replicas=r,rps=(x if i==0 else sampled[w][i]/3),
                    completed_rps=(x if i==0 else sampled[w][i]/3),true_completed=x,
                    sampled_span_count=sampled[w][i],
                    rps_measurement_var=(0 if i==0 else rps_trace_variance(cluster_squares[w][i])),
                    latency_p95_ms=quantile(own[w][i],.95) if own[w][i] else None,
                    cpu_cores=max(0,rng.gauss(mean,.03*mean)),cpu_limit_cores=(IDLE_TRUE[s]+COST_TRUE[s]*MU_TRUE[s])*r,
                    cpu_throttle_ratio=0.,active_sessions=service_active[w][i],
                    effective_service_latency_ms=(statistics.fmean(own[w][i])
                                                  if own[w][i] else None),
                    effective_latency_valid=bool(own[w][i]),
                    callee_normal=all(exact[w][CONTROLLED.index(b)]/30 < .8*MU_TRUE[b]*statistics.fmean(x[b] for x in series)
                                      for a,b in EDGES if a==s),
                    capture_valid=None,exact_client_count=i==0))
                d['ready_'+s]=r
            if cell['split']=='train' and cell.get('role','study')=='study': design.append(d)
        for a,b in EDGES:
            edge_rows.append(dict(split=cell['split'],source=a,target=b,call_rate=ec[a,b],source_rps=dc[a,b]))
    # Apply the same run-level statistical principle as the real observation
    # contract. The synthetic generator knows trace propagation is complete, but
    # capture is accepted only when the configured head probability is supported
    # by the generated frontend counts rather than by a literal True flag.
    by_cell=defaultdict(list)
    for row in rows:
        by_cell[row['cell_id']].append(row)
    for values in by_cell.values():
        frontend=[row for row in values if row['service']=='frontend']
        trials=sum(int(round(row['true_completed']*30)) for row in frontend)
        sampled=sum(int(row['sampled_span_count']) for row in frontend)
        low,high=wilson_interval(sampled,trials)
        valid=bool(trials>=100 and low is not None and low<=.10<=high)
        for row in values:
            row['capture_valid']=valid
    return rows,edge_rows,design


def fit_refactored_estimators(rows, edge_rows):
    train=[r for r in rows if r['split']=='train' and r.get('role','study')=='study']
    estimates={}
    for s in CONTROLLED:
        data=[r for r in train if r['service']==s]
        cpu=fit_cpu(data); capacity=identify_capacity(data)
        oracle=[dict(r,completed_rps=r['true_completed'],exact_client_count=True) for r in data]
        ref=fit_cpu(oracle)
        estimates[s]=dict(mu=capacity['mu'],idle=cpu['idle'],cost=cpu['cost'],
            oracle_idle=ref['idle'],oracle_cost=ref['cost'],capacity_identified=capacity['capacity_identified'])
    fanout={}
    for a,b in EDGES:
        data=[r for r in edge_rows if r['split']=='train' and r['source']==a and r['target']==b]
        denom=sum(r['source_rps'] for r in data)
        fanout[a+'->'+b]=sum(r['call_rate'] for r in data)/denom if denom else None
    return estimates,fanout


def summarize(recoveries, fanout_recoveries, design_vif, per_service_vif):
    services = {}
    for service in CONTROLLED:
        services[service] = {}
        for parameter, truth in (("mu", MU_TRUE[service]), ("idle", IDLE_TRUE[service]), ("cost", COST_TRUE[service])):
            values = [run[service][parameter] for run in recoveries if run[service][parameter] is not None]
            if not values:
                services[service][parameter] = dict(true=truth,mean_est=None,bias_pct=None,identified_fraction=0)
                continue
            relative = [abs(value - truth) / truth * 100 for value in values]
            services[service][parameter] = {
                "true": truth,
                "identified_fraction": len(values)/len(recoveries),
                "mean_est": statistics.fmean(values),
                "bias_pct": abs(statistics.fmean(values) - truth) / truth * 100,
                "median_abs_error_pct": quantile(relative, 0.50),
                "p90_abs_error_pct": quantile(relative, 0.90),
            }
        services[service]["capacity_identified_rate"] = statistics.fmean(
            float(run[service]["capacity_identified"]) for run in recoveries
        )
        services[service]["oracle_cpu"] = {
            "idle_bias_pct": abs(statistics.fmean(run[service]["oracle_idle"] for run in recoveries)
                                 - IDLE_TRUE[service]) / IDLE_TRUE[service] * 100,
            "cost_bias_pct": abs(statistics.fmean(run[service]["oracle_cost"] for run in recoveries)
                                 - COST_TRUE[service]) / COST_TRUE[service] * 100,
        }
    fanout = {}
    for edge, truth in FANOUT_TRUE.items():
        key = "->".join(edge)
        values = [run[key] for run in fanout_recoveries]
        fanout[key] = {
            "true": truth, "mean_est": statistics.fmean(values),
            "bias_pct": abs(statistics.fmean(values) - truth) / truth * 100,
        }
    acceptance = {
        "mu_bias_le_5pct": all(values["mu"]["bias_pct"] is not None and values['mu']['identified_fraction']>=.95 and values["mu"]["bias_pct"] <= 5.0 for values in services.values()),
        "cost_bias_le_3pct": all(values["cost"]["bias_pct"] <= 3.0 for values in services.values()),
        "idle_bias_le_5pct": all(values["idle"]["bias_pct"] <= 5.0 for values in services.values()),
        "fanout_bias_le_2pct": all(values["bias_pct"] <= 2.0 for values in fanout.values()),
    }
    acceptance["all_pass"] = all(acceptance.values())
    return {
        "monte_carlo_campaigns": len(recoveries),
        "fit_split": "train only (20/36 runs)",
        "estimator_versions": {
            "capacity": "focus_matched_X_N_binomial_mle",
            "cpu": "30s_corrected_score",
            "fanout": "pooled_root_sampled_counts",
        },
        "services": services,
        "fanout": fanout,
        "global_design_vif": design_vif,
        "per_service_vif": per_service_vif,
        "acceptance": acceptance,
    }


def run_monte_carlo(plan,cells,campaigns,seed,workers,misspecified=False):
    recoveries=[];fanout_recoveries=[];design_vif=per_service_vif=None
    jobs=[(plan,cells,seed+i,i==0,misspecified) for i in range(campaigns)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for iteration,(estimate,fanout,dv,sv) in enumerate(pool.map(campaign_fit,jobs)):
            recoveries.append(estimate);fanout_recoveries.append(fanout)
            if dv is not None: design_vif,per_service_vif=dv,sv
            print(f'{"Stress" if misspecified else "Baseline"} campaign {iteration+1}/{campaigns} complete',flush=True)
    return summarize(recoveries,fanout_recoveries,design_vif,per_service_vif)


def compare_summary(candidate, reference):
    """Small deterministic compatibility regression against a published aggregate.

    A 3-campaign mean cannot be bit-identical to a 20-campaign mean. We therefore
    report every relative difference and reuse the registered scientific tolerances;
    this is not a replacement for the 20-campaign recovery study.
    """
    thresholds={'mu':5.0,'cost':3.0,'idle':5.0}
    rows=[]
    for service,current in candidate['services'].items():
        old=reference['services'][service]
        for parameter,limit in thresholds.items():
            a=current[parameter].get('mean_est'); b=old[parameter].get('mean_est')
            if a is None or b is None:
                compatible=a is None and b is None
                difference=None
            else:
                truth=current[parameter]['true']
                difference=abs(a-b)/truth*100
                compatible=difference<=limit
            rows.append(dict(service=service,parameter=parameter,
                candidate_mean=a,reference_mean=b,difference_pct_of_truth=difference,
                tolerance_pct=limit,compatible=compatible))
    for edge,current in candidate['fanout'].items():
        old=reference['fanout'][edge]
        difference=abs(current['mean_est']-old['mean_est'])/current['true']*100
        rows.append(dict(service=edge,parameter='fanout',
            candidate_mean=current['mean_est'],reference_mean=old['mean_est'],
            difference_pct_of_truth=difference,tolerance_pct=2.0,
            compatible=difference<=2.0))
    return dict(compatible=all(row['compatible'] for row in rows),comparisons=rows,
                interpretation=('Compatibility regression only; the reference remains the '
                                '20-campaign scientific artifact.'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaigns", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--plan", default=str(HERE / "profiling-plan.json"))
    parser.add_argument('--output')
    parser.add_argument('--baseline-output',default='artifacts/rl/synthetic_recovery_v4.json',
                        help='baseline companion written by a stress run')
    parser.add_argument('--workers',type=int,default=4,help='independent local Monte Carlo worker processes')
    parser.add_argument('--stress-misspecification',action='store_true')
    parser.add_argument('--regression-reference',
                        help='existing 20-campaign JSON to compare without overwriting it')
    args = parser.parse_args()
    if args.campaigns < 2:
        raise SystemExit("Use at least two campaigns to measure recovery variation")
    plan = PROFILING.load_plan(args.plan, "main")
    cells = PROFILING.execution_cells(plan)
    if len(cells) != 39:
        raise RuntimeError(f"Expected 36 study + 3 control executions, got {len(cells)}")
    if not 1<=args.workers<=8: parser.error('workers must be 1..8')
    baseline=run_monte_carlo(plan,cells,args.campaigns,args.seed,args.workers,False)
    plan_hash=cells[0]['plan_sha256']
    code_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
        (Path(__file__),HERE/'calibration.py',HERE/'run-profiling.py',HERE/'arrival_models.py')}
    queue_note=('Synthetic fixed-mix DAG; baseline uses virtual-time FCFS and stress uses '
                'bounded fluid processor-sharing; neither is an exact Kubernetes queue.')
    def add_provenance(result):
        result['plan_sha256']=plan_hash
        result['simulated_runs_per_campaign']=len(cells)
        result['actual_contract_pass']=False
        result['actual_contract_reason']=('Synthetic recovery cannot validate the real '
            'cluster capture, Effective Service Latency or downstream-normal contract.')
        result['GO']=False
        result['estimator_versions']={'capacity':'focus_matched_X_N_binomial_mle','cpu':'30s_corrected_score','fanout':'pooled_root_sampled_counts'}
        result['estimator_sha256']=estimator_sha256()
        result['limitations']=[queue_note,
            'One root decision per HTTP operation, correlated child counts; no redirects/retries.',
            'Synthetic downstream-normal evidence is generator instrumentation, not a real observation.',
            'Confidence interval coverage not established.']
        result['code_sha256']=code_hashes
        return result
    if args.stress_misspecification:
        stressed=run_monte_carlo(plan,cells,args.campaigns,args.seed,args.workers,True)
        stress_cpu_pass=all(v['cost']['bias_pct'] is not None and v['cost']['bias_pct']<=8 and
                            v['idle']['bias_pct'] is not None and v['idle']['bias_pct']<=8
                            for v in stressed['services'].values())
        baseline=add_provenance(baseline)
        if not args.regression_reference:
            baseline_target=Path(args.baseline_output)
            baseline_target.parent.mkdir(parents=True,exist_ok=True)
            baseline_target.write_text(json.dumps(baseline,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        output=dict(schema_version=1,baseline=baseline,misspecified=stressed,
            stress_acceptance=dict(cpu_cost_idle_bias_le_8pct=stress_cpu_pass,
                                   estimator_completed=True,
                                   graceful_degradation_pass=stress_cpu_pass))
    else:
        output=baseline
    output=add_provenance(output)
    if args.regression_reference:
        reference=json.loads(Path(args.regression_reference).read_text(encoding='utf-8'))
        candidate_baseline=output['baseline'] if args.stress_misspecification else output
        reference_baseline=reference.get('baseline',reference)
        regression=dict(baseline=compare_summary(candidate_baseline,reference_baseline))
        if args.stress_misspecification:
            regression['misspecified']=compare_summary(output['misspecified'],reference['misspecified'])
        output['regression']=regression
    target=Path(args.output or ('artifacts/rl/synthetic_regression_3.json'
        if args.regression_reference else ('artifacts/rl/synthetic_stress_report.json'
        if args.stress_misspecification else 'artifacts/rl/synthetic_recovery_v4.json')))
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(output,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(f'Saved {target}; GO=False')


def campaign_fit(job):
    plan,cells,seed,compute_vif,misspecified=job
    rows,edges,design=simulate_campaign(plan,cells,seed,misspecified)
    estimate,fanout=fit_refactored_estimators(rows,edges)
    dv=sv=None
    if compute_vif:
        dv=vif_table(design,['frontend_load']+['ready_'+s for s in CONTROLLED])
        sv={s:vif_table([r for r in rows if r['split']=='train' and r['service']==s],['replicas','rps']) for s in CONTROLLED}
    return estimate,fanout,dv,sv


if __name__ == "__main__":
    main()
