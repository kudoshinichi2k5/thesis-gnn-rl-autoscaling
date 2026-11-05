"""Profiling entry point for calibrating the Kubernetes digital twin.

Named configurations live in profiling-plan.json: main and smoke.
Default only previews. --confirm collects; --export writes validated data.
All collection/validation code is here; run-profile-loadgen.sh runs on the loadgen.
"""
import argparse
import copy
import csv
import hashlib
import itertools
import json
import math
import random
import shlex
import signal
import subprocess
import time
from pathlib import Path


# ---- Within-run capacity schedule and capture ----

TWIN_PROTOCOL = 'digital_twin_calibration_v1'


def schedule_for(plan, seed, repeat, arm, salt=''):
    if arm != 'calibrate':
        raise ValueError("digital-twin profiling only supports arm='calibrate'")
    base = plan['configurations'][0]
    controls = plan['controlled_services']
    if len(plan['configurations']) != 1 or len(set(controls)) != len(controls) or not controls:
        raise ValueError('transition plan requires one baseline and unique controlled services')
    levels = plan['replica_levels']
    if (not levels or levels[0] != 1 or levels != list(range(1, levels[-1] + 1))
            or any(type(level) is not int or not 1 <= level <= 10 for level in levels)):
        raise ValueError('replica_levels must be contiguous integers starting at 1')
    if any(s not in base or base[s] != 1 or levels[-1] > plan['replica_limits'][s] for s in controls):
        raise ValueError('controlled service does not support the calibration replica levels')
    if any(type(plan['replica_limits'][s]) is not int or not 1 <= plan['replica_limits'][s] <= 10 for s in controls):
        raise ValueError('replica limits must be integers in 1..10')
    if plan['action_interval_sec'] < 60 or plan['warmup_sec'] < 180:
        raise ValueError('retain >=180s initial history and >=60s between interventions')
    configured_dwell = plan.get('scale_up_dwell_sec_by_service', {})
    if (any(s not in base for s in configured_dwell)
            or any(type(v) is not int or v < plan['action_interval_sec'] or v % 10
                   for v in configured_dwell.values())):
        raise ValueError('per-service scale-up dwell must name an application service, be >= action interval and align to 10s')
    # Inherited profiles may control a subset (for example smoke controls only
    # frontend); dwell overrides for services outside that subset are inert.
    dwell_by_service = {s: v for s, v in configured_dwell.items() if s in controls}
    order = list(controls)
    random.Random(f'{seed}:{repeat}:{arm}:{salt}:actions').shuffle(order)
    targets = []
    for service in order:
        dwell = dwell_by_service.get(service, plan['action_interval_sec'])
        targets.extend((dict(base, **{service: level}), dwell) for level in levels[1:])
        targets.append((dict(base), plan['recovery_dwell_sec']))
    if len(order) >= 2:
        joint = order[:2]
        joint_level = levels[min(1, len(levels) - 1)]
        targets.extend([(dict(base, **{s: joint_level for s in joint}),
                         max(dwell_by_service.get(s, plan['action_interval_sec']) for s in joint)),
                        (dict(base), plan['recovery_dwell_sec'])])
    events = []
    at_sec = plan['warmup_sec']
    for target, dwell_sec in targets:
        events.append(dict(at_sec=at_sec, replicas=target))
        at_sec += dwell_sec
    if events[-1]['at_sec'] + plan['action_interval_sec'] >= plan['duration_sec']:
        raise ValueError('duration must retain a recovery interval after final action')
    if any(sum(k.values()) > plan['max_total_app_pods'] for k, _ in targets):
        raise ValueError('intervention exceeds pod budget')
    return events


def observe(kubectl, services):
    data = json.loads(subprocess.check_output(kubectl + ['get', 'deployments', '-o', 'json'],
                                              text=True, timeout=15))
    by_name = {d['metadata']['name']: d for d in data['items']}
    observed = {}
    for service in services:
        deployment = by_name[service]
        status = deployment.get('status', {})
        ready = int(status.get('readyReplicas', 0))
        total = int(status.get('replicas', 0))
        observed[service] = dict(
            desired=int(deployment['spec']['replicas']),
            ready=ready,
            available=int(status.get('availableReplicas', 0)),
            total=total,
            pending=max(0, total - ready),
        )
    return observed


def execute_capture(process, ssh, remote, kubectl, plan, profile, journal):
    """Run concurrently with the loadgen SSH process; return after collection ends.

    On controller failure stop issuing actions, retain journal, and let the bounded
    loadgen finish before the outer runner restores capacity. No cluster is launched here.
    """
    services = sorted(profile['replicas'])
    with Path(journal).open('x', encoding='utf-8', buffering=1) as stream:
        def emit(kind, **fields):
            stream.write(json.dumps(dict(kind=kind, epoch=time.time(), **fields)) + '\n')

        try:
            deadline = time.monotonic() + 90
            marker = None
            while marker is None:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('loadgen did not publish a start marker')
                script = ("import json,pathlib,time; p=pathlib.Path(" + repr(remote + '/driver_started.json')
                          + "); print(json.dumps({'remote_now':time.time(),'marker':"
                          + "json.loads(p.read_text()) if p.exists() else None}))")
                before = time.time()
                result = subprocess.check_output(ssh + [shlex.join(['python3', '-c', script])],
                                                 text=True, timeout=15)
                after = time.time()
                response = json.loads(result)
                # Clocks must be synchronized, not silently corrected using an uncertain offset.
                uncertainty = (after - before) / 2
                skew = response['remote_now'] - (before + after) / 2
                if abs(skew) + uncertainty > plan['max_clock_error_sec']:
                    raise RuntimeError('management/loadgen clock uncertainty exceeds plan limit')
                marker = response['marker']
                if marker is None:
                    time.sleep(1)
            origin = float(marker['trace_start_epoch'])
            if marker['seed'] != profile['seed'] or marker['scenario'] != profile['scenario']:
                raise ValueError('driver start identity mismatch')
            emit('start', origin=origin, profile=profile, clock_error_bound_sec=abs(skew)+uncertainty)
            index = 0
            while time.time() < origin + plan['duration_sec']:
                if process.poll() is not None:
                    raise RuntimeError('loadgen exited before scheduled duration')
                elapsed = time.time() - origin
                if index < len(profile['schedule']) and elapsed >= profile['schedule'][index]['at_sec']:
                    event = profile['schedule'][index]
                    if elapsed - event['at_sec'] > plan['max_action_lateness_sec']:
                        raise RuntimeError('missed action deadline')
                    before_state = observe(kubectl, services)
                    emit('action_start', index=index, planned_sec=event['at_sec'],
                         target=event['replicas'], before=before_state)
                    for s in services:
                        if before_state[s]['desired'] != event['replicas'][s]:
                            started = time.time()
                            subprocess.run(kubectl + ['scale', f'deployment/{s}',
                                                     f'--replicas={event["replicas"][s]}'],
                                           check=True, timeout=15)
                            emit('command', index=index, service=s, desired=event['replicas'][s],
                                 started_epoch=started)
                    emit('action_end', index=index)
                    index += 1
                emit('snapshot', state=observe(kubectl, services))
                time.sleep(plan['capacity_poll_sec'])
            if index != len(profile['schedule']):
                raise RuntimeError('not all interventions executed')
            emit('complete', actions=index)
        except BaseException as exc:
            emit('failure', error=repr(exc))
            # Do not restore replicas while a healthy bounded load run is still active.
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                process.wait(timeout=plan['duration_sec'] + 600)
            raise
    return process.wait(timeout=600)


def validate_capture(root, profile, duration):
    rows = [json.loads(line) for line in (Path(root) / 'capacity_events.jsonl').read_text().splitlines()]
    if not rows or rows[0]['kind'] != 'start' or rows[-1]['kind'] != 'complete':
        raise ValueError('incomplete capacity journal')
    if rows[0]['profile'] != profile or any(r['kind'] == 'failure' for r in rows):
        raise ValueError('failed or mismatched capacity journal')
    times = [r['epoch'] for r in rows]
    if not all(math.isfinite(t) for t in times) or any(b < a for a, b in zip(times, times[1:])):
        raise ValueError('non-monotonic journal clock')
    origin = rows[0]['origin']
    if rows[-1]['epoch'] < origin + duration:
        raise ValueError('capacity capture ended early')
    starts = [r for r in rows if r['kind'] == 'action_start']
    ends = [r for r in rows if r['kind'] == 'action_end']
    expected = profile['schedule']
    if [r['index'] for r in starts] != list(range(len(expected))) or [r['index'] for r in ends] != list(range(len(expected))):
        raise ValueError('missing/duplicate capacity action')
    for start, end, event in zip(starts, ends, expected):
        if start['target'] != event['replicas'] or start['planned_sec'] != event['at_sec']:
            raise ValueError('executed action differs from planned intervention')
        if not 0 <= start['epoch']-origin-event['at_sec'] <= profile['max_action_lateness_sec']:
            raise ValueError('action outside timing tolerance')
        if end['epoch'] < start['epoch']:
            raise ValueError('action ended before it started')
        changed = {s for s,k in event['replicas'].items() if start['before'][s]['desired'] != k}
        commands = [r for r in rows if r['kind']=='command' and r['index']==start['index']]
        if {r['service'] for r in commands} != changed or len(commands) != len(changed):
            raise ValueError('missing/duplicate scale command')
        for command in commands:
            if (command['desired'] != event['replicas'][command['service']]
                    or not start['epoch'] <= command['started_epoch'] <= command['epoch'] <= end['epoch']):
                raise ValueError('command target/timing mismatch')
    snapshots = [r for r in rows if r['kind'] == 'snapshot']
    if not snapshots or snapshots[0]['epoch']-origin > 30:
        raise ValueError('missing initial capacity observation')
    gaps = [b['epoch']-a['epoch'] for a, b in zip(snapshots, snapshots[1:])]
    gaps.append(rows[-1]['epoch']-snapshots[-1]['epoch'])
    if max(gaps) > 30:
        raise ValueError('capacity capture gap exceeds 30s')
    services = set(profile['replicas'])
    for snapshot in snapshots:
        if set(snapshot['state']) != services:
            raise ValueError('missing capacity service')
        for value in snapshot['state'].values():
            if any(type(value[k]) is not int or value[k] < 0 for k in ('desired','ready','available','total')):
                raise ValueError('invalid capacity count')
            if 'pending' in value and (type(value['pending']) is not int
                                       or value['pending'] != max(0, value['total']-value['ready'])):
                raise ValueError('invalid pending capacity count')
    # Confirm each plateau was actually attained; a successful scale command alone is insufficient.
    plateaus = [(origin, profile['replicas'])] + [(e['epoch'], e['target']) for e in starts]
    for i, (begin, target) in enumerate(plateaus):
        end = plateaus[i+1][0] if i+1 < len(plateaus) else origin+duration
        if not any(begin <= r['epoch'] < end and all(
                r['state'][s]['desired'] == k and r['state'][s]['available'] == k
                for s,k in target.items()) for r in snapshots):
            changed = {s: k for s, k in target.items()
                       if i == 0 or plateaus[i-1][1].get(s) != k}
            raise ValueError(f'intervention plateau {i} never became available '
                             f'during {end-begin:.1f}s; changed targets={changed}')
    return rows


# ---- Plan resolution and dataset validation/export ----

SERVICES = tuple(sorted(("adservice", "cartservice", "checkoutservice", "currencyservice",
                        "emailservice", "frontend", "paymentservice", "productcatalogservice",
                        "recommendationservice", "shippingservice")))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def csv_rows(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def plan_cells(plan):
    if plan['protocol'] != TWIN_PROTOCOL or plan['sampling_percentage'] != 10:
        raise ValueError('unsupported digital-twin protocol or sampling != 10%')
    for name in ('capacity_poll_sec', 'max_clock_error_sec', 'max_action_lateness_sec'):
        if not math.isfinite(plan[name]) or plan[name] <= 0:
            raise ValueError(f'invalid {name}')
    if plan['capacity_poll_sec'] > 5 or plan['max_clock_error_sec'] > 2 or plan['max_action_lateness_sec'] > 10:
        raise ValueError('capture timing exceeds 10s alignment budget')
    if (type(plan['recovery_dwell_sec']) is not int or plan['recovery_dwell_sec'] < 60
            or plan['recovery_dwell_sec'] % 10):
        raise ValueError('recovery_dwell_sec must be an integer >=60 aligned to 10s')
    schedule_for(plan, 0, 0, 'calibrate')
    configurations = plan['configurations']
    if not configurations or len({digest(k) for k in configurations}) != len(configurations):
        raise ValueError('explicit configurations must be nonempty and unique')
    for k in configurations:
        if set(k) != set(SERVICES):
            raise ValueError('each configuration must specify every application service exactly once')
        if any(type(n) is not int or not 1 <= n <= 10 for n in k.values()):
            raise ValueError('replica counts must be integers in 1..10')
        if sum(k.values()) > plan['max_total_app_pods']:
            raise ValueError(f'configuration exceeds pod budget: {k}')
    if 'fixed_replicas' in plan:
        raise ValueError('fixed_replicas is a removed lookup-era option')
    seeds = list(itertools.chain.from_iterable(plan['seeds_by_split'].values()))
    if set(plan['seeds_by_split']) != {'train', 'validation', 'test'} or len(set(seeds)) != len(seeds):
        raise ValueError('train/validation/test seeds must be disjoint')
    if any(not x for x in plan['seeds_by_split'].values()):
        raise ValueError('each split needs a workload seed')
    if any(type(s) is not int or s < 0 for s in seeds):
        raise ValueError('nonnegative integer seeds required')
    for key in ('repeats', 'duration_sec', 'window_sec', 'min_frontend_spans'):
        if type(plan[key]) is not int or plan[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('warmup_sec', 'settle_sec', 'cooldown_sec'):
        if type(plan[key]) is not int or plan[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    if plan['window_sec'] % 10 or plan['warmup_sec'] % plan['window_sec']:
        raise ValueError('use windows aligned to 10s and warmup aligned to window size')
    if not 0 <= plan['warmup_sec'] < plan['duration_sec'] - plan['window_sec']:
        raise ValueError('duration must leave complete windows after warmup')
    if not 0 <= plan['max_unstarted_fraction'] < 1:
        raise ValueError('invalid max_unstarted_fraction')
    if any(s not in ('nhpp', 'mmpp', 'onoff', 'pareto') for s in plan['scenarios']):
        raise ValueError('unsupported scenario')
    if not plan['scenarios'] or not plan['load_multipliers']:
        raise ValueError('empty workload grid')
    if len(set(plan['scenarios'])) != len(plan['scenarios']) or len(set(plan['load_multipliers'])) != len(plan['load_multipliers']):
        raise ValueError('duplicate workload grid values')
    if any(not math.isfinite(x) or x <= 0 for x in plan['load_multipliers']):
        raise ValueError('load multipliers must be positive and finite')
    cells = []
    for split, split_seeds in plan['seeds_by_split'].items():
        for scenario, scale, seed, repeat in itertools.product(
                plan['scenarios'], plan['load_multipliers'], split_seeds, range(plan['repeats'])):
            for k in configurations:
                cell = dict(protocol=plan['protocol'], plan_sha256=digest(plan), split=split,
                            scenario=scenario, load_multiplier=scale, seed=seed, repeat=repeat,
                            replicas=k, config_id=digest(k)[:12], arm='calibrate',
                            schedule=schedule_for(plan, seed, repeat, 'calibrate',
                                                  f'{split}:{scenario}:{scale}'),
                            max_action_lateness_sec=plan['max_action_lateness_sec'])
                cell['cell_id'] = digest(cell)[:20]
                cells.append(cell)
    return cells


def validate_run(root, expected=None, max_unstarted_fraction=0.01):
    """Fail closed; sampled empty windows are distinct from failed API queries."""
    root = Path(root)
    meta, report, driver = (read_json(root / name) for name in
                            ('meta.json', 'collect_report.json', 'driver_report.json'))
    profile = meta.get('profiling')
    if not profile or (expected is not None and profile != expected):
        raise ValueError(f'{root}: profiling identity mismatch')
    if any(meta[k] != profile[k] for k in ('scenario', 'seed', 'load_multiplier')):
        raise ValueError(f'{root}: workload metadata mismatch')
    if meta.get('locust_exit_code') != 0 or float(meta['trace_sampling_percentage']) != 10:
        raise ValueError(f'{root}: Locust exit/sampling mismatch')
    if any(report.get(k, None) != [] for k in ('jaeger_failed_queries', 'prometheus_query_failures')):
        raise ValueError(f'{root}: missing query status or failed telemetry queries')
    if report.get('jaeger_truncated_windows') != 0 or report.get('raw_server_spans', 0) <= 0:
        raise ValueError(f'{root}: truncated/missing Jaeger data')
    required = ('cpu_cores', 'mem_bytes', 'replicas', 'replicas_desired')
    if any(report.get('prometheus_series', {}).get(k, 0) <= 0 for k in required):
        raise ValueError(f'{root}: missing required Prometheus metric')
    if set(report.get('jaeger_services', {})) != set(SERVICES):
        raise ValueError(f'{root}: missing traced service registry')
    trace = csv_rows(root / 'arrival_trace.csv')
    if hashlib.sha256((root / 'arrival_trace.csv').read_bytes()).hexdigest() != meta['trace_sha256']:
        raise ValueError(f'{root}: trace hash mismatch')
    for key in ('planned', 'started', 'completed', 'dropped', 'failed', 'trace_events_total'):
        if type(driver[key]) is not int or driver[key] < 0:
            raise ValueError(f'{root}: invalid driver counter {key}')
    if (not trace or driver['trace_events_total'] != len(trace) or driver['dropped'] or driver['failed']
            or driver['started'] != driver['planned'] or driver['planned'] > len(trace)
            or driver['planned'] < (1 - max_unstarted_fraction) * len(trace)
            or driver['completed'] > driver['started']):
        raise ValueError(f'{root}: incomplete or invalid offered workload')
    origin = float(driver['trace_start_epoch'])
    if not math.isfinite(origin) or not meta['start'] <= origin <= meta['end']:
        raise ValueError(f'{root}: invalid arrival clock')
    dynamic = profile.get('protocol') == TWIN_PROTOCOL
    if dynamic:
        journal = validate_capture(root, profile, meta['duration_sec'])
        if abs(journal[0]['origin'] - origin) > 0.01:
            raise ValueError('journal and driver clocks differ')
    nodes = csv_rows(root / 'node_metrics.csv')
    by_tick = {}
    step = int(report['step'])
    for row in nodes:
        if row['service'] in SERVICES:
            key = (int(row['timestamp']), row['service'])
            if key in by_tick:
                raise ValueError(f'{root}: duplicate node row')
            by_tick[key] = row
    for tick in range(int(report['start']), int(report['end']) + 1, step):
        for service in SERVICES:
            row = by_tick.get((tick, service))
            if row is None:
                raise ValueError(f'{root}: missing node tick {tick}/{service}')
            for field in required:
                if not math.isfinite(float(row[field])) or float(row[field]) < 0:
                    raise ValueError(f'{root}: nonfinite {field}')
            expected_count = profile['replicas'][service]
            if dynamic:
                # Preserve transient ready/desired differences. Bounds are not proof of tracking;
                # the independent journal checks each commanded plateau was reached.
                upper = max([expected_count] + [e['replicas'][service] for e in profile['schedule']])
                if float(row['replicas']) > upper or not 1 <= float(row['replicas_desired']) <= upper:
                    raise ValueError(f'{root}: capacity outside intervention bounds')
            elif float(row['replicas']) != expected_count or float(row['replicas_desired']) != expected_count:
                raise ValueError(f'{root}: replica mismatch at {tick}/{service}')
    if dynamic:
        starts = [r for r in journal if r['kind']=='action_start']
        plateaus = [(origin,profile['replicas'])] + [(r['epoch'],r['target']) for r in starts]
        for index,(begin,target) in enumerate(plateaus):
            end = plateaus[index+1][0] if index+1<len(plateaus) else origin+meta['duration_sec']
            for service,count in target.items():
                if not any(begin <= tick < end and name==service
                           and float(row['replicas'])==count and float(row['replicas_desired'])==count
                           for (tick,name),row in by_tick.items()):
                    raise ValueError(f'{root}: Prometheus never observed target plateau for {service}')
    return meta, driver


def build_twin_manifest(root, plan):
    """Portable, hashed raw-run manifest; never synthesizes or joins observations."""
    if plan['protocol'] != TWIN_PROTOCOL:
        raise ValueError('digital-twin manifest requires the calibration protocol')
    root = Path(root)
    cells = {c['cell_id']: c for c in plan_cells(plan)}
    entries, contexts, traces = [], set(), {}
    files = ('meta.json', 'collect_report.json', 'driver_report.json', 'driver_started.json',
             'capacity_events.jsonl', 'node_metrics.csv', 'service_metrics.csv',
             'edge_metrics.csv', 'locust_stats_history.csv', 'arrival_trace.csv',
             'designed_rate.csv', 'jaeger_server_spans.csv')
    for folder in sorted(p.parent for p in root.glob('*/meta.json')):
        actual = read_json(folder/'meta.json')['profiling']
        if {k:v for k,v in actual.items() if k != 'cluster_context_sha256'} != cells.get(folder.name):
            raise ValueError(f'foreign or changed profile: {folder}')
        meta, driver = validate_run(folder, actual, plan['max_unstarted_fraction'])
        if meta['duration_sec'] != plan['duration_sec']:
            raise ValueError('duration differs from plan')
        contexts.add(actual['cluster_context_sha256'])
        group = (actual['scenario'], actual['seed'], actual['load_multiplier'])
        traces.setdefault(group, set()).add(meta['trace_sha256'])
        journal = validate_capture(folder,actual,meta['duration_sec'])
        delay_audit=[]
        for command in (r for r in journal if r['kind']=='command'):
            service=command['service'];target=command['desired']
            next_action=min([r['epoch'] for r in journal if r['kind']=='action_start' and r['epoch']>command['epoch']]
                            +[journal[-1]['epoch']])
            attained=next((r for r in journal if r['kind']=='snapshot' and command['epoch']<=r['epoch']<next_action
                           and r['state'][service]['available']==target and r['state'][service]['desired']==target),None)
            delay_audit.append(dict(service=service,desired=target,command_epoch=command['epoch'],
                                    first_observed_available_delay_sec=attained['epoch']-command['epoch'] if attained else None))
        entries.append(dict(key='profiling/'+folder.name, directory=folder.name,
                            split={'validation':'val'}.get(actual['split'], actual['split']),
                            scenario=actual['scenario'], seed=actual['seed'], arm=actual['arm'],
                            repeat=actual['repeat'], load_multiplier=actual['load_multiplier'],capacity_delay_audit=delay_audit,
                            raw_sha256={f:hashlib.sha256((folder/f).read_bytes()).hexdigest() for f in files}))
    if not entries or len(contexts) != 1 or any(len(v) != 1 for v in traces.values()):
        raise ValueError('empty, mixed context or unpaired arrival traces')
    return dict(protocol=TWIN_PROTOCOL, plan=plan, runs=entries,
                missing_cells=sorted(set(cells)-{r['directory'] for r in entries}),
                context_sha256=next(iter(contexts)),
                interpretation='request-level service times plus measured replica lifecycle for digital-twin calibration')


def load_plan(path, profile="main"):
    """Resolve one named plan. Inheritance is shallow and explicit; never mutates defaults."""
    bundle = read_json(path)
    profiles = bundle["profiles"]
    def resolve(name, visited):
        if name in visited:
            raise ValueError("cycle in profiling plan inheritance")
        if name not in profiles:
            raise ValueError(f"unknown profiling profile: {name}")
        value = profiles[name]
        if "extends" in value:
            plan = resolve(value["extends"], visited | {name})
            plan.update(copy.deepcopy(value.get("overrides", {})))
            return plan
        return copy.deepcopy(value)
    return resolve(profile, set())


# ---- Cluster orchestration and CLI ----

def command(argv):
    return subprocess.check_output(argv, text=True).strip()


def run(args):
    plan = load_plan(args.plan, args.profile)
    all_cells = plan_cells(plan)
    if args.export:
        if any(value is not None for value in
               (args.scenario, args.split, args.load_multiplier, args.repeat)):
            raise ValueError('collection filters cannot be used with --export')
        value = build_twin_manifest(args.output, plan)
        with Path(args.export).open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
        print(f"Validated export: {args.export}")
        return
    filters = dict(scenario=args.scenario, split=args.split,
                   load_multiplier=args.load_multiplier, repeat=args.repeat)
    for key, value in filters.items():
        if value is not None and value not in {cell[key] for cell in all_cells}:
            raise ValueError(f'filter {key}={value!r} is not present in profile {args.profile}')
    cells = [cell for cell in all_cells
             if all(value is None or cell[key] == value for key, value in filters.items())]
    if not cells:
        raise ValueError('collection filters selected no cells')
    active_filters = ', '.join(f'{key}={value}' for key, value in filters.items() if value is not None)
    print(f"Selected {len(cells)}/{len(all_cells)} cells"
          f"{f' ({active_filters})' if active_filters else ''}, "
          f"{len(cells) * plan['duration_sec'] / 3600:.1f} load hours, "
          f"plus settle/cooldown/collection; max {args.max_new} NEW cells this invocation")
    print(f"Plan SHA256: {digest(plan)}; explicit configurations: {len(plan['configurations'])}; sampling 10%")
    for c in cells:
        print(c['cell_id'], c['split'], c['scenario'], c['load_multiplier'], c['seed'],
              c['repeat'], c['arm'], c['replicas'])
    if not args.confirm:
        return
    if not args.ssh_target or not args.ssh_key or args.max_new <= 0:
        raise ValueError('--confirm requires --ssh-target, --ssh-key and positive --max-new')
    if not args.remote_dir.startswith('/') or not args.remote_results.startswith('/'):
        raise ValueError('remote paths must be absolute')
    kubectl = ['kubectl', '-n', args.namespace]
    ssh = ['ssh', '-i', str(Path(args.ssh_key).expanduser()), '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=10', args.ssh_target]

    def get(resource):
        return json.loads(command(kubectl + ['get', resource, '-o', 'json']))

    def check_control():
        if get('hpa')['items']:
            raise ValueError('disable HPA before profiling')
        telemetry = json.loads(command(['kubectl', '-n', 'istio-system', 'get',
                                        'telemetry', 'mesh-default', '-o', 'json']))
        if telemetry['spec']['tracing'][0].get('randomSamplingPercentage') != 10:
            raise ValueError('mesh-default must declare sampling 10%')
        # Record all policies: scoped overrides must be reviewed with actual proxy config.
        policies = json.loads(command(['kubectl', 'get', 'telemetry', '-A', '-o', 'json']))
        if len(policies['items']) != 1:
            raise ValueError('additional Telemetry overrides require review before collection')

    check_control()
    deployments = {d['metadata']['name']: d for d in get('deployments')['items']}
    if not set(SERVICES) <= deployments.keys() or deployments.get('redis-cart', {}).get('spec', {}).get('replicas') != 1:
        raise ValueError('missing app deployments or redis-cart is not fixed at 1')
    original = {s: deployments[s]['spec']['replicas'] for s in SERVICES}
    # Hash templates (images, resources, pod configuration), namespace, and node inventory.
    context = dict(namespace=args.namespace, sampling=10,
                   templates={s: deployments[s]['spec']['template'] for s in (*SERVICES, 'redis-cart')},
                   nodes=json.loads(command(['kubectl', 'get', 'nodes', '-o', 'json'])))
    context['controller_sha256'] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('run-profiling.py',)}
    context['nodes'] = [{"name": n['metadata']['name'], "capacity": n['status']['capacity'],
                         "nodeInfo": n['status']['nodeInfo']} for n in context['nodes']['items']]
    context['nodes'].sort(key=lambda n: n['name'])
    context_hash = digest(context)
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    study_path = root / 'study.json'
    study = dict(plan=plan, cluster_context=context, cluster_context_sha256=context_hash)
    if study_path.exists():
        if read_json(study_path) != study:
            raise ValueError('study/context changed: use a new output and remote-results directory')
    else:
        with study_path.open('x', encoding='utf-8') as stream:
            json.dump(study, stream, indent=2)

    def set_capacity(k):
        for s in SERVICES:
            subprocess.run(kubectl + ['scale', f'deployment/{s}', f'--replicas={k[s]}'], check=True)
        for s in SERVICES:
            subprocess.run(kubectl + ['rollout', 'status', f'deployment/{s}', '--timeout=300s'], check=True)
        current = {d['metadata']['name']: d for d in get('deployments')['items']}
        for s in SERVICES:
            if current[s]['spec']['replicas'] != k[s] or current[s]['status'].get('readyReplicas', 0) != k[s]:
                raise ValueError(f'replica not ready: {s}')

    files = ['locustfile.py', 'arrival_models.py', 'collect_metrics.py', 'run-profile-loadgen.sh']
    files += [f'scenarios/{s}.py' for s in plan['scenarios']]
    code_root = Path(__file__).parent
    local_hashes = {Path(f).name: hashlib.sha256((code_root / f).read_bytes()).hexdigest() for f in files}
    for f in files:
        remote_hash = command(ssh + [shlex.join(['sha256sum', f'{args.remote_dir}/{f}'])]).split()[0]
        if remote_hash != local_hashes[Path(f).name]:
            raise ValueError(f'loadgen code mismatch: {f}; sync before collection')

    count = 0
    changed = False
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for cell in cells:
            profile = dict(cell, cluster_context_sha256=context_hash)
            dest = root / cell['cell_id']
            if dest.exists():
                meta, _ = validate_run(dest, profile, plan['max_unstarted_fraction'])
                if any(meta['code_sha256'].get(k) != v for k, v in local_hashes.items()
                       if k in files[:4] or k == cell['scenario'] + '.py'):
                    raise ValueError('saved run uses different code')
                continue
            if count >= args.max_new:
                print('Batch complete. Verify saved data/Jaeger storage before resuming same plan.')
                break
            check_control()
            remote = f'{args.remote_results}/{cell["cell_id"]}'
            command(ssh + [shlex.join(['test', '!', '-e', remote])])
            changed = True
            set_capacity(cell['replicas'])
            time.sleep(plan['settle_sec'])
            remote_command = ['env', 'TRACE_SAMPLING_PERCENTAGE=10', f'NAMESPACE={args.namespace}',
                              f'LOAD_MULTIPLIER={cell["load_multiplier"]}',
                              'PROFILE_CELL_JSON=' + json.dumps(profile, separators=(',', ':')),
                              'bash', f'{args.remote_dir}/run-profile-loadgen.sh',
                              cell['scenario'], str(cell['seed']), str(plan['duration_sec']), '10',
                              str(args.max_concurrent), remote]
            dest.mkdir()
            # Journal is local and survives SSH/collector failures. No pipe buffer can block SSH.
            with (dest/'loadgen.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen(ssh + [shlex.join(remote_command)], stdout=log, stderr=log)
                failure = None
                try:
                    returncode = execute_capture(process, ssh, remote, kubectl, plan, profile,
                                                 dest/'capacity_events.jsonl')
                except BaseException as exc:
                    failure = exc
                    returncode = process.poll()
                finally:
                    if process.poll() is None:
                        # Do not restore capacity while the workload is still running.
                        process.wait(timeout=plan['duration_sec'] + 600)
            transport = shlex.join(ssh[:-1])
            subprocess.run(['rsync', '-a', '-e', transport,
                            f'{args.ssh_target}:{shlex.quote(remote)}/', str(dest) + '/'], check=True)
            if failure is not None:
                raise failure
            validate_run(dest, profile, plan['max_unstarted_fraction'])
            if returncode:
                raise RuntimeError(f'loadgen/collector failed ({returncode}); preserved {dest}')
            count += 1
            time.sleep(plan['cooldown_sec'])
    finally:
        if changed:
            print('Restoring original replicas...')
            failed = []
            for s in SERVICES:
                result = subprocess.run(kubectl + ['scale', f'deployment/{s}', f'--replicas={original[s]}'], check=False)
                if result.returncode:
                    failed.append(s)
            for s in SERVICES:
                result = subprocess.run(kubectl + ['rollout', 'status', f'deployment/{s}', '--timeout=300s'], check=False)
                if result.returncode:
                    failed.append(s)
            if failed:
                raise RuntimeError(f'restore failed: {sorted(set(failed))}; inspect cluster manually')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', default=str(Path(__file__).with_name('profiling-plan.json')))
    parser.add_argument('--profile', choices=('main', 'smoke'), default='main')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--confirm', action='store_true', help='collect instead of read-only preview')
    mode.add_argument('--export', metavar='JSON', help='validate collected runs and write manifest/windows')
    parser.add_argument('--output', help='local raw-run directory; default depends on profile')
    parser.add_argument('--ssh-target', help='user@loadgen-host')
    parser.add_argument('--ssh-key')
    parser.add_argument('--namespace', default='online-boutique')
    parser.add_argument('--remote-dir', default='/home/ubuntu/load-testing')
    parser.add_argument('--remote-results', help='remote raw-run directory; default depends on profile')
    parser.add_argument('--max-new', type=int, default=1, help='maximum new calibration runs')
    parser.add_argument('--max-concurrent', type=int, default=400)
    parser.add_argument('--scenario', choices=('nhpp', 'mmpp', 'onoff', 'pareto'),
                        help='collect/preview only this scenario; does not change the study plan')
    parser.add_argument('--split', choices=('train', 'validation', 'test'),
                        help='collect/preview only this split; does not change the study plan')
    parser.add_argument('--load-multiplier', type=float,
                        help='collect/preview only this configured load multiplier')
    parser.add_argument('--repeat', type=int,
                        help='collect/preview only this configured repeat index')
    args = parser.parse_args(argv)
    folder = {'main':'digital-twin-calibration-v1', 'smoke':'digital-twin-smoke-v1'}[args.profile]
    if args.output is None: args.output = f'dataset/{folder}'
    if args.remote_results is None: args.remote_results = f'/home/ubuntu/dataset/{folder}'
    return args


if __name__ == '__main__':
    run(parse_args())
