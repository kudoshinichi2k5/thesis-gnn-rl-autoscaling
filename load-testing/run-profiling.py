"""Profiling entry point for calibrating the Kubernetes digital twin.

Named configurations live in profiling-plan.json: main and smoke.
Default only previews. --collect-only collects immutable raw data;
--analyze-collected runs the offline analyzer separately; --export writes a manifest.
All collection/integrity code is here; run-profile-loadgen.sh runs on the loadgen.
"""
import argparse
import copy
import csv
import hashlib
import importlib.util
import inspect
import itertools
import json
import math
import random
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


# ---- Within-run capacity schedule and capture ----

TWIN_PROTOCOL = 'digital_twin_calibration_v2'
PROTOCOL_VERSION = 'collection_v6_2'
_DESIGNED_PEAK_CACHE = {}
PRIOR_VALIDATOR_ONLY_RUNNER_SHA256 = (
    'cad613f63a4c5f505f95b3b9e7c8a20cbba97c2985a7e2b38348f31c8d541f2b'
)
VALIDATOR_V61_SHA256 = (
    '301088bc6bbc4d04c9a8f360ab0580d406cb6148cf0a2513fe57bb6813385148'
)
def collection_lock_hashes(plan_path=None):
    """Hashes that freeze collection, intentionally excluding offline analysis."""
    root = Path(__file__).parent
    paths = {
        'run-profiling.py': Path(__file__),
        'locustfile.py': root / 'locustfile.py',
        'collect_metrics.py': root / 'collect_metrics.py',
        'profiling-plan.json': Path(plan_path) if plan_path else root / 'profiling-plan.json',
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()}


def collection_lock_sha256(plan_path=None):
    return digest(collection_lock_hashes(plan_path))


def trace_definition_hashes():
    """Provenance locked in campaign order, separate from the four-file collection lock."""
    root = Path(__file__).parent
    paths = [root / 'arrival_models.py'] + sorted(
        path for path in (root / 'scenarios').glob('*.py')
        if path.name != '__init__.py'
    )
    return {str(path.relative_to(root)).replace('\\', '/'):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _load_local_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load collection module: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def designed_peak_rps(cell, code_root=None):
    """Return the seed-specific designed frontend HTTP-operation peak.

    This is generated from the same pure arrival model used by the load driver.
    It is 100% designed frontend RPS, not sampled Jaeger RPS and not a statistic
    from any collected campaign run.
    """
    root = Path(code_root) if code_root else Path(__file__).parent
    key = (str(root.resolve()), cell['scenario'], float(cell['load_multiplier']),
           int(cell['duration_sec']), int(cell['seed']))
    if key in _DESIGNED_PEAK_CACHE:
        return _DESIGNED_PEAK_CACHE[key]
    arrivals = _load_local_module('envelope_arrival_models', root / 'arrival_models.py')
    scenario = cell['scenario']
    scenario_module = _load_local_module(
        f'envelope_scenario_{scenario}', root / 'scenarios' / f'{scenario}.py'
    )
    config = arrivals.scaled_config(scenario_module.CONFIG, cell['load_multiplier'])
    trace = arrivals.build_trace(config, cell['duration_sec'], cell['seed'])
    rates = [float(row['expected_rps']) for row in trace['rate']]
    if not rates or any(not math.isfinite(value) or value < 0 for value in rates):
        raise ValueError(f'invalid designed rate trace for {cell.get("cell_id", scenario)}')
    peak = max(rates)
    _DESIGNED_PEAK_CACHE[key] = peak
    return peak


def cell_envelope_evidence(plan, cell, code_root=None):
    """Compute and enforce the registered v6.2 physical admission envelope."""
    gate = plan.get('envelope_gate')
    if not isinstance(gate, dict):
        raise ValueError('missing envelope_gate')
    peak = designed_peak_rps(cell, code_root)
    max_peak = float(gate['max_frontend_peak_rps'])
    max_rho = float(gate['max_rho_peak'])
    if not all(math.isfinite(value) and value > 0 for value in (max_peak, max_rho)):
        raise ValueError('invalid envelope thresholds')

    costs = gate['cost_real_cpu_core_per_rps']
    fanouts = gate['effective_fanout_from_frontend']
    limits = gate['cpu_limit_cores_per_pod']
    focus_services = ([cell['focus_service']] if cell.get('focus_service') else
                      list(plan['controlled_services']))
    rho_by_service = {}
    all_targets = [cell['replicas']] + [event['replicas'] for event in cell['schedule']]
    for service in focus_services:
        if service not in costs or service not in fanouts or service not in limits:
            raise ValueError(f'missing envelope coefficient for {service}')
        minimum_replicas = min(int(target[service]) for target in all_targets)
        cost = float(costs[service])
        fanout = float(fanouts[service])
        limit = float(limits[service])
        if (minimum_replicas < 1 or
                not all(math.isfinite(value) and value > 0 for value in (cost, fanout, limit))):
            raise ValueError(f'invalid envelope coefficient for {service}')
        rho_by_service[service] = (
            peak * cost * fanout / (limit * minimum_replicas)
        )
    worst_service = max(rho_by_service, key=rho_by_service.get)
    evidence = dict(
        designed_peak_frontend_rps=peak,
        peak_limit_rps=max_peak,
        rho_peak_by_service=rho_by_service,
        worst_rho_peak=rho_by_service[worst_service],
        worst_rho_service=worst_service,
        rho_limit=max_rho,
        rate_unit='designed 100% frontend HTTP operations/second',
    )
    if peak > max_peak + 1e-12:
        raise ValueError(
            f'envelope rejected {cell.get("cell_id", "cell")}: designed frontend peak '
            f'{peak:.6g} RPS exceeds {max_peak:.6g} RPS'
        )
    if evidence['worst_rho_peak'] > max_rho + 1e-12:
        raise ValueError(
            f'envelope rejected {cell.get("cell_id", "cell")}: rho_peak '
            f'{evidence["worst_rho_peak"]:.6g} for {worst_service} exceeds {max_rho:.6g}'
        )
    return evidence


def validator_only_order_compatible(registered, candidate):
    """Allow the frozen v6 campaign to adopt only the reviewed v6.1 validator fix.

    Acquisition inputs, execution order and plan must remain byte-identical.  The
    sole permitted difference is the controller file hash recorded by v6 before
    transport validation was corrected.  Fingerprinting the validator source keeps
    this exception one-way and prevents later controller edits from silently using it.
    """
    if (registered.get('protocol_version') != 'collection_v6'
            or candidate.get('protocol_version') != 'collection_v6'):
        return False
    current_validator = hashlib.sha256(
        inspect.getsource(validate_execution_artifacts).encode()
    ).hexdigest()
    if current_validator != VALIDATOR_V61_SHA256:
        return False
    old_hashes = registered.get('collection_code_sha256', {})
    new_hashes = candidate.get('collection_code_sha256', {})
    if old_hashes.get('run-profiling.py') != PRIOR_VALIDATOR_ONLY_RUNNER_SHA256:
        return False
    if any(old_hashes.get(name) != new_hashes.get(name) for name in
           ('locustfile.py', 'collect_metrics.py', 'profiling-plan.json')):
        return False
    stable_fields = ('plan_sha256', 'seed', 'max_concurrent', 'block_max_sec',
                     'protocol_version', 'executions')
    return all(registered.get(field) == candidate.get(field) for field in stable_fields)


def baseline_for_focus(plan, focus_service=None):
    base = dict(plan['configurations'][0])
    if focus_service is not None:
        for service, replicas in plan.get('focus_guard_replicas', {}).get(focus_service, {}).items():
            if service not in base or type(replicas) is not int or replicas < 1:
                raise ValueError('invalid focus guard replica configuration')
            base[service] = replicas
    if sum(base.values()) > plan['max_total_app_pods']:
        raise ValueError('focus baseline exceeds pod budget')
    return base


def schedule_for(plan, seed, repeat, arm, salt='', focus_service=None):
    if arm != 'calibrate':
        raise ValueError("digital-twin profiling only supports arm='calibrate'")
    base = baseline_for_focus(plan, focus_service)
    controls = plan['controlled_services']
    if len(plan['configurations']) != 1 or len(set(controls)) != len(controls) or not controls:
        raise ValueError('transition plan requires one baseline and unique controlled services')
    levels = plan['replica_levels']
    if (not levels or levels[0] != 1 or levels != list(range(1, levels[-1] + 1))
            or any(type(level) is not int or not 1 <= level <= 10 for level in levels)):
        raise ValueError('replica_levels must be contiguous integers starting at 1')
    nominal = plan['configurations'][0]
    if any(s not in nominal or nominal[s] != 1 or levels[-1] > plan['replica_limits'][s] for s in controls):
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
    if focus_service is not None and focus_service not in controls:
        raise ValueError('focus service must be one of controlled_services')
    order = [focus_service] if focus_service else list(controls)
    random.Random(f'{seed}:{repeat}:{arm}:{salt}:actions').shuffle(order)
    targets = []
    for service in order:
        dwell = dwell_by_service.get(service, plan['action_interval_sec'])
        targets.extend((dict(base, **{service: level}), dwell) for level in levels[1:])
        targets.append((dict(base), plan['recovery_dwell_sec']))
    # The legacy all-service schedule retains one joint probe. Study cells pass a
    # focus service and deliberately isolate it; otherwise 35 minutes divided by
    # five services leaves replica 2/3 structurally unidentifiable.
    if focus_service is None and len(order) >= 2:
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


def joint_schedule_for(plan, seed, repeat, salt=''):
    """Deterministic constrained random walk for validation/test interaction coverage."""
    base = dict(plan['configurations'][0])
    controls = list(plan['controlled_services'])
    rng = random.Random(f'{seed}:{repeat}:{salt}:joint-walk')
    current = {s: base[s] for s in controls}
    events = []
    at_sec = plan['warmup_sec']
    dwell = plan.get('joint_dwell_sec', 510)
    for _ in range(3):
        candidates = []
        for _attempt in range(256):
            proposal = {s: max(1, min(3, current[s] + rng.choice((-1, 0, 1)))) for s in controls}
            changed = sum(proposal[s] != current[s] for s in controls)
            target = dict(base, **proposal)
            if changed >= 2 and sum(target.values()) <= plan['max_total_app_pods']:
                candidates.append(target)
        if not candidates:
            raise ValueError('cannot construct joint schedule under pod budget')
        # Prefer a state that has not reused the same marginal vector.
        target = max(candidates, key=lambda k: (len({k[s] for s in controls}),
                                                sum(abs(k[s]-current[s]) for s in controls),
                                                rng.random()))
        events.append(dict(at_sec=at_sec, replicas=target, transition_mode='joint'))
        current = {s: target[s] for s in controls}
        at_sec += dwell
    events.append(dict(at_sec=at_sec, replicas=base, transition_mode='recovery'))
    if at_sec + plan['recovery_dwell_sec'] >= plan['duration_sec']:
        raise ValueError('duration must retain recovery after joint schedule')
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


def confirm_remote_stopped(ssh, remote, process, abort=False):
    """Verify the recorded Linux process group; never equate SSH exit with remote exit."""
    script='''import json,os,pathlib,signal,time
p=pathlib.Path(MARKER)
if not p.exists(): raise RuntimeError('missing process identity; inspect remote host')
m=json.loads(p.read_text()); pg=int(m['pgid'])
def members():
    found=[]
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            fields=(entry/'stat').read_text().split(') ',1)[1].split()
            if int(fields[2])==pg and fields[0]!='Z': found.append(entry.name)
        except (OSError,ValueError,IndexError): continue
    return found
live=members()
if live and ABORT:
    stat=pathlib.Path('/proc')/str(pg)/'stat'
    if not stat.exists() or stat.read_text().split(') ',1)[1].split()[19]!=m['start_ticks']:
        raise RuntimeError('group identity changed or leader missing; manual recovery required')
    os.killpg(pg,signal.SIGTERM)
for _ in range(20):
    if not members(): break
    time.sleep(.5)
if members() and ABORT:
    os.killpg(pg,signal.SIGKILL)
    time.sleep(1)
if members(): raise RuntimeError('remote process group still active')
print('REMOTE_STOP_CONFIRMED')
'''.replace('MARKER',repr(remote+'/process_identity.json')).replace('ABORT',repr(abort))
    result=command(ssh+[shlex.join(['python3','-c',script])])
    if result!='REMOTE_STOP_CONFIRMED': raise RuntimeError('remote stop not confirmed')
    if process is None:
        return
    try: process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def execute_capture(process, ssh, remote, kubectl, plan, profile, journal,
                    clock_measurement, health_check=None):
    """Run concurrently with the loadgen SSH process; return after collection ends.

    On controller failure stop issuing actions, retain journal, and let the bounded
    loadgen finish before the outer runner restores capacity. No cluster is launched here.
    """
    services = sorted(profile['replicas'])
    with Path(journal).open('x', encoding='utf-8', buffering=1) as stream:
        def emit(kind, **fields):
            stream.write(json.dumps(dict(kind=kind, epoch=time.time(), **fields)) + '\n')

        try:
            # This measurement is taken immediately before the loadgen process
            # is launched. Keeping the gate outside execute_capture prevents a
            # failed clock probe from starting an uncontrolled workload.
            clock_bound, skew, uncertainty = clock_measurement
            deadline = time.monotonic() + 90
            marker = None
            while marker is None:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('loadgen did not publish a start marker')
                script = ("import json,pathlib; p=pathlib.Path(" + repr(remote + '/driver_started.json')
                          + "); print(json.dumps(json.loads(p.read_text()) if p.exists() else None))")
                result = subprocess.check_output(ssh + [shlex.join(['python3', '-c', script])],
                                                 text=True, timeout=15)
                marker = json.loads(result)
                if marker is None:
                    time.sleep(1)
            origin = float(marker['trace_start_epoch'])
            if marker['seed'] != profile['seed'] or marker['scenario'] != profile['scenario']:
                raise ValueError('driver start identity mismatch')
            emit('start', origin=origin, profile=profile, clock_error_bound_sec=clock_bound,
                 clock_offset_sec=skew, clock_uncertainty_sec=uncertainty)
            index = 0
            next_health=0
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
                    commands=[]
                    for s in services:
                        if before_state[s]['desired'] != event['replicas'][s]:
                            started=time.time()
                            child=subprocess.Popen(kubectl + ['scale',f'deployment/{s}',
                                f'--replicas={event["replicas"][s]}'],stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,text=True)
                            commands.append((s,started,child))
                    for s,started,child in commands:
                        stdout,stderr=child.communicate(timeout=15)
                        if child.returncode:
                            raise RuntimeError(f'joint scale failed for {s}: {stderr.strip()}')
                        emit('command',index=index,service=s,desired=event['replicas'][s],
                             started_epoch=started,completed_epoch=time.time(),stdout=stdout.strip())
                    emit('action_end', index=index)
                    index += 1
                emit('snapshot', state=observe(kubectl, services))
                if health_check is not None and elapsed>=next_health:
                    emit('health',metrics=health_check())
                    next_health=elapsed+30
                time.sleep(plan['capacity_poll_sec'])
            if index != len(profile['schedule']):
                raise RuntimeError('not all interventions executed')
            emit('complete', actions=index)
        except BaseException as exc:
            emit('failure', error=repr(exc))
            # Outer finally stops and verifies the remote process group before restore.
            raise
    timeout = plan['drain_sec'] + plan['collection_timeout_sec']
    print(f'Intervention window complete; waiting up to {timeout}s for drain and telemetry export...',
          flush=True)
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            process.wait(timeout=min(60, remaining))
        except subprocess.TimeoutExpired:
            print(f'  loadgen/collector still running; {max(0, int(remaining))}s timeout budget remains',
                  flush=True)
    return process.returncode


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


def validate_driver_counters(driver, trace_count, max_unstarted_fraction=0):
    """Validate that every open-loop arrival was emitted and reached an outcome."""
    keys = ('planned', 'started', 'completed', 'dropped', 'failed', 'unfinished',
            'trace_events_total')
    for key in keys:
        if type(driver.get(key)) is not int or driver[key] < 0:
            raise ValueError(f'invalid driver counter {key}')
    minimum = math.ceil((1 - max_unstarted_fraction) * trace_count)
    if (driver['trace_events_total'] != trace_count
            or driver['planned'] > trace_count
            or driver['planned'] < minimum
            or driver['started'] != driver['planned']
            or driver['dropped'] != 0
            or driver['unfinished'] != 0
            or driver['completed'] + driver['failed'] != driver['started']):
        raise ValueError('incomplete or invalid offered workload')


def frontend_span_coverage(rows, origin, duration, warmup, window):
    """Return total frontend spans and empty complete windows after warm-up."""
    if window <= 0 or warmup < 0 or duration <= warmup:
        raise ValueError('invalid frontend coverage window')
    windows = (duration - warmup) // window
    if windows <= 0:
        raise ValueError('no complete frontend coverage window')
    counts = [0] * windows
    for row in rows:
        if row.get('service') != 'frontend':
            continue
        timestamp = float(row['timestamp'])
        if not math.isfinite(timestamp):
            raise ValueError('nonfinite raw span timestamp')
        index = math.floor((timestamp - (origin + warmup)) / window)
        if 0 <= index < windows:
            counts[index] += 1
    return sum(counts), sum(value == 0 for value in counts), windows


def _profile_cell(plan, split, scenario, scale, seed, repeat, focus_service,
                  joint_mode=False, canary=False):
    focus_base = (dict(plan['configurations'][0]) if joint_mode
                  else baseline_for_focus(plan, focus_service))
    schedule = (joint_schedule_for(plan, seed, repeat, f'{split}:{scenario}:{scale}')
                if joint_mode else schedule_for(
                    plan, seed, repeat, 'calibrate', f'{split}:{scenario}:{scale}', focus_service
                ))
    cell = dict(
        protocol=plan['protocol'], plan_sha256=digest(plan), split=split,
        scenario=scenario, load_multiplier=scale, seed=seed, repeat=repeat,
        replicas=focus_base, config_id=digest(focus_base)[:12], arm='calibrate',
        focus_service=None if joint_mode else focus_service,
        transition_design=('joint_constrained_random_walk' if joint_mode
                           else 'isolating_focus'),
        schedule=schedule, warmup_sec=plan['warmup_sec'], window_sec=plan['window_sec'],
        min_frontend_spans=plan['min_frontend_spans'],
        max_empty_frontend_window_fraction=plan['max_empty_frontend_window_fraction'],
        drain_sec=plan['drain_sec'], collection_timeout_sec=plan['collection_timeout_sec'],
        duration_sec=plan['duration_sec'],
        post_ready_warmup_sec=plan.get('post_ready_warmup_sec', 30),
        execution_schema_version=plan.get('schema_version', 1),
        abort_rules=plan.get('abort_rules', {}), role=plan.get('data_role', 'study'),
        max_action_lateness_sec=plan['max_action_lateness_sec'],
    )
    if canary:
        cell['safety_canary'] = True
    cell['cell_id'] = digest(cell)[:20]
    return cell


def safe_envelope_v62_cells(plan):
    """Build the pre-amended 11/8/8 study without consulting collected fits."""
    safe = plan.get('safe_scenario_multipliers', {})
    if set(safe) != set(plan['scenarios']):
        raise ValueError('safe_scenario_multipliers must cover every scenario')
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in safe.values()):
        raise ValueError('safe scenario multipliers must be finite and positive')
    fractions = plan.get('evaluation_fractions')
    if fractions != [0.5, 1.0]:
        raise ValueError('v6.2 validation/test require evaluation fractions [0.5, 1.0]')
    identification = plan.get('identification_services')
    if identification != ['frontend', 'currencyservice']:
        raise ValueError('v6.2 identification services must be frontend and currencyservice')
    non_bottleneck = plan.get('non_bottleneck_workloads')
    if set(non_bottleneck or {}) != {
        'cartservice', 'productcatalogservice', 'recommendationservice'
    }:
        raise ValueError('v6.2 non-bottleneck services/workloads are incomplete')
    if any(scenario not in plan['scenarios'] for scenario in non_bottleneck.values()):
        raise ValueError('invalid non-bottleneck workload scenario')

    seeds = plan['seeds_by_split']
    cells = []
    train_seed = seeds['train'][0]
    repeat = 0
    for service in identification:
        for scenario in plan['scenarios']:
            cells.append(_profile_cell(
                plan, 'train', scenario, safe[scenario], train_seed, repeat, service
            ))
            repeat += 1
    for service, scenario in non_bottleneck.items():
        cells.append(_profile_cell(
            plan, 'train', scenario, safe[scenario], train_seed, repeat, service,
            canary=(service == 'productcatalogservice' and scenario == 'pareto'),
        ))
        repeat += 1
    for split in ('validation', 'test'):
        seed = seeds[split][0]
        for repeat, (scenario, fraction) in enumerate(itertools.product(
                plan['scenarios'], fractions)):
            cells.append(_profile_cell(
                plan, split, scenario, safe[scenario] * fraction,
                seed, repeat, None, joint_mode=True,
            ))
    if len(cells) != 27:
        raise AssertionError('v6.2 study must contain 27 non-control cells')
    pareto_target = float(plan.get('pareto_peak_target_rps', float('nan')))
    full_pareto = [cell for cell in cells if cell['scenario'] == 'pareto'
                   and cell['load_multiplier'] == safe['pareto']]
    if (not math.isfinite(pareto_target) or pareto_target <= 0 or not full_pareto
            or any(not math.isclose(designed_peak_rps(cell), pareto_target,
                                    rel_tol=0, abs_tol=1e-9)
                   for cell in full_pareto)):
        raise ValueError('Pareto safe multiplier must normalize every full-load seed to 50 RPS')
    for cell in cells:
        cell_envelope_evidence(plan, cell)
    return cells


def plan_cells(plan):
    if plan['protocol'] != TWIN_PROTOCOL or plan['sampling_percentage'] != 10:
        raise ValueError('unsupported digital-twin protocol or sampling != 10%')
    if type(plan.get('max_concurrent')) is not int or plan['max_concurrent'] <= 0:
        raise ValueError('profile max_concurrent must be a positive integer')
    for name in ('capacity_poll_sec', 'max_clock_error_sec', 'max_action_lateness_sec'):
        if not math.isfinite(plan[name]) or plan[name] <= 0:
            raise ValueError(f'invalid {name}')
    if plan['capacity_poll_sec'] > 5 or plan['max_clock_error_sec'] > 2 or plan['max_action_lateness_sec'] > 10:
        raise ValueError('capture timing exceeds 10s alignment budget')
    if (type(plan['recovery_dwell_sec']) is not int or plan['recovery_dwell_sec'] < 60
            or plan['recovery_dwell_sec'] % 10):
        raise ValueError('recovery_dwell_sec must be an integer >=60 aligned to 10s')
    schedule_for(plan, 0, 0, 'calibrate', focus_service=plan['controlled_services'][0])
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
    expected_splits = {'train', 'validation', 'test'} if plan.get('data_role','study')=='study' else {'train'}
    if set(plan['seeds_by_split']) != expected_splits or len(set(seeds)) != len(seeds):
        raise ValueError('study splits must be disjoint; diagnostic/smoke use train only')
    if any(not x for x in plan['seeds_by_split'].values()):
        raise ValueError('each split needs a workload seed')
    if any(type(s) is not int or s < 0 for s in seeds):
        raise ValueError('nonnegative integer seeds required')
    for key in ('repeats', 'duration_sec', 'window_sec', 'min_frontend_spans'):
        if type(plan[key]) is not int or plan[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('warmup_sec', 'settle_sec', 'cooldown_sec', 'drain_sec'):
        if type(plan[key]) is not int or plan[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    if type(plan['collection_timeout_sec']) is not int or plan['collection_timeout_sec'] < 300:
        raise ValueError('collection_timeout_sec must be an integer >=300')
    if plan['window_sec'] % 10 or plan['warmup_sec'] % plan['window_sec']:
        raise ValueError('use windows aligned to 10s and warmup aligned to window size')
    if not 0 <= plan['warmup_sec'] < plan['duration_sec'] - plan['window_sec']:
        raise ValueError('duration must leave complete windows after warmup')
    if plan['max_unstarted_fraction'] != 0:
        raise ValueError('digital-twin profiling requires every planned arrival to start')
    if (not 0 <= plan['max_empty_frontend_window_fraction'] < 1
            or type(plan['min_frontend_spans']) is not int
            or plan['min_frontend_spans'] <= 0):
        raise ValueError('invalid frontend trace coverage gate')
    if any(s not in ('nhpp', 'mmpp', 'onoff', 'pareto') for s in plan['scenarios']):
        raise ValueError('unsupported scenario')
    if not plan['scenarios'] or not plan['load_multipliers']:
        raise ValueError('empty workload grid')
    if len(set(plan['scenarios'])) != len(plan['scenarios']) or len(set(plan['load_multipliers'])) != len(plan['load_multipliers']):
        raise ValueError('duplicate workload grid values')
    if any(not math.isfinite(x) or x <= 0 for x in plan['load_multipliers']):
        raise ValueError('load multipliers must be positive and finite')
    scenario_loads = plan.get('scenario_load_multipliers')
    if scenario_loads is not None and (
            set(scenario_loads) != set(plan['scenarios'])
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or value <= 0 or value not in plan['load_multipliers']
                   for value in scenario_loads.values())):
        raise ValueError('scenario_load_multipliers must map every scenario to a configured load')
    if plan.get('design') == 'safe_envelope_v6_2':
        return safe_envelope_v62_cells(plan)
    cells = []
    focus_services = list(plan['controlled_services'])
    focus_cycle = plan.get('focus_cycle')
    expected_focus_cycle = (2 * len(plan['scenarios']) if plan.get('design') == 'focus_balanced_v2'
                            else len(plan['scenarios']) * len(plan['load_multipliers']))
    if focus_cycle is not None and (len(focus_cycle) != expected_focus_cycle
                                    or any(s not in focus_services for s in focus_cycle)):
        raise ValueError('focus_cycle must assign one controlled service to every workload cell')
    for split_index, (split, split_seeds) in enumerate(plan['seeds_by_split'].items()):
        if plan.get('design') == 'focus_balanced_v2':
            if len(split_seeds) != 1 or plan['repeats'] != 1:
                raise ValueError('focus_balanced_v2 requires one seed per split and repeats=1')
            seed = split_seeds[0]
            if split == 'train':
                focus_loads = plan.get('focus_loads_by_service', {})
                if set(focus_loads) != set(focus_services) or any(
                        len(focus_loads[s]) != len(plan['scenarios']) or
                        any(x not in plan['load_multipliers'] for x in focus_loads[s])
                        for s in focus_services):
                    raise ValueError('focus_loads_by_service must assign a valid load to every service/scenario')
                combinations = [
                    (scenario, focus_loads[service][scenario_index], seed,
                     scenario_index, service)
                    for service_index, service in enumerate(focus_services)
                    for scenario_index, scenario in enumerate(plan['scenarios'])]
            else:
                evaluation_loads = plan.get('evaluation_loads',
                                            [plan['load_multipliers'][0], plan['load_multipliers'][-1]])
                if len(evaluation_loads) != 2 or any(x not in plan['load_multipliers'] for x in evaluation_loads):
                    raise ValueError('focus_balanced_v2 requires two valid evaluation loads')
                combinations = [(scenario, scale, seed, index, None)
                    for index, (scenario, scale) in enumerate(itertools.product(plan['scenarios'], evaluation_loads))]
        elif scenario_loads is not None:
            combinations = [
                (scenario, scenario_loads[scenario], seed, repeat, None)
                for scenario, seed, repeat in itertools.product(
                    plan['scenarios'], split_seeds, range(plan['repeats']))
            ]
        else:
            combinations = [(scenario, scale, seed, repeat, None)
                for scenario, scale, seed, repeat in itertools.product(
                    plan['scenarios'], plan['load_multipliers'], split_seeds, range(plan['repeats']))]
        for combination_index, (scenario, scale, seed, repeat, assigned_focus) in enumerate(combinations):
            # Twelve cells per split are rotated over five services (2--3 focus
            # runs each). Rotation by split avoids coupling a service to one
            # scenario/load cell across train, validation and test.
            joint_mode = plan.get('design') == 'focus_balanced_v2' and split in ('validation', 'test')
            if assigned_focus:
                focus_service = assigned_focus
            elif focus_cycle:
                focus_service = focus_cycle[(combination_index + 2 * split_index) % len(focus_cycle)]
            else:
                focus_service = focus_services[(combination_index + 2 * split_index) % len(focus_services)]
            for k in configurations:
                focus_base = (dict(plan['configurations'][0]) if joint_mode
                              else baseline_for_focus(plan, focus_service))
                schedule = (joint_schedule_for(plan, seed, repeat, f'{split}:{scenario}:{scale}')
                            if joint_mode else schedule_for(plan, seed, repeat, 'calibrate',
                                                           f'{split}:{scenario}:{scale}', focus_service))
                cell = dict(protocol=plan['protocol'], plan_sha256=digest(plan), split=split,
                            scenario=scenario, load_multiplier=scale, seed=seed, repeat=repeat,
                            replicas=focus_base, config_id=digest(focus_base)[:12], arm='calibrate',
                            focus_service=None if joint_mode else focus_service,
                            transition_design='joint_constrained_random_walk' if joint_mode else 'isolating_focus',
                            schedule=schedule,
                            warmup_sec=plan['warmup_sec'], window_sec=plan['window_sec'],
                            min_frontend_spans=plan['min_frontend_spans'],
                            max_empty_frontend_window_fraction=plan['max_empty_frontend_window_fraction'],
                            drain_sec=plan['drain_sec'],
                            collection_timeout_sec=plan['collection_timeout_sec'],
                            duration_sec=plan['duration_sec'],
                            post_ready_warmup_sec=plan.get('post_ready_warmup_sec',30),
                            execution_schema_version=plan.get('schema_version',1),
                            abort_rules=plan.get('abort_rules',{}), role=plan.get('data_role','study'),
                            max_action_lateness_sec=plan['max_action_lateness_sec'])
                cell['cell_id'] = digest(cell)[:20]
                cells.append(cell)
    return cells


def execution_cells(plan):
    """Three randomized waves balance scenario/load while rotating split assignment.

    One run per block; 90 min is a hard execution budget, not a Jaeger TTL theorem.
    Control repeats use identical seed/schedule and are excluded from all model splits.
    """
    cells=plan_cells(plan)
    rng=random.Random(plan.get('order_seed',20261105))
    groups={}
    for c in cells:
        groups.setdefault((c['scenario'],c['load_multiplier'],c['repeat']),[]).append(c)
    for values in groups.values(): rng.shuffle(values)
    ordered=[]
    for wave in range(max(map(len,groups.values()))):
        batch=[values[wave] for values in groups.values() if wave<len(values)]
        rng.shuffle(batch); ordered.extend(batch)
    canaries = [cell for cell in ordered if cell.get('safety_canary')]
    if len(canaries) > 1:
        raise ValueError('at most one safety canary is allowed')
    if canaries:
        ordered.remove(canaries[0])
        ordered.insert(0, canaries[0])
    spec=plan.get('control_cell')
    if spec:
        base=next(c for c in cells if c['scenario']==spec['scenario'] and c['load_multiplier']==spec['load_multiplier'])
        controls=[]
        for repeat in range(3):
            c=copy.deepcopy(base); c.pop('cell_id')
            c.update(split='control',seed=spec['seed'],repeat=repeat,role='drift_control')
            c['focus_service']=spec.get('focus_service', plan['controlled_services'][0])
            c['replicas']=baseline_for_focus(plan,c['focus_service'])
            c['config_id']=digest(c['replicas'])[:12]
            c['schedule']=schedule_for(plan,spec['seed'],0,'calibrate','drift-control',c['focus_service'])
            c['cell_id']=digest(c)[:20]; controls.append(c)
        if canaries:
            first, remaining = ordered[0], ordered[1:]
            mid=len(remaining)//2
            ordered=[first,controls[0]]+remaining[:mid]+[controls[1]]+remaining[mid:]+[controls[2]]
        else:
            mid=len(ordered)//2
            ordered=[controls[0]]+ordered[:mid]+[controls[1]]+ordered[mid:]+[controls[2]]
    for cell in ordered:
        cell_envelope_evidence(plan, cell)
    return ordered


def registered_order(plan, max_concurrent, plan_path=None):
    body=dict(plan_sha256=digest(plan),seed=plan.get('order_seed',20261105),
        max_concurrent=max_concurrent,block_max_sec=plan.get('block_max_sec',5400),
        protocol_version=PROTOCOL_VERSION,
        collection_lock_sha256=collection_lock_sha256(plan_path),
        collection_code_sha256=collection_lock_hashes(plan_path),
        trace_definition_sha256=trace_definition_hashes(),
        executions=[dict(block=i,cell_id=c['cell_id'],scenario=c['scenario'],split=c['split'],
                         load_multiplier=c['load_multiplier'],seed=c['seed'],role=c.get('role','study'),
                         envelope=cell_envelope_evidence(plan,c))
                    for i,c in enumerate(execution_cells(plan))])
    return dict(body,order_sha256=digest(body))


def validate_v62_canary_checkpoint(plan, all_cells, selected_cells,
                                   completed_ids, max_new):
    """Require the registered Pareto canary to complete as a one-cell checkpoint."""
    if plan.get('design') != 'safe_envelope_v6_2':
        return
    canaries = [cell for cell in all_cells if cell.get('safety_canary')]
    if len(canaries) != 1 or not all_cells or all_cells[0] != canaries[0]:
        raise ValueError('v6.2 requires exactly one first-position safety canary')
    canary_id = canaries[0]['cell_id']
    if canary_id in completed_ids:
        return
    if max_new != 1:
        raise ValueError('first v6.2 collection must use --max-new 1 for the safety canary')
    if not selected_cells or selected_cells[0]['cell_id'] != canary_id:
        raise ValueError('v6.2 safety canary cannot be skipped by collection filters')


def analyze_collected(root, plan, report_path=None):
    """Incrementally analyze collected runs without cluster access or raw mutation."""
    from calibration import analyze
    root = Path(root)
    cells = {cell['cell_id']: cell for cell in execution_cells(plan)}
    results = []
    for meta_path in sorted(root.glob('*/meta.json')):
        folder = meta_path.parent
        expected = cells.get(folder.name)
        if expected is None:
            results.append(dict(cell_id=folder.name, status='foreign', error='not in plan'))
            continue
        actual = read_json(meta_path).get('profiling')
        try:
            validate_run(folder, actual, plan['max_unstarted_fraction'])
            analysis = analyze(folder)
            results.append(dict(cell_id=folder.name, status='analyzed',
                                analyzer_sha256=analysis['code_sha256']))
        except Exception as exc:
            results.append(dict(cell_id=folder.name, status='analysis_failed', error=repr(exc)))
    for censored in plan.get('safety_censored_cells', []):
        results.append(dict(cell_id=censored['cell_id'], status='safety_censored',
                            exclude_from_fit=True, reason=censored['reason']))
    payload = dict(schema_version=1, protocol_version=PROTOCOL_VERSION,
                   analyzed_root=str(root), results=results,
                   analyzed=sum(row['status']=='analyzed' for row in results),
                   failed=sum(row['status']=='analysis_failed' for row in results),
                   safety_censored=sum(row['status']=='safety_censored' for row in results))
    target = Path(report_path) if report_path else root / 'analysis_progress.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    temporary.replace(target)
    print(json.dumps(payload, indent=2))
    return payload


def collected_run_ids(root, cells, expected_profile, max_unstarted_fraction):
    """Return cells with complete immutable raw artifacts.

    This scan intentionally knows nothing about calibration outputs.  A failed or
    stale analyzer therefore cannot turn a collected run back into an incomplete
    run, trigger archival, or make resume send workload again.
    """
    root = Path(root)
    completed = set()
    for cell in cells:
        folder = root / cell['cell_id']
        if not folder.exists():
            continue
        try:
            validate_run(folder, expected_profile(cell), max_unstarted_fraction)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        completed.add(cell['cell_id'])
    return completed


def validate_execution_artifacts(root, driver):
    """Phase-1 integrity only; no unmeasured performance thresholds are invented."""
    if driver.get('execution_schema_version') != 1 or driver.get('execution_logging_error'):
        raise ValueError('execution logger missing or failed')
    schemas = {
        'sessions.csv': ('session_id', 'scheduled_ts', 'actual_start_ts', 'planned_size',
                         'actual_size', 'end_ts', 'dropped', 'outcome', 'lag_ms'),
        'client_requests.csv': ('request_id', 'session_id', 'endpoint', 't_start', 't_end',
                                'status', 'latency_ms', 'redirect_count', 'error', 'censored_at'),
        'in_flight.csv': ('timestamp', 'sessions_in_flight', 'requests_in_flight'),
        'loadgen_resources.csv': ('timestamp', 'node_cpu_busy_pct', 'node_mem_available_bytes',
                                  'process_rss_bytes'),
    }
    for name, fields in schemas.items():
        validate_csv_contract(Path(root) / name, fields)
    sessions = csv_rows(Path(root) / 'sessions.csv')
    requests = csv_rows(Path(root) / 'client_requests.csv')
    session_ids = {row['session_id'] for row in sessions}
    if len(session_ids) != len(sessions) or len(sessions) != driver['planned']:
        raise ValueError('execution session count/identity mismatch')
    if (len({row['request_id'] for row in requests}) != len(requests)
            or len(requests) != driver['requests_started']
            or any(row['session_id'] not in session_ids for row in requests)):
        unique_requests=len({row['request_id'] for row in requests})
        unknown_sessions=sum(row['session_id'] not in session_ids for row in requests)
        raise ValueError(
            'execution request count/identity mismatch: '
            f'rows={len(requests)}, unique_ids={unique_requests}, '
            f'driver_started={driver.get("requests_started")}, '
            f'unknown_sessions={unknown_sessions}'
        )
    if driver['requests_started'] != driver['requests_completed'] or driver['request_censored']:
        raise ValueError('unfinished client requests in execution log')
    for row in requests:
        values = [float(row[key]) for key in ('t_start', 't_end', 'latency_ms')]
        if not all(math.isfinite(v) for v in values) or values[1] < values[0] or values[2] < 0:
            raise ValueError('invalid client request timing')
    if driver['driver_exceptions'] != driver['failed']:
        raise ValueError('driver exception counter mismatch')
    exception_rows = {
        row['session_id']: row
        for row in sessions if row.get('outcome') == 'driver_exception'
    }
    transport_requests = [
        row for row in requests
        if row.get('status') in ('', '0')
        and row.get('error') not in ('', 'right_censored')
    ]
    transport_by_session = defaultdict(list)
    for row in transport_requests:
        transport_by_session[row['session_id']].append(row)
    unexplained = []
    for session_id, session in exception_rows.items():
        candidates = transport_by_session.get(session_id, ())
        driver_error = session.get('driver_error', '')
        if not candidates or (driver_error and not any(
                request.get('error') in (driver_error, 'transport_error')
                for request in candidates)):
            unexplained.append(session_id)
    # A Locust response can carry status=0/error without raising to the session
    # greenlet.  Such a request is still a valid measured transport outcome and
    # the session may finish normally.  The required relation is therefore
    # driver-exception sessions ⊆ transport-error sessions, not equality.
    if (len(exception_rows) != driver['driver_exceptions']
            or len(transport_requests) != driver.get('transport_errors')
            or unexplained):
        raise ValueError(
            'driver/transport evidence mismatch: '
            f'exception_sessions={len(exception_rows)}, '
            f'driver_exceptions={driver.get("driver_exceptions")}, '
            f'transport_rows={len(transport_requests)}, '
            f'transport_errors={driver.get("transport_errors")}, '
            f'unexplained_sessions={unexplained}'
        )


def loadgen_failure_with_evidence(failure, root):
    """Attach the driver's fail-closed reason to an early-exit controller error."""
    if not (isinstance(failure, RuntimeError)
            and str(failure) == 'loadgen exited before scheduled duration'):
        return failure
    try:
        driver = read_json(Path(root) / 'driver_report.json')
    except (OSError, ValueError, json.JSONDecodeError):
        return failure
    reason = driver.get('execution_logging_error')
    if not reason:
        return failure
    details = []
    for key in ('planned', 'started', 'dropped', 'max_in_flight', 'max_concurrent'):
        if key in driver:
            details.append(f'{key}={driver[key]}')
    suffix = f" ({', '.join(details)})" if details else ''
    return RuntimeError(f'loadgen aborted before scheduled duration: {reason}{suffix}')


def validate_locust_process(meta, driver):
    """Separate Locust process health from measured HTTP failures.

    Locust normally exits with status 1 when any request is marked failed.  HTTP
    5xx responses are an observed system outcome in a saturation experiment, not
    evidence that the load generator crashed.  New wrappers disable that exit
    policy.  Status 1 is accepted only for artifacts produced before that change
    and only when the independent execution journal proves a complete run.
    """
    sampling = meta.get('trace_sampling_percentage')
    try:
        sampling = float(sampling)
    except (TypeError, ValueError):
        raise ValueError(f'invalid trace sampling percentage: {sampling!r}') from None
    if not math.isfinite(sampling) or sampling != 10:
        raise ValueError(f'trace sampling percentage is {sampling!r}, expected 10')

    exit_code = meta.get('locust_exit_code')
    if exit_code == 0:
        return
    legacy_complete = (
        exit_code == 1
        and driver.get('execution_schema_version') == 1
        and not driver.get('execution_logging_error')
        and all(type(driver.get(key)) is int for key in (
            'planned', 'started', 'completed', 'dropped', 'failed', 'unfinished',
            'trace_events_total'))
        and driver['trace_events_total'] == driver['planned']
        and driver['planned'] == driver['started']
        and driver['completed'] + driver['failed'] == driver['started']
        and driver['dropped'] == driver['unfinished'] == 0
    )
    if not legacy_complete:
        raise ValueError(
            f'Locust process exit code {exit_code!r} without a complete execution journal'
        )


def validate_run(root, expected=None, max_unstarted_fraction=0):
    """Fail closed; sampled empty windows are distinct from failed API queries."""
    root = Path(root)
    meta, report, driver = (read_json(root / name) for name in
                            ('meta.json', 'collect_report.json', 'driver_report.json'))
    profile = meta.get('profiling')
    if not profile or (expected is not None and profile != expected):
        raise ValueError(f'{root}: profiling identity mismatch')
    if any(meta[k] != profile[k] for k in ('scenario', 'seed', 'load_multiplier')):
        raise ValueError(f'{root}: workload metadata mismatch')
    if meta.get('duration_sec') != profile['duration_sec']:
        raise ValueError(f'{root}: measurement duration mismatch')
    expected_lock = profile.get('collection_lock_sha256')
    if expected_lock is not None and meta.get('collection_lock_sha256') != expected_lock:
        raise ValueError(f'{root}: collection lock hash mismatch')
    try:
        validate_locust_process(meta, driver)
    except ValueError as exc:
        raise ValueError(f'{root}: {exc}') from exc
    if meta.get('execution_schema_version') == 1:
        validate_execution_artifacts(root, driver)
    if profile.get('execution_schema_version',1)>=2:
        if meta.get('calibration_schema_version')!=2 or meta.get('execution_schema_version')!=1:
            raise ValueError('v2 profile requires new execution and calibration schemas')
        validate_csv_contract(root/'node_windows_30s.csv',('window_end','service','cpu_cores','replicas'))
        validate_csv_contract(root/'cpu_counter_10s.csv',('timestamp','series_id','service','cpu_seconds_total'))
        validate_csv_contract(root/'jaeger_server_spans.csv',
                              ('timestamp','end_timestamp','trace_id','span_id','latency_ms'))
    if any(report.get(k, None) != [] for k in ('jaeger_failed_queries', 'prometheus_query_failures')):
        raise ValueError(f'{root}: missing query status or failed telemetry queries')
    if report.get('jaeger_truncated_windows') != 0 or report.get('raw_server_spans', 0) <= 0:
        raise ValueError(f'{root}: truncated/missing Jaeger data')
    required = ('cpu_cores', 'mem_bytes', 'replicas', 'replicas_desired')
    if any(report.get('prometheus_series', {}).get(k, 0) <= 0 for k in required):
        raise ValueError(f'{root}: missing required Prometheus metric')
    if set(report.get('jaeger_services', {})) != set(SERVICES):
        raise ValueError(f'{root}: missing traced service registry')
    validate_csv_contract(
        root / 'service_metrics.csv',
        ('timestamp', 'service', 'request_count', 'error_count', 'latency_p50_ms', 'latency_p95_ms'),
    )
    validate_csv_contract(root / 'locust_stats_history.csv', ('Timestamp', 'Name'))
    trace = csv_rows(root / 'arrival_trace.csv')
    if hashlib.sha256((root / 'arrival_trace.csv').read_bytes()).hexdigest() != meta['trace_sha256']:
        raise ValueError(f'{root}: trace hash mismatch')
    if not trace:
        raise ValueError(f'{root}: empty offered workload')
    try:
        validate_driver_counters(driver, len(trace), max_unstarted_fraction)
    except ValueError as exc:
        raise ValueError(f'{root}: {exc}') from exc
    origin = float(driver['trace_start_epoch'])
    if not math.isfinite(origin) or not meta['start'] <= origin <= meta['end']:
        raise ValueError(f'{root}: invalid arrival clock')
    if (meta.get('drain_sec') != profile['drain_sec']
            or not 0 <= origin - meta['start'] < 1
            or not 0 <= meta['end'] - (origin + meta['duration_sec']) < 1):
        raise ValueError(f'{root}: measurement/drain boundary mismatch')
    raw_spans = csv_rows(root / 'jaeger_server_spans.csv')
    if not raw_spans:
        raise ValueError(f'{root}: empty raw span export')
    # Capture density is a scientific analysis result, not collection integrity.
    # A sparse-but-complete raw run remains collected and can correctly produce
    # capture_valid=False or mu=None during offline analysis.
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
    cells = {c['cell_id']: c for c in execution_cells(plan)}
    entries, contexts, traces = [], set(), {}
    files = ('meta.json', 'collect_report.json', 'driver_report.json', 'driver_started.json',
             'capacity_events.jsonl', 'node_metrics.csv', 'service_metrics.csv',
             'edge_metrics.csv', 'locust_stats_history.csv', 'arrival_trace.csv',
             'designed_rate.csv', 'jaeger_server_spans.csv')
    if plan.get('schema_version', 1) >= 2:
        files += ('sessions.csv', 'client_requests.csv', 'in_flight.csv',
                  'loadgen_resources.csv', 'cpu_counter_10s.csv', 'node_windows_30s.csv',
                  'process_identity.json')
    for folder in sorted(p.parent for p in root.glob('*/meta.json')):
        actual = read_json(folder/'meta.json')['profiling']
        runtime_fields = {'cluster_context_sha256', 'collection_protocol_version',
                          'collection_lock_sha256'}
        if {k:v for k,v in actual.items() if k not in runtime_fields} != cells.get(folder.name):
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
                            role=actual.get('role', 'study'),
                            split={'validation':'val'}.get(actual['split'], actual['split']),
                            scenario=actual['scenario'], seed=actual['seed'], arm=actual['arm'],
                            repeat=actual['repeat'], load_multiplier=actual['load_multiplier'],capacity_delay_audit=delay_audit,
                            raw_sha256={f:hashlib.sha256((folder/f).read_bytes()).hexdigest() for f in files}))
    if not entries or len(contexts) != 1 or any(len(v) != 1 for v in traces.values()):
        raise ValueError('empty, mixed context or unpaired arrival traces')
    return dict(protocol=TWIN_PROTOCOL, schema_version=plan.get('schema_version',1), plan=plan,
                runs=[r for r in entries if r['role']=='study'],
                excluded_runs=[r for r in entries if r['role']!='study'],
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
    return subprocess.check_output(argv, text=True, timeout=30).strip()


def span_drop_counters(payload):
    counters={}
    for line in payload.splitlines():
        if not line or line.startswith('#'): continue
        parts=line.rsplit(None,1)
        if len(parts)!=2: continue
        name=parts[0].split('{',1)[0]
        if re.search(r'(spans_dropped|spans_rejected|refused_spans)(?:_total)?$',name):
            value=float(parts[1])
            if not math.isfinite(value) or value<0: raise ValueError('invalid Jaeger drop counter')
            counters[parts[0]]=value
    return counters


def parse_df_available_kib(output):
    """Parse the final POSIX `df -Pk` data row."""
    rows = [line.split() for line in output.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[-1]) < 4:
        raise ValueError('cannot parse df -Pk output')
    value = int(rows[-1][3])
    if value < 0:
        raise ValueError('negative available disk reported')
    return value


def cpu_quantity_cores(value):
    """Parse the Kubernetes CPU quantity forms used by the deployment templates."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cores = float(value)
    elif isinstance(value, str):
        text = value.strip()
        units = {'n': 1e-9, 'u': 1e-6, 'm': 1e-3}
        suffix = text[-1:] if text else ''
        try:
            cores = float(text[:-1]) * units[suffix] if suffix in units else float(text)
        except ValueError:
            raise ValueError(f'invalid Kubernetes CPU quantity: {value!r}') from None
    else:
        raise ValueError(f'invalid Kubernetes CPU quantity: {value!r}')
    if not math.isfinite(cores) or cores <= 0:
        raise ValueError(f'invalid Kubernetes CPU quantity: {value!r}')
    return cores


def validate_envelope_cpu_limits(plan, deployments):
    """Bind rho_peak denominators to the live application-container CPU limits."""
    expected = plan['envelope_gate']['cpu_limit_cores_per_pod']
    for service, registered_limit in expected.items():
        if service not in deployments:
            raise RuntimeError(f'envelope service deployment missing: {service}')
        containers = deployments[service]['spec']['template']['spec'].get('containers', [])
        application = [container for container in containers
                       if container.get('name') != 'istio-proxy']
        if len(application) != 1:
            raise RuntimeError(
                f'{service}: expected exactly one application container for envelope validation'
            )
        actual_value = application[0].get('resources', {}).get('limits', {}).get('cpu')
        actual = cpu_quantity_cores(actual_value)
        expected_value = float(registered_limit)
        if not math.isclose(actual, expected_value, rel_tol=0, abs_tol=1e-12):
            raise RuntimeError(
                f'{service}: live CPU limit {actual:g} cores differs from '
                f'registered envelope {expected_value:g} cores'
            )


def remote_clock_error_bound(ssh, samples=15):
    """Return the lowest-delay midpoint clock bound from repeated samples.

    The SSH base enables ControlPersist, so these probes reuse an established
    connection. Including a fresh SSH handshake in a single sample can add
    hundreds of milliseconds of uncertainty and falsely fail a 100 ms gate.
    Each sample yields a conservative error bound. Selecting the smallest
    complete bound across repeated probes remains conservative while avoiding
    a false failure caused by one asymmetric SSH round trip.
    """
    if samples < 1:
        raise ValueError('clock probe requires at least one sample')
    measured = []
    for _ in range(samples):
        before = time.time_ns()
        remote_ns = int(command(ssh + ["python3 -c 'import time; print(time.time_ns())'"]))
        after = time.time_ns()
        midpoint = (before + after) / 2
        offset = (remote_ns - midpoint) / 1e9
        uncertainty = (after - before) / 2e9
        measured.append((abs(offset) + uncertainty, offset, uncertainty))
    return min(measured, key=lambda item: item[0])


def directory_bytes(path):
    return sum(item.stat().st_size for item in Path(path).rglob('*') if item.is_file())


def storage_estimate(smoke_root, plan):
    """Extrapolate exported artifacts from validated smoke runs.

    Jaeger Badger is bounded by the configured 3h TTL, so the estimate reports
    peak store input rather than incorrectly multiplying Badger by all 21h.
    """
    samples = []
    for meta_path in Path(smoke_root).glob('*/meta.json'):
        folder = meta_path.parent
        try:
            meta = read_json(meta_path)
            validate_run(folder)
            if meta['duration_sec'] <= 0:
                continue
            profile = meta['profiling']
            multiplier = float(profile['load_multiplier'])
            if multiplier <= 0 or profile['scenario'] not in plan['scenarios']:
                continue
            total_rate = directory_bytes(folder) / meta['duration_sec']
            spans = folder / 'jaeger_server_spans.csv'
            node = folder / 'node_metrics.csv'
            samples.append(dict(
                scenario=profile['scenario'],
                multiplier=multiplier,
                total=total_rate,
                spans=spans.stat().st_size / meta['duration_sec'] if spans.is_file() else 0,
                prometheus=node.stat().st_size / meta['duration_sec'] if node.is_file() else 0,
            ))
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
    if not samples:
        return None
    by_scenario = {
        scenario: [sample for sample in samples if sample['scenario'] == scenario]
        for scenario in plan['scenarios']
    }
    cells = execution_cells(plan)

    def projected_rate(cell, key, scale_with_load):
        candidates = by_scenario[cell['scenario']]
        if not candidates:
            return 0
        if scale_with_load:
            # Smoke is intentionally collected at load 0.5. Scale trace/client
            # volume to each main cell instead of treating that low-load byte
            # rate as representative of the 1.0/1.5 cells. max() is deliberate:
            # storage is a safety guard, not an unbiased statistical estimate.
            return max(sample[key] / sample['multiplier'] * cell['load_multiplier']
                       for sample in candidates)
        return max(sample[key] for sample in candidates)

    exported = sum(projected_rate(cell, 'total', True) * plan['duration_sec']
                   for cell in cells)
    prometheus = sum(projected_rate(cell, 'prometheus', False) * plan['duration_sec']
                     for cell in cells)
    spans = sum(projected_rate(cell, 'spans', True) * plan['duration_sec']
                for cell in cells)
    max_main_multiplier = max(cell['load_multiplier'] for cell in cells)
    peak_span_rate = max(
        sample['spans'] / sample['multiplier'] * max_main_multiplier
        for sample in samples
    )
    return dict(
        smoke_samples=len(samples),
        covered_scenarios=sorted(scenario for scenario, values in by_scenario.items() if values),
        exported_bytes=exported,
        prometheus_export_bytes=prometheus,
        client_and_other_bytes=max(0, exported - spans - prometheus),
        # Conservative Badger overhead factor; TTL is pinned to 3h in Compose.
        jaeger_peak_bytes=peak_span_rate * 3 * 3600 * 3.0,
    )


def write_progress(path, plan, context_hash, completed, total, status, current=None, error=None):
    payload = dict(
        schema_version=1,
        plan_sha256=digest(plan),
        cluster_context_sha256=context_hash,
        status=status,
        completed_cell_ids=sorted(completed),
        completed_count=len(completed),
        total_count=total,
        current_cell_id=current,
        error=error,
        updated_epoch=time.time(),
        note='Validated run directories are the resume source of truth; this file is an atomic checkpoint.',
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def validate_csv_contract(path, required_columns):
    path = Path(path)
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        if not set(required_columns) <= fields:
            raise ValueError(f'{path}: missing CSV columns {sorted(set(required_columns)-fields)}')
        if next(reader, None) is None:
            raise ValueError(f'{path}: CSV contains no data rows')


def validate_app_pod(pod):
    """Validate application readiness and either legacy or native Istio sidecar.

    Kubernetes native sidecars are restartable init containers, so inspecting
    only spec.containers/containerStatuses incorrectly reports them as missing.
    Ordinary completed init containers are not required to remain Ready.
    """
    metadata = pod.get('metadata', {})
    status = pod.get('status', {})
    spec = pod.get('spec', {})
    name = metadata.get('name', '<unknown>')
    app_statuses = status.get('containerStatuses', [])
    init_statuses = status.get('initContainerStatuses', [])
    declared = {
        container.get('name')
        for key in ('containers', 'initContainers')
        for container in spec.get(key, [])
    }
    status_by_name = {
        item.get('name'): item
        for item in (*app_statuses, *init_statuses)
    }
    if status.get('phase') != 'Running' or not app_statuses or not all(
            item.get('ready') for item in app_statuses):
        raise RuntimeError(f'{name}: pod/application containers are not Ready')
    proxy = status_by_name.get('istio-proxy')
    if 'istio-proxy' not in declared or proxy is None or not proxy.get('ready'):
        raise RuntimeError(f'{name}: Istio proxy is missing or not Ready')


@contextmanager
def campaign_lock(args):
    """Atomic create, no TTL-based takeover of a potentially active experiment."""
    import fcntl  # Live controller is Linux; offline preview/tests stay portable.
    local=Path('/tmp/digital-twin-profiling-controller.lock')
    with local.open('a+') as stream:
        try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another local profiling controller holds the lock')
        token=uuid.uuid4().hex
        kube=['kubectl','--request-timeout=15s','-n',args.namespace]
        name='digital-twin-profiling-lock'
        obj=dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name=name,namespace=args.namespace),
                 data=dict(holder=token,output=str(Path(args.output).resolve()),created_epoch=str(time.time())))
        subprocess.run(kube+['create','-f','-'],input=json.dumps(obj),text=True,check=True,timeout=30)
        try:
            yield
        finally:
            if getattr(args,'_remote_unconfirmed',False):
                print('Remote stop unconfirmed: cluster lock retained. Inspect remote process before manual unlock.',flush=True)
            else:
                current=json.loads(command(kube+['get','configmap',name,'-o','json']))
                if current.get('data',{}).get('holder')==token:
                    subprocess.run(kube+['delete','configmap',name],check=True,timeout=30)


def run(args):
    if args.analyze_collected:
        plan = load_plan(args.plan, args.profile)
        result = analyze_collected(args.output, plan, args.analysis_report)
        if result['failed']:
            raise RuntimeError(
                f"offline analysis failed for {result['failed']} collected run(s); raw runs remain collected"
            )
        return
    if args.audit_diagnostic:
        plan=load_plan(args.plan,'main')
        diagnostic_plan=load_plan(args.plan,'diagnostic')
        expected_cells={cell['cell_id']:cell for cell in execution_cells(diagnostic_plan)}
        diagnostic_roots=[Path(args.output),*(Path(path) for path in args.diagnostic_input)]
        roots=sorted(
            (meta for root in diagnostic_roots for meta in root.glob('*/meta.json')),
            key=lambda path:(path.parent.name,str(path)),
        )
        duplicate_ids=sorted({path.parent.name for path in roots
                              if sum(other.parent.name==path.parent.name for other in roots)>1})
        if duplicate_ids:
            raise ValueError(f'duplicate diagnostic run ids across inputs: {duplicate_ids}')
        lag_pass=True; pressure={'frontend':0,'currencyservice':0}; unit_total=unit_valid=0
        contract_runs=[]; trace_overlap=[]
        def finite_csv(row,key):
            try: value=float(row.get(key,''))
            except (TypeError,ValueError): return None
            return value if math.isfinite(value) and value>=0 else None
        for meta_path in roots:
            folder=meta_path.parent
            if folder.name not in expected_cells:
                raise ValueError(f'foreign diagnostic run directory: {folder}')
            meta=read_json(meta_path)
            actual=meta.get('profiling')
            if not actual:
                raise ValueError(f'missing profiling identity: {folder}')
            identity={k:v for k,v in actual.items() if k!='cluster_context_sha256'}
            if identity!=expected_cells[folder.name]:
                raise ValueError(f'diagnostic profile changed or mismatched: {folder}')
            validate_run(folder,actual,diagnostic_plan['max_unstarted_fraction'])
            diagnostics=read_json(folder/'calibration_diagnostics.json')
            contract=diagnostics.get('observation_contract',{})
            sampling_services=contract.get('sampling',{}).get('services',{})
            trace_overlap.append(dict(cell_id=folder.name,
                frontend=sampling_services.get('frontend'),
                cartservice=sampling_services.get('cartservice'),
                currencyservice=sampling_services.get('currencyservice')))
            contract_runs.append(dict(cell_id=folder.name,
                actual_contract_pass=contract.get('actual_contract_pass') is True,
                services=contract.get('services',{})))
            report=read_json(folder/'driver_report.json')
            lag=float(report.get('scheduling_lag_ms',{}).get('p99',float('inf')))
            lag_pass &= math.isfinite(lag) and lag<=1000
            feature_path=folder/'calibration_features_30s.csv'
            if feature_path.exists():
                for row in csv_rows(feature_path):
                    service=row['service']; replicas=float(row['replicas'])
                    if service in pressure and replicas==1 and row['stable']=='True':
                        limit=finite_csv(row,'cpu_limit_cores')
                        used=finite_csv(row,'cpu_cores')
                        throttle=finite_csv(row,'cpu_throttle_ratio')
                        if ((throttle is not None and throttle>=.10)
                                or (limit is not None and limit>0 and used is not None
                                    and used/limit>=.90)):
                            pressure[service]+=1
                    if service=='frontend' and row.get('R_real') and float(row['R_real'])>0:
                        unit_total+=1
                        expected_capture=float(row['R_obs'])/float(row['R_real'])
                        unit_valid+=abs(float(row['p_dynamic'])-expected_capture)<=1e-9
        missing=sorted(set(expected_cells)-{path.parent.name for path in roots})
        if missing:
            raise ValueError(f'missing diagnostic runs: {missing}')
        cells=plan_cells(plan)
        joint_pass=all(c.get('transition_design')=='joint_constrained_random_walk'
                       for c in cells if c['split'] in ('validation','test'))
        stress=read_json(args.stress_report)
        stress_pass=stress.get('stress_acceptance',{}).get('graceful_degradation_pass') is True
        criteria=dict(scheduling_lag_p99_le_1s=lag_pass and bool(roots),
            frontend_currency_cpu_pressure=all(v>=4 for v in pressure.values()),
            joint_validation_test=joint_pass,misspecification_stress=stress_pass,
            observable_unit_identity=(unit_total>0 and unit_valid==unit_total))
        actual_contract_pass=(len(contract_runs)==len(expected_cells) and
                              all(row['actual_contract_pass'] for row in contract_runs))
        result=dict(schema_version=2,main_plan_sha256=digest(plan),
            diagnostic_plan_sha256=digest(diagnostic_plan),
            diagnostic_runs=len(roots),raw_artifacts_validated=True,criteria=criteria,
            all_pass=all(criteria.values()),pressure_windows=pressure,
            unit_windows=dict(total=unit_total,valid=unit_valid),
            observation_contract_runs=contract_runs,
            trace_overlap_audit=trace_overlap,
            promotable_to_main=False,
            actual_contract_pass=actual_contract_pass,
            reason=(None if actual_contract_pass else
                    'Dynamic capture, Effective Service Latency or downstream-normal '
                    'evidence is insufficient in one or more diagnostic runs. Diagnostic '
                    'data remains protocol-selection evidence and is never merged into train.'))
        target=Path(args.audit_diagnostic);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        print(json.dumps(result,indent=2,allow_nan=False));return
    if args.confirm or args.collect_only:
        # Protocol v6 collection is gated only by operational integrity. Scientific
        # identifiability/capture results are offline outputs and may validly be None.
        with campaign_lock(args):
            return run_impl(args)
    return run_impl(args)


def run_impl(args):
    plan = load_plan(args.plan, args.profile)
    if args.max_concurrent is None:
        args.max_concurrent = plan['max_concurrent']
    all_cells = execution_cells(plan)
    candidate_order=registered_order(plan,args.max_concurrent,args.plan)
    order=candidate_order
    if args.register_order:
        target=Path(args.output)/'campaign_order.json'; target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if read_json(target)!=candidate_order:
                raise ValueError('registered order differs; choose a new output')
        else:
            with target.open('x',encoding='utf-8') as stream:
                json.dump(candidate_order,stream,indent=2)
        print(f'Registered {len(all_cells)} executions: {target}; SHA256={order["order_sha256"]}')
        return
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
    print(f"Plan SHA256: {digest(plan)}; explicit configurations: {len(plan['configurations'])}; "
          f"sampling 10%; max concurrent {args.max_concurrent}")
    for c in cells:
        print(c['cell_id'], c['split'], c['scenario'], c['load_multiplier'], c['seed'],
              c['repeat'], c['arm'], c['replicas'])
    if not (args.confirm or args.collect_only or args.preflight_only):
        return
    if args.confirm or args.collect_only:
        registered=read_json(Path(args.output)/'campaign_order.json')
        if registered == candidate_order:
            order=registered
        elif validator_only_order_compatible(registered,candidate_order):
            order=registered
            print('Using registered v6 acquisition order with reviewed v6.1 '
                  'validator-only compatibility; no workload definition changed.')
        else:
            raise ValueError('plan/code/concurrency/order changed since registration')
    if (not args.ssh_target or not args.observability_ssh_target or not args.k3s_ssh_target
            or not args.ssh_key or ((args.confirm or args.collect_only) and args.max_new <= 0)):
        raise ValueError('live preflight requires loadgen/observability/K3s SSH targets and --ssh-key; collection also requires positive --max-new')
    if args.max_concurrent <= 0:
        raise ValueError('--max-concurrent must be a positive integer')
    if args.min_free_disk_gib < 10:
        raise ValueError('--min-free-disk-gib cannot weaken the 10 GiB safety floor')
    if not 0 < args.max_clock_skew_ms <= 100:
        raise ValueError('--max-clock-skew-ms must be in (0,100]')
    if not args.remote_dir.startswith('/') or not args.remote_results.startswith('/'):
        raise ValueError('remote paths must be absolute')
    kubectl = ['kubectl', '--request-timeout=15s', '-n', args.namespace]
    ssh = ['ssh', '-i', str(Path(args.ssh_key).expanduser()), '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
           '-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
           '-o', 'ControlPath=/tmp/twin-profile-%C', args.ssh_target]
    observability_ssh = ssh[:-1] + [args.observability_ssh_target]
    k3s_ssh = ssh[:-1] + [args.k3s_ssh_target]

    def get(resource):
        return json.loads(command(kubectl + ['get', resource, '-o', 'json']))

    def check_app_health():
        current = {d['metadata']['name']: d for d in get('deployments')['items']}
        if (not set(SERVICES) <= current.keys()
                or current.get('redis-cart', {}).get('spec', {}).get('replicas') != 1):
            raise ValueError('missing app deployments or redis-cart is not fixed at 1')
        for service in (*SERVICES, 'redis-cart'):
            deployment = current[service]
            desired = int(deployment['spec']['replicas'])
            if deployment.get('status', {}).get('availableReplicas', 0) != desired:
                raise RuntimeError(f'{service}: available replicas do not match desired before profiling')
        pods = [
            pod for pod in get('pods')['items']
            if (pod.get('metadata', {}).get('labels', {}).get('app')
                in {*SERVICES, 'redis-cart'}
                and not pod.get('metadata', {}).get('deletionTimestamp'))
        ]
        if not pods:
            raise RuntimeError('no Online Boutique pods found')
        for pod in pods:
            validate_app_pod(pod)
        return current

    def check_control():
        if get('hpa')['items']:
            raise ValueError('disable HPA before profiling')
        telemetry = json.loads(command(['kubectl', '--request-timeout=15s', '-n', 'istio-system', 'get',
                                        'telemetry', 'mesh-default', '-o', 'json']))
        if telemetry['spec']['tracing'][0].get('randomSamplingPercentage') != 10:
            raise ValueError('mesh-default must declare sampling 10%')
        # Record all policies: scoped overrides must be reviewed with actual proxy config.
        policies = json.loads(command(['kubectl', '--request-timeout=15s', 'get', 'telemetry', '-A', '-o', 'json']))
        if len(policies['items']) != 1:
            raise ValueError('additional Telemetry overrides require review before collection')

    check_control()
    if command(kubectl + ['get', '--raw=/readyz']) != 'ok':
        raise RuntimeError('K3s API /readyz is not ok')
    deployments = check_app_health()
    validate_envelope_cpu_limits(plan, deployments)

    print('Operational preflight: clocks, memory, disk, Prometheus and Jaeger...')
    max_clock_error = min(args.max_clock_skew_ms / 1000.0, plan['max_clock_error_sec'])
    k3s_nodes = get('nodes')['items']
    k3s_user = args.k3s_ssh_target.split('@', 1)[0] if '@' in args.k3s_ssh_target else None
    if not k3s_user:
        raise ValueError('--k3s-ssh-target must include user@host so worker ProxyJump can be built')
    clock_targets = [('loadgen', ssh), ('observability', observability_ssh),
                     ('k3s-control-plane', k3s_ssh)]
    for node in k3s_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if labels.get('node-role') == 'control-plane':
            continue
        addresses = {item['type']: item['address'] for item in node.get('status', {}).get('addresses', [])}
        internal_ip = addresses.get('InternalIP')
        if not internal_ip:
            raise RuntimeError(f"{node['metadata']['name']}: missing InternalIP")
        if args.direct_worker_ssh:
            # A controller running on the K3s control-plane can reach worker
            # private IPs directly. ProxyJumping through itself is both
            # unnecessary and may fail because the jump connection has its
            # own SSH identity selection.
            worker_ssh = ssh[:-1] + [f'{k3s_user}@{internal_ip}']
        else:
            worker_ssh = ssh[:-1] + ['-J', args.k3s_ssh_target, f'{k3s_user}@{internal_ip}']
        clock_targets.append((f"k3s-{node['metadata']['name']}", worker_ssh))
    def check_clocks(prefix='clock'):
        measurements = {}
        for label, remote_ssh in clock_targets:
            synchronized = command(remote_ssh + ['timedatectl show -p NTPSynchronized --value']).lower()
            if synchronized not in ('yes', 'true'):
                raise RuntimeError(f'{label}: NTP is not synchronized')
            measurement = remote_clock_error_bound(remote_ssh)
            bound, offset, uncertainty = measurement
            measurements[label] = measurement
            print(f'  {prefix} {label}: offset={offset*1000:.1f}ms '
                  f'uncertainty={uncertainty*1000:.1f}ms bound={bound*1000:.1f}ms')
            if bound > max_clock_error:
                raise RuntimeError(
                    f'{label}: clock error bound exceeds {max_clock_error*1000:g}ms; '
                    'sync NTP before profiling'
                )
        return measurements

    check_clocks()

    minimum_kib = int(args.min_free_disk_gib * 1024 * 1024)
    remote_results_path = PurePosixPath(args.remote_results)
    loadgen_disk_path = '/home/ubuntu' if str(remote_results_path).startswith('/home/ubuntu/') else '/'
    remote_disks = (
        ('loadgen', ssh, loadgen_disk_path),
        ('observability', observability_ssh, '/var/lib/docker'),
    )
    for label, remote_ssh, path in remote_disks:
        available = parse_df_available_kib(command(remote_ssh + [shlex.join(['df', '-Pk', path])]))
        print(f'  disk {label}: {available/1024/1024:.1f} GiB free at {path}')
        if available < minimum_kib:
            raise RuntimeError(f'{label}: less than {args.min_free_disk_gib:g} GiB free disk')
        mem_kib = int(command(remote_ssh + ["awk '/MemAvailable:/ {print $2}' /proc/meminfo"]))
        print(f'  memory {label}: {mem_kib/1024:.0f} MiB available')
        if mem_kib < 512 * 1024:
            raise RuntimeError(f'{label}: less than 512 MiB available memory')

    def check_observability():
        prometheus_ready = command(observability_ssh + [
            'curl -fsS --max-time 10 http://127.0.0.1:9090/-/ready'
        ])
        if 'ready' not in prometheus_ready.lower():
            raise RuntimeError('Prometheus is not ready')
        targets = json.loads(command(observability_ssh + [
            'curl -fsS --max-time 10 http://127.0.0.1:9090/api/v1/targets'
        ])).get('data', {}).get('activeTargets', [])
        required_jobs = {'kubelet-cadvisor', 'kube-state-metrics'}
        jobs = {target.get('labels', {}).get('job') for target in targets
                if target.get('health') == 'up'}
        if not required_jobs <= jobs:
            raise RuntimeError('required Prometheus cAdvisor/KSM targets are not healthy')
        unhealthy_required = [
            target for target in targets
            if target.get('labels', {}).get('job') in required_jobs
            and target.get('health') != 'up'
        ]
        if unhealthy_required:
            raise RuntimeError('one or more required Prometheus targets are down')
        jaeger_services = json.loads(command(observability_ssh + [
            'curl -fsS --max-time 10 http://127.0.0.1:16686/api/services'
        ]))
        if not isinstance(jaeger_services.get('data'), list):
            raise RuntimeError('Jaeger services API returned malformed data')
        containers = json.loads(command(observability_ssh + ['docker inspect prometheus jaeger']))
        if len(containers) != 2 or not all(item.get('State', {}).get('Running') for item in containers):
            raise RuntimeError('Prometheus/Jaeger containers are not both running')
        jaeger = next(item for item in containers if item.get('Name') == '/jaeger')
        jaeger_args = jaeger.get('Args', []) + jaeger.get('Config', {}).get('Cmd', [])
        if '--badger.span-store-ttl=3h0m0s' not in jaeger_args:
            raise RuntimeError('Jaeger must use the reviewed 3h Badger TTL before the long campaign')

    check_observability()

    health_baseline={}
    jaeger_restart_baseline=[None]
    def runtime_health():
        pods=get('pods')['items']
        for pod in pods:
            if pod.get('metadata',{}).get('labels',{}).get('app') not in (*SERVICES,'redis-cart'): continue
            for status in pod.get('status',{}).get('containerStatuses',[])+pod.get('status',{}).get('initContainerStatuses',[]):
                if any(status.get(k,{}).get('terminated',{}).get('reason')=='OOMKilled' for k in ('state','lastState')):
                    raise RuntimeError('OOMKilled: '+pod['metadata']['name'])
        container=json.loads(command(observability_ssh+['docker inspect jaeger']))[0]
        if not container.get('State',{}).get('Running') or container.get('State',{}).get('OOMKilled'):
            raise RuntimeError('Jaeger stopped or OOMKilled')
        restarts=int(container.get('RestartCount',0))
        if jaeger_restart_baseline[0] is None:
            jaeger_restart_baseline[0]=restarts
        elif restarts!=jaeger_restart_baseline[0]:
            raise RuntimeError('Jaeger restarted during the campaign')
        addresses=[n.get('IPAddress') for n in container.get('NetworkSettings',{}).get('Networks',{}).values() if n.get('IPAddress')]
        if not addresses: raise RuntimeError('Jaeger container IP missing for admin metrics')
        address=addresses[0]
        if not re.fullmatch(r'[0-9.]+',address): raise RuntimeError('unsupported Jaeger admin address')
        payload=command(observability_ssh+[shlex.join(['curl','-fsS','--max-time','10',f'http://{address}:14269/metrics'])])
        counters=span_drop_counters(payload)
        if not counters:
            raise RuntimeError('Jaeger dropped/refused span counter unavailable: inspect admin metrics before collection')
        if health_baseline and (any(k not in counters or counters[k]!=value for k,value in health_baseline.items())
                                or any(value>0 for k,value in counters.items() if k not in health_baseline)):
            raise RuntimeError('Jaeger drop counter increased/reset or disappeared during run')
        health_baseline.update(counters)
        return dict(jaeger_drop_counters=counters,oom_checked=True)

    runtime_health()

    local_probe = Path(args.output).resolve()
    while not local_probe.exists() and local_probe != local_probe.parent:
        local_probe = local_probe.parent
    local_free = shutil.disk_usage(local_probe).free
    print(f'  disk controller: {local_free/2**30:.1f} GiB free at {local_probe}')
    if local_free < args.min_free_disk_gib * 2**30:
        raise RuntimeError(f'controller: less than {args.min_free_disk_gib:g} GiB free disk')

    estimate = storage_estimate(args.smoke_output, plan) if args.profile == 'main' else None
    if args.profile == 'main':
        if estimate is None:
            raise RuntimeError('no completed smoke artifact for storage estimate; run --profile smoke first')
        missing_smoke = sorted(set(plan['scenarios']) - set(estimate['covered_scenarios']))
        if missing_smoke:
            raise RuntimeError(f'storage estimate lacks validated smoke scenarios: {missing_smoke}')
        gib = lambda value: value / 2**30
        print(f"  storage estimate from {estimate['smoke_samples']} smoke run(s): "
              f"exports={gib(estimate['exported_bytes']):.2f}GiB, "
              f"Prometheus CSV={gib(estimate['prometheus_export_bytes']):.2f}GiB, "
              f"client/other={gib(estimate['client_and_other_bytes']):.2f}GiB, "
              f"Jaeger peak with 3h TTL and 3x overhead={gib(estimate['jaeger_peak_bytes']):.2f}GiB")
        if local_free < estimate['exported_bytes'] + args.min_free_disk_gib * 2**30:
            raise RuntimeError('controller disk cannot hold estimated exports plus 10 GiB reserve')
    original = {s: deployments[s]['spec']['replicas'] for s in SERVICES}
    # Hash templates (images, resources, pod configuration), namespace, and node inventory.
    context = dict(namespace=args.namespace, sampling=10,
                   driver_max_concurrent=args.max_concurrent,
                   templates={s: deployments[s]['spec']['template'] for s in (*SERVICES, 'redis-cart')},
                   nodes=json.loads(command(['kubectl', 'get', 'nodes', '-o', 'json'])))
    # Use the acquisition hash from the registered order.  A reviewed offline
    # validator-only correction must not make an unchanged cluster context appear
    # different or force an already collected run to be replayed.
    context['controller_sha256'] = {
        'run-profiling.py': order['collection_code_sha256']['run-profiling.py']
    }
    context['nodes'] = [{"name": n['metadata']['name'], "capacity": n['status']['capacity'],
                         "nodeInfo": n['status']['nodeInfo']} for n in context['nodes']['items']]
    context['nodes'].sort(key=lambda n: n['name'])
    context_hash = digest(context)

    def expected_profile(cell):
        return dict(cell, cluster_context_sha256=context_hash,
                    collection_protocol_version=order['protocol_version'],
                    collection_lock_sha256=order['collection_lock_sha256'])

    def set_capacity(k):
        for s in SERVICES:
            subprocess.run(kubectl + ['scale', f'deployment/{s}', f'--replicas={k[s]}'], check=True,timeout=30)
        for s in SERVICES:
            subprocess.run(kubectl + ['rollout', 'status', f'deployment/{s}', '--timeout=300s'], check=True,timeout=330)
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

    if args.preflight_only:
        print('Operational preflight PASS; no workload or scaling action was started.')
        return

    # A read-only preflight must not pin a study context. Persist provenance only
    # when collection is actually authorized; otherwise a later code-only safety
    # fix would make an empty preflight directory reject the real campaign.
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    study_path = root / 'study.json'
    study = dict(plan=plan, cluster_context=context, cluster_context_sha256=context_hash)
    if study_path.exists():
        if read_json(study_path) != study:
            has_run_data = any(root.glob('*/meta.json')) or (root / 'profiling_progress.json').exists()
            if has_run_data:
                raise ValueError('study/context changed: use a new output and remote-results directory')
            archived = root / f'study.preflight-stale.{int(time.time())}.json'
            study_path.replace(archived)
            print(f'Archived stale preflight-only study context to {archived}')
    if not study_path.exists():
        with study_path.open('x', encoding='utf-8') as stream:
            json.dump(study, stream, indent=2)

    count = 0
    changed = False
    completed_ids = collected_run_ids(
        root, all_cells, expected_profile, plan['max_unstarted_fraction']
    )
    validate_v62_canary_checkpoint(
        plan, all_cells, cells, completed_ids, args.max_new
    )
    progress_path = root / 'profiling_progress.json'
    write_progress(progress_path, plan, context_hash, completed_ids, len(all_cells), 'scanning')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for cell in cells:
            profile = expected_profile(cell)
            dest = root / cell['cell_id']
            if cell['cell_id'] in completed_ids:
                print(f"Resume: validated and skipped completed cell {cell['cell_id']}")
                continue
            if dest.exists():
                try:
                    validate_run(dest, profile, plan['max_unstarted_fraction'])
                    completed_ids.add(cell['cell_id'])
                    write_progress(progress_path, plan, context_hash, completed_ids,
                                   len(all_cells), 'running')
                    print(f"Resume: validated and skipped completed cell {cell['cell_id']}")
                    continue
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    if not args.resume:
                        raise RuntimeError(
                            f'incomplete/invalid run {dest}; rerun with --resume to archive and restart this cell'
                        ) from exc
                    archive_root = root / '_incomplete'
                    archive_root.mkdir(exist_ok=True)
                    archive = archive_root / f'{cell["cell_id"]}-{int(time.time())}'
                    dest.rename(archive)
                    print(f'Resume: archived incomplete local run to {archive}')
            if count >= args.max_new:
                print('Batch complete. Verify saved data/Jaeger storage before resuming same plan.')
                break
            envelope = cell_envelope_evidence(plan, cell)
            print(
                f'Envelope PASS {cell["cell_id"]}: peak='
                f'{envelope["designed_peak_frontend_rps"]:.3f} RPS, '
                f'worst rho_peak={envelope["worst_rho_peak"]:.3f} '
                f'({envelope["worst_rho_service"]})'
            )
            for label, remote_ssh, path in remote_disks:
                available = parse_df_available_kib(command(remote_ssh + [shlex.join(['df', '-Pk', path])]))
                if available < minimum_kib:
                    raise RuntimeError(f'{label}: disk fell below {args.min_free_disk_gib:g} GiB before {cell["cell_id"]}')
                mem_kib = int(command(remote_ssh + ["awk '/MemAvailable:/ {print $2}' /proc/meminfo"]))
                if mem_kib < 512 * 1024:
                    raise RuntimeError(f'{label}: memory fell below 512 MiB before {cell["cell_id"]}')
            controller_free = shutil.disk_usage(root).free
            if controller_free < args.min_free_disk_gib * 2**30:
                raise RuntimeError(
                    f'controller: disk fell below {args.min_free_disk_gib:g} GiB '
                    f'before {cell["cell_id"]}'
                )
            check_control()
            check_observability()
            validate_envelope_cpu_limits(plan, check_app_health())
            remote = f'{args.remote_results}/{cell["cell_id"]}'
            remote_exists = subprocess.run(
                ssh + [shlex.join(['test', '-e', remote])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            ).returncode == 0
            if remote_exists:
                if not args.resume:
                    raise RuntimeError(f'remote run exists: {remote}; use --resume to archive and restart it')
                confirm_remote_stopped(ssh,remote,None,abort=False)
                remote_archive = f'{remote}.incomplete.{int(time.time())}'
                command(ssh + [shlex.join(['mv', remote, remote_archive])])
                print(f'Resume: archived incomplete remote run to {remote_archive}')
            changed = True
            block_started=time.monotonic()
            write_progress(progress_path, plan, context_hash, completed_ids,
                           len(all_cells), 'running', current=cell['cell_id'])
            set_capacity(cell['replicas'])
            check_app_health()
            time.sleep(plan['settle_sec'])
            # Recheck every clock domain at every run boundary. A 25+ hour
            # campaign cannot rely on the single preflight measurement because
            # Prometheus, Jaeger, K3s and the load generator timestamp different
            # pieces of the same transition.
            boundary_clocks = check_clocks(prefix='run-boundary clock')
            health_baseline.clear()
            runtime_health()
            clock_measurement = boundary_clocks['loadgen']
            remote_command = ['env', 'TRACE_SAMPLING_PERCENTAGE=10', f'NAMESPACE={args.namespace}',
                              f'LOAD_MULTIPLIER={cell["load_multiplier"]}',
                              f'DRAIN_SEC={plan["drain_sec"]}',
                              f'COLLECTION_PROTOCOL_VERSION={order["protocol_version"]}',
                              'COLLECTION_LOCK_JSON=' + json.dumps(
                                  order['collection_code_sha256'], separators=(',', ':')),
                              'PROFILE_CELL_JSON=' + json.dumps(profile, separators=(',', ':')),
                              'setsid','timeout','--signal=TERM','--kill-after=15s',
                              str(plan['duration_sec']+plan['drain_sec']+plan['collection_timeout_sec']+120),
                              'bash', f'{args.remote_dir}/run-profile-loadgen.sh',
                              cell['scenario'], str(cell['seed']), str(plan['duration_sec']), '10',
                              str(args.max_concurrent), remote]
            dest.mkdir()
            # Journal is local and survives SSH/collector failures. No pipe buffer can block SSH.
            with (dest/'loadgen.log').open('w', encoding='utf-8') as log:
                args._remote_unconfirmed=True
                process = subprocess.Popen(ssh + [shlex.join(remote_command)], stdout=log, stderr=log)
                failure = None
                try:
                    returncode = execute_capture(process, ssh, remote, kubectl, plan, profile,
                                                 dest/'capacity_events.jsonl', clock_measurement,runtime_health)
                except BaseException as exc:
                    failure = exc
                    returncode = process.poll()
                finally:
                    confirm_remote_stopped(ssh,remote,process,abort=failure is not None)
                    args._remote_unconfirmed=False
            transport = shlex.join(ssh[:-1])
            subprocess.run(['rsync', '-a','--timeout=120', '-e', transport,
                            f'{args.ssh_target}:{shlex.quote(remote)}/', str(dest) + '/'], check=True,timeout=600)
            if failure is not None:
                enriched = loadgen_failure_with_evidence(failure, dest)
                if enriched is failure:
                    raise failure
                raise enriched from failure
            runtime_health()
            validate_run(dest, profile, plan['max_unstarted_fraction'])
            if returncode:
                raise RuntimeError(f'loadgen/collector failed ({returncode}); preserved {dest}')
            completed_ids.add(cell['cell_id'])
            write_progress(progress_path, plan, context_hash, completed_ids,
                           len(all_cells), 'running')
            count += 1
            time.sleep(plan['cooldown_sec'])
            if time.monotonic()-block_started>plan.get('block_max_sec',5400):
                raise RuntimeError('execution block exceeded registered 90-minute budget')
        final_status = 'complete' if len(completed_ids) == len(all_cells) else 'paused'
        write_progress(progress_path, plan, context_hash, completed_ids,
                       len(all_cells), final_status)
    except BaseException as exc:
        write_progress(progress_path, plan, context_hash, completed_ids,
                       len(all_cells), 'failed', error=repr(exc))
        raise
    finally:
        if changed and not getattr(args,'_remote_unconfirmed',False):
            print('Restoring original replicas...')
            failed = []
            for s in SERVICES:
                result = subprocess.run(kubectl + ['scale', f'deployment/{s}', f'--replicas={original[s]}'], check=False,timeout=30)
                if result.returncode:
                    failed.append(s)
            for s in SERVICES:
                result = subprocess.run(kubectl + ['rollout', 'status', f'deployment/{s}', '--timeout=300s'], check=False,timeout=330)
                if result.returncode:
                    failed.append(s)
            if failed:
                raise RuntimeError(f'restore failed: {sorted(set(failed))}; inspect cluster manually')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', default=str(Path(__file__).with_name('profiling-plan.json')))
    parser.add_argument('--profile', choices=('main', 'smoke','diagnostic'), default='main')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--confirm', action='store_true',
                      help='legacy alias for --collect-only')
    mode.add_argument('--collect-only', action='store_true',
                      help='collect and validate immutable raw artifacts; never run analyzer')
    mode.add_argument('--export', metavar='JSON', help='validate collected runs and write manifest/windows')
    mode.add_argument('--preflight-only', action='store_true', help='run live read-only operational checks, then exit')
    mode.add_argument('--register-order', action='store_true',help='write deterministic order/hash locally, no cluster calls')
    mode.add_argument('--audit-diagnostic',metavar='JSON',help='offline audit diagnostic artifacts and write evidence JSON')
    mode.add_argument('--analyze-collected', action='store_true',
                      help='offline incremental analysis of collected runs; never contacts cluster')
    parser.add_argument('--analysis-report', help='optional JSON report for --analyze-collected')
    parser.add_argument('--output', help='local raw-run directory; default depends on profile')
    parser.add_argument('--diagnostic-input', action='append', default=[], metavar='DIR',
                        help='additional raw diagnostic root used only by --audit-diagnostic')
    parser.add_argument('--ssh-target', help='user@loadgen-host')
    parser.add_argument('--observability-ssh-target', help='user@observability-host')
    parser.add_argument('--k3s-ssh-target', help='user@K3s control-plane host')
    parser.add_argument('--direct-worker-ssh', action='store_true',
                        help='reach K3s worker private IPs directly (controller runs inside the cluster network)')
    parser.add_argument('--ssh-key')
    parser.add_argument('--namespace', default='online-boutique')
    parser.add_argument('--remote-dir', default='/home/ubuntu/load-testing')
    parser.add_argument('--remote-results', help='remote raw-run directory; default depends on profile')
    parser.add_argument('--max-new', type=int, default=1, help='maximum new calibration runs')
    parser.add_argument('--max-concurrent', type=int, default=None,
                        help='override the versioned per-profile concurrency budget')
    parser.add_argument('--resume', action='store_true',
                        help='archive incomplete local/remote cell directories and restart that cell')
    parser.add_argument('--min-free-disk-gib', type=float, default=10.0)
    parser.add_argument('--max-clock-skew-ms', type=float, default=100.0)
    parser.add_argument('--smoke-output', default='dataset/digital-twin-smoke-v2',
                        help='validated smoke artifacts used to extrapolate main-campaign storage')
    parser.add_argument('--recovery-report',default='artifacts/rl/synthetic_recovery_v4.json',
                        help='local evidence gate for main collection; diagnostic/smoke do not need it')
    parser.add_argument('--stress-report',default='artifacts/rl/synthetic_stress_report.json')
    parser.add_argument('--contract-report',default='artifacts/rl/actual_contract_report.json')
    parser.add_argument('--scenario', choices=('nhpp', 'mmpp', 'onoff', 'pareto'),
                        help='collect/preview only this scenario; does not change the study plan')
    parser.add_argument('--split', choices=('train', 'validation', 'test'),
                        help='collect/preview only this split; does not change the study plan')
    parser.add_argument('--load-multiplier', type=float,
                        help='collect/preview only this configured load multiplier')
    parser.add_argument('--repeat', type=int,
                        help='collect/preview only this configured repeat index')
    args = parser.parse_args(argv)
    folder = {'main':'digital-twin-calibration-v2-official-v6-2', 'smoke':'digital-twin-smoke-v2',
              'diagnostic':'digital-twin-diagnostic-v5'}[args.profile]
    if args.output is None: args.output = f'dataset/{folder}'
    if args.remote_results is None: args.remote_results = f'/home/ubuntu/dataset/{folder}'
    return args


if __name__ == '__main__':
    run(parse_args())
