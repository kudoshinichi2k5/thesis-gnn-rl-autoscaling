"""Offline 30s calibration diagnostics and conservative estimators (stdlib only).

No cluster calls. Used by the collector, tests and synthetic recovery study.
"""
import argparse
import csv
import hashlib
import inspect
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit


RULES = dict(window_sec=30, little_relative_error=0.15, min_samples=4,
             n_growth=1.5, max_x_growth=1.10, cpu_limit_fraction=0.90,
             throttle_fraction=0.10, pressure_fraction=0.75,
             effective_latency_growth=1.5,
             # 99% rather than pointwise 95%: the contract is tested repeatedly
             # across endpoints and runs, so 95% creates avoidable family-wise
             # false rejections even when the configured sampler is correct.
             capture_probability=0.10, capture_confidence_z=2.5758293035489004,
             min_capture_trials=100, min_capture_expected=10,
             min_endpoint_capture_coverage=0.80,
             min_trace_propagation=0.95, min_effective_latency_spans=3,
             downstream_latency_multiplier=1.5, min_contract_windows=4,
             min_controlled_resource_coverage=0.90,
             min_auxiliary_resource_coverage=0.75,
             mu_bias_pct=5, cost_bias_pct=3, idle_bias_pct=5, fanout_bias_pct=2,
             identified_fraction=0.95, bootstrap_seed=20261105)


# Static deployment topology. Edge rates are dynamic, but the deployed call graph is
# not. redis-cart is TCP and therefore has no HTTP server span; its resource telemetry
# is still required when deciding whether cartservice's downstream was normal.
DIRECT_CALLEES = {
    'frontend': ('productcatalogservice', 'currencyservice', 'cartservice',
                 'recommendationservice', 'shippingservice', 'checkoutservice',
                 'adservice'),
    'recommendationservice': ('productcatalogservice',),
    'checkoutservice': ('paymentservice', 'productcatalogservice', 'shippingservice',
                        'emailservice', 'currencyservice', 'cartservice'),
    'cartservice': ('redis-cart',),
}


def wilson_interval(successes, trials, z=RULES['capture_confidence_z']):
    """Binomial Wilson interval; invalid count pairs fail closed."""
    if trials <= 0 or successes < 0 or successes > trials:
        return None, None
    p = successes / trials
    denominator = 1 + z*z/trials
    center = (p + z*z/(2*trials)) / denominator
    radius = z * math.sqrt(p*(1-p)/trials + z*z/(4*trials*trials)) / denominator
    return max(0, center-radius), min(1, center+radius)


def infer_capture_contract(capture_rows, raw_spans, sampling, rules=RULES):
    """Validate head sampling and trace propagation without inventing internal R_real.

    Client redirect hops are the observable frontend trial population. Istio head
    sampling is chosen at the frontend root and propagated through the whole trace,
    therefore a validated frontend sampling probability applies to internal sampled
    server spans too. Trace-id overlap is retained as an audit diagnostic, not a
    second gate: service-specific Jaeger queries can retain different portions of a
    trace (lookback/chunk boundaries), so requiring complete overlap would reject a
    correctly sampled internal service without measuring a distinct probability.
    """
    summaries = [row for row in capture_rows if row.get('record_type') == 'summary']
    total_trials = sum(int(row['client_hops']) for row in summaries)
    total_successes = sum(int(row['frontend_spans']) for row in summaries)
    low, high = wilson_interval(total_successes, total_trials, rules['capture_confidence_z'])
    enough = (total_trials >= rules['min_capture_trials'] and
              total_trials * sampling >= rules['min_capture_expected'])
    global_valid = bool(enough and low is not None and low <= sampling <= high)

    eligible = [row for row in summaries
                if int(row['client_hops']) >= rules['min_capture_trials']]
    consistent_trials = 0
    endpoint_evidence = []
    for row in eligible:
        trials, successes = int(row['client_hops']), int(row['frontend_spans'])
        endpoint_low, endpoint_high = wilson_interval(successes, trials,
                                                      rules['capture_confidence_z'])
        valid = bool(endpoint_low is not None and endpoint_low <= sampling <= endpoint_high)
        consistent_trials += trials if valid else 0
        endpoint_evidence.append(dict(endpoint=row['endpoint'], trials=trials,
            sampled=successes, ci_low=endpoint_low, ci_high=endpoint_high, valid=valid))
    endpoint_coverage = consistent_trials / total_trials if total_trials else 0.0
    endpoint_valid = bool(eligible and
        endpoint_coverage >= rules['min_endpoint_capture_coverage'])
    frontend_valid = global_valid and endpoint_valid

    traces = defaultdict(set)
    for span in raw_spans:
        trace_id = span.get('trace_id')
        if trace_id:
            traces[span['service']].add(trace_id)
    roots = traces.get('frontend', set())
    services = {}
    for service, service_traces in traces.items():
        shared = len(service_traces & roots)
        overlap = shared / len(service_traces) if service_traces else 0.0
        services[service] = dict(sampled_traces=len(service_traces),
            traces_shared_with_frontend=shared,
            shared_fraction_of_service_traces=overlap,
            # Compatibility alias retained for older audit readers.
            frontend_trace_overlap=overlap,
            propagation_observed=bool(service == 'frontend' or
                                      overlap >= rules['min_trace_propagation']),
            valid=bool(frontend_valid and
                       len(service_traces) >= rules['min_capture_expected']))
    return dict(configured_probability=sampling, global_trials=total_trials,
        global_sampled=total_successes, global_ci_low=low, global_ci_high=high,
        global_valid=global_valid, endpoint_coverage=endpoint_coverage,
        endpoint_valid=endpoint_valid, frontend_valid=frontend_valid,
        endpoints=endpoint_evidence, services=services)


def rps_trace_variance(sampled_cluster_squares, sampling=0.1, window=30):
    """Horvitz-Thompson variance for independently sampled HTTP roots.

    Each sampled root contributes m service spans to one window. Within-root
    multiplicity is squared; a span-level Binomial correction would understate it.
    """
    if not 0 < sampling < 1 or window <= 0 or sampled_cluster_squares < 0:
        raise ValueError('invalid trace variance inputs')
    return (1-sampling)/sampling**2 * sampled_cluster_squares/window**2


def read_csv(path):
    with Path(path).open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo = int(pos)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (pos - lo)


def exclusive_ms(start_us, duration_us, children):
    """Subtract union of clipped CHILD_OF intervals, never overlapping durations twice."""
    end = start_us + duration_us
    intervals = sorted((max(start_us, a), min(end, b)) for a, b in children
                       if min(end, b) > max(start_us, a))
    covered, cursor = 0, start_us
    for a, b in intervals:
        covered += max(0, b - max(cursor, a))
        cursor = max(cursor, b)
    return max(0, duration_us - covered) / 1000


def endpoint(path, method='GET'):
    path = urlsplit(path).path
    if path.startswith('/product/'):
        path = '/product/[id]'
    return method.upper() + ' ' + (path or '/')


def occupancy(records, begin, end):
    return sum(max(0, min(end, finish if finish is not None else end) - max(begin, start))
               for start, finish in records) / (end - begin)


def window_rows(records, origin, duration, window=30):
    """records: t_start, t_end optional, latency_ms, error. Completion-cohort latency.

    Occupancy counts records intersecting the window, including censored ones. Little
    residual is diagnostic only unless steady_state is independently established.
    """
    result = []
    for i in range(int(duration // window)):
        begin, end = origin + i * window, origin + (i + 1) * window
        arrivals = [r for r in records if begin <= r['t_start'] < end]
        completions = [r for r in records if r['t_end'] is not None and begin <= r['t_end'] < end]
        left = sum(r['t_start'] < begin and (r['t_end'] is None or r['t_end'] >= begin) for r in records)
        right = sum(r['t_start'] < end and (r['t_end'] is None or r['t_end'] >= end) for r in records)
        latencies = [r['latency_ms'] for r in completions if r['latency_ms'] is not None]
        inflight = occupancy([(r['t_start'], r['t_end']) for r in records], begin, end)
        little = (len(completions) / window * statistics.fmean(latencies) / 1000) if latencies else None
        result.append(dict(timestamp=begin, window_end=end, arrival_count=len(arrivals),
            completion_count=len(completions), error_count=sum(bool(r['error']) for r in completions),
            latency_p50_ms=quantile(latencies, .5), latency_p95_ms=quantile(latencies, .95),
            latency_mean_ms=statistics.fmean(latencies) if latencies else None,
            inflight_mean=inflight, inflight_left=left, inflight_right=right,
            conservation_residual=right-left-len(arrivals)+len(completions),
            little_relative_error=abs(inflight-little)/max(inflight, 1e-9) if little is not None else None,
            censored_at_measurement_end=sum(r['t_start'] < origin+duration and
                (r['t_end'] is None or r['t_end'] >= origin+duration) for r in arrivals)))
    return result


def plateau_label(begin, end, journal, warmup, post_ready):
    starts = [r for r in journal if r['kind'] == 'start']
    if not starts:
        return 'unknown'
    origin = starts[0]['origin']
    if begin < origin + warmup:
        return 'initial_warmup'
    actions = [r for r in journal if r['kind'] == 'action_start']
    if any(begin < r['epoch'] < end for r in actions):
        return 'transition'
    before = [r for r in actions if r['epoch'] <= begin]
    action = before[-1] if before else None
    at = action['epoch'] if action else origin
    target = action['target'] if action else starts[0]['profile']['replicas']
    snaps = [r for r in journal if r['kind'] == 'snapshot' and at <= r['epoch'] <= end]
    matched = [r for r in snaps if all(r['state'][s]['available'] == n and
               r['state'][s]['desired'] == n for s, n in target.items())]
    if not matched or begin < matched[0]['epoch'] + post_ready:
        return 'post_ready_warmup'
    if any(not all(r['state'][s]['available'] == n and r['state'][s]['desired'] == n
                   for s, n in target.items()) for r in snaps if r['epoch'] >= begin):
        return 'transition'
    return 'stable'


def available_replicas_at(journal, service, timestamp, max_age=15):
    """Return observed Available replicas from the local 5s capacity journal.

    Prometheus deployment gauges can be absent at an otherwise valid aligned
    query timestamp.  The controller journal is the authoritative intervention
    clock, but it may only be used when a recent snapshot exists; never fill a
    missing value with the plan target or a constant.
    """
    snapshots = [row for row in journal
                 if row.get('kind') == 'snapshot'
                 and row.get('epoch') is not None
                 and float(row['epoch']) <= float(timestamp)
                 and service in row.get('state', {})]
    if not snapshots:
        raise ValueError(f'missing capacity snapshot for {service} at {timestamp}')
    latest = max(snapshots, key=lambda row: float(row['epoch']))
    age = float(timestamp) - float(latest['epoch'])
    if age < 0 or age > max_age:
        raise ValueError(f'stale capacity snapshot for {service} at {timestamp}: age={age:.3f}s')
    value = latest['state'][service].get('available')
    try:
        replicas = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'invalid capacity snapshot for {service} at {timestamp}') from None
    if not math.isfinite(replicas) or replicas < 0 or not replicas.is_integer():
        raise ValueError(f'invalid Available replicas for {service} at {timestamp}: {value!r}')
    return replicas


def required_float(row, field, context):
    value = row.get(field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'missing/non-numeric {field} for {context}') from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'invalid {field} for {context}: {value!r}')
    return number


def measured_float(row, field):
    """Parse a non-negative measurement, returning None for an absent sample."""
    if row is None or row.get(field) in (None, ''):
        return None
    try:
        number = float(row[field])
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def required_resource_coverage(service, controlled_services):
    return (RULES['min_controlled_resource_coverage']
            if service == 'frontend' or service in set(controlled_services)
            else RULES['min_auxiliary_resource_coverage'])


def observation_contract_services(profile):
    """Return the services whose contract this run was designed to excite.

    A diagnostic run intentionally overloads one focus service. Requiring frontend's
    downstream-normal condition while currency/cart is deliberately stressed is a
    contradictory gate. Frontend capture remains a global prerequisite inside
    infer_capture_contract; only the service-local latency/downstream contract is scoped.
    """
    focus = profile.get('focus_service')
    if profile.get('role') == 'diagnostic' and focus:
        return {focus}
    controlled = set(profile.get('controlled_services', ()))
    return {'frontend', *controlled} if controlled else {'frontend'}


def aliasing(rows):
    """Audit per-series raw counter deltas. Double-delta is a heuristic, not proof."""
    groups = defaultdict(list)
    for row in rows:
        groups[row['series_id']].append((float(row['timestamp']), float(row['cpu_seconds_total'])))
    out = []
    for key, values in groups.items():
        values.sort()
        differences = [(b[0]-a[0], b[1]-a[1]) for a, b in zip(values, values[1:])]
        valid = [dv for dt, dv in differences if 9 <= dt <= 11 and dv >= 0]
        positives = [dv for dv in valid if dv > 1e-12]
        median = statistics.median(positives) if positives else None
        out.append(dict(series_id=key, deltas=len(valid), resets=sum(dv < 0 for _, dv in differences),
            gaps=sum(not 9 <= dt <= 11 for dt, _ in differences),
            zero_fraction=sum(dv <= 1e-12 for dv in valid)/len(valid) if valid else None,
            double_fraction=sum(1.75*median <= dv <= 2.25*median for dv in valid)/len(valid)
                if median and valid else None))
    return out


def identify_capacity(rows, rules=RULES):
    """X(N) by fixed replicas, matched scenario/mix and stable plateaus; fail closed.

    Effective Service Latency is the proxy-inclusive server-span duration. It is not
    claimed to be application-exclusive time, so downstream-normal evidence is required
    before its growth can support a service-local capacity conclusion.
    """
    data = [r for r in rows if r.get('split') == 'train' and r.get('role', 'study') == 'study'
            and r.get('stable') is True and r.get('effective_latency_valid') is True
            and r.get('callee_normal') is True and r.get('capture_valid') is True
            and (r.get('focus_service') is None or r.get('focus_service') == r.get('service'))]
    groups = defaultdict(list)
    for r in data:
        required = ['active_sessions', 'completed_rps', 'replicas',
                    'effective_service_latency_ms',
                    'cpu_cores', 'cpu_limit_cores', 'cpu_throttle_ratio']
        if all(isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) and r[k] >= 0 for k in required):
            # Keep both endpoint mix and workload scenario fixed. Pooling unlike
            # arrival processes can manufacture a false plateau (Simpson's
            # paradox); the focus schedule supplies enough windows per stratum.
            groups[(r['replicas'], r.get('scenario'), r.get('mix_id'))].append(r)
    estimates, evidence = [], []
    for group, values in groups.items():
        values.sort(key=lambda r: r['active_sessions'])
        n = len(values)//3
        if n < rules['min_samples'] or group[0] <= 0:
            continue
        low, middle, high = values[:n], values[n:2*n], values[-n:]
        avg = lambda rs, key: statistics.fmean(r[key] for r in rs)
        saturated = (avg(high, 'active_sessions') >= rules['n_growth']*max(avg(middle, 'active_sessions'), 1e-9)
                     and avg(high, 'completed_rps') <= rules['max_x_growth']*avg(middle, 'completed_rps')
                     and avg(high, 'completed_rps') >= 0.9*avg(middle, 'completed_rps'))
        pressure = statistics.fmean(float(
            r['cpu_throttle_ratio'] >= rules['throttle_fraction'] or
            (r['cpu_limit_cores'] > 0 and r['cpu_cores']/r['cpu_limit_cores'] >= rules['cpu_limit_fraction']))
            for r in high) >= rules['pressure_fraction']
        latency_slow = (avg(high, 'effective_service_latency_ms') >=
            rules['effective_latency_growth'] *
            max(avg(low, 'effective_service_latency_ms'), 1e-9))
        if saturated and pressure and latency_slow:
            pressured = [r for r in high if r['cpu_throttle_ratio'] >= rules['throttle_fraction'] or
                         (r['cpu_limit_cores'] > 0 and
                          r['cpu_cores']/r['cpu_limit_cores'] >= rules['cpu_limit_fraction'])]
            if len(pressured) < rules['min_samples']:
                continue
            # For a verified saturated plateau, sum(K)/(p*T*sum(replicas))
            # is the binomial MLE. completed_rps is already the HT count rate,
            # so the exposure-weighted expression below is equivalent and does
            # not select a noisy upper/lower quantile.
            estimates.append(sum(r['completed_rps'] for r in pressured)
                             / sum(r['replicas'] for r in pressured))
            evidence.append(dict(replicas=group[0], scenario=group[1], mix_id=group[2],
                                 samples=len(pressured)))
    identified = bool(estimates)
    return dict(capacity_identified=identified, mu=statistics.fmean(estimates) if identified else None,
                evidence=evidence, reason=None if identified else
                'insufficient matched X(N), effective-latency, pressure, capture or downstream evidence')


def fit_cpu(rows, sampling=0.1, window=30):
    """Corrected score on matched windows; only train, known-valid capture and stable rows."""
    rows = [r for r in rows if r.get('split') == 'train' and r.get('role', 'study') == 'study'
            and r.get('stable') and r.get('capture_valid')]
    if len(rows) < 20:
        return dict(identified=False, idle=None, cost=None, reason='insufficient stable capture support')
    a=b=c=d=e=0.0
    for r in rows:
        n, x, y = r['replicas'], r['completed_rps'], r['cpu_cores']
        if not all(math.isfinite(v) and v >= 0 for v in (n,x,y)):
            return dict(identified=False, idle=None, cost=None, reason='invalid measurement')
        variance = (0 if r.get('exact_client_count') else
                    r.get('rps_measurement_var', x*(1-sampling)/(sampling*window)))
        if not math.isfinite(variance) or variance < 0:
            return dict(identified=False, idle=None, cost=None, reason='invalid observation variance')
        a+=n*n; b+=n*x; c+=x*x-variance; d+=n*y; e+=x*y
    det=a*c-b*b
    if c <= 0 or det <= 1e-10*max(1,a*c):
        return dict(identified=False, idle=None, cost=None, reason='singular corrected Gram')
    idle, cost = (d*c-e*b)/det, (e*a-d*b)/det
    if idle <= 0 or cost <= 0:
        return dict(identified=False, idle=None, cost=None, reason='nonpositive interior solution')
    return dict(identified=True, idle=idle, cost=cost, reason=None)


def estimator_sha256():
    """Semantic estimator provenance; recorded but never blocks raw collection."""
    body = dict(
        rules=RULES,
        functions={fn.__name__: inspect.getsource(fn) for fn in
                   (rps_trace_variance, identify_capacity, fit_cpu)},
    )
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()


def analyze(root):
    root=Path(root)
    meta=json.loads((root/'meta.json').read_text())
    driver=json.loads((root/'driver_report.json').read_text())
    origin, duration=driver['trace_start_epoch'], meta['duration_sec']
    clients=read_csv(root/'client_requests.csv')
    sessions=read_csv(root/'sessions.csv')
    records=[]
    for index,r in enumerate(clients):
        chain=json.loads(r.get('redirect_chain') or '[]')
        redirect_count=(int(r['redirect_count']) if r.get('redirect_count') not in (None,'')
                        else max(0,len(chain)-1))
        records.append(dict(request_id=r.get('request_id',str(index+1)),
            t_start=float(r['t_start']),t_end=float(r['t_end']) if r['t_end'] else None,
            latency_ms=float(r['latency_ms']) if r['latency_ms'] else None,
            error=bool(r['error']), status=int(r.get('status') or (200 if chain else 0)),
            endpoint=r['endpoint'],redirect_count=redirect_count,
            redirect_chain=r.get('redirect_chain','')))
    for record in records:
        chain=json.loads(record['redirect_chain'] or '[]')
        expected_hops=record['redirect_count']+(1 if record['status'] else 0)
        if len(chain)!=expected_hops:
            raise ValueError(f'redirect evidence mismatch for request {record["request_id"]}: '
                             f'{len(chain)} != {expected_hops}')
    journal=[json.loads(line) for line in (root/'capacity_events.jsonl').read_text().splitlines()]
    windows=window_rows(records,origin,duration)
    intervals=[(float(r['actual_start_ts']),float(r['end_ts']) if r['end_ts'] else None)
               for r in sessions if r['actual_start_ts']]
    for row in windows:
        row['phase']=plateau_label(row['timestamp'],row['window_end'],journal,
                                  meta['profiling']['warmup_sec'],meta['profiling'].get('post_ready_warmup_sec',30))
        row['active_sessions_mean']=occupancy(intervals,row['timestamp'],row['window_end'])
    write_csv(root/'client_windows_30s.csv',windows,list(windows[0]))
    raw=read_csv(root/'jaeger_server_spans.csv')
    observed_services={r['service'] for r in raw}
    missing=set(meta['profiling'].get('controlled_services',()))-observed_services
    if missing:
        raise ValueError('no sampled spans for controlled service(s): '+', '.join(sorted(missing)))
    service_windows=[]
    # One head decision samples an entire trace; RPC multiplicity within that
    # trace changes count variance. Horvitz-Thompson cluster variance for 30s RPS.
    trace_clusters=defaultdict(int)
    for span in raw:
        finish=float(span.get('end_timestamp') or
                     (float(span['timestamp'])+float(span['latency_ms'])/1000))
        if origin <= finish < origin+duration:
            bucket=int((finish-origin)//30)
            trace_clusters[(span['service'],bucket,span['trace_id'])]+=1
    cluster_variance=defaultdict(float)
    for (service,bucket,_),count in trace_clusters.items():
        cluster_variance[(service,bucket)]+=rps_trace_variance(count**2)
    for svc in sorted({r['service'] for r in raw}):
        data=[dict(t_start=float(r['timestamp']),t_end=float(r.get('end_timestamp') or
                   (float(r['timestamp'])+float(r['latency_ms'])/1000)),
                   latency_ms=float(r['latency_ms']),error=int(r['error'])) for r in raw if r['service']==svc]
        for row in window_rows(data,origin,duration):
            observed_completion_count = row['completion_count']
            effective_mean = row['latency_mean_ms']
            effective_count = observed_completion_count
            row['service']=svc
            row['count_source']='client' if svc=='frontend' else 'sampled_jaeger'
            # Frontend contract uses actual client counts, never Jaeger-scaled counts.
            if svc=='frontend':
                row.update({k:v for k,v in windows[int((row['timestamp']-origin)/30)].items()
                            if k in row and k not in ('service','count_source')})
            row['phase'] = windows[int((row['timestamp']-origin)/30)]['phase']
            row['effective_service_latency_ms'] = effective_mean
            row['effective_latency_span_count'] = effective_count
            row['R_obs'] = observed_completion_count / 30
            row['R_real'] = (row['completion_count'] / 30 if svc == 'frontend' else None)
            row['p_dynamic'] = (row['R_obs'] / row['R_real']
                                if row['R_real'] and row['R_real'] > 0 else None)
            # X(N) requires service-local concurrency, not the number of whole
            # client sessions. Internal concurrency is Horvitz-Thompson scaled
            # from trace-sampled server-span occupancy; its uncertainty remains
            # part of the capture gate.
            row['service_concurrency_mean'] = (row['inflight_mean'] if svc == 'frontend'
                else row['inflight_mean'] / (meta['profiling'].get('sampling_percentage',10) / 100))
            service_windows.append(row)
    if service_windows:
        write_csv(root/'service_windows_30s.csv',service_windows,list(service_windows[0]))
    # Ratios are descriptive. Block bootstrap CI handles overdispersion; no binomial
    # "probability" claim when multiple spans/redirects occur per operation.
    bins=defaultdict(lambda: defaultdict(lambda: [0,0,0]))
    for r in records:
        if origin <= r['t_start'] < origin+duration:
            i=int((r['t_start']-origin)//30)
            bins[r['endpoint']][i][0]+=1
            for hop in json.loads(r['redirect_chain'] or '[]'):
                bins[endpoint(hop['path'],hop['method'])][i][1]+=1
    for r in raw:
        if r['service']=='frontend' and origin <= float(r['timestamp']) < origin+duration:
            bins[r.get('endpoint') or 'UNMAPPED'][int((float(r['timestamp'])-origin)//30)][2]+=1
    capture=[]; rng=random.Random(RULES['bootstrap_seed']); nb=int(duration//30)
    for ep, counts in sorted(bins.items()):
        values=[counts[i] for i in range(nb)]
        operations,hops,spans=map(sum,zip(*values))
        draws=[]
        for _ in range(300):
            sampled=[rng.choice(values) for _ in values]
            denom=sum(v[1] for v in sampled)
            if denom: draws.append(sum(v[2] for v in sampled)/denom)
        capture.append(dict(endpoint=ep,client_operations=operations,client_hops=hops,frontend_spans=spans,
            timestamp=None,record_type='summary',p_dynamic=spans/hops if hops else None,
            span_per_operation=spans/operations if operations else None,
            span_per_hop=spans/hops if hops else None,ratio_ci_low=quantile(draws,.025),
            ratio_ci_high=quantile(draws,.975),ci_method='30s-block-bootstrap',
            boundary_note='redirect hops attributed to operation start; censor/boundary may bias ratio'))
        for i,(window_operations,window_hops,window_spans) in enumerate(values):
            capture.append(dict(endpoint=ep,client_operations=window_operations,client_hops=window_hops,
                frontend_spans=window_spans,timestamp=origin+i*30,record_type='window',
                p_dynamic=window_spans/window_hops if window_hops else None,
                span_per_operation=window_spans/window_operations if window_operations else None,
                span_per_hop=window_spans/window_hops if window_hops else None,
                ratio_ci_low=None,ratio_ci_high=None,ci_method='none-window-count',
                boundary_note='30s operation-start attribution'))
    if capture: write_csv(root/'capture_by_endpoint.csv',capture,list(capture[0]))
    sampling = meta['profiling'].get('sampling_percentage', 10) / 100
    capture_contract = infer_capture_contract(capture, raw, sampling)
    aliases=aliasing(read_csv(root/'cpu_counter_10s.csv')) if (root/'cpu_counter_10s.csv').exists() else []
    node={(round(float(r['window_end']),3),r['service']):r
          for r in read_csv(root/'node_windows_30s.csv')}
    cart_operations=[]
    grouped_cart=defaultdict(list)
    for span in raw:
        if span['service']=='cartservice':
            finish=float(span.get('end_timestamp') or
                         (float(span['timestamp'])+float(span['latency_ms'])/1000))
            if origin <= finish < origin+duration:
                grouped_cart[(origin+int((finish-origin)//30)*30,
                              span.get('operation') or 'UNKNOWN')].append(float(span['latency_ms']))
    for (timestamp,operation),latencies in sorted(grouped_cart.items()):
        resource=node.get((round(timestamp+30,3),'cartservice'))
        cart_operations.append(dict(timestamp=timestamp,operation=operation,count=len(latencies),
            latency_p95_ms=quantile(latencies,.95),latency_p99_ms=quantile(latencies,.99),
            cpu_throttle_ratio=(required_float(resource,'cpu_throttle_ratio',
                f'cartservice window {timestamp}') if resource and resource.get('cpu_throttle_ratio') else None)))
    if cart_operations:
        write_csv(root/'cart_operation_latency_30s.csv',cart_operations,list(cart_operations[0]))
    service_window_index={(r['service'],round(r['timestamp'],3)):r for r in service_windows}
    latency_limits={}
    for service in observed_services:
        values=[r['effective_service_latency_ms'] for r in service_windows
                if r['service']==service
                and r['effective_latency_span_count'] >= RULES['min_effective_latency_spans']
                and r['effective_service_latency_ms'] is not None]
        latency_limits[service]=(quantile(values,.95)*RULES['downstream_latency_multiplier']
                                 if values else None)

    def downstream_normal(service, timestamp):
        """Conservative per-window downstream evidence from spans and cgroups."""
        failures=[]
        for callee in DIRECT_CALLEES.get(service, ()):
            resource=node.get((round(timestamp+30,3),callee))
            throttle=measured_float(resource,'cpu_throttle_ratio')
            used=measured_float(resource,'cpu_cores')
            limit=measured_float(resource,'cpu_limit_cores')
            if throttle is None:
                failures.append(f'{callee}:missing_throttle')
                continue
            if throttle >= RULES['throttle_fraction']:
                failures.append(f'{callee}:throttled')
                continue
            if limit is not None and limit > 0 and (used is None or
                    used/limit >= RULES['cpu_limit_fraction']):
                failures.append(f'{callee}:cpu_pressure')
                continue
            downstream=service_window_index.get((callee,round(timestamp,3)))
            ceiling=latency_limits.get(callee)
            if callee in observed_services:
                if (downstream is None or ceiling is None or
                        downstream['effective_latency_span_count'] <
                        RULES['min_effective_latency_spans'] or
                        downstream['effective_service_latency_ms'] > ceiling):
                    failures.append(f'{callee}:effective_latency')
            elif callee != 'redis-cart':
                failures.append(f'{callee}:missing_span')
            elif limit is None or limit <= 0 or used is None:
                # TCP-only redis has no HTTP span, so resource headroom is the only
                # available downstream evidence and must be complete.
                failures.append(f'{callee}:missing_resource')
        return not failures, ';'.join(failures)

    features=[]
    resource_counts=defaultdict(lambda: {'total':0,'usable':0})
    for r in service_windows:
        n=node.get((round(r['window_end'],3),r['service']))
        w=windows[int(round((r['timestamp']-origin)/30))]
        context=f'{r["service"]} window ending {r["window_end"]}'
        journal_replicas=available_replicas_at(journal,r['service'],r['window_end'])
        prometheus_replicas=measured_float(n,'replicas')
        if (prometheus_replicas is not None and w['phase']=='stable'
                and prometheus_replicas != journal_replicas):
            raise ValueError(f'replica gauge/journal mismatch for {context}: '
                             f'{prometheus_replicas} != {journal_replicas}')
        resource_counts[r['service']]['total']+=1
        cpu_cores=measured_float(n,'cpu_cores')
        cpu_limit=measured_float(n,'cpu_limit_cores')
        throttle=measured_float(n,'cpu_throttle_ratio')
        if cpu_cores is None:
            # A range query can miss one evaluation timestamp while the raw
            # counter and surrounding windows remain valid. Drop this estimator
            # row and enforce run-level coverage below; never impute CPU.
            continue
        resource_counts[r['service']]['usable']+=1
        if cpu_limit is not None and throttle is not None:
            resource_counts[r['service']]['pressure_usable'] = (
                resource_counts[r['service']].get('pressure_usable',0)+1)
        service_capture = capture_contract['services'].get(r['service'], {})
        effective_valid=(r['effective_latency_span_count'] >=
                         RULES['min_effective_latency_spans'] and
                         r['effective_service_latency_ms'] is not None)
        capture_valid=service_capture.get('valid') is True
        p_for_ht=sampling if capture_valid else None
        r_ht=(r['R_obs']/p_for_ht if p_for_ht else None)
        callee_ok, callee_reason=downstream_normal(r['service'],r['timestamp'])
        features.append(dict(service=r['service'],timestamp=r['timestamp'],
            split=meta['profiling']['split'],role=meta['profiling'].get('role','study'),
            scenario=meta['scenario'],mix_id='unverified',stable=w['phase']=='stable',
            focus_service=meta['profiling'].get('focus_service'),
            replicas=journal_replicas,replicas_source='capacity_events_available',
            replicas_prometheus=prometheus_replicas,
            cpu_cores=cpu_cores,cpu_limit_cores=cpu_limit,cpu_throttle_ratio=throttle,
            R_real=r['R_real'],R_obs=r['R_obs'],p_dynamic=r['p_dynamic'],
            sampling_probability=(None if r['count_source']=='client' else p_for_ht),
            R_ht=(None if r['count_source']=='client' else r_ht),
            completed_rps=(r['R_real'] if r['count_source']=='client' else r_ht),
            rps_measurement_var=(0 if r['count_source']=='client' else
                cluster_variance[(r['service'],int(round((r['timestamp']-origin)/30)))]),
            active_sessions=r['service_concurrency_mean'],
            effective_service_latency_ms=r['effective_service_latency_ms'],
            effective_latency_span_count=r['effective_latency_span_count'],
            effective_latency_valid=effective_valid,
            callee_normal=callee_ok,callee_failure_reason=callee_reason,
            capture_valid=capture_valid,
            exact_client_count=r['count_source']=='client'))
    controlled_services=meta['profiling'].get('controlled_services',())
    if not controlled_services and meta['profiling'].get('focus_service'):
        controlled_services=(meta['profiling']['focus_service'],)
    critical_services={'frontend',*controlled_services}
    resource_coverage={service:dict(total=counts['total'],usable=counts['usable'],
        pressure_usable=counts.get('pressure_usable',0),critical=service in critical_services,
        required_fraction=required_resource_coverage(service,controlled_services),
        fraction=(counts['usable']/counts['total'] if counts['total'] else 0.0))
        for service,counts in sorted(resource_counts.items())}
    below=[service for service,value in resource_coverage.items()
           if value['fraction'] < value['required_fraction']-1e-12]
    if below:
        detail=', '.join(f'{service}={resource_coverage[service]["usable"]}/'
                         f'{resource_coverage[service]["total"]}' for service in below)
        raise ValueError(f'CPU/resource window coverage below registered service threshold: {detail}')
    if features: write_csv(root/'calibration_features_30s.csv',features,list(features[0]))
    estimates={svc:dict(capacity=identify_capacity([r for r in features if r['service']==svc]),
                        cpu=fit_cpu([r for r in features if r['service']==svc]))
               for svc in sorted({r['service'] for r in features})}
    contract_services={}
    for service in sorted(observation_contract_services(meta['profiling'])):
        rows=[r for r in features if r['service']==service and r['stable']]
        valid=[r for r in rows if r['capture_valid'] and r['effective_latency_valid']
               and r['callee_normal']]
        failure_counts=defaultdict(int)
        for row in rows:
            if not row['capture_valid']:
                failure_counts['capture_invalid']+=1
            if not row['effective_latency_valid']:
                failure_counts['effective_latency_insufficient']+=1
            if not row['callee_normal']:
                for reason in filter(None,row['callee_failure_reason'].split(';')):
                    failure_counts[reason]+=1
        contract_services[service]=dict(stable_windows=len(rows),valid_windows=len(valid),
            capture_valid=capture_contract['services'].get(service,{}).get('valid') is True,
            failure_counts=dict(sorted(failure_counts.items())),
            pass_contract=len(valid)>=RULES['min_contract_windows'])
    observation_contract=dict(
        sampling=capture_contract,
        effective_latency_definition=('Istio server-span duration including application, '
                                      'Envoy proxy and downstream waiting'),
        internal_rate_definition=('Horvitz-Thompson R_obs/configured_probability; '
                                  'not directly observed 100% R_real'),
        services=contract_services,
        actual_contract_pass=bool(contract_services) and
            all(value['pass_contract'] for value in contract_services.values()))
    result=dict(schema_version=2,window_sec=30,aliasing=aliases,
                resource_coverage=resource_coverage,estimates=estimates,
                conservation_pass=all(r['conservation_residual']==0 for r in windows),
                little_is_diagnostic=True,observation_contract=observation_contract,
                capacity_identified=any(value['capacity']['capacity_identified']
                                        for value in estimates.values()),
                capacity_reason=('Per-service capacity remains fail-closed unless matched '
                                 'X(N), effective latency, pressure, capture and downstream '
                                 'evidence all pass.'),
                rules=RULES,code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                estimator_sha256=estimator_sha256(),
                input_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
                              if p.name in ('meta.json','driver_report.json','client_requests.csv','sessions.csv',
                                            'jaeger_server_spans.csv','capacity_events.jsonl','cpu_counter_10s.csv',
                                            'node_windows_30s.csv')})
    (root/'calibration_diagnostics.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directory')
    args=parser.parse_args()
    print(json.dumps(analyze(args.run_directory),indent=2,allow_nan=False))
