"""
locustfile.py - Partly-open session driver with execution telemetry.

Standard Locust users form a CLOSED loop (a user waits for the response before sending
again), so offered load drops when the system slows down. Here a single driver user
replays a pre-generated trace (arrival_models.build_trace): every planned arrival is
started at its scheduled time in its own greenlet, whether earlier requests have finished
or not. Requests WITHIN a session remain sequential and response-dependent.
ON/OFF retains direct request arrivals from persistent clients. Each session / client
has its own HttpSession. Execution CSVs distinguish session and request in-flight;
automatic redirect hops belong to one top-level client request.

Run (from this directory, see run-scenario.sh):
    SCENARIO=mmpp SEED=1001 RUN_DURATION_SEC=1800 TRACE_DIR=results/mmpp/run_01 \
    locust -f locustfile.py --headless --users 1 --spawn-rate 1 --run-time 1800s --host http://...

Env: SCENARIO (nhpp|mmpp|pareto|onoff), SEED, RUN_DURATION_SEC, TRACE_DIR,
     MAX_CONCURRENT (default 400): cap on in-flight sessions/requests to protect the 1-vCPU
     load generator. Arrivals beyond the cap are DROPPED and counted in driver_report.json.
"""
import importlib
import csv
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import gevent
from gevent.pool import Pool
from locust import HttpUser, constant, events, task
from locust.clients import HttpSession

import arrival_models as am

SCENARIO = os.environ.get('SCENARIO', 'nhpp')
SEED = int(os.environ.get('SEED', '1'))
DURATION = int(os.environ.get('RUN_DURATION_SEC', 1800))
TRACE_DIR = os.environ.get('TRACE_DIR', os.path.join('results', SCENARIO, 'adhoc'))
MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', 400))
CONFIG = am.scaled_config(importlib.import_module(f'scenarios.{SCENARIO}').CONFIG,
                          float(os.environ.get('LOAD_MULTIPLIER', '1')))

PRODUCT_IDS = ('0PUK6V6EV0', '1YMWWN1N4O', '2ZYFJ3GM2N', '66VCHSJNUP', '6E92ZMYYFZ',
               '9SIQT8TOJO', 'L9ECAV7KIM', 'LS4PSXUNUM', 'OLJCESPC7Z')
CURRENCIES = ('USD', 'EUR', 'CAD', 'JPY', 'GBP', 'TRY')
# Browse-heavy action mix with occasional checkout.
ACTIONS = (('browse', 12), ('product', 7), ('view_cart', 3), ('add', 3), ('checkout', 1), ('currency', 1))
CHECKOUT_FORM = {
    'email': 'math-shopper@example.com', 'street_address': '1600 Amphitheatre Parkway', 'zip_code': '94043',
    'city': 'Mountain View', 'state': 'CA', 'country': 'United States',
    'credit_card_number': '4111111111111111',  # Visa test number, passes Luhn
    'credit_card_expiration_month': '12',
    'credit_card_expiration_year': str(datetime.now().year + 2), 'credit_card_cvv': '672',
}

STATS = {'planned': 0, 'started': 0, 'dropped': 0, 'completed': 0, 'failed': 0, 'max_in_flight': 0}
RECORDER = None
SAMPLER = None


class ExecutionLog:
    """Single-process, cooperative driver log. Epoch timestamps; monotonic durations.

    A session row represents one arrival event (ON/OFF remains request-level).
    actual_size counts top-level request attempts, excluding automatic redirects.
    Open work is right-censored at logging end, never fabricated as completed.
    """

    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = {}
        self.writers = {}
        schemas = {
            'sessions': ['session_id', 'scheduled_ts', 'actual_start_ts', 'planned_size',
                         'actual_size', 'end_ts', 'dropped', 'kind', 'source',
                         'planned_requests', 'lag_ms', 'outcome', 'driver_error', 'censored_at'],
            'client_requests': ['session_id', 'endpoint', 't_start', 't_end', 'status',
                                'latency_ms', 'redirect_count', 'error', 'request_id',
                                'method', 'redirect_chain', 'censored_at'],
            'in_flight': ['timestamp', 'elapsed_sec', 'sessions_started', 'sessions_completed',
                         'driver_exceptions', 'sessions_in_flight', 'requests_started',
                         'requests_completed', 'requests_in_flight'],
            'loadgen_resources': ['timestamp', 'elapsed_sec', 'node_cpu_busy_pct',
                                  'node_mem_total_bytes', 'node_mem_available_bytes',
                                  'process_cpu_cores', 'process_rss_bytes'],
        }
        for name, fields in schemas.items():
            stream = (self.root / (name + '.csv')).open('x', encoding='utf-8', newline='')
            self.files[name] = stream
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            self.writers[name] = writer
        self.sessions = {}
        self.requests = {}
        self.lags = []
        self.counts = dict(sessions_started=0, sessions_completed=0, driver_exceptions=0,
                           requests_started=0, requests_completed=0, http_error_responses=0,
                           http_4xx_responses=0, http_5xx_responses=0, transport_errors=0)
        self.origin_mono = time.monotonic()
        self.previous_resource = None
        self.closed = False
        self.recent_requests = []
        self.recent_lags = []
        self.dropped = 0
        self.last_snapshot_mono = None

    def start_session(self, session_id, event, origin, dispatch_mono, dropped=False):
        actual = time.time()
        lag = max(0.0, (time.monotonic() - dispatch_mono - event['t']) * 1000)
        planned = (event['size'] + (3 if CONFIG['model'] == 'pareto' else 1)
                   if event['kind'] == 'session' else 1)
        row = dict(session_id=session_id, scheduled_ts=origin + event['t'],
                   actual_start_ts='' if dropped else actual, planned_size=event['size'],
                   actual_size=0, end_ts='', dropped=int(dropped), kind=event['kind'],
                   source=event['source'], planned_requests=planned, lag_ms=lag,
                   outcome='dropped' if dropped else 'running', driver_error='', censored_at='')
        if dropped:
            self.dropped += 1
            self.writers['sessions'].writerow(row)
        else:
            self.sessions[session_id] = row
            self.counts['sessions_started'] += 1
            self.lags.append(lag)
            self.recent_lags.append((time.monotonic(), lag))

    def finish_session(self, session_id, error=None):
        row = self.sessions.pop(session_id)
        row.update(end_ts=time.time(), outcome='driver_exception' if error else 'completed',
                   driver_error=type(error).__name__ if error else '')
        self.counts['driver_exceptions' if error else 'sessions_completed'] += 1
        self.writers['sessions'].writerow(row)

    def begin_request(self, session_id, method, endpoint):
        self.counts['requests_started'] += 1
        request_id = self.counts['requests_started']
        self.sessions[session_id]['actual_size'] += 1
        self.requests[request_id] = dict(session_id=session_id, endpoint=endpoint,
            t_start=time.time(), t_end='', status='', latency_ms='', redirect_count='',
            error='', request_id=request_id, method=method, redirect_chain='', censored_at='')
        return request_id, time.monotonic()

    def finish_request(self, request_id, start_mono, response=None, error=None):
        # Keep the request in-flight until its row is durably written.  If
        # evidence formatting or CSV I/O fails, close() will mark it censored
        # and the validator will reject the run instead of silently losing it.
        row = self.requests[request_id]
        status = int(getattr(response, 'status_code', 0) or 0)
        history = list(getattr(response, 'history', ()) or ())
        chain = history + ([response] if response is not None else [])
        statuses = [int(getattr(hop, 'status_code', 0) or 0) for hop in chain]
        self.counts['http_4xx_responses'] += sum(400 <= code < 500 for code in statuses)
        self.counts['http_5xx_responses'] += sum(500 <= code < 600 for code in statuses)
        self.counts['http_error_responses'] += sum(400 <= code < 600 for code in statuses)
        transport = error or getattr(response, 'error', None)
        if status == 0:
            self.counts['transport_errors'] += 1
        chain_evidence = []
        for hop in chain:
            raw_url = getattr(hop, 'url', '')
            try:
                path = urlsplit(raw_url if isinstance(raw_url, (str, bytes)) else str(raw_url)).path
            except (TypeError, ValueError, UnicodeError):
                path = ''
            chain_evidence.append(dict(
                path=path,
                method=str(getattr(getattr(hop, 'request', None), 'method', '') or ''),
                status=int(getattr(hop, 'status_code', 0) or 0),
            ))
        row.update(t_end=time.time(), status=status,
                   latency_ms=max(0.0, (time.monotonic() - start_mono) * 1000),
                   redirect_count=len(history),
                   error=type(transport).__name__ if transport else (
                       'HTTP_' + str(status) if status >= 400 else ('transport_error' if status == 0 else '')),
                   redirect_chain=json.dumps(chain_evidence, separators=(',', ':')))
        self.writers['client_requests'].writerow(row)
        self.requests.pop(request_id)
        self.counts['requests_completed'] += 1
        self.recent_requests.append((time.monotonic(), row['latency_ms'], bool(row['error'])))

    def check_abort(self, rules):
        if not rules:
            return
        now = time.monotonic()
        self.recent_requests = [r for r in self.recent_requests if now-r[0] <= rules['window_sec']]
        self.recent_lags = [r for r in self.recent_lags if now-r[0] <= rules['window_sec']]
        if self.dropped > rules['max_dropped']:
            raise RuntimeError('abort: dropped arrivals')
        if len(self.recent_requests) >= rules['min_requests']:
            latencies = sorted(r[1] for r in self.recent_requests)
            if sum(r[2] for r in self.recent_requests)/len(latencies) > rules['max_http_error_fraction']:
                raise RuntimeError('abort: client error fraction')
            if latencies[int(.95*(len(latencies)-1))] > rules['max_client_p95_ms']:
                raise RuntimeError('abort: client p95')
        if len(self.recent_lags) >= rules['min_lag_samples']:
            lags = sorted(r[1] for r in self.recent_lags)
            if lags[int(.99*(len(lags)-1))] > rules['max_scheduling_p99_ms']:
                raise RuntimeError('abort: scheduling lag')
        mem = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        if mem['MemAvailable'] < rules['min_loadgen_available_mib']*1024:
            raise RuntimeError('abort: loadgen available memory')
        if self.last_snapshot_mono is not None and now-self.last_snapshot_mono > rules['max_sampler_gap_sec']:
            raise RuntimeError('abort: resource sampler stalled')
        self.last_snapshot_mono = now

    def snapshot(self):
        stamp = time.time()
        elapsed = time.monotonic() - self.origin_mono
        c = self.counts
        self.writers['in_flight'].writerow(dict(timestamp=stamp, elapsed_sec=elapsed,
            sessions_started=c['sessions_started'], sessions_completed=c['sessions_completed'],
            driver_exceptions=c['driver_exceptions'], sessions_in_flight=len(self.sessions),
            requests_started=c['requests_started'], requests_completed=c['requests_completed'],
            requests_in_flight=len(self.requests)))
        # Linux /proc, no extra dependency; guest times are already included in user/nice.
        cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        total, idle = sum(cpu), cpu[3] + cpu[4]
        mem = {line.split(':')[0]: int(line.split()[1]) * 1024
               for line in Path('/proc/meminfo').read_text().splitlines()}
        rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        proc = time.process_time()
        busy, cores = '', ''
        if self.previous_resource is not None:
            pt, pi, pp, pe = self.previous_resource
            if total > pt:
                busy = 100 * (1 - (idle - pi) / (total - pt))
            if elapsed > pe:
                cores = (proc - pp) / (elapsed - pe)
        self.previous_resource = (total, idle, proc, elapsed)
        self.writers['loadgen_resources'].writerow(dict(timestamp=stamp, elapsed_sec=elapsed,
            node_cpu_busy_pct=busy, node_mem_total_bytes=mem['MemTotal'],
            node_mem_available_bytes=mem['MemAvailable'], process_cpu_cores=cores,
            process_rss_bytes=rss))
        for stream in self.files.values():
            stream.flush()

    def close(self):
        if self.closed:
            return
        cutoff = time.time()
        for row in self.requests.values():
            row.update(error='right_censored', censored_at=cutoff)
            self.writers['client_requests'].writerow(row)
        for row in self.sessions.values():
            row.update(outcome='right_censored', censored_at=cutoff)
            self.writers['sessions'].writerow(row)
        for stream in self.files.values():
            stream.close()
        self.closed = True

    def summary(self):
        values = sorted(self.lags)
        def percentile(q):
            if not values:
                return None
            index = (len(values) - 1) * q
            lo = int(index)
            hi = min(lo + 1, len(values) - 1)
            return values[lo] + (values[hi] - values[lo]) * (index - lo)
        return dict(self.counts, execution_schema_version=1,
                    request_censored=len(self.requests), session_censored=len(self.sessions),
                    scheduling_lag_ms=dict(count=len(values), p50=percentile(.5),
                        p95=percentile(.95), p99=percentile(.99), max=max(values) if values else None))


class Client:
    """One shopper with its own cookie jar (shop_session-id) and cart state."""

    def __init__(self, user, rng):
        self.http = HttpSession(base_url=user.host, request_event=user.environment.events.request, user=user)
        self.rng = rng
        self.cart_has_items = False

    def request(self, session_id, method, path, name, **kwargs):
        request_id, started = RECORDER.begin_request(session_id, method, name)
        try:
            response = self.http.request(method, path, name=name, **kwargs)
        except Exception as exc:
            RECORDER.finish_request(request_id, started, error=exc)
            raise
        RECORDER.finish_request(request_id, started, response=response)
        return response

    def do(self, action, session_id):
        rng = self.rng
        if action == 'browse':
            self.request(session_id, 'GET', '/', name='GET /')
        elif action == 'product':
            self.request(session_id, 'GET', f'/product/{rng.choice(PRODUCT_IDS)}', name='GET /product/[id]')
        elif action == 'view_cart':
            self.request(session_id, 'GET', '/cart', name='GET /cart')
        elif action == 'add':
            r = self.request(session_id, 'POST', '/cart', data={'product_id': rng.choice(PRODUCT_IDS), 'quantity': 1}, name='POST /cart')
            self.cart_has_items = self.cart_has_items or 0 < r.status_code < 400
        elif action == 'checkout':
            if not self.cart_has_items:            # an empty-cart checkout is not a real request
                return self.do('add', session_id)
            self.request(session_id, 'POST', '/cart/checkout', data=CHECKOUT_FORM, name='POST /cart/checkout')
            self.cart_has_items = False
        elif action == 'currency':
            self.request(session_id, 'POST', '/setCurrency', data={'currency_code': rng.choice(CURRENCIES)}, name='POST /setCurrency')

    def random_action(self):
        names, weights = zip(*ACTIONS)
        return self.rng.choices(names, weights=weights)[0]

    def think(self, mean):
        gevent.sleep(am.exponential(self.rng, mean))


class ArrivalDriver(HttpUser):
    """Exactly one instance (--users 1): replays the arrival trace in real time."""
    wait_time = constant(0)

    def _session(self, ev, session_id):
        rng = random.Random(SEED * 1_000_003 + ev['source'])
        client = Client(self, rng)
        think = CONFIG.get('think_mean_sec', 2.0)
        try:
            client.do('browse', session_id)
            if CONFIG['model'] == 'pareto':
                for _ in range(ev['size']):        # heavy-tailed work: S items then checkout
                    client.think(think)
                    client.do('add', session_id)
                client.think(think)
                client.do('view_cart', session_id)
                client.think(think)
                client.do('checkout', session_id)
            else:
                for _ in range(ev['size']):        # K ~ Geometric actions
                    client.think(think)
                    client.do(client.random_action(), session_id)
        finally:
            client.http.close()

    def _request(self, ev, session_id):
        client = self.clients.get(ev['source'])
        if client is None:
            client = self.clients[ev['source']] = Client(self, random.Random(SEED * 1_000_003 + ev['source']))
        client.do(client.random_action(), session_id)

    def _run(self, ev, session_id, origin, dispatch_mono):
        RECORDER.start_session(session_id, ev, origin, dispatch_mono)
        try:
            (self._session if ev['kind'] == 'session' else self._request)(ev, session_id)
            STATS['completed'] += 1
            RECORDER.finish_session(session_id)
        except Exception as exc:
            STATS['failed'] += 1
            RECORDER.finish_session(session_id, error=exc)

    def on_stop(self):
        if hasattr(self, 'pool'):
            self.pool.kill(block=True)
        for client in getattr(self, 'clients', {}).values():
            client.http.close()

    @task
    def drive(self):
        global RECORDER, SAMPLER
        if RECORDER is not None:
            raise RuntimeError('Exactly one arrival driver per process is required')
        trace = am.build_trace(CONFIG, DURATION, SEED)
        am.write_trace(trace, TRACE_DIR)
        RECORDER = ExecutionLog(TRACE_DIR)
        # A missing /proc source is a collection failure, not fabricated zero usage.
        RECORDER.snapshot()
        abort_rules = json.loads(os.environ.get('PROFILE_CELL_JSON', '{}')).get('abort_rules', {})
        def sample_resources():
            deadline = time.monotonic() + 1
            while True:
                gevent.sleep(max(0, deadline - time.monotonic()))
                try:
                    RECORDER.check_abort(abort_rules)
                    RECORDER.snapshot()
                except Exception as exc:
                    STATS['execution_logging_error'] = str(exc)
                    print(f'ABORT: {exc}', flush=True)
                    # test_stop kills SAMPLER; do not invoke it from SAMPLER itself.
                    gevent.spawn(self.environment.runner.quit)
                    return
                deadline = max(deadline + 1, time.monotonic())
        SAMPLER = gevent.spawn(sample_resources)
        STATS.update(expected_mean_rps=trace['expected_mean_rps'], designed_mean_rps=trace['designed_mean_rps'])
        self.clients = {}
        pool = self.pool = Pool(MAX_CONCURRENT)
        t0 = time.monotonic()
        STATS['trace_start_epoch'] = time.time()
        STATS['trace_events_total'] = len(trace['events'])
        marker = os.path.join(TRACE_DIR, 'driver_started.json')
        with open(marker + '.tmp', 'w') as stream:
            json.dump(dict(trace_start_epoch=STATS['trace_start_epoch'],
                           scenario=SCENARIO, seed=SEED), stream)
        os.replace(marker + '.tmp', marker)
        for event_id, ev in enumerate(trace['events']):
            delay = ev['t'] - (time.monotonic() - t0)
            if delay > 0:
                gevent.sleep(delay)
            STATS['planned'] += 1
            session_id = f'{SCENARIO}-{SEED}-{event_id}'
            if pool.full():
                STATS['dropped'] += 1
                RECORDER.start_session(session_id, ev, STATS['trace_start_epoch'], t0, dropped=True)
                continue
            STATS['started'] += 1
            pool.spawn(self._run, ev, session_id, STATS['trace_start_epoch'], t0)
            STATS['max_in_flight'] = max(STATS['max_in_flight'], len(pool))
        while True:                                # trace exhausted: idle until --run-time stops the test
            gevent.sleep(30)


@events.test_stop.add_listener
def _report(environment, **_):
    if SAMPLER is not None:
        SAMPLER.kill(block=True)
    if RECORDER is not None:
        try:
            RECORDER.snapshot()
        except Exception as exc:
            STATS['execution_logging_error'] = type(exc).__name__
        RECORDER.close()
        STATS.update(RECORDER.summary())
    STATS['unfinished'] = STATS['started'] - STATS['completed'] - STATS['failed']
    STATS['dropped_ratio'] = STATS['dropped'] / STATS['planned'] if STATS['planned'] else 0.0
    STATS.update(scenario=SCENARIO, seed=SEED, duration=DURATION, max_concurrent=MAX_CONCURRENT)
    os.makedirs(TRACE_DIR, exist_ok=True)
    with open(os.path.join(TRACE_DIR, 'driver_report.json'), 'w') as f:
        json.dump(STATS, f, indent=1)
    if STATS['dropped']:
        print(f"[driver] WARN {STATS['dropped']} arrivals dropped (MAX_CONCURRENT={MAX_CONCURRENT}) - "
              f"load generator saturated, offered load lower than designed")
